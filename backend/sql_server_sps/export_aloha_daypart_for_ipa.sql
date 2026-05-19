/*
 * export_aloha_daypart_for_ipa.sql
 *
 * One-time historic export from dev_aloha.dbo.tbl_SalesDataByDayPart for the
 * IPA Sales Export's LY (last-year) data path. Pulls per-(date, store, daypart)
 * sales rows with the per-3PD breakouts that aloha_sales_data lacks.
 *
 * HOW TO RUN:
 *   1. Open this file in SSMS.
 *   2. Make sure your default database has access to dev_aloha (or run against
 *      the dev_aloha database directly).
 *   3. Tools > Options > Query Results > SQL Server > Results to Grid:
 *        - "Include column headers when copying or saving the results"  -> CHECKED
 *   4. Execute (F5).
 *   5. Right-click the results grid > "Save Results As..." > pick CSV format.
 *      Save as: aloha_daypart_export.csv  (anywhere local — e.g. Desktop\Kim\)
 *   6. Tell Claude where you saved it.
 *
 * Date range: 2024-01-01 onward. That covers LY for any export through 2026
 * and beyond. Historic Aloha data doesn't change, so this is a one-time pull
 * (no ongoing SQL Server dependency).
 */

SELECT
    -- Identity / grain
    CONVERT(varchar(10), DateOfBusiness, 23)    AS business_date,   -- YYYY-MM-DD
    RestaurantID                                AS location,
    DayPartID                                   AS daypart_id,
    DayPart                                     AS daypart,

    -- Validation totals (sum across channels should equal net_sales, modulo GNG cross-cut)
    NetSales                                    AS net_sales,
    GrossSales                                  AS gross_sales,

    -- Catering
    CateringNetSales                            AS catering_net_sales,
    EZCaterNetSales                             AS ezcater_net_sales,

    -- Retail core
    DineInNetSales                              AS dinein_net_sales,
    OLODineInNetSales                           AS olo_dinein_net_sales,
    ToGoNetSales                                AS togo_net_sales,
    DriveThruNetSales                           AS drivethru_net_sales,
    OLODriveThruNetSales                        AS olo_drivethru_net_sales,
    CallInnetsales                              AS callin_net_sales,
    OLOCallInnetsales                           AS olo_callin_net_sales,

    -- 1PD (online / curbside / Newk's-own delivery)
    OLONetSales                                 AS olo_net_sales,
    CurbSideNetSales                            AS curbside_net_sales,
    CurbSideOLONetSales                         AS curbside_olo_net_sales,
    NwkDeliveryNetSales                         AS nwk_delivery_net_sales,
    DispatchOLONetSales                         AS dispatch_olo_net_sales,

    -- 3PD splits (the big win — these are the columns aloha_sales_data lumps into one DeliveryNetSales)
    DoorDashNetSales                            AS doordash_net_sales,
    UberEatsNetSales                            AS ubereats_net_sales,
    GrubHubRailsNet                             AS grubhub_net_sales,
    PostmatesNetSales                           AS postmates_net_sales,
    BiteSquadNetSales                           AS bitesquad_net_sales,
    FavorNet                                    AS favor_net_sales,
    FoodsbyNet                                  AS foodsby_net_sales,
    GoogleNetSales                              AS google_net_sales,
    DeliverClubNetSales                         AS deliverclub_net_sales,
    RailsOLONetSales                            AS rails_olo_net_sales,
    WaitrNet                                    AS waitr_net_sales,
    DeliveryNetSales                            AS delivery_net_sales,  -- legacy combined; often 0 in newer rows

    -- Cross-cut (item category, NOT a channel — pulled for completeness, not summed elsewhere)
    GNGNetSales                                 AS gng_net_sales,

    -- Check counts at daypart grain (still aggregate across channels within the daypart)
    CheckCount                                  AS check_count,
    ChecksMinusCatering                         AS checks_minus_catering
FROM dev_aloha.dbo.tbl_SalesDataByDayPart
WHERE DateOfBusiness >= '2024-01-01'
ORDER BY DateOfBusiness, RestaurantID, DayPartID;
