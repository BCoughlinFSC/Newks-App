-- ============================================================
-- ytd_dinein_pct_by_store.sql   (run in SSMS)
--
-- Per-store YTD Dine-In % of net sales (one-off for the boss).
-- Same source + bucket definition as the Daily Sales Flash Report, so it ties:
--   DineInNet = DineInNetSales + OLODineInNetSales  (OrderModeIDs 1, 107, 104)
--   Net       = NetSales
--   Dine-In % = DineInNet / Net
--
-- BASIS: CALENDAR YTD (Jan 1 -> @AsOf), matching the Flash's YTD column.
-- Store set = get_StoresIncluded(9,'4') = "All Reportable Restaurants".
--   (swap '4' for a different StoreGroupId to scope it differently.)
--
-- EXPORT: run, then right-click the grid -> Save Results As -> CSV.
-- ============================================================
USE dev_aloha;
DECLARE @Start date = '2026-01-01';
DECLARE @AsOf  date = '2026-06-22';   -- YTD through this date (set to your as-of)

SELECT
    RestaurantID     = CASE WHEN GROUPING(sddp.RestaurantID) = 1 THEN NULL
                            ELSE sddp.RestaurantID END,
    RestaurantName   = CASE WHEN GROUPING(sddp.RestaurantID) = 1 THEN 'ALL REPORTABLE (chain total)'
                            ELSE MAX(sddp.RestaurantName) END,
    NetSales         = SUM(sddp.NetSales),
    DineInNet        = SUM(sddp.DineInNetSales + sddp.OLODineInNetSales),
    DineIn_Pct_ofNet = CASE WHEN SUM(sddp.NetSales) = 0 THEN NULL
                            ELSE ROUND(100.0 * SUM(sddp.DineInNetSales + sddp.OLODineInNetSales)
                                              / SUM(sddp.NetSales), 1) END
FROM dev_aloha.dbo.tbl_SalesDataByDayPart sddp
WHERE sddp.DateOfBusiness BETWEEN @Start AND @AsOf
  AND sddp.RestaurantID IN (SELECT storeid FROM dev_aloha.dbo.get_StoresIncluded(9, '4', @AsOf))
GROUP BY sddp.RestaurantID WITH ROLLUP
ORDER BY GROUPING(sddp.RestaurantID), DineIn_Pct_ofNet DESC;
