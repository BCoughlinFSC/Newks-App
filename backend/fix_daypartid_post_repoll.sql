-- ============================================================
-- fix_daypartid_post_repoll.sql
-- Run in SSMS against toast / dev_aloha after repoll_chain_apr17_22.py
-- completes all 6 dates (4/17-4/22).
--
-- PURPOSE
-- -------
-- The chain-wide repoll populated hstItem.DayPart (GUID varchar) correctly
-- from the Toast API, but left hstItem.DayPartID = NULL because parse_orders
-- doesn't do the GUID→integer lookup.  DayPartID = NULL causes the Flash
-- Report channel breakdown (DineIn / ToGo / CallIn / OLO / GNG / CheckCount)
-- to show 0 for all repoll'd dates.  This script fixes that cleanly without
-- touching NetSales values or re-running sp_Update_Sold_Items_From_Check.
--
-- RUN ORDER
-- ---------
-- Execute each section ONE AT A TIME (highlight → F5).
-- Section 0 must be verified before running Section 2.
-- ============================================================

USE toast
GO

-- ============================================================
-- SECTION 0 — INSPECT cfg_RestaurantService
-- Run this first. Confirm the GUID column name and the integer
-- ID column name before running Section 2.
-- ============================================================

SELECT TOP 20 * FROM toast.dbo.cfg_RestaurantService ORDER BY 1

-- Also check what column names exist:
SELECT COLUMN_NAME, DATA_TYPE
FROM toast.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'cfg_RestaurantService'
ORDER BY ORDINAL_POSITION

GO

-- ============================================================
-- SECTION 1 — SANITY CHECK: count NULLs before fix
-- Expected: large number (one row per item for all repoll'd stores/dates)
-- ============================================================

SELECT
    COUNT(*)              AS NullDayPartID_rows,
    COUNT(DISTINCT StoreNum) AS Stores,
    MIN(DateOfBusiness)   AS EarliestDate,
    MAX(DateOfBusiness)   AS LatestDate
FROM toast.dbo.hstItem
WHERE DateOfBusiness BETWEEN '2026-04-17' AND '2026-04-22'
  AND DayPartID IS NULL
  AND DayPart IS NOT NULL   -- has the GUID, just missing the integer ID

GO

-- ============================================================
-- SECTION 2 — UPDATE DayPartID
--
-- !! STOP HERE !! — Before running this section, verify from
-- Section 0 output that:
--   • The GUID column in cfg_RestaurantService is named "GUID"
--   • The integer ID column is named "ID"
-- If the column names differ, update the JOIN and SET below.
-- ============================================================

UPDATE hi
SET    hi.DayPartID = rs.DayPartID
FROM   toast.dbo.hstItem        hi
JOIN   toast.dbo.cfg_RestaurantService rs
    ON rs.GUID = hi.DayPart
WHERE  hi.DateOfBusiness BETWEEN '2026-04-17' AND '2026-04-22'
  AND  hi.DayPartID IS NULL
  AND  hi.DayPart   IS NOT NULL

-- Print rows affected:
SELECT @@ROWCOUNT AS RowsUpdated

GO

-- ============================================================
-- SECTION 3 — VERIFY: count NULLs after fix
-- Expected: 0 (or close to 0 — a small number means those items
-- had a DayPart GUID not found in cfg_RestaurantService, which
-- is acceptable and won't cause issues)
-- ============================================================

SELECT
    COUNT(*)              AS NullDayPartID_rows_remaining,
    COUNT(DISTINCT StoreNum) AS Stores
FROM toast.dbo.hstItem
WHERE DateOfBusiness BETWEEN '2026-04-17' AND '2026-04-22'
  AND DayPartID IS NULL
  AND DayPart IS NOT NULL

GO

-- ============================================================
-- SECTION 4 — REBUILD ROLLUP (tbl_SalesDataByDayPart)
-- This refreshes the Flash Report data for all 6 repoll'd dates.
-- Runtime: ~2-5 minutes.
-- ============================================================

-- 4a: Delete stale rows
DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE DateOfBusiness BETWEEN '2026-04-17' AND '2026-04-22'

SELECT @@ROWCOUNT AS RollupRowsDeleted

GO

-- 4b: Rebuild from hstItem (now has correct DayPartID)
EXEC dev_aloha.dbo.sp_toast_SalesDataByDayPart
    @StartDate     = '2026-04-17',
    @EndDate       = '2026-04-22',
    @StoreGrouping = 9,
    @StoreGroup    = 243

GO

-- ============================================================
-- SECTION 5 — REBUILD ROYALTY TABLE
-- Royalty values are computed from tbl_SalesDataByDayPart.
-- NetSales shouldn't change (DayPartID only affects channel
-- distribution, not totals), but run this to be safe and to
-- keep the royalty table consistent with the fresh rollup.
-- Runtime: ~1 minute.
-- ============================================================

EXEC dev_aloha.dbo.sp_tbl_Royalty_Values
    @StartDate = '2026-04-13',
    @EndDate   = '2026-04-22'

GO

-- ============================================================
-- SECTION 6 — FINAL CHECK: spot-check a store in rollup
-- Verify NetSales and CheckCount look reasonable for one store.
-- Replace 1005 with any store you want to spot-check.
-- ============================================================

SELECT
    DateOfBusiness,
    RestaurantID,
    DayPartID,
    NetSales,
    CheckCount,
    DineInNet,
    ToGoNet,
    CallInNet
FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE RestaurantID  = 1005           -- Hattiesburg — change as needed
  AND DateOfBusiness BETWEEN '2026-04-17' AND '2026-04-22'
ORDER BY DateOfBusiness, DayPartID

GO
