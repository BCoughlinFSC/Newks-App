"""
Hierarchical Report Generator
==============================
Generates nested reports showing categories with drill-down to items.

Option 3: Both (Hierarchical)
- Show category totals
- Expand to see items within each category
- Support location breakdown at both levels
"""

import pandas as pd
from typing import Dict, List, Optional, Tuple
from sales_consolidator import SalesConsolidator


class HierarchicalReportGenerator:
    """Generate hierarchical reports with category → item drill-down."""
    
    def __init__(self, consolidator: SalesConsolidator):
        """
        Initialize with a SalesConsolidator instance.
        
        Args:
            consolidator: Configured SalesConsolidator
        """
        self.consolidator = consolidator
    
    def generate_hierarchical_comparison(
        self,
        comparison_df: pd.DataFrame,
        period1_label: str = "2024",
        period2_label: str = "2025"
    ) -> Dict:
        """
        Generate hierarchical comparison report.
        
        Returns dict with:
        - category_summary: Category-level totals
        - category_details: Items grouped by category
        - full_hierarchy: Complete nested structure
        """
        
        # Build column names
        p1_sales = f'{period1_label}_Sales'
        p2_sales = f'{period2_label}_Sales'
        
        # 1. Category-level summary
        category_summary = comparison_df.groupby('Category').agg({
            p1_sales: 'sum',
            p2_sales: 'sum',
            'Sales_Change': 'sum'
        }).reset_index()
        
        category_summary['Sales_Change_Pct'] = (
            (category_summary['Sales_Change'] / category_summary[p1_sales] * 100)
            .round(1)
        )
        
        category_summary = category_summary.sort_values('Sales_Change', ascending=False)
        
        # 2. Build hierarchical structure
        hierarchy = []
        
        for _, cat_row in category_summary.iterrows():
            category_name = cat_row['Category']
            
            # Get all items in this category
            category_items = comparison_df[
                comparison_df['Category'] == category_name
            ].sort_values('Sales_Change', ascending=False)
            
            hierarchy.append({
                'category': category_name,
                'category_totals': {
                    f'{period1_label}_sales': float(cat_row[p1_sales]),
                    f'{period2_label}_sales': float(cat_row[p2_sales]),
                    'change_amount': float(cat_row['Sales_Change']),
                    'change_percent': float(cat_row['Sales_Change_Pct'])
                },
                'items': [
                    {
                        'name': row['AlohaItem'],
                        f'{period1_label}_sales': float(row[p1_sales]),
                        f'{period2_label}_sales': float(row[p2_sales]),
                        f'{period1_label}_qty': float(row[f'{period1_label}_Qty']),
                        f'{period2_label}_qty': float(row[f'{period2_label}_Qty']),
                        'change_amount': float(row['Sales_Change']),
                        'change_percent': float(row['Sales_Change_Pct']),
                        'qty_change_percent': float(row['Qty_Change_Pct'])
                    }
                    for _, row in category_items.iterrows()
                ],
                'item_count': len(category_items)
            })
        
        return {
            'category_summary': category_summary,
            'hierarchy': hierarchy,
            'period1_label': period1_label,
            'period2_label': period2_label
        }
    
    def print_hierarchical_report(
        self,
        hierarchy_data: Dict,
        show_top_items: int = 5,
        show_bottom_items: int = 3
    ):
        """
        Print formatted hierarchical report to console.
        
        Args:
            hierarchy_data: Output from generate_hierarchical_comparison
            show_top_items: Number of top items to show per category
            show_bottom_items: Number of bottom items to show per category
        """
        p1 = hierarchy_data['period1_label']
        p2 = hierarchy_data['period2_label']
        
        print("\n" + "="*100)
        print(f"HIERARCHICAL YEAR-OVER-YEAR COMPARISON: {p1} vs {p2}")
        print("="*100)
        
        # Category summary
        print("\n📊 CATEGORY SUMMARY (Top Level)")
        print("-"*100)
        print(f"{'Category':<35} {p1:>15} {p2:>15} {'Change':>15} {'Change %':>10}")
        print("-"*100)
        
        cat_summary = hierarchy_data['category_summary']
        for _, row in cat_summary.iterrows():
            change_symbol = "📈" if row['Sales_Change'] > 0 else "📉" if row['Sales_Change'] < 0 else "➡️"
            print(
                f"{row['Category']:<35} "
                f"${row[f'{p1}_Sales']:>14,.0f} "
                f"${row[f'{p2}_Sales']:>14,.0f} "
                f"${row['Sales_Change']:>14,.0f} "
                f"{change_symbol} {row['Sales_Change_Pct']:>7.1f}%"
            )
        
        # Grand totals
        total_p1 = cat_summary[f'{p1}_Sales'].sum()
        total_p2 = cat_summary[f'{p2}_Sales'].sum()
        total_change = total_p2 - total_p1
        total_change_pct = (total_change / total_p1 * 100)
        
        print("-"*100)
        print(
            f"{'TOTAL':<35} "
            f"${total_p1:>14,.0f} "
            f"${total_p2:>14,.0f} "
            f"${total_change:>14,.0f} "
            f"{'📈' if total_change > 0 else '📉'} {total_change_pct:>7.1f}%"
        )
        print("="*100)
        
        # Detailed breakdown by category
        print("\n\n📋 DETAILED BREAKDOWN (Drill-Down by Category)")
        print("="*100)
        
        for cat_data in hierarchy_data['hierarchy']:
            category = cat_data['category']
            totals = cat_data['category_totals']
            items = cat_data['items']
            
            # Category header
            print(f"\n\n┏━━ {category}")
            print(f"┃   Total: ${totals[f'{p1}_sales']:,.0f} → ${totals[f'{p2}_sales']:,.0f} "
                  f"({totals['change_percent']:+.1f}%)")
            print(f"┃   Items in category: {cat_data['item_count']}")
            print("┃")
            
            # Top performers in this category
            if show_top_items > 0 and len(items) > 0:
                print(f"┃   🏆 Top {min(show_top_items, len(items))} Items:")
                print(f"┃   {'Item':<40} {p1:>12} {p2:>12} {'Change':>10}")
                print("┃   " + "-"*75)
                
                for item in items[:show_top_items]:
                    symbol = "⬆️" if item['change_percent'] > 0 else "⬇️" if item['change_percent'] < 0 else "➡️"
                    print(
                        f"┃   {item['name'][:40]:<40} "
                        f"${item[f'{p1}_sales']:>11,.0f} "
                        f"${item[f'{p2}_sales']:>11,.0f} "
                        f"{symbol} {item['change_percent']:>6.1f}%"
                    )
            
            # Bottom performers in this category (if enough items)
            if show_bottom_items > 0 and len(items) > show_top_items + show_bottom_items:
                print(f"┃   ...")
                print(f"┃   ⚠️  Bottom {show_bottom_items} Items:")
                print(f"┃   {'Item':<40} {p1:>12} {p2:>12} {'Change':>10}")
                print("┃   " + "-"*75)
                
                for item in items[-show_bottom_items:]:
                    symbol = "⬆️" if item['change_percent'] > 0 else "⬇️" if item['change_percent'] < 0 else "➡️"
                    print(
                        f"┃   {item['name'][:40]:<40} "
                        f"${item[f'{p1}_sales']:>11,.0f} "
                        f"${item[f'{p2}_sales']:>11,.0f} "
                        f"{symbol} {item['change_percent']:>6.1f}%"
                    )
            
            print("┗" + "━"*95)
        
        print("\n" + "="*100)
        print("END OF HIERARCHICAL REPORT")
        print("="*100 + "\n")
    
    def export_hierarchical_excel(
        self,
        hierarchy_data: Dict,
        output_file: str = "hierarchical_comparison.xlsx"
    ):
        """
        Export hierarchical report to Excel with multiple sheets.
        
        Sheets:
        1. Category_Summary - Top-level category totals
        2. Full_Detail - All items with category grouping
        3. Category_[Name] - Separate sheet for each category
        """
        print(f"\nExporting hierarchical report to {output_file}...")
        
        p1 = hierarchy_data['period1_label']
        p2 = hierarchy_data['period2_label']
        
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            
            # Sheet 1: Category Summary
            cat_summary = hierarchy_data['category_summary']
            cat_summary.to_excel(writer, sheet_name='Category_Summary', index=False)
            
            # Sheet 2: Full Detail (all items, sorted by category)
            all_items = []
            for cat_data in hierarchy_data['hierarchy']:
                for item in cat_data['items']:
                    all_items.append({
                        'Category': cat_data['category'],
                        'Item': item['name'],
                        f'{p1}_Sales': item[f'{p1}_sales'],
                        f'{p2}_Sales': item[f'{p2}_sales'],
                        f'{p1}_Qty': item[f'{p1}_qty'],
                        f'{p2}_Qty': item[f'{p2}_qty'],
                        'Sales_Change': item['change_amount'],
                        'Sales_Change_Pct': item['change_percent'],
                        'Qty_Change_Pct': item['qty_change_percent']
                    })
            
            full_detail_df = pd.DataFrame(all_items)
            full_detail_df.to_excel(writer, sheet_name='Full_Detail', index=False)
            
            # Sheet 3+: One sheet per category (limit to top 10 categories to avoid too many sheets)
            for i, cat_data in enumerate(hierarchy_data['hierarchy'][:10]):
                # Clean category name for sheet name (Excel sheet name limits)
                sheet_name = cat_data['category'].replace('* Consolidated ', '')[:31]
                
                items_df = pd.DataFrame(cat_data['items'])
                items_df.to_excel(writer, sheet_name=sheet_name, index=False)
        
        print(f"✓ Exported hierarchical report to {output_file}")
        print(f"  - Sheet 1: Category_Summary")
        print(f"  - Sheet 2: Full_Detail (all items)")
        print(f"  - Sheets 3+: Individual category sheets")
    
    def generate_location_hierarchy(
        self,
        comparison_by_location: pd.DataFrame,
        period1_label: str = "2024",
        period2_label: str = "2025",
        location_col: str = 'restaurantName'
    ) -> Dict:
        """
        Generate hierarchical report WITH location breakdown.
        
        Structure:
        Category → Location → Items
        
        Example:
        * Consolidated Salads
          ├─ Mobile, AL
          │  ├─ Caesar Salad
          │  ├─ Black & Bleu
          │  └─ Greek Salad
          ├─ Flowood, MS
          │  ├─ Caesar Salad
          │  └─ ...
        """
        
        p1_sales = f'{period1_label}_Sales'
        p2_sales = f'{period2_label}_Sales'
        
        hierarchy = []
        
        # Group by category
        for category in comparison_by_location['Category'].unique():
            cat_data = comparison_by_location[
                comparison_by_location['Category'] == category
            ]
            
            # Calculate category totals across all locations
            cat_totals = cat_data.groupby('Category').agg({
                p1_sales: 'sum',
                p2_sales: 'sum'
            }).iloc[0]
            
            cat_change = cat_totals[p2_sales] - cat_totals[p1_sales]
            cat_change_pct = (cat_change / cat_totals[p1_sales] * 100) if cat_totals[p1_sales] > 0 else 0
            
            # Build location breakdown
            locations = []
            for location in cat_data[location_col].unique():
                loc_data = cat_data[cat_data[location_col] == location]
                
                # Items at this location in this category
                items = [
                    {
                        'name': row['AlohaItem'],
                        f'{period1_label}_sales': float(row[p1_sales]),
                        f'{period2_label}_sales': float(row[p2_sales]),
                        'change_percent': float(row['Sales_Change_Pct'])
                    }
                    for _, row in loc_data.iterrows()
                ]
                
                loc_totals = loc_data[[p1_sales, p2_sales]].sum()
                loc_change = loc_totals[p2_sales] - loc_totals[p1_sales]
                loc_change_pct = (loc_change / loc_totals[p1_sales] * 100) if loc_totals[p1_sales] > 0 else 0
                
                locations.append({
                    'location': location,
                    'totals': {
                        f'{period1_label}_sales': float(loc_totals[p1_sales]),
                        f'{period2_label}_sales': float(loc_totals[p2_sales]),
                        'change_amount': float(loc_change),
                        'change_percent': float(loc_change_pct)
                    },
                    'items': items,
                    'item_count': len(items)
                })
            
            # Sort locations by performance
            locations.sort(key=lambda x: x['totals']['change_percent'], reverse=True)
            
            hierarchy.append({
                'category': category,
                'category_totals': {
                    f'{period1_label}_sales': float(cat_totals[p1_sales]),
                    f'{period2_label}_sales': float(cat_totals[p2_sales]),
                    'change_amount': float(cat_change),
                    'change_percent': float(cat_change_pct)
                },
                'locations': locations,
                'location_count': len(locations)
            })
        
        # Sort categories by performance
        hierarchy.sort(key=lambda x: x['category_totals']['change_percent'], reverse=True)
        
        return {
            'hierarchy': hierarchy,
            'period1_label': period1_label,
            'period2_label': period2_label
        }
    
    def print_location_hierarchy(
        self,
        location_hierarchy: Dict,
        show_top_locations: int = 3,
        show_bottom_locations: int = 2
    ):
        """Print hierarchical report with location breakdown."""
        
        p1 = location_hierarchy['period1_label']
        p2 = location_hierarchy['period2_label']
        
        print("\n" + "="*100)
        print(f"HIERARCHICAL REPORT WITH LOCATION BREAKDOWN: {p1} vs {p2}")
        print("="*100)
        
        for cat_data in location_hierarchy['hierarchy']:
            category = cat_data['category']
            cat_totals = cat_data['category_totals']
            locations = cat_data['locations']
            
            # Category header
            print(f"\n\n{'='*100}")
            print(f"📂 {category}")
            print(f"   Total (All Locations): ${cat_totals[f'{p1}_sales']:,.0f} → ${cat_totals[f'{p2}_sales']:,.0f} "
                  f"({cat_totals['change_percent']:+.1f}%)")
            print(f"   Locations: {cat_data['location_count']}")
            print("="*100)
            
            # Top performing locations
            if show_top_locations > 0:
                print(f"\n🏆 Top {min(show_top_locations, len(locations))} Locations:")
                print(f"{'Location':<30} {p1:>15} {p2:>15} {'Change':>12}")
                print("-"*75)
                
                for loc in locations[:show_top_locations]:
                    symbol = "📈" if loc['totals']['change_percent'] > 0 else "📉"
                    print(
                        f"{loc['location'][:30]:<30} "
                        f"${loc['totals'][f'{p1}_sales']:>14,.0f} "
                        f"${loc['totals'][f'{p2}_sales']:>14,.0f} "
                        f"{symbol} {loc['totals']['change_percent']:>8.1f}%"
                    )
                    
                    # Show top items at this location
                    print(f"   Top items:")
                    for item in sorted(loc['items'], key=lambda x: x['change_percent'], reverse=True)[:3]:
                        print(f"   • {item['name'][:35]:<35} {item['change_percent']:>6.1f}%")
            
            # Bottom performing locations
            if show_bottom_locations > 0 and len(locations) > show_top_locations:
                print(f"\n⚠️  Bottom {min(show_bottom_locations, len(locations) - show_top_locations)} Locations:")
                print(f"{'Location':<30} {p1:>15} {p2:>15} {'Change':>12}")
                print("-"*75)
                
                for loc in locations[-show_bottom_locations:]:
                    symbol = "📈" if loc['totals']['change_percent'] > 0 else "📉"
                    print(
                        f"{loc['location'][:30]:<30} "
                        f"${loc['totals'][f'{p1}_sales']:>14,.0f} "
                        f"${loc['totals'][f'{p2}_sales']:>14,.0f} "
                        f"{symbol} {loc['totals']['change_percent']:>8.1f}%"
                    )
        
        print("\n" + "="*100)
        print("END OF LOCATION HIERARCHY REPORT")
        print("="*100 + "\n")


