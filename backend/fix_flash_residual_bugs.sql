-- ============================================================
-- fix_flash_residual_bugs.sql
-- The three small bugs left after the May 2026 DayPartID repair
-- (fix_daypartid_may2026.sql). Together ≈ $40K/month chain-wide:
--
--   1. X-Dispatch (Generic) dining option unmapped        ~$11.1K/mo
--   2. OrderModeID 7 (Union Aramark / POS 2) no MERGE     ~$11.1K/mo
--   3. DineInOLO MERGE string mismatch in the proc        ~$19.6K/mo
--
-- Bugs 2 and 3 are edits INSIDE sp_toast_SalesDataByDayPart —
-- do those by hand in SSMS (Part B below). Bug 1 + the rebuild +
-- verification are runnable sections in this file.
-- ============================================================

USE toast
GO

-- ============================================================
-- PART A — Map X-Dispatch (Generic) → Delivery
-- GUID confirmed unmapped on 2026-06-10: $11,062 / 1,075 items
-- in May 2026 fell through to NetSales with no channel column.
-- OrderModeID 102 = 'Delivery' → DeliveryNetSales, which the
-- Flash Report's DeliveryNet formula already includes.
-- ============================================================

-- A1: confirm it's still unmapped (expect 0 rows)
SELECT * FROM toast.dbo.cfg_map_DiningOptionToOrderMode
WHERE GUID = '7eaf47bf-34be-40dc-8e81-662005846864'
GO

-- A2: add the mapping
INSERT INTO toast.dbo.cfg_map_DiningOptionToOrderMode (GUID, OrderModeID, FlashTab, FlashSort)
VALUES ('7eaf47bf-34be-40dc-8e81-662005846864', 102, 'DeliveryNet', 6)
GO

-- ============================================================
-- PART B — Edit sp_toast_SalesDataByDayPart (manual, in SSMS)
--
-- Object Explorer → dev_aloha → Programmability → Stored Procedures
-- → sp_toast_SalesDataByDayPart → right-click → Script Stored
-- Procedure as → ALTER To → New Query Editor Window. Then:
--
-- EDIT 1 (DineInOLO mismatch, ~$19.6K/mo):
--   The sales_cat CASE labels OrderModeID 104 as 'DineInOLO', but
--   the MERGE that fills OLODineInNetSales looks for the string
--   'OLODineInNetSales' — it never matches, so X-Dine-In-OLO sales
--   vanish from the channel breakdown.
--   FIND:     and r.sales_cat = 'OLODineInNetSales'
--   REPLACE:  and r.sales_cat = 'DineInOLO'
--
-- EDIT 2 (POS2 never populated, ~$11.1K/mo):
--   OrderModeID 7 (Union Aramark / POS 2) falls into the CASE's
--   else branch (sales_cat = '7') and no MERGE matches it, so the
--   POS2 column — and the report's POS2 tab — is always 0.
--   2a. In the SECOND CASE (the one returning names like 'GrubHub',
--       'OLO', 'Catering'), add this line just above the final else:
--           when OrderModeID = 7 then 'POS2'
--   2b. Add a MERGE block for it. Copy any existing block (e.g. the
--       DispatchSkipCart one) and paste this after it:
--
--           merge #SDDP s
--           using #Results r
--           on s.DateOfBusiness = r.DateOfBusiness
--           and s.RestaurantID = r.fkStoreID
--           and s.DayPartID = r.Period
--           and r.sales_cat = 'POS2'
--           when matched then update set
--            POS2 = isnull(r.Net,0);
--
-- Then execute the ALTER. It should report "Commands completed
-- successfully."
-- ============================================================

-- ============================================================
-- PART C — Rebuild May with all three fixes in place
-- ============================================================

DELETE FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE DateOfBusiness BETWEEN '2026-05-01' AND '2026-05-31'

SELECT @@ROWCOUNT AS RollupRowsDeleted
GO

EXEC dev_aloha.dbo.sp_toast_SalesDataByDayPart
    @StartDate     = '2026-05-01',
    @EndDate       = '2026-05-31',
    @StoreGrouping = 9,
    @StoreGroup    = 243
GO

-- ============================================================
-- PART D — Verify: Internal_Gap should drop from ~$39.8K to
-- near zero (a few hundred dollars of genuinely-unattributable
-- rows, e.g. items whose ordermode GUID isn't in cfg_DiningOptions
-- at all — $68 in May).
-- ============================================================

SELECT
    SUM(NetSales)         AS Total_NetSales,
    SUM(OLODineInNetSales) AS DineInOLO_now_populated,
    SUM(POS2)              AS POS2_now_populated,
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

-- ============================================================
-- NOTES
-- -----
-- * These three bugs are historical — April and earlier periods
--   have the same leaks. Once May verifies clean, you can rebuild
--   older periods the same way (Part C with different dates) if
--   anyone needs accurate channel history. POS2 (Aramark) sales
--   only started 2026-05-01, so it only matters from May onward.
-- * Royalty rebuild not needed: these fixes move dollars BETWEEN
--   channel columns; NetSales (which royalty reads) is unchanged.
-- ============================================================
