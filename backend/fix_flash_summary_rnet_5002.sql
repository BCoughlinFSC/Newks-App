-- ============================================================
-- Run against the dev_aloha database (this proc lives there). Without this
-- USE, SSMS runs it against whatever DB the query window is on and ALTER
-- fails with Msg 208 "Invalid object name". ALTER PROCEDURE must be the
-- first statement in its batch, so the GO after USE is required.
USE [dev_aloha];
GO
-- ============================================================
-- fix_flash_summary_rnet_5002.sql   (run in SSMS on dev_aloha / SQL-PROD)
--
-- FIX: Daily Sales Flash Report "Smmary" tab R-Net (Net) "Total Restaurants"
-- was understating net sales by excluding ghost-kitchen / virtual-site stores.
-- Root cause (two legacy assumptions in the R-Net total path only):
--   (1) INNER `join #Labor` -> a store with no labor rows (e.g. Rockwall 5002,
--       a virtual ghost kitchen) is dropped before it is counted.
--   (2) hardcoded `n.StoreID between 1000 and 2000` -> store 5002 is outside
--       the range and is excluded from the total.
-- Every other metric (Catering, NonCatering, OLO, GNG) aggregates from
-- #<metric>Net JOIN #StoresIncluded with no labor join and no range guard,
-- which is why they already include 5002 and R-Net did not.
--
-- THIS PATCH (surgical, R-Net total only):
--   * from #Net n  ->  left join #Labor l   (keeps labor-less stores; SUM ignores
--     the NULL labor, so labor $ / Labor % are unchanged)
--   * n.StoreID between 1000 and 2000  ->  n.StoreID >= 1000  (aggregate marker
--     rows use StoreID 1 and 2, so >= 1000 still excludes them but now includes
--     5xxx ghost kitchens). Applied to the "Total Restaurants" and "Total Comp"
--     R-Net inserts.
--
-- EXPECTED RESULT (business date 2026-06-29): R-Net Total Restaurants goes from
--   564,351.55  ->  566,541.90  (= NonCatering 499,099.23 + Catering 67,442.67),
-- matching the operational total. Total Comp Restaurants is unchanged (5002 is a
-- non-comp franchise store).
--
-- NOTE: the same two assumptions repeat elsewhere in this proc (DineIn/Delivery
-- totals) and across ~13 sibling procs (sp_rpt_Daily_Sales_FLash_Report,
-- sp_rpt_Weekly_Sales_FLash_Report, sp_rpt_check_and_pp_average, the True_Totals
-- and DMA reports, QBR dashboard, Period_Comparison). They do not currently
-- mis-state 5002 because 5002 only rings catering, but they are latent for any
-- future ghost kitchen with dine-in/delivery. Track a separate, tested pass.
--
-- TEST BEFORE DEPLOY:
--   1. Back up current definition (script the proc to a file first).
--   2. Run this ALTER.
--   3. EXEC dbo.sp_rpt_Daily_Sales_FLash_Report_Summary
--        @EndDate='2026-06-29', @StoreGrouping='9', @StoreGroup='4';
--      -> R-Net (Tab='Net', StoreID=1) Daily should be 566541.90.
--   4. Re-run for a normal recent day and diff every metric vs the current
--      report to confirm nothing else moved.
-- ROLLBACK: re-run the original CREATE/ALTER from SSRS_Backup.
-- ============================================================

-- Database:  dev_aloha
-- Schema:    dbo
-- Procedure: sp_rpt_Daily_Sales_FLash_Report_Summary
-- Extracted: 2026-04-29 13:06

ALTER procedure [dbo].[sp_rpt_Daily_Sales_FLash_Report_Summary] 
	(
		@StartDate date = null,
		@EndDate date,
		@StoreGrouping varchar(75),
		@StoreGroup varchar(1000),
		@YearsBack int = 1
	)
AS
/**
## Procedure Name       *[dev_aloha].[dbo].[sp_rpt_Daily_Sales_FLash_Report_Summary]*  
**Procedure Type**      *stored procedure*  
**Author**              Wilson, Don < dwilson@newks.com >  
**Version**             1  
**Created**             20171010  
**Report Name**         *Sales and KPIs > Daily Sales Flash Report*  
**Summary**  
*Summary tab of Daily Internal Flash Report. Please expect a decent run time.    
Metrics provided:  Net, GNG, Catering (cat), OLO, NonCatering, NonCateringChecks, CheckCount, GuestCount, DineInNet, DeliveryNet, CurbSide*  
  
### Parameters  
```sql  
StartDate date              Start date of the range  
EndDate date                End Date of the range  
StoreGrouping int           The grouping type of stores  
StoreGroup varchar(1500)    The store / store group / dma / etc to run for  
```  
  
### Output  
*Tab  
StartDate  
StartDateLY  
WTDStart  
WTDEnd  
WTDStartLY  
WTDEndLY  
PTDStart  
PTDEnd  
PTDStartLY  
PTDEndLY  
QTDStart  
QTDEnd  
QTDStartLY  
QTDEndLY  
YTDStart  
YTDEnd  
YTDStartLY  
YTDEndLY  
Daily  
DailyLY  
DailyDiff  
Dailydiffpct  
DailyLabor  
DailyLaborPct  
CurWk  
CurWkLY  
CurWkDiff  
CurWkdiffpct  
CurWTDLabor  
CurWTDLaborPct  
CurPTD  
CurPTDLY  
CurPTDDiff  
CurPTDdiffpct  
CurPTDLabor  
CurPTDLaborPct  
CurQTD  
CurQTDLY  
CurQTDDiff  
CurQTDdiffpct  
CurQTDLabor  
CurQTDLaborPct  
CurYTD  
CurYTDLY  
CurYTDDiff  
CurYTDdiffpct  
CurYTDLabor  
CurYTDLaborPct  
NACurYTD  
NACurYTDLY  
NAYTDDiff  
NAYTDDiffPct*  
  
### Change History  	
**Change Date:**            *20230907*  
**Author:**                 *Wilson, Don < dwilson@newks.com >*  
**Version**:                6  
**Change:**  
*Add order mode #8 Foodsby*  
  
**Change Date:**            *20220104*  
**Author:**                 *Wilson, Don < dwilson@newks.com >*  
**Version**:                5  
**Change:**  
*Alter calculation of Last Year dates to be able to account for 53 week years  
Per Finance / Emily Moore <emoore@newks.com>  
All Last Year dates now ignore the week, period, quarter number and simply  
subtract 52 weeks times the number of years back*  

**Change Date:**            *20211020*  
**Author:**                 *Wilson, Don < dwilson@newks.com >*  
**Version**:                4  
**Change:**  
*Add tabs for PPA and Check Average*  

**Change Date:**            *20210427*  
**Author:**                 *Wilson, Don < dwilson@newks.com >*  
**Version**:                3  
**Change:**  
*Add Call In / ToGo into the mix, per Adam and Frank*    

**Change Date:**            *20200326*  
**Author:**                 *Wilson, Don < dwilson@newks.com >*  
**Version**:                2  
**Change:**  
*Add documentation header*  
  
-------------------   
 **/