def example_usage():
    """Example of how to use the hierarchical report generator."""
    
    print("""
    EXAMPLE USAGE:
    ==============
    
    # 1. Initialize
    from sales_consolidator import SalesConsolidator
    from hierarchical_reports import HierarchicalReportGenerator
    
    consolidator = SalesConsolidator("enhanced_mapping.xlsx")
    hierarchy_gen = HierarchicalReportGenerator(consolidator)
    
    # 2. Load and consolidate data
    aloha_df = consolidator.load_aloha_data("aloha_nov_2024.xls")
    toast_df = pd.read_excel("toast_nov_2025.xlsx")
    toast_consolidated = consolidator.consolidate_toast_data(toast_df)
    
    # 3. Compare
    comparison = consolidator.compare_yoy(aloha_df, toast_consolidated)
    
    # 4. Generate hierarchical report
    hierarchy = hierarchy_gen.generate_hierarchical_comparison(
        comparison,
        period1_label="Nov 2024",
        period2_label="Nov 2025"
    )
    
    # 5. Print to console
    hierarchy_gen.print_hierarchical_report(hierarchy)
    
    # 6. Export to Excel
    hierarchy_gen.export_hierarchical_excel(hierarchy, "nov_hierarchy.xlsx")
    
    # 7. WITH LOCATION BREAKDOWN:
    toast_by_location = consolidator.consolidate_toast_data(
        toast_df,
        location_col='restaurantName'
    )
    comparison_by_location = consolidator.compare_yoy(aloha_df, toast_by_location)
    
    location_hierarchy = hierarchy_gen.generate_location_hierarchy(
        comparison_by_location
    )
    
    hierarchy_gen.print_location_hierarchy(location_hierarchy)
    """)


if __name__ == "__main__":
    example_usage()
