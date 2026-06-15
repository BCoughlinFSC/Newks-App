"""
SQL Server write helpers for the Toast Puller.

Writes directly to toast.dbo.hstCheck and toast.dbo.hstItem on SQL-PROD
via pyodbc MERGE statements (idempotent upserts).

Uses the same SQL Server as the Aloha importer but targets the 'toast'
database instead of 'dev_aloha'.
"""

import os
import logging

import pyodbc
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

log = logging.getLogger(__name__)


# ── Connection ───────────────────────────────────────────────────────────────

def _build_toast_connection_string() -> str:
    """Build pyodbc connection string for the toast database on SQL-PROD."""
    server = os.getenv("ALOHA_SQL_SERVER", "sql-prod")
    database = os.getenv("TOAST_SQL_DATABASE", "toast")
    username = os.getenv("ALOHA_SQL_USERNAME")
    password = os.getenv("ALOHA_SQL_PASSWORD")
    driver = os.getenv("ALOHA_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    trusted = os.getenv("ALOHA_SQL_TRUSTED_CONNECTION", "").lower() in ("1", "true", "yes")

    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    parts.append("TrustServerCertificate=yes")

    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        if not username or not password:
            raise ValueError("Missing ALOHA_SQL_USERNAME or ALOHA_SQL_PASSWORD in .env")
        parts.append(f"UID={username}")
        parts.append(f"PWD={password}")

    return ";".join(parts)


def get_toast_connection() -> pyodbc.Connection:
    """Get a pyodbc connection to the toast database."""
    conn_str = _build_toast_connection_string()
    return pyodbc.connect(conn_str, autocommit=False)


# ── Store lookup ─────────────────────────────────────────────────────────────

def lookup_store_num(conn: pyodbc.Connection, restaurant_guid: str) -> int:
    """Look up the integer StoreNum from toast.dbo.restaurants by GUID."""
    row = conn.execute(
        "SELECT StoreID FROM dbo.restaurants WHERE GUID = ?",
        restaurant_guid
    ).fetchone()
    if not row:
        raise ValueError(f"Restaurant GUID {restaurant_guid} not found in toast.dbo.restaurants")
    return row[0]


def get_live_toast_stores(conn: pyodbc.Connection) -> list:
    """Get all live Toast stores from the view used by the nightly cron."""
    rows = conn.execute(
        "SELECT StoreID, GUID, StoreName FROM dbo.vw_live_toast_stores ORDER BY StoreID"
    ).fetchall()
    return [{"store_num": r[0], "guid": r[1], "name": r[2]} for r in rows]


# ── Upsert helpers ───────────────────────────────────────────────────────────

def upsert_checks(conn: pyodbc.Connection, rows: list[dict]) -> int:
    """
    Delete existing checks for this store/date, then bulk INSERT all new rows.
    Much faster than per-row MERGE (~seconds instead of ~minutes).
    """
    if not rows:
        return 0

    # All rows share the same store/date — use first row to determine scope
    store_num = rows[0]["StoreNum"]
    biz_date = rows[0]["DateOfBusiness"]

    cursor = conn.cursor()

    # Delete existing rows for this store/date
    cursor.execute(
        "DELETE FROM dbo.hstCheck WHERE StoreNum = ? AND DateOfBusiness = ?",
        store_num, biz_date
    )

    insert_sql = """
    INSERT INTO dbo.hstCheck (
        CheckID, DateOfBusiness, DayPart, StoreID, StoreNum, OrderID,
        DisplayNumber, PaymentStatus, TabName, TaxExempt, TaxExemptState,
        TaxExemptNumber, Amount, DiscountAmount, TotalAmount, TaxAmount,
        CreatedDevice, CustomerID, LastModifiedDevice, OrderMode,
        CreatedTime, OpenedTime, ClosedTime, ModifiedTime,
        Deleted, DeletedTime, Voided, VoidTime
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row in rows:
        params = (
            _trunc(row["CheckID"], 100), row["DateOfBusiness"],
            _trunc(row.get("DayPart"), 100),
            _trunc(row["StoreID"], 100), row["StoreNum"],
            _trunc(row["OrderID"], 100),
            _trunc(row.get("DisplayNumber"), 100),
            _trunc(row.get("PaymentStatus"), 50),
            _trunc(row.get("TabName"), 100),
            row.get("TaxExempt", 0),
            _trunc(row.get("TaxExemptState"), 50),
            _trunc(row.get("TaxExemptNumber"), 50),
            row.get("Amount", 0), row.get("DiscountAmount"),
            row.get("TotalAmount", 0), row.get("TaxAmount", 0),
            _trunc(row.get("CreatedDevice"), 100),
            _trunc(row.get("CustomerID"), 100),
            _trunc(row.get("LastModifiedDevice"), 100),
            _trunc(row.get("OrderMode"), 100),
            _safe_dt(row.get("CreatedTime")), _safe_dt(row.get("OpenedTime")),
            _safe_dt(row.get("ClosedTime")), _safe_dt(row.get("ModifiedTime")),
            row.get("Deleted", 0), _safe_dt(row.get("DeletedTime")),
            row.get("Voided", 0), _safe_dt(row.get("VoidTime")),
        )
        cursor.execute(insert_sql, params)
    conn.commit()
    log.info("Bulk inserted %d check rows (delete + insert)", len(rows))
    return len(rows)


def _safe_dt(val):
    """Ensure datetime is in SQL Server's valid range and timezone-naive."""
    if val is None:
        return None
    if hasattr(val, 'replace') and hasattr(val, 'tzinfo') and val.tzinfo is not None:
        val = val.replace(tzinfo=None)
    if hasattr(val, 'year'):
        if val.year < 1753 or val.year > 9999:
            return None
    return val


def _trunc(val, maxlen):
    """Truncate string to max length, handling None."""
    if val is None: return None
    s = str(val)
    return s[:maxlen] if len(s) > maxlen else s


def upsert_employees(conn: pyodbc.Connection, rows: list[dict]) -> tuple[int, int]:
    """
    Upsert employee-master rows into toast.dbo.Employees, MERGEd on GUID.

    MERGE (not DELETE+INSERT): we NEVER remove an employee record, because a
    past payroll run still needs people who have since left. Toast's
    /labor/v1/employees returns a restaurant's current roster; we update
    matched rows and insert new ones, stamping LastUpdatedBy='API' to match
    the convention Don's old puller used.

    The GUID is Toast's per-restaurant employee GUID — globally unique and
    exactly what hstShift.Employee carries — so matching on GUID alone is
    correct, and a person who works multiple stores legitimately gets one row
    per store (each with its own GUID + StoreGUID). That's what lets the
    payroll export's `hs.Employee = e.GUID AND hs.Store = e.StoreGUID` join
    resolve cross-store shifts once every store's roster is current.

    Returns (inserted, updated).
    """
    if not rows:
        return (0, 0)

    cur = conn.cursor()
    merge_sql = """
    MERGE toast.dbo.Employees WITH (HOLDLOCK) AS t
    USING (SELECT ? AS GUID) AS s
    ON (t.GUID = s.GUID)
    WHEN MATCHED THEN UPDATE SET
        t.StoreGUID = ?, t.StoreID = ?, t.First = ?, t.Last = ?,
        t.ChosenName = ?, t.Email = ?, t.Phone = ?, t.ExternalId = ?,
        t.ExternalEmployeeId = ?, t.CreatedDate = ?, t.Deleted = ?,
        t.DeletedDate = ?, t.ModifiedDate = ?, t.v2EmployeeGuid = ?,
        t.LastUpdated = GETDATE(), t.LastUpdatedBy = 'API'
    WHEN NOT MATCHED THEN INSERT
        (GUID, StoreGUID, StoreID, First, Last, ChosenName, Email, Phone,
         ExternalId, ExternalEmployeeId, CreatedDate, Deleted, DeletedDate,
         ModifiedDate, v2EmployeeGuid, LastUpdated, LastUpdatedBy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), 'API')
    OUTPUT $action;
    """
    inserted = updated = 0
    for r in rows:
        guid     = _trunc(r.get("GUID"), 50)
        store_g  = _trunc(r.get("StoreGUID"), 50)
        store_id = r.get("StoreID")
        first    = _trunc(r.get("First"), 100)
        last     = _trunc(r.get("Last"), 100)
        chosen   = _trunc(r.get("ChosenName"), 100)
        email    = _trunc(r.get("Email"), 200)
        phone    = _trunc(r.get("Phone"), 20)
        ext_id   = _trunc(r.get("ExternalId"), 100)
        ext_emp  = _trunc(r.get("ExternalEmployeeId"), 100)
        created  = _safe_dt(r.get("CreatedDate"))
        deleted  = _trunc(r.get("Deleted"), 10)
        del_dt   = _safe_dt(r.get("DeletedDate"))
        modified = _safe_dt(r.get("ModifiedDate"))
        v2guid   = _trunc(r.get("v2EmployeeGuid"), 50)

        params = (
            guid,                                                    # USING
            store_g, store_id, first, last, chosen, email, phone,    # UPDATE SET
            ext_id, ext_emp, created, deleted, del_dt, modified, v2guid,
            guid, store_g, store_id, first, last, chosen, email, phone,  # INSERT
            ext_id, ext_emp, created, deleted, del_dt, modified, v2guid,
        )
        action = cur.execute(merge_sql, params).fetchone()
        if action and str(action[0]).upper() == "INSERT":
            inserted += 1
        else:
            updated += 1
    conn.commit()
    return (inserted, updated)


def upsert_orders(conn: pyodbc.Connection, rows: list[dict]) -> int:
    """
    Delete existing orders for this store/date, then bulk INSERT.
    Much faster than per-row MERGE.
    """
    if not rows:
        return 0

    store_num = rows[0]["StoreNum"]
    biz_date = rows[0]["DateOfBusiness"]

    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM dbo.hstOrder WHERE StoreNum = ? AND DateOfBusiness = ?",
        store_num, biz_date
    )

    insert_sql = """
    INSERT INTO dbo.hstOrder (
        OrderID, DateOfBusiness, StoreID, StoreNum, RevenueCenter, OrderMode,
        Server, DisplayNumber, LastModifiedDevice, Source, Duration, DayPart,
        ExcessFood, Tab, RequiredPrepTime, ApprovalStatus, CreatedDevice,
        NumberOfGuests, ChannelID, PricingFeatures, CreatedDate, OpenedDate,
        EstimatedFulfillmentDate, ClosedDate, Deleted, DeletedDate,
        ModifiedDate, PromisedDate, Voided, VoidDate, VoidBusinessDate
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row in rows:
        params = (
            _trunc(row["OrderID"], 100), row["DateOfBusiness"],
            _trunc(row["StoreID"], 100), row["StoreNum"],
            _trunc(row.get("RevenueCenter"), 100),
            _trunc(row.get("OrderMode"), 100),
            _trunc(row.get("Server"), 100),
            _trunc(row.get("DisplayNumber"), 100),
            _trunc(row.get("LastModifiedDevice"), 100),
            _trunc(row.get("Source"), 100),
            row.get("Duration"),
            _trunc(row.get("DayPart"), 100),
            _trunc(row.get("ExcessFood"), 100),
            _trunc(row.get("Tab"), 100),
            _trunc(row.get("RequiredPrepTime"), 100),
            _trunc(row.get("ApprovalStatus"), 100),
            _trunc(row.get("CreatedDevice"), 100),
            row.get("NumberOfGuests", 0),
            _trunc(row.get("ChannelID"), 100),
            _trunc(row.get("PricingFeatures"), 100),
            _safe_dt(row.get("CreatedDate")), _safe_dt(row.get("OpenedDate")),
            _safe_dt(row.get("EstimatedFulfillmentDate")), _safe_dt(row.get("ClosedDate")),
            row.get("Deleted", 0), _safe_dt(row.get("DeletedDate")),
            _safe_dt(row.get("ModifiedDate")), _safe_dt(row.get("PromisedDate")),
            row.get("Voided", 0), _safe_dt(row.get("VoidDate")),
            row.get("VoidBusinessDate"),
        )
        cursor.execute(insert_sql, params)
    conn.commit()
    log.info("Bulk inserted %d order rows (delete + insert)", len(rows))
    return len(rows)


def upsert_payments(
    conn: pyodbc.Connection,
    rows: list[dict],
    *,
    store_num: int = None,
    biz_date=None,
) -> int:
    """
    Delete existing hstPayment rows for this store/date, then bulk INSERT.
    Idempotent — safe to re-run for the same store/date.

    store_num/biz_date are passed explicitly so we DELETE the scope correctly
    even when the orders pull returned zero payments (e.g. a 100% cash day —
    don't want to leave stale rows from a prior pull).
    """
    cursor = conn.cursor()

    # Resolve scope — prefer kwargs, fall back to first row
    if store_num is None and rows:
        store_num = rows[0]["StoreNum"]
    if biz_date is None and rows:
        biz_date = rows[0]["DateOfBusiness"]
    if store_num is None or biz_date is None:
        log.warning("upsert_payments called with no rows and no scope — skipping")
        return 0

    cursor.execute(
        "DELETE FROM dbo.hstPayment WHERE StoreNum = ? AND DateOfBusiness = ?",
        store_num, biz_date
    )

    if not rows:
        conn.commit()
        log.info("Cleared hstPayment for store %s on %s (no new rows to insert)", store_num, biz_date)
        return 0

    insert_sql = """
    INSERT INTO dbo.hstPayment (
        PaymentID, DateOfBusiness, DayPart, StoreID, StoreNum, PaymentType,
        OrderID, CheckID, MCARepaymentAmount, CreatedDevice, TenderTransactionGuid,
        Last4Digits, Refund, TipRefundAmount, RefundTxn, RefundStrategy,
        Server, SurchargeAmount, LastModifiedDevice, RefundStatus, OtherPaymentID,
        CardEntryMode, PaymentStatus, Amount, TipAmount, AmountTendered,
        CardType, HouseAccount, CardProcessorType, PaidTime, PaidBusinessDate,
        RefundTime, RefundBusinessDate, PaymentMethodID
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row in rows:
        params = (
            _trunc(row["PaymentID"], 36), row["DateOfBusiness"],
            _trunc(row.get("DayPart"), 36),
            _trunc(row.get("StoreID"), 36), row.get("StoreNum"),
            _trunc(row.get("PaymentType"), 50),
            _trunc(row.get("OrderID"), 36), _trunc(row.get("CheckID"), 36),
            row.get("MCARepaymentAmount"),
            _trunc(row.get("CreatedDevice"), 16),
            _trunc(row.get("TenderTransactionGuid"), 36),
            _trunc(row.get("Last4Digits"), 4),
            row.get("Refund", 0), row.get("TipRefundAmount", 0),
            row.get("RefundTxn"),       # varchar(MAX)
            row.get("RefundStrategy"),  # varchar(MAX)
            _trunc(row.get("Server"), 36),
            row.get("SurchargeAmount"),
            _trunc(row.get("LastModifiedDevice"), 16),
            _trunc(row.get("RefundStatus"), 50),
            _trunc(row.get("OtherPaymentID"), 36),
            _trunc(row.get("CardEntryMode"), 25),
            _trunc(row.get("PaymentStatus"), 50),
            row.get("Amount", 0), row.get("TipAmount", 0), row.get("AmountTendered", 0),
            _trunc(row.get("CardType"), 50),
            _trunc(row.get("HouseAccount"), 500),
            _trunc(row.get("CardProcessorType"), 50),
            _safe_dt(row.get("PaidTime")), row.get("PaidBusinessDate"),
            _trunc(row.get("RefundTime"), 50),
            _trunc(row.get("RefundBusinessDate"), 10),
            _trunc(row.get("PaymentMethodID"), 36),
        )
        cursor.execute(insert_sql, params)
    conn.commit()
    log.info("Bulk inserted %d payment rows (delete + insert)", len(rows))
    return len(rows)


def upsert_applied_discounts(
    conn: pyodbc.Connection,
    rows: list[dict],
    *,
    store_num: int = None,
    biz_date=None,
) -> int:
    """Delete existing hstAppliedDiscounts rows for this store/date, then bulk INSERT."""
    cursor = conn.cursor()

    if store_num is None and rows:
        store_num = rows[0]["StoreNum"]
    if biz_date is None and rows:
        biz_date = rows[0]["DateOfBusiness"]
    if store_num is None or biz_date is None:
        log.warning("upsert_applied_discounts called with no rows and no scope — skipping")
        return 0

    cursor.execute(
        "DELETE FROM dbo.hstAppliedDiscounts WHERE StoreNum = ? AND DateOfBusiness = ?",
        store_num, biz_date
    )

    if not rows:
        conn.commit()
        log.info("Cleared hstAppliedDiscounts for store %s on %s (no new rows to insert)", store_num, biz_date)
        return 0

    insert_sql = """
    INSERT INTO dbo.hstAppliedDiscounts (
        AppliedDiscountID, DateOfBusiness, StoreID, StoreNum, OrderID, CheckID,
        Approver, LoyaltyVendor, LoyaltyReference, DiscountID, DiscountType,
        DiscountName, DiscountPercent, DiscountAmount, AppliedPromoCode,
        NonTaxDiscountAmount
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row in rows:
        params = (
            _trunc(row["AppliedDiscountID"], 36), row["DateOfBusiness"],
            _trunc(row.get("StoreID"), 36), row.get("StoreNum"),
            _trunc(row.get("OrderID"), 36), _trunc(row.get("CheckID"), 36),
            _trunc(row.get("Approver"), 36),
            _trunc(row.get("LoyaltyVendor"), 100),
            _trunc(row.get("LoyaltyReference"), 100),
            _trunc(row.get("DiscountID"), 36),
            _trunc(row.get("DiscountType"), 50),
            _trunc(row.get("DiscountName"), 100),
            row.get("DiscountPercent"),
            row.get("DiscountAmount", 0),
            _trunc(row.get("AppliedPromoCode"), 100),
            row.get("NonTaxDiscountAmount", 0),
        )
        cursor.execute(insert_sql, params)
    conn.commit()
    log.info("Bulk inserted %d applied discount rows (delete + insert)", len(rows))
    return len(rows)


def upsert_service_charges(
    conn: pyodbc.Connection,
    rows: list[dict],
    *,
    store_num: int = None,
    biz_date=None,
) -> int:
    """Delete existing hstServiceCharges rows for this store/date, then bulk INSERT."""
    cursor = conn.cursor()

    if store_num is None and rows:
        store_num = rows[0]["StoreNum"]
    if biz_date is None and rows:
        biz_date = rows[0]["DateOfBusiness"]
    if store_num is None or biz_date is None:
        log.warning("upsert_service_charges called with no rows and no scope — skipping")
        return 0

    cursor.execute(
        "DELETE FROM dbo.hstServiceCharges WHERE StoreNum = ? AND DateOfBusiness = ?",
        store_num, biz_date
    )

    if not rows:
        conn.commit()
        log.info("Cleared hstServiceCharges for store %s on %s (no new rows to insert)", store_num, biz_date)
        return 0

    insert_sql = """
    INSERT INTO dbo.hstServiceCharges (
        GUID, DateOfBusiness, StoreID, StoreNum, OrderID, CheckID,
        ChargeType, ChargeAmount
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    for row in rows:
        params = (
            _trunc(row["GUID"], 36), row["DateOfBusiness"],
            _trunc(row.get("StoreID"), 36), row.get("StoreNum"),
            _trunc(row.get("OrderID"), 36), _trunc(row.get("CheckID"), 36),
            _trunc(row.get("ChargeType"), 75),
            row.get("ChargeAmount", 0),
        )
        cursor.execute(insert_sql, params)
    conn.commit()
    log.info("Bulk inserted %d service charge rows (delete + insert)", len(rows))
    return len(rows)


def upsert_items(
    conn: pyodbc.Connection,
    rows: list[dict],
    skip_truncate: bool = False,
    skip_proc: bool = False,
) -> int:
    """
    INSERT item rows into toast.dbo.hstItem_temp, then call
    sp_Update_Sold_Items_From_Check to merge them into hstItem.

    We use the temp table approach (same as Don's pipeline) because:
    1. hstItem has a complex composite MERGE key that's fragile
    2. sp_Update_Sold_Items_From_Check also populates DayPartID, ParentID,
       ItemID from cfg_RestaurantService and cfg_menu_items
    3. The proc is idempotent — running it on already-merged data is safe

    IMPORTANT: With default args (both skip flags False), this function must
    run serially — it TRUNCATEs the shared dbo.hstItem_temp and runs a proc.

    For batch backfill, callers can set skip_truncate=True and skip_proc=True
    so they can TRUNCATE once up front, run many parallel inserts, then call
    run_items_merge_proc() once at the end. In that mode the per-store work
    (DELETE from hstItem + INSERT into hstItem_temp) is safe to parallelize.
    """
    if not rows:
        return 0

    # Get store/date from first row to scope the delete
    store_id = rows[0]["StoreID"]      # restaurant GUID
    biz_date = rows[0]["DateOfBusiness"]

    cursor = conn.cursor()

    # Ensure hstItem_temp is empty (skipped in batch mode)
    if not skip_truncate:
        cursor.execute("TRUNCATE TABLE dbo.hstItem_temp")

    # DELETE existing hstItem rows for this store/date BEFORE the merge.
    # This prevents duplicates when our Python field values differ slightly
    # from Don's PHP field values in the MERGE key.
    log.info("Deleting existing hstItem rows for store %s on %s...", rows[0].get("StoreNum"), biz_date)
    cursor.execute(
        "DELETE FROM dbo.hstItem WHERE StoreID = ? AND DateOfBusiness = ?",
        store_id, biz_date
    )
    deleted = cursor.rowcount
    log.info("Deleted %d existing hstItem rows", deleted)
    conn.commit()

    # Multi-row VALUES batching — sends ~50 rows per round trip instead of 1.
    # Avoids fast_executemany's buffer-inference bug while still being 50x
    # faster than a per-row execute() loop. SQL Server parameter limit is
    # 2100, so with 32 cols per row we keep batch size at 50 (1600 params).
    column_list = """
        DateOfBusiness, StoreID, StoreNum, OrderID, CheckID,
        DayPart, DayPartID, SelectionID, ParentGUID, ItemGUID,
        ParentID, ItemID, ModificationID, ModifierItemID,
        SubModificationID, SubModifierItemID,
        Deferred, PreModifier, Quantity, SalesCategoryGUID,
        OrderMode, Price, DiscPric, Tax, TaxInclusion,
        SystemTime, Voided, VoidDate, VoidReason,
        AppliedDiscountAmount, MenuItemMLI, ParentMLI
    """
    COLS_PER_ROW = 32
    ROWS_PER_BATCH = 50
    single_row_placeholder = "(" + ",".join(["?"] * COLS_PER_ROW) + ")"

    def _row_params(row):
        return (
            row["DateOfBusiness"], _trunc(row["StoreID"], 100), row["StoreNum"],
            _trunc(row["OrderID"], 100), _trunc(row["CheckID"], 100),
            _trunc(row.get("DayPart"), 100), row.get("DayPartID"),
            _trunc(row.get("SelectionID"), 100),
            _trunc(row.get("ParentGUID"), 100),
            _trunc(row.get("ItemGUID"), 100),
            row.get("ParentID"), row.get("ItemID"),
            _trunc(row.get("ModificationID", "0"), 100),
            _trunc(row.get("ModifierItemID", ""), 100),
            _trunc(row.get("SubModificationID", "0"), 100),
            _trunc(row.get("SubModifierItemID", ""), 100),
            row.get("Deferred", 0),
            _trunc(row.get("PreModifier"), 200),
            row.get("Quantity", 0),
            _trunc(row.get("SalesCategoryGUID"), 100),
            _trunc(row.get("OrderMode"), 100),
            row.get("Price", 0), row.get("DiscPric", 0),
            row.get("Tax", 0),
            _trunc(row.get("TaxInclusion"), 50),
            _safe_dt(row.get("SystemTime")),
            row.get("Voided", 0),
            _safe_dt(row.get("VoidDate")),
            _trunc(row.get("VoidReason"), 200),
            row.get("AppliedDiscountAmount", 0),
            row.get("MenuItemMLI"), row.get("ParentMLI"),
        )

    for i in range(0, len(rows), ROWS_PER_BATCH):
        chunk = rows[i:i + ROWS_PER_BATCH]
        placeholders = ",".join([single_row_placeholder] * len(chunk))
        batch_sql = f"INSERT INTO dbo.hstItem_temp ({column_list}) VALUES {placeholders}"
        flat_params = []
        for row in chunk:
            flat_params.extend(_row_params(row))
        cursor.execute(batch_sql, flat_params)
    conn.commit()
    log.info("Bulk inserted %d rows into hstItem_temp (batched)", len(rows))

    # Now run sp_Update_Sold_Items_From_Check to merge temp → hstItem
    # This also populates DayPartID, ParentID, ItemID from config tables
    if not skip_proc:
        log.info("Running sp_Update_Sold_Items_From_Check...")
        cursor.execute("EXEC dbo.sp_Update_Sold_Items_From_Check")
        conn.commit()
        log.info("sp_Update_Sold_Items_From_Check complete — items merged into hstItem")

    return len(rows)


def run_items_merge_proc(conn: pyodbc.Connection) -> None:
    """Explicit entry point for running the items merge proc once after a
    batch backfill has accumulated rows in dbo.hstItem_temp."""
    cursor = conn.cursor()
    log.info("Running sp_Update_Sold_Items_From_Check (batch mode, once)...")
    cursor.execute("EXEC dbo.sp_Update_Sold_Items_From_Check")
    conn.commit()
    log.info("sp_Update_Sold_Items_From_Check complete.")


def direct_copy_items_from_temp(conn: pyodbc.Connection,
                                 store_num: int, biz_date: str) -> int:
    """
    Copy items directly from hstItem_temp → hstItem WITHOUT calling
    sp_Update_Sold_Items_From_Check.

    WHY THIS EXISTS
    ---------------
    sp_Update_Sold_Items_From_Check is encrypted and applies JOIN logic
    against cfg_RestaurantService / cfg_menu_items that produces a consistent
    ~8% row/amount inflation (confirmed 2026-04-23 during Memphis 1032 and
    Albany 1154 repolls — e.g. Memphis 4/17 went from $7,405 → $7,907 after
    the proc ran on fresh API data).

    This direct copy preserves every field that parse_orders provides from
    the Toast API. DayPartID will be NULL if parse_orders did not populate it;
    sp_toast_SalesDataByDayPart groups on DayPart (varchar) which IS populated
    from the API, so net-sales totals are unaffected.

    SCHEMA NOTE
    -----------
    hstItem has a MenuGroupMLI column that hstItem_temp does not — written NULL.
    UniqueID is an IDENTITY PK — omitted so SQL Server auto-assigns it.
    """
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO dbo.hstItem (
            DateOfBusiness, StoreID, StoreNum, OrderID, CheckID,
            DayPart, DayPartID, SelectionID, ParentGUID, ItemGUID,
            ParentID, ItemID, ModificationID, ModifierItemID,
            SubModificationID, SubModifierItemID,
            Deferred, PreModifier, Quantity, SalesCategoryGUID,
            OrderMode, Price, DiscPric, Tax, TaxInclusion,
            SystemTime, Voided, VoidDate, VoidReason,
            AppliedDiscountAmount, MenuGroupMLI, MenuItemMLI, ParentMLI
        )
        SELECT
            DateOfBusiness, StoreID, StoreNum, OrderID, CheckID,
            DayPart, DayPartID, SelectionID, ParentGUID, ItemGUID,
            ParentID, ItemID, ModificationID, ModifierItemID,
            SubModificationID, SubModifierItemID,
            Deferred, PreModifier, Quantity, SalesCategoryGUID,
            OrderMode, Price, DiscPric, Tax, TaxInclusion,
            SystemTime, Voided, VoidDate, VoidReason,
            AppliedDiscountAmount, NULL, MenuItemMLI, ParentMLI
        FROM dbo.hstItem_temp
        WHERE StoreNum = ? AND DateOfBusiness = ?
    """, store_num, biz_date)
    inserted = cursor.rowcount
    conn.commit()
    log.info(
        "direct_copy_items_from_temp: %d rows → hstItem  store=%s  date=%s",
        inserted, store_num, biz_date,
    )
    return inserted


# ── Pipeline Health queries ───────────────────────────────────────────────────

def get_overnight_job_status(conn: pyodbc.Connection, hours_back: int = 24) -> list:
    """Get SQL Agent job history for the last N hours."""
    rows = conn.execute("""
        SELECT TOP 50
            j.name AS job_name,
            j.enabled,
            h.run_date,
            h.run_time,
            h.run_duration,
            CASE h.run_status
                WHEN 0 THEN 'Failed'
                WHEN 1 THEN 'Succeeded'
                WHEN 2 THEN 'Retry'
                WHEN 3 THEN 'Canceled'
                WHEN 4 THEN 'In Progress'
            END AS status,
            h.message,
            h.step_id
        FROM msdb.dbo.sysjobhistory h
        JOIN msdb.dbo.sysjobs j ON h.job_id = j.job_id
        WHERE h.step_id = 0
          AND CONVERT(datetime,
              STUFF(STUFF(CAST(h.run_date AS varchar), 5, 0, '-'), 8, 0, '-')
              + ' ' +
              STUFF(STUFF(RIGHT('000000' + CAST(h.run_time AS varchar), 6), 3, 0, ':'), 6, 0, ':')
          ) > DATEADD(HOUR, ?, GETDATE())
        ORDER BY h.run_date DESC, h.run_time DESC
    """, -abs(hours_back)).fetchall()
    return [{
        "job_name": r[0], "enabled": r[1], "run_date": r[2],
        "run_time": r[3], "run_duration": r[4], "status": r[5],
        "message": (r[6] or "")[:300], "step_id": r[7],
    } for r in rows]


def get_table_build_status(conn: pyodbc.Connection, hours_back: int = 24) -> list:
    """Get recent Table_Builds status from Logging database."""
    rows = conn.execute("""
        SELECT TOP 30
            DB_Name, Proc_Name, Start_Time, Completion_Time,
            Completion_Flag, Error_Number, Error_Line, Error_Message
        FROM Logging.dbo.Table_Builds
        WHERE Start_Time > DATEADD(HOUR, ?, GETDATE())
        ORDER BY Start_Time DESC
    """, -abs(hours_back)).fetchall()
    return [{
        "db_name": r[0], "proc_name": r[1],
        "start_time": str(r[2]) if r[2] else None,
        "completion_time": str(r[3]) if r[3] else None,
        "success": bool(r[4]) if r[4] is not None else None,
        "error_number": r[5], "error_line": r[6],
        "error_message": (r[7] or "")[:300],
    } for r in rows]


def get_proc_run_status(conn: pyodbc.Connection, hours_back: int = 24) -> list:
    """Get recent SP_Run_Times from Logging database."""
    rows = conn.execute("""
        SELECT TOP 50
            DB_Name, Proc_Name, Start_Time, Completion_Time,
            Completion_Flag, Error_Number, Error_Line, Error_Message
        FROM Logging.dbo.SP_Run_Times
        WHERE Start_Time > DATEADD(HOUR, ?, GETDATE())
        ORDER BY Start_Time DESC
    """, -abs(hours_back)).fetchall()
    return [{
        "db_name": r[0], "proc_name": r[1],
        "start_time": str(r[2]) if r[2] else None,
        "completion_time": str(r[3]) if r[3] else None,
        "success": bool(r[4]) if r[4] is not None else None,
        "error_number": r[5], "error_line": r[6],
        "error_message": (r[7] or "")[:300],
    } for r in rows]


def get_toast_poll_errors(conn: pyodbc.Connection, hours_back: int = 24) -> list:
    """Get recent Toast puller errors from php_logs."""
    rows = conn.execute("""
        SELECT TOP 30
            uniqueid, channel, level,
            LEFT(message, 500) AS message, log_time
        FROM Logging.dbo.php_logs
        WHERE channel = 'newks/toast_read_api'
          AND level NOT LIKE '%%Info%%'
          AND log_time > DATEADD(HOUR, ?, GETDATE())
        ORDER BY uniqueid DESC
    """, -abs(hours_back)).fetchall()
    return [{
        "id": r[0], "channel": r[1], "level": r[2],
        "message": r[3], "log_time": str(r[4]) if r[4] else None,
    } for r in rows]


def get_store_poll_completeness(conn: pyodbc.Connection, business_date: str) -> list:
    """
    Compare hstCheck counts to hstItem distinct check counts per store.
    Flags stores where item data might be incomplete.
    """
    rows = conn.execute("""
        WITH check_counts AS (
            SELECT StoreNum, COUNT(*) AS check_count,
                   MIN(OpenedTime) AS earliest_open,
                   MAX(ClosedTime) AS latest_close,
                   SUM(Amount) AS check_amount
            FROM toast.dbo.hstCheck
            WHERE DateOfBusiness = ?
              AND Voided = 0 AND Deleted = 0
            GROUP BY StoreNum
        ),
        item_counts AS (
            SELECT StoreNum, COUNT(DISTINCT CheckID) AS item_check_count,
                   SUM(DiscPric) AS item_net
            FROM toast.dbo.hstItem
            WHERE DateOfBusiness = ?
              AND Voided = 0 AND Deferred = 0
            GROUP BY StoreNum
        ),
        rollup AS (
            SELECT RestaurantID, SUM(NetSales) AS rollup_net, SUM(CheckCount) AS rollup_checks
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            WHERE DateOfBusiness = ?
            GROUP BY RestaurantID
        )
        SELECT
            c.StoreNum,
            c.check_count,
            ISNULL(i.item_check_count, 0) AS item_check_count,
            c.check_count - ISNULL(i.item_check_count, 0) AS check_item_gap,
            c.earliest_open,
            c.latest_close,
            c.check_amount,
            ISNULL(i.item_net, 0) AS item_net,
            ISNULL(r.rollup_net, 0) AS rollup_net,
            ISNULL(r.rollup_checks, 0) AS rollup_checks
        FROM check_counts c
        LEFT JOIN item_counts i ON c.StoreNum = i.StoreNum
        LEFT JOIN rollup r ON c.StoreNum = r.RestaurantID
        WHERE c.check_count - ISNULL(i.item_check_count, 0) > 5
           OR ISNULL(i.item_check_count, 0) = 0
        ORDER BY (c.check_count - ISNULL(i.item_check_count, 0)) DESC
    """, business_date, business_date, business_date).fetchall()
    return [{
        "store_num": r[0], "check_count": r[1], "item_check_count": r[2],
        "gap": r[3], "earliest_open": str(r[4]) if r[4] else None,
        "latest_close": str(r[5]) if r[5] else None,
        "check_amount": float(r[6] or 0), "item_net": float(r[7] or 0),
        "rollup_net": float(r[8] or 0), "rollup_checks": int(r[9] or 0),
    } for r in rows]


def get_truncated_poll_days(conn: pyodbc.Connection, business_date: str) -> list:
    """
    Find stores where the latest check close time is suspiciously early
    compared to normal (before 7 PM local CDT). This is the fingerprint
    of a Toast API 504 timeout that truncated the data mid-day.

    Compares each store's latest_close to the median across all stores
    for the same date. Any store closing 2+ hours before the median is flagged.
    """
    rows = conn.execute("""
        WITH store_closes AS (
            SELECT
                StoreNum,
                COUNT(*) AS check_count,
                DATEADD(HOUR, -5, MAX(ClosedTime)) AS latest_close_cdt,
                SUM(Amount) AS check_amount
            FROM toast.dbo.hstCheck
            WHERE DateOfBusiness = ?
              AND Voided = 0 AND Deleted = 0
            GROUP BY StoreNum
        ),
        ranked AS (
            SELECT latest_close_cdt,
                   ROW_NUMBER() OVER (ORDER BY latest_close_cdt DESC) AS rn,
                   COUNT(*) OVER () AS total
            FROM store_closes
        ),
        normal_close AS (
            SELECT latest_close_cdt AS normal_close_time
            FROM ranked
            WHERE rn = total / 4
        )
        SELECT
            sc.StoreNum,
            sc.check_count,
            sc.latest_close_cdt,
            nc.normal_close_time,
            DATEDIFF(MINUTE, sc.latest_close_cdt, nc.normal_close_time) AS minutes_early,
            sc.check_amount
        FROM store_closes sc
        CROSS JOIN normal_close nc
        WHERE DATEDIFF(MINUTE, sc.latest_close_cdt, nc.normal_close_time) > 90
          AND sc.check_count > 5
        ORDER BY minutes_early DESC
    """, business_date).fetchall()
    return [{
        "store_num": r[0], "check_count": r[1],
        "latest_close_cdt": str(r[2]) if r[2] else None,
        "normal_close_time": str(r[3]) if r[3] else None,
        "minutes_early": r[4],
        "check_amount": float(r[5] or 0),
    } for r in rows]


def get_aloha_toast_transition_gaps(conn: pyodbc.Connection) -> list:
    """
    Find stores that transitioned from Aloha to Toast where the rollup table
    is missing the Aloha-era data (before ToastLive date).

    Root cause: sp_SalesDataByDayPart (Aloha version) has an EXCEPT clause
    that excludes all stores in storegroup 243 (Toast stores) — even for
    dates when they were still on Aloha. This creates a gap between
    the store's open date and their ToastLive date.
    """
    rows = conn.execute("""
        SELECT
            s.StoreID,
            s.StoreName,
            s.ToastLive,
            g.OpenDate,
            r.rollup_earliest
        FROM contacts.dbo.store s
        JOIN contacts.dbo.internal_StoreGroupMembers m
            ON s.StoreID = m.StoreID AND m.StoreGroupID = 243
        JOIN dev_aloha.dbo.gblstore g
            ON s.StoreID = g.StoreId
        LEFT JOIN (
            SELECT RestaurantID, MIN(DateOfBusiness) AS rollup_earliest
            FROM dev_aloha.dbo.tbl_SalesDataByDayPart
            GROUP BY RestaurantID
        ) r ON s.StoreID = r.RestaurantID
        WHERE s.ToastLive > g.OpenDate
          AND s.ToastLive < '9999-01-01'
          AND s.ToastLive > '2025-01-01'
          AND (r.rollup_earliest IS NULL OR r.rollup_earliest >= s.ToastLive)
    """).fetchall()

    gaps = []
    for r in rows:
        store_id, store_name, toast_live, open_date, rollup_earliest = r
        gaps.append({
            "store_id": store_id,
            "store_name": store_name,
            "toast_live": str(toast_live) if toast_live else None,
            "open_date": str(open_date) if open_date else None,
            "rollup_earliest": str(rollup_earliest) if rollup_earliest else None,
            "aloha_source_rows": 0,
            "missing_days": ((toast_live.date() if hasattr(toast_live, 'date') else toast_live) - (open_date.date() if hasattr(open_date, 'date') else open_date)).days if toast_live and open_date else 0,
        })
    return gaps


def rerun_proc(conn: pyodbc.Connection, proc_name: str, params: str = "") -> dict:
    """Re-run a stored procedure by name."""
    allowed_procs = [
        "dev_aloha.dbo.sp_toast_SalesDataByDayPart",
        "dev_aloha.dbo.sp_SalesDataByDayPart",
        "toast.dbo.sp_Update_Sold_Items_From_Check",
        "toast.dbo.sp_Update_Check_Discounts",
        "toast.dbo.sp_toast_update_guest_counts_from_items_sold",
        "toast.dbo.sp_copy_from_psql_devjan02",
    ]
    if proc_name not in allowed_procs:
        return {"success": False, "error": f"Proc '{proc_name}' not in allowed list"}
    try:
        sql = f"EXEC {proc_name} {params}" if params else f"EXEC {proc_name}"
        log.info("Re-running: %s", sql)
        conn.execute(sql)
        conn.commit()
        return {"success": True, "proc": proc_name}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Rollup helper ────────────────────────────────────────────────────────────

def run_rollup(conn: pyodbc.Connection, start_date: str, end_date: str,
               store_grouping: int = 8, store_group: int = None) -> None:
    """
    Run sp_toast_SalesDataByDayPart to rebuild the rollup table.
    This is what SSRS reports read from.
    """
    sql = (
        f"EXEC dev_aloha.dbo.sp_toast_SalesDataByDayPart "
        f"@StartDate = '{start_date}', @EndDate = '{end_date}', "
        f"@StoreGrouping = {store_grouping}, @StoreGroup = {store_group}"
    )
    log.info("Running rollup: %s", sql)
    conn.execute(sql)
    conn.commit()
    log.info("Rollup complete")


def fix_daypart_ids(conn: pyodbc.Connection, start_date: str, end_date: str) -> int:
    """
    Back-fill hstItem.DayPartID from the GUID stored in hstItem.DayPart.

    The nightly PHP pull and Python repoll both populate hstItem.DayPart (a GUID
    varchar) but leave hstItem.DayPartID = NULL because the GUID→integer lookup
    is not done at insert time.  DayPartID = NULL causes the Flash Report channel
    breakdown (DineIn / ToGo / CallIn / OLO / GNG / CheckCount) to show 0.

    Joins cfg_RestaurantService to resolve GUID → integer ID and updates in one
    statement.  Safe to run multiple times — the WHERE DayPartID IS NULL clause
    means already-fixed rows are never touched.

    Returns the number of rows updated.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE hi
        SET    hi.DayPartID = rs.DayPartID
        FROM   toast.dbo.hstItem hi
        JOIN   toast.dbo.cfg_RestaurantService rs
            ON rs.GUID = hi.DayPart
        WHERE  hi.DateOfBusiness BETWEEN ? AND ?
          AND  hi.DayPartID IS NULL
          AND  hi.DayPart   IS NOT NULL
    """, start_date, end_date)
    conn.commit()
    updated = cur.rowcount
    log.info("fix_daypart_ids %s → %s: %d rows updated", start_date, end_date, updated)
    return updated