BEGIN
    declare 
        -- If this is for a table build, then Tab, if not, then Proc
        @RunType varchar(4) = 'Proc' 
        -- The time the procedure begins running
       ,@StartTime datetime = getdate()
       -- The database the procedure is running in
       ,@DBName varchar(150) = DB_NAME()
       -- The name of the procedure itself
       ,@ProcName varchar(150) = OBJECT_NAME(@@PROCID)
       -- Default the error information to no error
       ,@ErrorNumber int = 0
       ,@ErrorMessage varchar(250) = ''
       ,@ErrorLine int = 0
       
    BEGIN TRY          
	   declare @Run int = 0

	   select @Run = completion_flag from logging.dbo.table_builds
	   where Proc_Name = 'sp_Check_Data';

	   if @Run = 0 THROW 50000, 'Dev_Aloha.dbo.CheckData table build failed', 1;

        -- if this is a table build, perform it as a transaction
        if (@RunType = 'Tab') begin transaction;
        
        -- SET NOCOUNT ON added to prevent extra result sets from
        -- interfering with SELECT statements.
        SET NOCOUNT ON;
        
	   set datefirst 1
	   declare @Week int
       declare @Day int
	   declare @Period int
	   declare @Quarter int
	   declare @Year int
	   declare @SDate date
	   declare @SDateLY date
	   declare @WTDStart date
	   declare @WTDEnd date
	   declare @WTDStartLY date
	   declare @WTDEndLY date
	   declare @PTDStart date
	   declare @PTDEnd date
	   declare @PTDStartLY date
	   declare @PTDEndLY date
	   declare @QTDStart date
	   declare @QTDEnd date
	   declare @QTDStartLY date
	   declare @QTDEndLY date
	   declare @YTDStart date
	   declare @YTDEnd date
	   declare @YTDStartLY date
	   declare @YTDEndLY date

		select
			@Day = FiscalDay,
			@Week = fiscalweek,
			@Period = fiscalperiod,
			@Quarter = fiscalquarter,
			@Year = fiscalyear
		from dev_aloha.dbo.calendarv2
		where date = @EndDate

		set @SDate = @EndDate
		set @SDateLY = (select LYDate from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)
		set @WTDStart = (select WeekStart from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)
		set @WTDStartLY = (select LYWeekStart from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)
		set @WTDEnd = @EndDate
		set @WTDEndLY = (select LYWeekEnd from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)

		set @PTDStart = (select PeriodStart from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)
		set @PTDStartLY = (select LYPeriodStart from dev_aloha.dbo.CalendarV2 where [Date] = @PTDStart)
		set @PTDEnd = @EndDate
		set @PTDEndLY = (select [Date] from dev_aloha.dbo.CalendarV2 where [Year] = @Year - 1 and FiscalDay = @Day)

		set @QTDStart = (select QuarterStart from dev_aloha.dbo.calendarv2 where [Date] = @SDate)
		set @QTDStartLY = (select LYQuarterStart from dev_aloha.dbo.CalendarV2 where [Date] = @QTDStart)
		set @QTDEnd = @EndDate
		set @QTDEndLY = (select [Date] from dev_aloha.dbo.CalendarV2 where [Year] = @Year - 1 and FiscalDay = @Day)

		set @YTDStart = (select YearStart from dev_aloha.dbo.CalendarV2 where [Date] = @SDate)
		set @YTDStartLY = (select LYYearStart from dev_aloha.dbo.CalendarV2 where [Date] = @YTDStart)
		set @YTDEnd = @EndDate
		set @YTDEndLY = (select [Date] from dev_aloha.dbo.CalendarV2 where [Year] = @Year - 1 and FiscalDay = @Day)

	    --Create Stores Included Temp Table      
	    select *
	    into #StoresIncluded
	    from dev_aloha.dbo.get_StoresIncluded (@StoreGrouping, @StoreGroup, @EndDate)

	   select * 
	   into #StoreDatesToReport
	   from dev_aloha.dbo.[fn_StoreDatesToReportv2] (@EndDate, @StoreGrouping, @StoreGroup, @YearsBack)
		
	   --Net Sales
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.NetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.NetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.NetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.NetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.NetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.NetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.NetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.NetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.NetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.NetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.NetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.NetSales else 0 end)
	   into #Net
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping

        --CallInNet Sales
        select
            si.FBCID,
            FBC,
            CASE
                WHEN sddp.Comping = 'y' THEN 'C'
                ELSE ''
            END AS Comping,
            RestaurantID,
            RestaurantName,
            DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY then sddp.CallInNetSales + sddp.OLOCallInNetSales ELSE 0 END ),
            CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end),
            TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.CallInNetSales + sddp.OLOCallInNetSales else 0 end)
        into #CallInNet
        FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
        join #StoreDatesToReport sdr
        on sddp.restaurantid = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
        GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

        --ToGoNet Sales
        select
            si.FBCID,
            FBC,
            CASE
                WHEN sddp.Comping = 'y' THEN 'C'
                ELSE ''
            END AS Comping,
            RestaurantID,
            RestaurantName,
            DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales ELSE 0 END ),
            CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end),
            TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.ToGoNetSales + sddp.FoodsbyNet + sddp.DriveThruNetSales + sddp.OLODriveThruNetSales else 0 end)
        into #ToGoNet
        FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
        join #StoreDatesToReport sdr
        on sddp.restaurantid = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
        GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --DineInNet Sales
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.DineInNetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.DineInNetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.DineInNetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.DineInNetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.DineInNetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.DineInNetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.DineInNetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.DineInNetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.DineInNetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.DineInNetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.DineInNetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.DineInNetSales else 0 end)
	   into #DineInNet
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --DeliveryNet Sales
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
            DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END ),
            TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.DispatchOLONetSales + sddp.DeliveryNetSales + sddp.NwkDeliveryNetSales + sddp.RailsOLONetSales + sddp.BiteSquadNetSales + sddp.DoorDashNetSales + sddp.PostmatesNetSales + sddp.GoogleNetSales + sddp.DeliverClubNetSales + sddp.UberEatsNetSales + sddp.WaitrNet + sddp.FavorNet + sddp.NwkDelivNetDisp + sddp.DoorDashNetDisp + sddp.PostmatesNetDisp + sddp.LyftNetDisp + sddp.DeliverLogicNetDisp + sddp.GrubHubRailsNet + sddp.DispatchSkipCart + sddp.DispatchRailsAI + sddp.FoodsbyNet ELSE 0 END )
	   into #DeliveryNet
	   FROM dbo.tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   -- Labor Dollars -- All
        create table #Labor (FBCID int, StoreID int, Daily float, DailyPct float, Weekly float, WeeklyPct float, PeriodtoDate float, PTDPct float, QuartertoDate float, QTDPct float, YearToDate float, YTDPct float)
        insert into #Labor (FBCID, StoreID, Daily, Weekly, PeriodToDate, QuartertoDate, YearToDate)
        select
            si.fbcid,
            RestaurantID,
            Daily = sum(case when hs.dateofbusiness = sdr.SDate then (hs.RegularDollars + hs.OTDollars) else 0 end),
            Weekly = sum(case when hs.dateofbusiness between sdr.WTDStart and sdr.WTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            PeriodtoDate = sum(case when hs.dateofbusiness between sdr.PTDStart and sdr.PTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            QuartertoDate = sum(case when hs.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            YeartoDate = sum(case when hs.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end)
        from dev_aloha.dbo.tbl_LaborDataByDaypart hs with (NOLOCK)
        join #StoreDatesToReport sdr
        on hs.RestaurantID = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
		where hs.jobcodeid not in (1, 10, 11, 13, 55, 105, 900, 901, 902, 950, 975, 977, 998, 999)
        GROUP BY si.FBCID, RestaurantID

		-- Labor Hours -- All
        create table #LaborHours (FBCID int, StoreID int, Daily float, DailyPct float, Weekly float, WeeklyPct float, PeriodtoDate float, PTDPct float, QuartertoDate float, QTDPct float, YearToDate float, YTDPct float)
        insert into #LaborHours (FBCID, StoreID, Daily, Weekly, PeriodToDate, QuartertoDate, YearToDate)
        select
            si.fbcid,
            RestaurantID,
            Daily = sum(case when hs.dateofbusiness = sdr.SDate then (hs.RegularHours + hs.OTHours) else 0 end),
            Weekly = sum(case when hs.dateofbusiness between sdr.WTDStart and sdr.WTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            PeriodtoDate = sum(case when hs.dateofbusiness between sdr.PTDStart and sdr.PTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            QuartertoDate = sum(case when hs.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            YeartoDate = sum(case when hs.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then (hs.RegularHours + hs.OTHours) else 0 end)
        from dev_aloha.dbo.tbl_LaborDataByDaypart hs with (NOLOCK)
        join #StoreDatesToReport sdr
        on hs.RestaurantID = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
		where hs.jobcodeid not in (1, 10, 11, 13, 55, 105, 900, 901, 902, 950, 975, 977, 998, 999)
        GROUP BY si.FBCID, RestaurantID
		
        -- Labor Dollars -- Hourly Manager and Shift Lead
        create table #Labor_Mgr_Lead (FBCID int, StoreID int, Daily float, DailyPct float, Weekly float, WeeklyPct float, PeriodtoDate float, PTDPct float, QuartertoDate float, QTDPct float, YearToDate float, YTDPct float)
        insert into #Labor_Mgr_Lead (FBCID, StoreID, Daily, Weekly, PeriodToDate, QuartertoDate, YearToDate)
        select
            si.fbcid,
            RestaurantID,
            Daily = sum(case when hs.dateofbusiness = sdr.SDate then (hs.RegularDollars + hs.OTDollars) else 0 end),
            Weekly = sum(case when hs.dateofbusiness between sdr.WTDStart and sdr.WTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            PeriodtoDate = sum(case when hs.dateofbusiness between sdr.PTDStart and sdr.PTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            QuartertoDate = sum(case when hs.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end),
            YeartoDate = sum(case when hs.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then (hs.RegularDollars + hs.OTDollars) else 0 end)
        from dev_aloha.dbo.tbl_LaborDataByDaypart hs with (NOLOCK)
        join #StoreDatesToReport sdr
        on hs.RestaurantID = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
        where hs.jobcodeid in (12, 5)
        GROUP BY si.FBCID, RestaurantID

		-- Labor Hours -- Hourly Manager and Shift Lead
        create table #LaborHours_Mgr_Lead (FBCID int, StoreID int, Daily float, DailyPct float, Weekly float, WeeklyPct float, PeriodtoDate float, PTDPct float, QuartertoDate float, QTDPct float, YearToDate float, YTDPct float)
        insert into #LaborHours_Mgr_Lead (FBCID, StoreID, Daily, Weekly, PeriodToDate, QuartertoDate, YearToDate)
        select
            si.fbcid,
            RestaurantID,
            Daily = sum(case when hs.dateofbusiness = sdr.SDate then (hs.RegularHours + hs.OTHours) else 0 end),
            Weekly = sum(case when hs.dateofbusiness between sdr.WTDStart and sdr.WTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            PeriodtoDate = sum(case when hs.dateofbusiness between sdr.PTDStart and sdr.PTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            QuartertoDate = sum(case when hs.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then (hs.RegularHours + hs.OTHours) else 0 end),
            YeartoDate = sum(case when hs.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then (hs.RegularHours + hs.OTHours) else 0 end)
        from dev_aloha.dbo.tbl_LaborDataByDaypart hs with (NOLOCK)
        join #StoreDatesToReport sdr
        on hs.RestaurantID = sdr.storeid
        join #StoresIncluded si
        on sdr.storeid = si.storeid
        where hs.jobcodeid in (12, 5)
        GROUP BY si.FBCID, RestaurantID

        --Catering
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.CateringNetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.CateringNetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.CateringNetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.CateringNetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.CateringNetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.CateringNetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.CateringNetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.CateringNetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.CateringNetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.CateringNetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.CateringNetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.CateringNetSales else 0 end)
	   into #CateringNet
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --GNG
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.GNGNetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.GNGNetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.GNGNetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.GNGNetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.GNGNetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.GNGNetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.GNGNetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.GNGNetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.GNGNetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.GNGNetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.GNGNetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.GNGNetSales else 0 end)
	   into #GNGNet
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --CurbSide
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.CurbSideOLONetSales + sddp.CurbSideNetSales else 0 end)
	   into #CurbSideNet
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --OLO
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.OLONetSales else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.OLONetSales ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.OLONetSales else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.OLONetSales else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.OLONetSales else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.OLONetSales else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.OLONetSales else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.OLONetSales else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.OLONetSales else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.OLONetSales else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.OLONetSales else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.OLONetSales else 0 end)
	   into #OLONet
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --POS2 Net Sales
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.POS2 else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.POS2 ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.POS2 else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.POS2 else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.POS2 else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.POS2 else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.POS2 else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.POS2 else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.POS2 else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.POS2 else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.POS2 else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.POS2 else 0 end)
	   into #POS2
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   -- NON CATERING SALES
	   SELECT
		  *
	   INTO #NonCatering
	   FROM #Net

	   update #NonCatering
	   set DailyCY = nmc.DailyCY - c.DailyCY,
		  DailyLY = nmc.DailyLY - c.DailyLY,
		  CurWk = nmc.CurWk - c.CurWk,
		  CurWkLY = nmc.CurWkLy - c.CurWkLY,
		  CurPTD = nmc.CurPTD - c.CurPTD,
		  CurPTDLY = nmc.CurPTDLY - c.CUrPTDLY,
		  CurQTD = nmc.CurQTD - c.CurQTD,
		  CurQTDLY = nmc.CurQTDLY - c.CurQTDLY,
		  CurYTD = nmc.CurYTD - c.CurYTD,
		  CurYTDLY = nmc.CurYTDLY - c.CurYTDLY,
		  TrueCurYTD = nmc.TrueCurYTD - c.TrueCurYTD,
		  TrueCurYTDLY = nmc.TrueCurYTDLY - c.TrueCurYTDLY
	   from #NonCatering nmc
	   join #Cateringnet c
	   on nmc.RestaurantID = c.RestaurantID

	   --ChecksMinusCatering
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.ChecksMinusCatering else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.ChecksMinusCatering ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.ChecksMinusCatering else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.ChecksMinusCatering else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.ChecksMinusCatering else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.ChecksMinusCatering else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.ChecksMinusCatering else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.ChecksMinusCatering else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.ChecksMinusCatering else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.ChecksMinusCatering else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.ChecksMinusCatering else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.ChecksMinusCatering else 0 end)
	   into #ChecksMinusCatering
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --CheckCount
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.CheckCount else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.CheckCount ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.CheckCount else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.CheckCount else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.CheckCount else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.CheckCount else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.CheckCount else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.CheckCount else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.CheckCount else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.CheckCount else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.CheckCount else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.CheckCount else 0 end)
	   into #CheckCount
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   where sddp.CheckCount != 0
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

	   --GuestCount
	   select
		  si.FBCID,
		  FBC,
		  CASE
			 WHEN sddp.Comping = 'y' THEN 'C'
			 ELSE ''
		  END AS Comping,
		  RestaurantID,
		  RestaurantName,
		  DailyCY = sum(case when sddp.DateOfBusiness = sdr.SDate then sddp.GuestCount else 0 end),
		  DailyLY = sum(CASE WHEN sddp.DateOfBusiness = sdr.SDateLY THEN sddp.GuestCount ELSE 0 END ),
		  CurWk = sum(case when sddp.DateOfBusiness between sdr.WTDStart and sdr.WTDEnd then sddp.GuestCount else 0 end),
		  CurWkLY = sum(case when sddp.DateOfBusiness between sdr.WTDStartLY and sdr.WTDEndLY then sddp.GuestCount else 0 end),
		  CurPTD = sum(case when sddp.DateOfBusiness between sdr.PTDStart and sdr.PTDEnd then sddp.GuestCount else 0 end),
		  CurPTDLY = sum(case when sddp.DateOfBusiness between sdr.PTDStartLY and sdr.PTDEndLY then sddp.GuestCount else 0 end),
		  CurQTD = sum(case when sddp.DateOfBusiness between sdr.QTDStart and sdr.QTDEnd then sddp.GuestCount else 0 end),
		  CurQTDLY = sum(case when sddp.DateOfBusiness between sdr.QTDStartLY and sdr.QTDEndLY then sddp.GuestCount else 0 end),
		  CurYTD = sum(case when sddp.DateOfBusiness between sdr.YTDStart and sdr.YTDEnd then sddp.GuestCount else 0 end),
		  CurYTDLY = sum(case when sddp.DateOfBusiness between sdr.YTDStartLY and sdr.YTDEndLY then sddp.GuestCount else 0 end),
		  TrueCurYTD = sum(case when sddp.DateOfBusiness between @YTDStart and @YTDEnd then sddp.GuestCount else 0 end),
		  TrueCurYTDLY = sum(case when sddp.DateOfBusiness between @YTDStartLY and @YTDEndLY then sddp.GuestCount else 0 end)
	   into #GuestCount
	   FROM tbl_SalesDataByDayPart sddp with (NOLOCK)
	   join #StoreDatesToReport sdr
	   on sddp.restaurantid = sdr.storeid
	   join #StoresIncluded si
	   on sdr.storeid = si.storeid
	   where sddp.GuestCount != 0
	   GROUP BY si.FBCID, FBC, RestaurantID, RestaurantName, sddp.Comping 

        select
            FBCID,
            FBC,
            Comping,
            RestaurantID,
            RestaurantName,
            DailyCY NetDailyCY,
            convert(float,0.0) CheckDailyCY,
            convert(float,0.0) AvgDailyCY,
            DailyLY NetDailyLY,
            convert(float,0.0) CheckDailyLY,
            convert(float,0.0) AvgDailyLY,
            CurWk NetCurWk,
            convert(float,0.0) CheckCurWk,
            convert(float,0.0) AvgCurWk,
            CurWkLY NetCurWkLY,
            convert(float,0.0) CheckCurWkLY,
            convert(float,0.0) AvgCurWkLY,
            CurPTD NetCurPTD,
            convert(float,0.0) CheckCurPTD,
            convert(float,0.0) AvgCurPTD,
            CurPTDLY NetCurPTDLY,
            convert(float,0.0) CheckCurPTDLY,
            convert(float,0.0) AvgCurPTDLY,
            CurQTD NetCurQTD,
            convert(float,0.0) CheckCurQTD,
            convert(float,0.0) AvgCurQTD,
            CurQTDLY NetCurQTDLY,
            convert(float,0.0) CheckCurQTDLY,
            convert(float,0.0) AvgCurQTDLY,
            CurYTD NetCurYTD,
            convert(float,0.0) CheckCurYTD,
            convert(float,0.0) AvgCurYTD,
            CurYTDLY NetCurYTDLY,
            convert(float,0.0) CheckCurYTDLY,
            convert(float,0.0) AvgCurYTDLY
        into #CheckAvgTemp
        from #NonCatering
        
        merge #CheckAvgTemp ct
        using #ChecksMinusCatering cc
        on ct.FBCID = cc.FBCID
        and ct.FBC = cc.FBC
        and ct.RestaurantID = cc.RestaurantID
        when matched then update set 
            ct.CheckDailyCY = cc.DailyCY,
            ct.CheckDailyLY = cc.DailyLY,
            ct.CheckCurWk = cc.CurWk,
            ct.CheckCurWkLY = cc.CurWkLY,
            ct.CheckCurPTD = cc.CurPTD,
            ct.CheckCurPTDLY = cc.CurPTDLY,
            ct.CheckCurQTD = cc.CurQTD,
            ct.CheckCurQTDLY = cc.CurQTDLY,
            ct.CheckCurYTD = cc.CurYTD,
            ct.CheckCurYTDLY = cc.CurYTDLY;
        
        insert into #CheckAvgTemp (FBCID, FBC, comping, RestaurantID, NetDailyCY, CheckDailyCY, NetDailyLY, CheckDailyLY, NetCurWk, CheckCurWk, NetCurWkLY, CheckCurWkLY, NetCurPTD, CheckCurPTD, NetCurPTDLY, CheckCurPTDLY, NetCurQTD, CheckCurQTD, NetCurQTDLY, CheckCurQTDLY, NetCurYTD, CheckCurYTD, NetCurYTDLY, CheckCurYTDLY)
        select FBCID, FBC, '', 3, sum(NetDailyCY), sum(CheckDailyCY), sum(NetDailyLY), sum(CheckDailyLY), sum(NetCurWk), sum(CheckCurWk), sum(NetCurWkLY), sum(CheckCurWkLY), 
            sum(NetCurPTD), sum(CheckCurPTD), sum(NetCurPTDLY), sum(CheckCurPTDLY), sum(NetCurQTD), sum(CheckCurQTD), sum(NetCurQTDLY), sum(CheckCurQTDLY), sum(NetCurYTD), sum(CheckCurYTD), sum(NetCurYTDLY), sum(CheckCurYTDLY)
        from #CheckAvgTemp
        group by fbcid, fbc, comping
        
        insert into #CheckAvgTemp (FBCID, FBC, comping, RestaurantID, NetDailyCY, CheckDailyCY, NetDailyLY, CheckDailyLY, NetCurWk, CheckCurWk, NetCurWkLY, CheckCurWkLY, NetCurPTD, CheckCurPTD, NetCurPTDLY, CheckCurPTDLY, NetCurQTD, CheckCurQTD, NetCurQTDLY, CheckCurQTDLY, NetCurYTD, CheckCurYTD, NetCurYTDLY, CheckCurYTDLY)
        select FBCID, FBC, '', 4, sum(NetDailyCY), sum(CheckDailyCY), sum(NetDailyLY), sum(CheckDailyLY), sum(NetCurWk), sum(CheckCurWk), sum(NetCurWkLY), sum(CheckCurWkLY), 
            sum(NetCurPTD), sum(CheckCurPTD), sum(NetCurPTDLY), sum(CheckCurPTDLY), sum(NetCurQTD), sum(CheckCurQTD), sum(NetCurQTDLY), sum(CheckCurQTDLY), sum(NetCurYTD), sum(CheckCurYTD), sum(NetCurYTDLY), sum(CheckCurYTDLY)
        from #CheckAvgTemp
        where comping = 'C'
        group by fbcid, fbc, comping
        
        insert into #CheckAvgTemp (Comping, RestaurantID, NetDailyCY, CheckDailyCY, NetDailyLY, CheckDailyLY, NetCurWk, CheckCurWk, NetCurWkLY, CheckCurWkLY, NetCurPTD, CheckCurPTD, 
            NetCurPTDLY, CheckCurPTDLY, NetCurQTD, CheckCurQTD, NetCurQTDLY, CheckCurQTDLY, NetCurYTD, CheckCurYTD, NetCurYTDLY, CheckCurYTDLY)
        select '', 1, sum(NetDailyCY), sum(CheckDailyCY), sum(NetDailyLY), sum(CheckDailyLY), sum(NetCurWk), sum(CheckCurWk), sum(NetCurWkLY), sum(CheckCurWkLY), 
            sum(NetCurPTD), sum(CheckCurPTD), sum(NetCurPTDLY), sum(CheckCurPTDLY), sum(NetCurQTD), sum(CheckCurQTD), sum(NetCurQTDLY), sum(CheckCurQTDLY), sum(NetCurYTD), sum(CheckCurYTD), sum(NetCurYTDLY), sum(CheckCurYTDLY)
        from #CheckAvgTemp
        where RestaurantID between 1000 and 2000
        
        insert into #CheckAvgTemp (Comping, RestaurantID, NetDailyCY, CheckDailyCY, NetDailyLY, CheckDailyLY, NetCurWk, CheckCurWk, NetCurWkLY, CheckCurWkLY, NetCurPTD, CheckCurPTD, 
            NetCurPTDLY, CheckCurPTDLY, NetCurQTD, CheckCurQTD, NetCurQTDLY, CheckCurQTDLY, NetCurYTD, CheckCurYTD, NetCurYTDLY, CheckCurYTDLY)
        select '', 2, sum(NetDailyCY), sum(CheckDailyCY), sum(NetDailyLY), sum(CheckDailyLY), sum(NetCurWk), sum(CheckCurWk), sum(NetCurWkLY), sum(CheckCurWkLY), 
            sum(NetCurPTD), sum(CheckCurPTD), sum(NetCurPTDLY), sum(CheckCurPTDLY), sum(NetCurQTD), sum(CheckCurQTD), sum(NetCurQTDLY), sum(CheckCurQTDLY), sum(NetCurYTD), sum(CheckCurYTD), sum(NetCurYTDLY), sum(CheckCurYTDLY)
        from #CheckAvgTemp
        where comping = 'C'
        and  RestaurantID between 1000 and 2000

        update #CheckAvgTemp set
            AvgDailyCY = 
                case
                    when NetDailyCY = 0 then 0
                    when CheckDailyCY = 0 then 0
                    else NetDailyCY / convert(float,CheckDailyCY)
                end,
            AvgDailyLY = 
                case
                    when NetDailyLY = 0 then 0
                    when CheckDailyLY = 0 then 0
                    else NetDailyLY / convert(float,CheckDailyLY)
                end,
            AvgCurWk = 
                case
                    when NetCurWk = 0 then 0
                    when CheckCurWk = 0 then 0
                    else NetCurWk / convert(float,CheckCurWk)
                end,
            AvgCurWkLY = 
                case
                    when NetCurWkLY = 0 then 0
                    when CheckCurWkLY = 0 then 0
                    else NetCurWkLY / convert(float,CheckCurWkLY)
                end,
            AvgCurPTD =
                case
                    when NetCurPTD = 0 then 0
                    when CheckCurPTD = 0 then 0
                    else NetCurPTD / convert(float,CheckCurPTD)
                end,
            AvgCurPTDLY =
                case
                    when NetCurPTDLY = 0 then 0
                    when CheckCurPTDLY = 0 then 0
                    else NetCurPTDLY / convert(float,CheckCurPTDLY)
                end,
            AvgCurQTD =
                case
                    when NetCurQTD = 0 then 0
                    when CheckCurQTD = 0 then 0
                    else NetCurQTD / convert(float,CheckCurQTD)
                end,
            AvgCurQTDLY = 
                case
                    when NetCurQTDLY = 0 then 0
                    when CheckCurQTDLY = 0 then 0
                    else NetCurQTDLY / convert(float,CheckCurQTDLY)
                end,
            AvgCurYTD =
                case
                    when NetCurYTD = 0 then 0
                    when CheckCurYTD = 0 then 0
                    else NetCurYTD / convert(float,CheckCurYTD)
                end,
            AvgCurYTDLY =
                case
                    when NetCurYTDLY = 0 then 0
                    when CheckCurYTDLY = 0 then 0
                    else NetCurYTDLY / convert(float,CheckCurYTDLY)
                end

        select
            FBCID,
            FBC,
            Comping,
            RestaurantID,
            RestaurantName,
            DailyCY NetDailyCY,
            convert(float,0.0) GuestDailyCY,
            convert(float,0.0) AvgDailyCY,
            DailyLY NetDailyLY,
            convert(float,0.0) GuestDailyLY,
            convert(float,0.0) AvgDailyLY,
            CurWk NetCurWk,
            convert(float,0.0) GuestCurWk,
            convert(float,0.0) AvgCurWk,
            CurWkLY NetCurWkLY,
            convert(float,0.0) GuestCurWkLY,
            convert(float,0.0) AvgCurWkLY,
            CurPTD NetCurPTD,
            convert(float,0.0) GuestCurPTD,
            convert(float,0.0) AvgCurPTD,
            CurPTDLY NetCurPTDLY,
            convert(float,0.0) GuestCurPTDLY,
            convert(float,0.0) AvgCurPTDLY,
            CurQTD NetCurQTD,
            convert(float,0.0) GuestCurQTD,
            convert(float,0.0) AvgCurQTD,
            CurQTDLY NetCurQTDLY,
            convert(float,0.0) GuestCurQTDLY,
            convert(float,0.0) AvgCurQTDLY,
            CurYTD NetCurYTD,
            convert(float,0.0) GuestCurYTD,
            convert(float,0.0) AvgCurYTD,
            CurYTDLY NetCurYTDLY,
            convert(float,0.0) GuestCurYTDLY,
            convert(float,0.0) AvgCurYTDLY
        into #GuestAvgTemp
        from #NonCatering
        
        
        merge #GuestAvgTemp ct
        using #GuestCount cc
        on ct.FBCID = cc.FBCID
        and ct.FBC = cc.FBC
        and ct.RestaurantID = cc.RestaurantID
        when matched then update set 
            ct.GuestDailyCY = cc.DailyCY,
            ct.GuestDailyLY = cc.DailyLY,
            ct.GuestCurWk = cc.CurWk,
            ct.GuestCurWkLY = cc.CurWkLY,
            ct.GuestCurPTD = cc.CurPTD,
            ct.GuestCurPTDLY = cc.CurPTDLY,
            ct.GuestCurQTD = cc.CurQTD,
            ct.GuestCurQTDLY = cc.CurQTDLY,
            ct.GuestCurYTD = cc.CurYTD,
            ct.GuestCurYTDLY = cc.CurYTDLY;
        
        insert into #GuestAvgTemp (FBCID, FBC, comping, RestaurantID, NetDailyCY, GuestDailyCY, NetDailyLY, GuestDailyLY, NetCurWk, GuestCurWk, NetCurWkLY, GuestCurWkLY, NetCurPTD, GuestCurPTD, NetCurPTDLY, GuestCurPTDLY, NetCurQTD, GuestCurQTD, NetCurQTDLY, GuestCurQTDLY, NetCurYTD, GuestCurYTD, NetCurYTDLY, GuestCurYTDLY)
        select FBCID, FBC, '', 3, sum(NetDailyCY), sum(GuestDailyCY), sum(NetDailyLY), sum(GuestDailyLY), sum(NetCurWk), sum(GuestCurWk), sum(NetCurWkLY), sum(GuestCurWkLY), 
            sum(NetCurPTD), sum(GuestCurPTD), sum(NetCurPTDLY), sum(GuestCurPTDLY), sum(NetCurQTD), sum(GuestCurQTD), sum(NetCurQTDLY), sum(GuestCurQTDLY), sum(NetCurYTD), sum(GuestCurYTD), sum(NetCurYTDLY), sum(GuestCurYTDLY)
        from #GuestAvgTemp
        group by fbcid, fbc
        
        insert into #GuestAvgTemp (FBCID, FBC, comping, RestaurantID, NetDailyCY, GuestDailyCY, NetDailyLY, GuestDailyLY, NetCurWk, GuestCurWk, NetCurWkLY, GuestCurWkLY, NetCurPTD, GuestCurPTD, NetCurPTDLY, GuestCurPTDLY, NetCurQTD, GuestCurQTD, NetCurQTDLY, GuestCurQTDLY, NetCurYTD, GuestCurYTD, NetCurYTDLY, GuestCurYTDLY)
        select FBCID, FBC, '', 4, sum(NetDailyCY), sum(GuestDailyCY), sum(NetDailyLY), sum(GuestDailyLY), sum(NetCurWk), sum(GuestCurWk), sum(NetCurWkLY), sum(GuestCurWkLY), 
            sum(NetCurPTD), sum(GuestCurPTD), sum(NetCurPTDLY), sum(GuestCurPTDLY), sum(NetCurQTD), sum(GuestCurQTD), sum(NetCurQTDLY), sum(GuestCurQTDLY), sum(NetCurYTD), sum(GuestCurYTD), sum(NetCurYTDLY), sum(GuestCurYTDLY)
        from #GuestAvgTemp
        where comping = 'C'
        group by fbcid, fbc
        
        insert into #GuestAvgTemp (Comping, RestaurantID, NetDailyCY, GuestDailyCY, NetDailyLY, GuestDailyLY, NetCurWk, GuestCurWk, NetCurWkLY, GuestCurWkLY, NetCurPTD, GuestCurPTD, 
            NetCurPTDLY, GuestCurPTDLY, NetCurQTD, GuestCurQTD, NetCurQTDLY, GuestCurQTDLY, NetCurYTD, GuestCurYTD, NetCurYTDLY, GuestCurYTDLY)
        select '', 1, sum(NetDailyCY), sum(GuestDailyCY), sum(NetDailyLY), sum(GuestDailyLY), sum(NetCurWk), sum(GuestCurWk), sum(NetCurWkLY), sum(GuestCurWkLY), 
            sum(NetCurPTD), sum(GuestCurPTD), sum(NetCurPTDLY), sum(GuestCurPTDLY), sum(NetCurQTD), sum(GuestCurQTD), sum(NetCurQTDLY), sum(GuestCurQTDLY), sum(NetCurYTD), sum(GuestCurYTD), sum(NetCurYTDLY), sum(GuestCurYTDLY)
        from #GuestAvgTemp
        where RestaurantID between 1000 and 2000
        
        insert into #GuestAvgTemp (Comping, RestaurantID, NetDailyCY, GuestDailyCY, NetDailyLY, GuestDailyLY, NetCurWk, GuestCurWk, NetCurWkLY, GuestCurWkLY, NetCurPTD, GuestCurPTD, 
            NetCurPTDLY, GuestCurPTDLY, NetCurQTD, GuestCurQTD, NetCurQTDLY, GuestCurQTDLY, NetCurYTD, GuestCurYTD, NetCurYTDLY, GuestCurYTDLY)
        select '', 2, sum(NetDailyCY), sum(GuestDailyCY), sum(NetDailyLY), sum(GuestDailyLY), sum(NetCurWk), sum(GuestCurWk), sum(NetCurWkLY), sum(GuestCurWkLY), 
            sum(NetCurPTD), sum(GuestCurPTD), sum(NetCurPTDLY), sum(GuestCurPTDLY), sum(NetCurQTD), sum(GuestCurQTD), sum(NetCurQTDLY), sum(GuestCurQTDLY), sum(NetCurYTD), sum(GuestCurYTD), sum(NetCurYTDLY), sum(GuestCurYTDLY)
        from #GuestAvgTemp
        where comping = 'C'
        and  RestaurantID between 1000 and 2000

        update #GuestAvgTemp set
            AvgDailyCY = 
                case
                    when NetDailyCY = 0 then 0
                    when GuestDailyCY = 0 then 0
                    else NetDailyCY / convert(float,GuestDailyCY)
                end,
            AvgDailyLY = 
                case
                    when NetDailyLY = 0 then 0
                    when GuestDailyLY = 0 then 0
                    else NetDailyLY / convert(float,GuestDailyLY)
                end,
            AvgCurWk = 
                case
                    when NetCurWk = 0 then 0
                    when GuestCurWk = 0 then 0
                    else NetCurWk / convert(float,GuestCurWk)
                end,
            AvgCurWkLY = 
                case
                    when NetCurWkLY = 0 then 0
                    when GuestCurWkLY = 0 then 0
                    else NetCurWkLY / convert(float,GuestCurWkLY)
                end,
            AvgCurPTD =
                case
                    when NetCurPTD = 0 then 0
                    when GuestCurPTD = 0 then 0
                    else NetCurPTD / convert(float,GuestCurPTD)
                end,
            AvgCurPTDLY =
                case
                    when NetCurPTDLY = 0 then 0
                    when GuestCurPTDLY = 0 then 0
                    else NetCurPTDLY / convert(float,GuestCurPTDLY)
                end,
            AvgCurQTD =
                case
                    when NetCurQTD = 0 then 0
                    when GuestCurQTD = 0 then 0
                    else NetCurQTD / convert(float,GuestCurQTD)
                end,
            AvgCurQTDLY = 
                case
                    when NetCurQTDLY = 0 then 0
                    when GuestCurQTDLY = 0 then 0
                    else NetCurQTDLY / convert(float,GuestCurQTDLY)
                end,
            AvgCurYTD =
                case
                    when NetCurYTD = 0 then 0
                    when GuestCurYTD = 0 then 0
                    else NetCurYTD / convert(float,GuestCurYTD)
                end,
            AvgCurYTDLY =
                case
                    when NetCurYTDLY = 0 then 0
                    when GuestCurYTDLY = 0 then 0
                    else NetCurYTDLY / convert(float,GuestCurYTDLY)
                end

	   create table #ComparisonFinal (sort int, Tab varchar(50), FBCID int, FBCName varchar(50), StartDate date, StartDateLY date, WTDStart date, WTDEnd date, WTDStartLY date, WTDEndLY date, 
        PTDStart date, PTDEnd date, PTDStartLY date, PTDEndLY date, QTDStart date, QTDEnd date, QTDStartLY date, QTDEndLY date, YTDStart date, YTDEnd date, YTDStartLY date, YTDEndLY date, StoreID int,
        StoreName varchar(100), Daily Float, DailyLY Float, DailyDiff Float, Dailydiffpct float, DailyLabor float, DailyLaborPct float, CurWk Float, CurWkLY Float, CurWkDiff Float, CurWkdiffpct float, 
        CurWTDLabor float, CurWTDLaborPct float, CurPTD Float, CurPTDLY Float, CurPTDDiff Float, CurPTDdiffpct float, CurPTDLabor float, CurPTDLaborPct float, CurQTD Float, CurQTDLY Float, 
        CurQTDDiff Float, CurQTDdiffpct float, CurQTDLabor float, CurQTDLaborPct float, CurYTD Float, CurYTDLY Float, CurYTDDiff Float, CurYTDdiffpct float, CurYTDLabor float, 
        CurYTDLaborPct float, NACurYTD float, NACurYTDLY float, NAYTDDiff float, NAYTDDiffPct float, comping varchar(15), OpenDate Date, DailyLaborMgrLead float, DailyLaborHours float, 
		DailyLaborHoursMgrLead float, CurWTDLaborMgrLead float, CurWTDLaborHours float, CurWTDLaborHoursMgrLead float, CurPTDLaborMgrLead float, CurPTDLaborHours float, CurPTDLaborHoursMgrLead float,
		CurQTDLaborMgrLead float, CurQTDLaborHours float, CurQTDLaborHoursMgrLead float, CurYTDLaborMgrLead float, CurYTDLaborHours float, CurYTDLaborHoursMgrLead float, DailySalesPerLaborHour float,
		WTDSalesPerLaborHour float, PTDSalesPerLaborHour float, QTDSalesPerLaborHour float, YTDSalesPerLaborHour float)

        insert into #ComparisonFinal (Sort, Tab, FBCID, FBCName, StoreID, StoreName, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor,
        CurYTD, CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select
        1, 'Net', n.fbcid, n.FBC, n.RestaurantID, n.RestaurantName, n.DailyCY, n.DailyLY, l.Daily, n.CurWk, n.CurWkLY, l.Weekly, n.CurPTD, n.CurPTDLY, l.PeriodToDate, n.CurQTD, n.CurQTDLY, l.QuarterToDate, 
		n.CurYTD, n.CurYTDLY, l.YearToDate, n.TrueCurYTD, n.TrueCurYTDLY
        from #Net n
        left join #Labor l
        on n.RestaurantID = l.storeid
        
        update #ComparisonFinal set
            DailyLaborHours = lml.Daily,
            CurWTDLaborHours = lml.Weekly,
            CurPTDLaborHours = lml.PeriodToDate,
            CurQTDLaborHours = lml.QuarterToDate,
            CurYTDLaborHours = lml.YearToDate
        from #ComparisonFinal cf
        join #LaborHours lml
        on cf.StoreID = lml.StoreID
        
        update #ComparisonFinal set
            DailyLaborMgrLead = lml.Daily,
            CurWTDLaborMgrLead = lml.Weekly,
            CurPTDLaborMgrLead = lml.PeriodToDate,
            CurQTDLaborMgrLead = lml.QuarterToDate,
            CurYTDLaborMgrLead = lml.YearToDate
        from #ComparisonFinal cf
        join #Labor_Mgr_Lead lml
        on cf.StoreID = lml.StoreID
        
        update #ComparisonFinal set
            DailyLaborHoursMgrLead = lml.Daily,
            CurWTDLaborHoursMgrLead = lml.Weekly,
            CurPTDLaborHoursMgrLead = lml.PeriodToDate,
            CurQTDLaborHoursMgrLead = lml.QuarterToDate,
            CurYTDLaborHoursMgrLead = lml.YearToDate
        from #ComparisonFinal cf
        join #Labor_Mgr_Lead lml
        on cf.StoreID = lml.StoreID
        
        update #ComparisonFinal set
            DailyLaborHours = isnull(DailyLaborHours, 0),
            CurWTDLaborHours = isnull(CurWTDLaborHours, 0),
            CurPTDLaborHours = isnull(CurPTDLaborHours, 0),
            CurQTDLaborHours = isnull(CurQTDLaborHours, 0),
            CurYTDLaborHours = isnull(CurYTDLaborHours, 0),
            DailyLaborMgrLead = isnull(DailyLaborMgrLead, 0),
            CurWTDLaborMgrLead = isnull(CurWTDLaborMgrLead, 0),
            CurPTDLaborMgrLead = isnull(CurPTDLaborMgrLead, 0),
            CurQTDLaborMgrLead = isnull(CurQTDLaborMgrLead, 0),
            CurYTDLaborMgrLead = isnull(CurYTDLaborMgrLead, 0),
            DailyLaborHoursMgrLead = isnull(DailyLaborHoursMgrLead, 0),
            CurWTDLaborHoursMgrLead = isnull(CurWTDLaborHoursMgrLead, 0),
            CurPTDLaborHoursMgrLead = isnull(CurPTDLaborHoursMgrLead, 0),
            CurQTDLaborHoursMgrLead = isnull(CurQTDLaborHoursMgrLead, 0),
            CurYTDLaborHoursMgrLead = isnull(CurYTDLaborHoursMgrLead, 0)

        insert into #ComparisonFinal (Sort, Tab, StoreID,  Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor,
        CurYTD, CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY, DailyLaborMgrLead, DailyLaborHours,  DailyLaborHoursMgrLead, CurWTDLaborMgrLead, CurWTDLaborHours, CurWTDLaborHoursMgrLead, CurPTDLaborMgrLead, 
		CurPTDLaborHours, CurPTDLaborHoursMgrLead,  CurQTDLaborMgrLead, CurQTDLaborHours, CurQTDLaborHoursMgrLead, CurYTDLaborMgrLead, CurYTDLaborHours, CurYTDLaborHoursMgrLead)
        select
        1, 'Net', 1, sum(Daily), sum(DailyLY), sum(DailyLabor), sum(CurWk), sum(CurWkLY), sum(CurWTDLabor), sum(CurPTD), sum(CurPTDLY), sum(CurPTDLabor), sum(CurQTD), sum(CurQTDLY), 
        sum(CurQTDLabor), sum(CurYTD), sum(CurYTDLY), sum(CurYTDLabor), sum(NACurYTD), sum(NACurYTDLY), sum(DailyLaborMgrLead), sum(DailyLaborHours), sum( DailyLaborHoursMgrLead), sum(CurWTDLaborMgrLead),
        sum(CurWTDLaborHours), sum(CurWTDLaborHoursMgrLead), sum(CurPTDLaborMgrLead), sum(CurPTDLaborHours), sum(CurPTDLaborHoursMgrLead), sum( CurQTDLaborMgrLead), sum(CurQTDLaborHours), 
        sum(CurQTDLaborHoursMgrLead), sum(CurYTDLaborMgrLead), sum(CurYTDLaborHours), sum(CurYTDLaborHoursMgrLead)
        from #ComparisonFinal n
        join #StoresIncluded si
        on n.StoreID = si.storeid
		where n.StoreID >= 1000
        
        insert into #ComparisonFinal (Sort, Tab, StoreID,  Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor,
        CurYTD, CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY, DailyLaborMgrLead, DailyLaborHours,  DailyLaborHoursMgrLead, CurWTDLaborMgrLead, CurWTDLaborHours, CurWTDLaborHoursMgrLead, CurPTDLaborMgrLead, 
		CurPTDLaborHours, CurPTDLaborHoursMgrLead,  CurQTDLaborMgrLead, CurQTDLaborHours, CurQTDLaborHoursMgrLead, CurYTDLaborMgrLead, CurYTDLaborHours, CurYTDLaborHoursMgrLead)
        select
        1, 'Net', 2, sum(Daily), sum(DailyLY), sum(DailyLabor), sum(CurWk), sum(CurWkLY), sum(CurWTDLabor), sum(CurPTD), sum(CurPTDLY), sum(CurPTDLabor), sum(CurQTD), sum(CurQTDLY), 
        sum(CurQTDLabor), sum(CurYTD), sum(CurYTDLY), sum(CurYTDLabor), sum(NACurYTD), sum(NACurYTDLY), sum(DailyLaborMgrLead), sum(DailyLaborHours), sum( DailyLaborHoursMgrLead), sum(CurWTDLaborMgrLead),
        sum(CurWTDLaborHours), sum(CurWTDLaborHoursMgrLead), sum(CurPTDLaborMgrLead), sum(CurPTDLaborHours), sum(CurPTDLaborHoursMgrLead), sum( CurQTDLaborMgrLead), sum(CurQTDLaborHours), 
        sum(CurQTDLaborHoursMgrLead), sum(CurYTDLaborMgrLead), sum(CurYTDLaborHours), sum(CurYTDLaborHoursMgrLead)
        from #ComparisonFinal n
        join #StoresIncluded si
        on n.StoreID = si.storeid
        where si.comping = 1
		and n.StoreID >= 1000

		delete from #ComparisonFinal
		where StoreID > 1000

		update #ComparisonFinal
		set
			DailySalesPerLaborHour = 
				case 
					when DailyLaborHours = 0 then 0
					else Daily / DailyLaborHours
				end,
			WTDSalesPerLaborHour = 
				case 
					when CurWTDLaborHours = 0 then 0
					else CurWk / CurWTDLaborHours
				end,
			PTDSalesPerLaborHour = 
				case 
					when CurPTDLaborHours = 0 then 0
					else CurPTD / CurPTDLaborHours
				end,
			QTDSalesPerLaborHour =
				case
					when CurQTDLaborHours = 0 then 0
					else CurQTD / CurQTDLaborHours
				end,
			YTDSalesPerLaborHour = 
				case 
					when CurYTDLaborHours = 0 then 0
					else CurYTD / CurYTDLaborHours
				end

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        2, 'CallInNet', 1, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CallInNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        2, 'CallInNet', 2, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CallInNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        3,'ToGoNet', 1, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #ToGoNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        3,'ToGoNet',2, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #ToGoNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY,  CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        4,'GNG',1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #GNGNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        4,'GNG',2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #GNGNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY,  CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        5,'Catering (cat)',1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CateringNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        5,'Catering (cat)',2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), 
        sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CateringNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        6, 'OLO', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #OLONet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY,CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        6, 'OLO', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #OLONet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        7, 'NonCatering', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #NonCatering n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY,CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        7, 'NonCatering', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #NonCatering n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        8, 'NonCateringChecks', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #ChecksMinusCatering n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY,CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        8, 'NonCateringChecks', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #ChecksMinusCatering n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        9, 'CheckCount', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CheckCount n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY,CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        9, 'CheckCount', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CheckCount n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1
