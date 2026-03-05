"""
Sales Data Consolidation Module
================================
Consolidates Toast sales data to match Aloha item structure for YoY comparison.

This module:
1. Loads Toast sales data (from API or Excel)
2. Loads Aloha sales data (from Excel exports)
3. Uses mapping table to consolidate Toast → Aloha
4. Produces comparable reports
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from datetime import datetime
import json


class SalesConsolidator:
    """Consolidates Toast sales to match Aloha structure."""
    
    def __init__(self, mapping_file: str):
        """
        Initialize with mapping table.
        
        Args:
            mapping_file: Path to Excel file with Toast-Aloha mapping
        """
        print(f"Loading mapping table from {mapping_file}...")
        self.mapping_df = pd.read_excel(mapping_file)
        
        # Build quick lookup dictionaries
        self._build_lookup_maps()
        
        print(f"✓ Loaded {len(self.mapping_df)} item mappings")
        print(f"  - {len(self.toast_to_aloha_map)} Toast items with Aloha equivalents")
        print(f"  - {len(self.aloha_consolidation_map)} unique Aloha items")
    
    def _build_lookup_maps(self):
        """Build lookup maps for fast consolidation."""
        # Toast GUID → Aloha item name
        self.toast_to_aloha_map = {}
        # Toast name → Aloha item name (normalized)
        self.toast_name_to_aloha_map = {}
        
        # Aloha item → List of Toast GUIDs
        self.aloha_consolidation_map = {}
        
        # Toast GUID → Category
        self.toast_category_map = {}
        
        # Prefer item_guid when present, fall back to MultiLocationId for newer mappings.
        if 'item_guid' in self.mapping_df.columns:
            id_col = 'item_guid'
        elif 'MultiLocationId' in self.mapping_df.columns:
            id_col = 'MultiLocationId'
        elif 'ToastPLU' in self.mapping_df.columns:
            id_col = 'ToastPLU'
        else:
            raise ValueError(
                "Mapping file missing identifier column. Expected 'item_guid' or 'MultiLocationId' or 'ToastPLU'."
            )

        for _, row in self.mapping_df.iterrows():
            toast_guid = row[id_col]
            toast_name = row['Toast_Item_Name']
            aloha_name = row['AlohaItemName']
            category = row['ConsolidatedCat']
            
            # Store category
            if pd.notna(toast_guid):
                self.toast_category_map[toast_guid] = category
            
            # Only map if Aloha name exists
            if pd.notna(aloha_name) and pd.notna(toast_guid):
                self.toast_to_aloha_map[toast_guid] = aloha_name
                
                if aloha_name not in self.aloha_consolidation_map:
                    self.aloha_consolidation_map[aloha_name] = {
                        'toast_guids': [],
                        'toast_names': [],
                        'category': category
                    }
                
                self.aloha_consolidation_map[aloha_name]['toast_guids'].append(toast_guid)
                self.aloha_consolidation_map[aloha_name]['toast_names'].append(toast_name)

            # Also map by Toast item name for fallback matching
            if pd.notna(aloha_name) and pd.notna(toast_name):
                normalized = self.normalize_name(str(toast_name))
                if normalized:
                    self.toast_name_to_aloha_map[normalized] = aloha_name

    @staticmethod
    def normalize_name(value: str) -> str:
        """Normalize names for fuzzy matching between Toast and Aloha."""
        if value is None:
            return ""
        return " ".join(str(value).strip().lower().split())
    
    def consolidate_toast_data(self, toast_data: pd.DataFrame, 
                               guid_col: str = 'menuItemGuid',
                               qty_col: str = 'quantitySold',
                               sales_col: str = 'netSalesAmount',
                               date_col: Optional[str] = 'businessDate',
                               location_col: Optional[str] = 'restaurantName') -> pd.DataFrame:
        """
        Consolidate Toast sales data to Aloha item level.
        
        Args:
            toast_data: DataFrame with Toast sales data
            guid_col: Column name for item GUID
            qty_col: Column name for quantity sold
            sales_col: Column name for sales amount
            date_col: Column name for business date (optional)
            location_col: Column name for location (optional)
        
        Returns:
            Consolidated DataFrame with Aloha item names
        """
        print(f"\nConsolidating {len(toast_data)} Toast sales records...")
        
        # Add Aloha item name to each row
        toast_data['AlohaItem'] = toast_data[guid_col].map(self.toast_to_aloha_map)
        toast_data['Category'] = toast_data[guid_col].map(self.toast_category_map)
        
        # Count mapped vs unmapped
        mapped_count = toast_data['AlohaItem'].notna().sum()
        unmapped_count = len(toast_data) - mapped_count
        
        print(f"  - {mapped_count:,} records mapped to Aloha items ({mapped_count/len(toast_data)*100:.1f}%)")
        print(f"  - {unmapped_count:,} records unmapped (Toast-only items)")
        
        # Build grouping columns
        group_cols = ['AlohaItem', 'Category']
        if date_col and date_col in toast_data.columns:
            group_cols.append(date_col)
        if location_col and location_col in toast_data.columns:
            group_cols.append(location_col)
        
        # Aggregate by Aloha item
        consolidated = toast_data[toast_data['AlohaItem'].notna()].groupby(group_cols, dropna=False).agg({
            qty_col: 'sum',
            sales_col: 'sum'
        }).reset_index()
        
        # Rename columns for clarity
        consolidated = consolidated.rename(columns={
            qty_col: 'TotalQuantity',
            sales_col: 'TotalSales'
        })
        
        # Add average price
        consolidated['AvgPrice'] = (consolidated['TotalSales'] / consolidated['TotalQuantity']).round(2)
        
        print(f"\n✓ Consolidated to {len(consolidated)} unique Aloha items")
        
        return consolidated
    
    def load_aloha_data(self, aloha_file: str, 
                        skip_rows: int = 3,
                        item_col: str = None,
                        qty_col: str = None,
                        sales_col: str = None) -> pd.DataFrame:
        """
        Load Aloha product mix export.
        
        Args:
            aloha_file: Path to Aloha Excel export
            skip_rows: Rows to skip at top of file
            item_col: Column name/index for item names
            qty_col: Column name/index for quantity
            sales_col: Column name/index for sales
        
        Returns:
            DataFrame with Aloha data
        """
        print(f"\nLoading Aloha data from {aloha_file}...")
        
        # Read with header detection
        if aloha_file.lower().endswith('.csv'):
            df = pd.read_csv(aloha_file, header=None)
        else:
            df = pd.read_excel(aloha_file, header=None)
        
        # Find the header row (contains "Item" and "Qty" or similar)
        header_row = None
        for idx, row in df.iterrows():
            row_str = ' '.join([str(x).lower() for x in row if pd.notna(x)])
            if 'qty' in row_str and ('item' in row_str or 'sales' in row_str):
                header_row = idx
                break
        
        headerless = False
        if header_row is None:
            print("Warning: Could not auto-detect header. Treating file as headerless.")
            headerless = True
        
        # Re-read with correct header (or no header)
        if aloha_file.lower().endswith('.csv'):
            df = pd.read_csv(aloha_file, header=None if headerless else header_row)
        else:
            df = pd.read_excel(aloha_file, header=None if headerless else header_row)
        
        # Auto-detect columns if not specified
        if not headerless:
            if item_col is None:
                # Find column with "item" or "name" in header
                for col in df.columns:
                    if 'item' in str(col).lower() or 'name' in str(col).lower():
                        item_col = col
                        break
            
            if qty_col is None:
                for col in df.columns:
                    if 'qty' in str(col).lower() or 'quantity' in str(col).lower():
                        qty_col = col
                        break
            
            if sales_col is None:
                for col in df.columns:
                    if 'sales' in str(col).lower() or 'amount' in str(col).lower():
                        sales_col = col
                        break
            # Optional: location/store for multi-location Aloha data
            location_col = None
            for col in df.columns:
                if str(col).strip().lower() in {'location', 'store', 'restaurant', 'locationname', 'storeid', 'site', 'unit'}:
                    location_col = col
                    break
        else:
            # Headerless override: prefer column 4 as item name when present
            if item_col is None and len(df.columns) >= 4:
                item_col = df.columns[3]
                print("  Headerless override: using column 4 for item names")
            # Headerless override: prefer column 6 for quantity, column 7 for sales
            if qty_col is None and len(df.columns) >= 6:
                qty_col = df.columns[5]
                print("  Headerless override: using column 6 for quantity")
            if sales_col is None and len(df.columns) >= 7:
                sales_col = df.columns[6]
                print("  Headerless override: using column 7 for sales")
        
        # Fallback column inference if any still missing
        if item_col is None or qty_col is None or sales_col is None:
            print("Warning: Could not detect all columns by header. Inferring by data shape...")
            # Identify numeric columns by convertibility
            numeric_scores = {}
            numeric_series = {}
            for col in df.columns:
                series = pd.to_numeric(df[col], errors='coerce')
                numeric_scores[col] = series.notna().sum()
                numeric_series[col] = series
            
            numeric_cols = sorted(numeric_scores.items(), key=lambda x: x[1], reverse=True)
            numeric_cols = [c for c, score in numeric_cols if score > 0]
            
            # Pick item column based on text uniqueness (headerless files)
            if item_col is None:
                text_candidates = []
                for col in df.columns:
                    series = df[col].dropna()
                    if series.empty:
                        continue
                    # Skip numeric-heavy columns
                    numeric_ratio = pd.to_numeric(series, errors='coerce').notna().mean()
                    if numeric_ratio > 0.5:
                        continue
                    # Skip date-like columns
                    date_ratio = pd.to_datetime(series, errors='coerce').notna().mean()
                    if date_ratio > 0.5:
                        continue
                    unique_count = series.astype(str).nunique()
                    avg_len = series.astype(str).str.len().mean()
                    text_candidates.append((unique_count, avg_len, col))
                if text_candidates:
                    text_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    item_col = text_candidates[0][2]
            
            # Pick qty/sales as top two numeric columns (by non-null count)
            if (qty_col is None or sales_col is None) and len(numeric_cols) > 0:
                # Rank numeric columns by "integer-like" ratio for qty
                numeric_rank = []
                for col in numeric_cols:
                    series = numeric_series[col]
                    non_null = series.dropna()
                    if non_null.empty:
                        continue
                    integer_like = (non_null % 1 == 0).mean()
                    numeric_rank.append((integer_like, non_null.count(), col))
                numeric_rank.sort(key=lambda x: (x[0], x[1]), reverse=True)
                
                if qty_col is None and numeric_rank:
                    qty_col = numeric_rank[0][2]
            if sales_col is None:
                for _, _, col in numeric_rank:
                    if col != qty_col:
                        sales_col = col
                        break
                if sales_col is None and len(numeric_rank) > 1:
                    sales_col = numeric_rank[1][2]
            location_col = None  # headerless: no location column assumed
        
        if item_col is None or qty_col is None or sales_col is None:
            raise ValueError(
                f"Unable to detect columns (item_col={item_col}, qty_col={qty_col}, sales_col={sales_col}). "
                "Please specify column names explicitly."
            )
        
        print(f"  Detected columns:")
        print(f"    - Item: {item_col}")
        print(f"    - Quantity: {qty_col}")
        print(f"    - Sales: {sales_col}")
        if location_col is not None:
            print(f"    - Location: {location_col}")
        
        # Clean and filter
        keep = [item_col, qty_col, sales_col]
        if location_col is not None:
            keep = [item_col, qty_col, sales_col, location_col]
        df = df[keep].copy()
        df.columns = ['AlohaItem', 'Quantity', 'Sales'] + (['Location'] if location_col is not None else [])
        
        # Remove summary rows and nulls
        df = df[df['AlohaItem'].notna()]
        df = df[~df['AlohaItem'].astype(str).str.contains('Total|TOTAL|Grand', case=False, na=False)]
        
        # Convert numeric columns
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce').fillna(0)
        df['Sales'] = pd.to_numeric(df['Sales'], errors='coerce').fillna(0)
        
        # Add average price
        df['AvgPrice'] = (df['Sales'] / df['Quantity']).replace([np.inf, -np.inf], 0).round(2)
        
        loc_info = f" ({df['Location'].nunique()} locations)" if 'Location' in df.columns else ""
        print(f"✓ Loaded {len(df)} Aloha items{loc_info}")
        
        return df
    
    def compare_yoy(self, aloha_data: pd.DataFrame, 
                    toast_consolidated: pd.DataFrame,
                    aloha_period: str = "2024",
                    toast_period: str = "2025") -> pd.DataFrame:
        """
        Compare year-over-year performance.
        
        Args:
            aloha_data: Aloha sales data
            toast_consolidated: Consolidated Toast data
            aloha_period: Label for Aloha period
            toast_period: Label for Toast period
        
        Returns:
            Comparison DataFrame
        """
        print(f"\nComparing {aloha_period} (Aloha) vs {toast_period} (Toast)...")
        
        # Ensure both have same columns
        aloha_summary = aloha_data.groupby('AlohaItem').agg({
            'Quantity': 'sum',
            'Sales': 'sum'
        }).reset_index()
        aloha_summary[f'{aloha_period}_Qty'] = aloha_summary['Quantity']
        aloha_summary[f'{aloha_period}_Sales'] = aloha_summary['Sales']
        aloha_summary[f'{aloha_period}_AvgPrice'] = (aloha_summary['Sales'] / aloha_summary['Quantity']).round(2)
        
        toast_summary = toast_consolidated.groupby('AlohaItem').agg({
            'TotalQuantity': 'sum',
            'TotalSales': 'sum'
        }).reset_index()
        toast_summary[f'{toast_period}_Qty'] = toast_summary['TotalQuantity']
        toast_summary[f'{toast_period}_Sales'] = toast_summary['TotalSales']
        toast_summary[f'{toast_period}_AvgPrice'] = (toast_summary['TotalSales'] / toast_summary['TotalQuantity']).round(2)
        
        # Merge
        comparison = pd.merge(
            aloha_summary[['AlohaItem', f'{aloha_period}_Qty', f'{aloha_period}_Sales', f'{aloha_period}_AvgPrice']],
            toast_summary[['AlohaItem', f'{toast_period}_Qty', f'{toast_period}_Sales', f'{toast_period}_AvgPrice']],
            on='AlohaItem',
            how='outer'
        ).fillna(0)
        
        # Calculate changes
        comparison['Qty_Change'] = comparison[f'{toast_period}_Qty'] - comparison[f'{aloha_period}_Qty']
        comparison['Qty_Change_Pct'] = ((comparison[f'{toast_period}_Qty'] / comparison[f'{aloha_period}_Qty'].replace(0, np.nan) - 1) * 100).round(1)
        
        comparison['Sales_Change'] = comparison[f'{toast_period}_Sales'] - comparison[f'{aloha_period}_Sales']
        comparison['Sales_Change_Pct'] = ((comparison[f'{toast_period}_Sales'] / comparison[f'{aloha_period}_Sales'].replace(0, np.nan) - 1) * 100).round(1)
        
        comparison['Price_Change'] = comparison[f'{toast_period}_AvgPrice'] - comparison[f'{aloha_period}_AvgPrice']
        comparison['Price_Change_Pct'] = ((comparison[f'{toast_period}_AvgPrice'] / comparison[f'{aloha_period}_AvgPrice'].replace(0, np.nan) - 1) * 100).round(1)
        
        # Add category from mapping
        item_to_category = self.mapping_df[self.mapping_df['AlohaItemName'].notna()].set_index('AlohaItemName')['ConsolidatedCat'].to_dict()
        comparison['Category'] = comparison['AlohaItem'].map(item_to_category)
        
        # Sort by sales change
        comparison = comparison.sort_values('Sales_Change', ascending=False)
        
        print(f"✓ Comparison complete for {len(comparison)} items")
        
        return comparison
    
    def generate_summary_report(self, comparison_df: pd.DataFrame) -> str:
        """Generate a text summary of the comparison."""
        report = []
        report.append("="*80)
        report.append("YEAR-OVER-YEAR COMPARISON SUMMARY")
        report.append("="*80)
        
        # Overall totals
        periods = [col.split('_')[0] for col in comparison_df.columns if '_Sales' in col and 'Change' not in col]
        
        report.append("\nOVERALL PERFORMANCE:")
        report.append("-"*80)
        for period in periods:
            total_sales = comparison_df[f'{period}_Sales'].sum()
            total_qty = comparison_df[f'{period}_Qty'].sum()
            report.append(f"{period}:")
            report.append(f"  Total Sales: ${total_sales:,.2f}")
            report.append(f"  Total Quantity: {total_qty:,.0f} units")
        
        if len(periods) == 2:
            sales_change = comparison_df['Sales_Change'].sum()
            sales_change_pct = (sales_change / comparison_df[f'{periods[0]}_Sales'].sum() * 100)
            report.append(f"\nChange: ${sales_change:,.2f} ({sales_change_pct:+.1f}%)")
        
        # Top winners and losers
        report.append("\n\nTOP 10 WINNERS (by sales change):")
        report.append("-"*80)
        winners = comparison_df.nlargest(10, 'Sales_Change')
        for _, row in winners.iterrows():
            report.append(f"{row['AlohaItem']:<40} ${row['Sales_Change']:>10,.2f} ({row['Sales_Change_Pct']:>6.1f}%)")
        
        report.append("\n\nTOP 10 LOSERS (by sales change):")
        report.append("-"*80)
        losers = comparison_df.nsmallest(10, 'Sales_Change')
        for _, row in losers.iterrows():
            report.append(f"{row['AlohaItem']:<40} ${row['Sales_Change']:>10,.2f} ({row['Sales_Change_Pct']:>6.1f}%)")
        
        # Category performance
        report.append("\n\nPERFORMANCE BY CATEGORY:")
        report.append("-"*80)
        cat_summary = comparison_df.groupby('Category').agg({
            'Sales_Change': 'sum',
            f'{periods[0]}_Sales': 'sum'
        })
        cat_summary['Change_Pct'] = (cat_summary['Sales_Change'] / cat_summary[f'{periods[0]}_Sales'] * 100).round(1)
        cat_summary = cat_summary.sort_values('Sales_Change', ascending=False)
        
        for cat, row in cat_summary.iterrows():
            report.append(f"{str(cat):<40} ${row['Sales_Change']:>10,.2f} ({row['Change_Pct']:>6.1f}%)")
        
        return "\n".join(report)
    
    def export_comparison(self, comparison_df: pd.DataFrame, 
                         output_file: str = "yoy_comparison.xlsx"):
        """Export comparison to Excel."""
        print(f"\nExporting comparison to {output_file}...")
        
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            # Full comparison
            comparison_df.to_excel(writer, sheet_name='Full_Comparison', index=False)
            
            # Category summary
            periods = [col.split('_')[0] for col in comparison_df.columns if '_Sales' in col and 'Change' not in col]
            cat_summary = comparison_df.groupby('Category').agg({
                f'{periods[0]}_Sales': 'sum',
                f'{periods[1]}_Sales': 'sum',
                'Sales_Change': 'sum',
                'Sales_Change_Pct': 'mean'
            }).round(2)
            cat_summary.to_excel(writer, sheet_name='Category_Summary')
            
            # Top movers
            comparison_df.nlargest(50, 'Sales_Change').to_excel(writer, sheet_name='Top_Winners', index=False)
            comparison_df.nsmallest(50, 'Sales_Change').to_excel(writer, sheet_name='Top_Losers', index=False)
        
        print(f"✓ Exported to {output_file}")


def main():
    """Example usage."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Consolidate and compare Toast vs Aloha sales')
    parser.add_argument('--mapping', type=str, required=True,
                       help='Path to mapping Excel file')
    parser.add_argument('--toast-data', type=str,
                       help='Path to Toast sales data (Excel or CSV)')
    parser.add_argument('--aloha-data', type=str,
                       help='Path to Aloha sales data (Excel)')
    parser.add_argument('--output', type=str, default='yoy_comparison.xlsx',
                       help='Output file for comparison')
    
    args = parser.parse_args()
    
    # Initialize consolidator
    consolidator = SalesConsolidator(args.mapping)
    
    # Example workflow
    if args.toast_data and args.aloha_data:
        # Load Toast data
        toast_df = pd.read_excel(args.toast_data) if args.toast_data.endswith('.xlsx') else pd.read_csv(args.toast_data)
        
        # Consolidate Toast to Aloha level
        toast_consolidated = consolidator.consolidate_toast_data(toast_df)
        
        # Load Aloha data
        aloha_df = consolidator.load_aloha_data(args.aloha_data)
        
        # Compare
        comparison = consolidator.compare_yoy(aloha_df, toast_consolidated)
        
        # Generate report
        report = consolidator.generate_summary_report(comparison)
        print("\n" + report)
        
        # Export
        consolidator.export_comparison(comparison, args.output)
    else:
        print("\nConsolidator initialized. Provide --toast-data and --aloha-data to run comparison.")


if __name__ == "__main__":
    main()
