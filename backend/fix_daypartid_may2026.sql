-- ============================================================
-- fix_daypartid_may2026.sql
-- Run in SSMS against toast / dev_aloha.
--
-- PURPOSE
-- -------
-- Same bug as April (see fix_daypartid_post_repoll.sql): a May 4-29
-- repoll/backfill populated hstItem.DayPart (GUID) correctly but left
-- hstItem.DayPartID = NULL.  Don's sp_toast_SalesDataByDayPart MERGEs
-- every channel column on DayPartID, and NULL never matches a MERGE
-- join — so $8.46M of May sales landed in NetSales with NO channel
-- breakdown.  Flash Report showed NonCatering correct but every order
-- mode (DineIn/ToGo/CallIn/OLO/Delivery/CurbSide) short by 30-45%.
--
-- Diagnosed 2026-06-10.  NULL DayPartID rows: 968,585 items, $8.46M,
-- all 99 stores, dates 2026-05-04 .. 2026-05-29.
--
-- RUN ORDER
-- ---------
-- Execute each section ONE AT A TIME (highlight → F5).
-- ============================================================

USE toast
GO

-- ============================================================
-- SECTION 1 — SANITY CHECK: count NULLs before fix
-- Expected: ~968,585 rows, 99 stores, 2026-05-04 .. 2026-05-29
-- ============================================================

SELECT
    COUNT(*)                 AS NullDayPartID_rows,
    COUNT(DISTINCT StoreNum) AS Stores,
    MIN(DateOfBusiness)      AS EarliestDate,
    MAX(DateOfBusiness)      AS LatestDate
FROM toast.dbo.hstItem
WHERE DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'
  AND DayPartID IS NULL
  AND DayPart IS NOT NULL   -- has the GUID, just missing the integer ID

GO

-- ============================================================
-- SECTION 2 — UPDATE DayPartID
-- Same join as April fix + nightly_pull's fix_daypart_ids.
-- Idempotent: WHERE DayPartID IS NULL never touches fixed rows.
-- ============================================================

UPDATE hi
SET    hi.DayPartID = rs.DayPartID
FROM   toast.dbo.hstItem               hi
JOIN   toast.dbo.cfg_RestaurantService rs
    ON rs.GUID = hi.DayPart
WHERE  hi.DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'
  AND  hi.DayPartID IS NULL
  AND  hi.DayPart   IS NOT NULL

SELECT @@ROWCOUNT AS RowsUpdated

GO

-- ============================================================
-- SECTION 3 — VERIFY: count NULLs after fix
-- Expected: 0 or near 0 (leftovers = DayPart GUIDs not present in
-- cfg_RestaurantService; small remainder is acceptable)
-- ============================================================

SELECT
    COUNT(*)                 AS NullDayPartID_rows_remaining,
    SUM(DiscPric)            AS NetSales_remaining,
    COUNT(DISTINCT StoreNum) AS Stores
FROM toast.dbo.hstItem
WHERE DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'
  AND DayPartID IS NULL
  AND DayPart IS NOT NULL

GO

-- ============================================================
-- SECTION 4 — REBUILD ROLLUP (tbl_SalesDataByDayPart) FOR MAY
-- Runtime: a few minutes.
-- ============================================================

-- 4a: Delete stale rows (belt and suspenders — the proc also deletes
-- the store/date combos it rebuilds, but this catches any combos the
-- proc's store filter would miss)
DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'

SELECT @@ROWCOUNT AS RollupRowsDeleted

GO

-- 4b: Rebuild from hstItem (now with correct DayPartID)
EXEC dev_aloha.dbo.sp_toast_SalesDataByDayPart
    @StartDate     = '2026-05-01',
    @EndDate       = '2026-05-31',
    @StoreGrouping = 9,
    @StoreGroup    = 243

GO

-- ============================================================
-- SECTION 5 — REBUILD ROYALTY TABLE
-- Royalty values are computed from tbl_SalesDataByDayPart.
-- Start at 2026-04-27 = fiscal week start (Monday) of the week
-- containing May 1, mirroring the April fix's pattern.
-- ============================================================

EXEC dev_aloha.dbo.sp_tbl_Royalty_Values
    @StartDate = '2026-04-27',
    @EndDate   = '2026-05-31'

GO

-- ============================================================
-- SECTION 6 — RECONCILE: channel columns vs NetSales
-- Internal_Gap should drop from $5.5M to roughly $50K
-- (residual known-small bugs: unmapped X-Dispatch Generic ~$11K,
-- OrderModeID=7/POS2 no-MERGE ~$11K, DineInOLO MERGE string
-- mismatch ~$20K, catering-mode/non-catering-category ~$10K).
-- Expected channel ballparks from raw hstItem (May 2026):
--   DineIn ~$7.93M, OLO ~$2.43M, ToGo ~$2.35M, DoorDash ~$1.80M,
--   CallIn ~$570K, UberEats ~$424K
-- ============================================================

SELECT
    SUM(NetSales)         AS Total_NetSales,
    SUM(DineInNetSales)   AS DineIn,
    SUM(OLONetSales)      AS OLO,
    SUM(ToGoNetSales)     AS ToGo,
    SUM(DoorDashNetSales) AS DoorDash,
    SUM(CallInnetsales)   AS CallIn,
    SUM(UberEatsNetSales) AS UberEats,
    SUM(NetSales) - SUM(
        CallInnetsales + OLOCallInnetsales +
        ToGoNetSales + DriveThruNetSales + OLODriveThruNetSales +
        OLONetSales +
        DineInNetSales + OLODineInNetSales +
        DispatchOLONetSales + DeliveryNetSales + NwkDeliveryNetSales + RailsOLONetSales +
        BiteSquadNetSales + DoorDashNetSales + PostmatesNetSales + GoogleNetSales +
        DeliverClubNetSales + UberEatsNetSales + WaitrNet + FavorNet +
        NwkDelivNetDisp + DoorDashNetDisp + PostmatesNetDisp + LyftNetDisp +
        DeliverLogicNetDisp + WaitrNetDisp + GrubHubRailsNet +
        DispatchSkipCart + DispatchRailsAI + FoodsbyNet +
        CurbSideOLONetSales + CurbSideNetSales +
        CateringNetSales + EZCaterNetSales + POS2
    ) AS Internal_Gap
FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'

GO