/*
        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        10, 'GuestCount', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #GuestCount n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY,CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        10, 'GuestCount', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #GuestCount n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1
*/
        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        11, 'DineInNet', 1, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #DineInNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        11, 'DineInNet', 2, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #DineInNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        12, 'DeliveryNet', 1, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #DeliveryNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, DailyLabor, CurWk, CurWkLY, CurWTDLabor, CurPTD, CurPTDLY, CurPTDLabor, CurQTD, CurQTDLY, CurQTDLabor, CurYTD, 
        CurYTDLY, CurYTDLabor, NACurYTD, NACurYTDLY)
        select 
        12, 'DeliveryNet', 2, sum(n.DailyCY), sum(n.DailyLY), sum(l.Daily), sum(n.CurWk), sum(n.CurWkLY), sum(l.Weekly), sum(n.CurPTD), sum(n.CurPTDLY), sum(l.PeriodToDate), sum(n.CurQTD), 
        sum(n.CurQTDLY), sum(l.QuarterToDate), sum(n.CurYTD), sum(n.CurYTDLY), sum(l.yearToDate), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #DeliveryNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        join #Labor l
        on si.storeid = l.storeid
        where si.comping = 1

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY,  CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        13, 'CurbSide', 1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CurbSideNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        13, 'CurbSide', 2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #CurbSideNet n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1
/*
        insert into #ComparisonFinal (Sort, Tab, FBCID, FBCName, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY)
        select
        14, 'PPA', fbcid, fbc, restaurantid, AvgDailyCY, AvgDailyLY, AvgCurWk, AvgCurWkLY, AvgCurPTD, AvgCurPTDLY, AvgCurQTD, AvgCurQTDLY, AvgCurYTD, AvgCurYTDLY
        from #GuestAvgTemp
        where restaurantid in (1,2)
*/
        insert into #ComparisonFinal (Sort, Tab, FBCID, FBCName, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY, CurYTD, CurYTDLY)
        select
        15, 'CheckAverage', fbcid, fbc, restaurantid, AvgDailyCY, AvgDailyLY, AvgCurWk, AvgCurWkLY, AvgCurPTD, AvgCurPTDLY, AvgCurQTD, AvgCurQTDLY, AvgCurYTD, AvgCurYTDLY
        from #CheckAvgTemp
        where restaurantid in (1,2)

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY,  CurWk, CurWkLY,  CurPTD, CurPTDLY,  CurQTD, CurQTDLY,  CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        16,'POS2',1, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #POS2 n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid

        insert into #ComparisonFinal (Sort, Tab, StoreID, Daily, DailyLY, CurWk, CurWkLY, CurPTD, CurPTDLY, CurQTD, CurQTDLY, CurYTD, CurYTDLY, NACurYTD, NACurYTDLY)
        select 
        16,'POS2',2, sum(n.DailyCY), sum(n.DailyLY), sum(n.CurWk), sum(n.CurWkLY), sum(n.CurPTD), sum(n.CurPTDLY), sum(n.CurQTD), sum(n.CurQTDLY), sum(n.CurYTD), sum(n.CurYTDLY), sum(n.TrueCurYTD), sum(n.TrueCurYTDLY)
        from #POS2 n
        join #StoresIncluded si
        on n.RestaurantID = si.storeid
        where si.comping = 1

	   update #Comparisonfinal
	   set DailyLabor = isnull(DailyLabor,0),
		   CurWTDLabor = isnull(CurWTDLabor,0),
		   CurPTDLabor = isnull(CurPTDLabor,0),
		   CurQTDLabor = isnull(CurQTDLabor,0),
		   CurYTDLabor = isnull(CurYTDLabor,0)

	   update #ComparisonFinal
	   set DailyLaborPct = 
			   case
				   when dailylabor = 0 then 0
				   when daily = 0 then 0
				   else dailylabor / daily * 100
			   end,
		   CurWTDLaborPCT = 
			   case
				   when CurWTDLabor = 0 then 0
				   when CurWk = 0 then 0
				   else CurWTDLabor / CurWk * 100
			   end,
		   CurPTDLaborPct = 
			   case
				   when CurPTDLabor = 0 then 0
				   when CurPTD = 0 then 0
				   else CurPTDLabor / CurPTD * 100
			   end,
		   CurQTDLaborPct = 
			   case
				   when CurQTDLabor = 0 then 0
				   when CurQTD = 0 then 0
				   else CurQTDLabor / CurQTD * 100
			   end,
		   CurYTDLaborPct = 
			   case
				   when CurYTDLabor = 0 then 0
				   when CurYTD = 0 then 0
				   else CurYTDLabor / CurYTD * 100
			   end

	   update #ComparisonFinal
	   set CurPTDLY = 
			   case
				   when comping = 0 then 0
				   else CurPTDLY
			   end

	   update #ComparisonFinal
	   set DailyDiff = Daily - DailyLY,
		   CurWkDiff = CurWk - CurWkLY,
		   CurPTDDiff = CurPTD - CurPTDLY,
		   CurQTDDiff = CurQTD - CurQTDLY,
		   CurYTDDiff = CurYTD - CurYTDLY,
		   NAYTDDiff = NACurYTD - NACurYTDLY

	   update #ComparisonFinal
	   set Dailydiffpct = 
			   case
				   when DailyLY = 0 then 0
				   when Daily = 0 then 0
				   else DailyDiff / DailyLY * 100
			   end,
		   CurWkDiffPct = 
			   case
				   when CurWkLY = 0 then 0
				   when CurWk = 0 then 0
				   else CurWkDiff / CurWkLY * 100
			   end,
		   CurPTDdiffpct = 
			   case
				   when CurPTDLY = 0 then 0
				   when CurPTD = 0 then 0
				   else CurPTDDiff / CurPTDLY * 100
			   end,
		   CurQTDdiffpct = 
			   case
				   when CurQTDLY = 0 then 0
				   when CurQTD = 0 then 0
				   else CurQTDDiff / CurQTDLY * 100
			   end,
		   CurYTDdiffpct = 
			   case
				   when CurYTDLY = 0 then 0
				   when CurYTD = 0 then 0
				   else CurYTDDiff / CurYTDLY * 100
			   end,
		  NAYTDdiffpct = 
			 case
				when NACurYTDLY = 0 then 0
				when NACurYTD = 0 then 0
				else NAYTDDiff / NACurYTDLY * 100
			 end

	   update #ComparisonFinal
	   set StartDate = @SDate,
		   StartDateLY = @SDateLY,
		   WTDStart = @WTDStart,
		   WTDEnd = @WTDEnd,
		   WTDStartLY = @WTDStartLY,
		   WTDEndLY = @WTDEndLY,
		   PTDStart = @PTDStart,
		   PTDEnd = @PTDEnd,
		   PTDStartLY = @PTDStartLY,
		   PTDEndLY = @PTDEndLY,
		   QTDStart = @QTDStart,
		   QTDEnd = @QTDEnd,
		   QTDStartLY = @QTDStartLY,
		   QTDEndLY = @QTDEndLY,
		   YTDStart = @YTDStart,
		   YTDEnd = @YTDEnd,
		   YTDStartLY = @YTDStartLY,
		   YTDEndLY = @YTDEndLY

	   update #ComparisonFinal
	    set fbcname = 
			    case
				    when storeid = 1 then 'Total Restaurants'
				    when storeid = 2 then 'Total Comp Restaurants'
				    else fbcname
			    end,
		    storename = 
			    case
				    when storeid = 1 then 'Total Restaurants'
				    when storeid = 2 then 'Total Comp Restaurants'
				    else storename
			    end

	    update #ComparisonFinal
	    set storename =
			    case
				    when storeid = 3 then 'Total Restaurants'
				    when storeid = 4 then 'Total Comp Restaurants'
				    else storename
			    end

	   update #ComparisonFinal
	    set storename = si.StoreName,
		    Comping = si.Comping,
		    OpenDate = si.opendate
	    from #ComparisonFinal cf
	    join #StoresIncluded si
	    on cf.storeid = si.storeid

	   update #ComparisonFinal
	    set fbcID = 0 
	    where storeid in (1,2)

	   update #ComparisonFinal
	    set CurPTDLY = 
			    case
				    when comping = 0 then 0
				    else CurPTDLY
			    end

	   SELECT * FROM #ComparisonFinal cf
	   ORDER BY Sort, FBCID, Daily DESC

	   drop table #Labor
	   drop table #StoresIncluded
	   drop table #StoreDatesToReport
	  -- drop table #CompClose
	   drop table #Net
	   drop table #GNGNet
	   drop table #CateringNet
	   drop table #NonCatering
	   drop table #ChecksMinusCatering
	   drop table #OLONet
	   drop table #CheckCount
	   drop table #GuestCount
	   drop table #ComparisonFinal
	   
        -- If the table build has no error, then commit the transaction
        if (@RunType = 'Tab') commit transaction;        
        
        -- Log the successful completion
        exec Logging.dbo.Write_Procedure_Execution
             @RunType = @RunType
            ,@StartTime = @StartTime
            ,@DBName = @DBName
            ,@ProcName = @ProcName;
            
    END TRY
    BEGIN CATCH
        -- If there are errors and a transaction was started, roll it back
        IF @@TRANCOUNT > 0
        ROLLBACK TRANSACTION;
        
        -- Record the error details
        set @ErrorNumber  = ERROR_NUMBER();
        set @ErrorLine  = ERROR_LINE();
        set @ErrorMessage  = ERROR_MESSAGE();
        
        -- Log the error
        exec Logging.dbo.Write_Procedure_Execution
             @RunType = @RunType
            ,@StartTime = @StartTime
            ,@DBName = @DBName
            ,@ProcName = @ProcName
            ,@ErrorNumber = @ErrorNumber
            ,@ErrorMessage = @ErrorMessage
            ,@ErrorLine = @ErrorLine;
        THROW;
    END CATCH
END
