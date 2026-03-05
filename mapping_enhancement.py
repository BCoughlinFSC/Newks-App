"""
POS Mapping Enhancement Tool
============================
Analyzes existing Aloha-Toast mapping and suggests improvements.

Usage:
    python mapping_enhancement.py --mapping Items_with_MLI_GUID_PLU_for_gsheet_6.xlsx
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
import re
from difflib import SequenceMatcher
import warnings
warnings.filterwarnings('ignore')


class POSMappingAnalyzer:
    """Analyzes and enhances POS item mappings between Aloha and Toast."""
    
    def __init__(self, mapping_file: str):
        """Load the existing mapping table."""
        print(f"Loading mapping table from {mapping_file}...")
        self.df = pd.read_excel(mapping_file)
        self.total_items = len(self.df)
        self.mapped_items = self.df['AlohaItemName'].notna().sum()
        self.unmapped_items = self.total_items - self.mapped_items
        
        print(f"✓ Loaded {self.total_items:,} total items")
        print(f"  - {self.mapped_items:,} mapped to Aloha")
        print(f"  - {self.unmapped_items:,} Toast-only (unmapped)")
        
    def analyze_mapping_quality(self) -> pd.DataFrame:
        """Analyze the quality of existing mappings."""
        print("\n" + "="*80)
        print("MAPPING QUALITY ANALYSIS")
        print("="*80)
        
        results = []
        
        # 1. Category coverage
        print("\n1. Coverage by Category:")
        category_stats = self.df.groupby('ConsolidatedCat').agg({
            'Toast_Item_Name': 'count',
            'AlohaItemName': lambda x: x.notna().sum()
        }).rename(columns={
            'Toast_Item_Name': 'Total',
            'AlohaItemName': 'Mapped'
        })
        category_stats['Unmapped'] = category_stats['Total'] - category_stats['Mapped']
        category_stats['Coverage %'] = (category_stats['Mapped'] / category_stats['Total'] * 100).round(1)
        category_stats = category_stats.sort_values('Coverage %')
        
        print(category_stats)
        
        # 2. Many-to-one analysis
        print("\n2. Toast-to-Aloha Ratio (Many-to-One Mapping):")
        mapped = self.df[self.df['AlohaItemName'].notna()].copy()
        ratio_stats = mapped.groupby('AlohaItemName').size().describe()
        print(f"   Average Toast items per Aloha item: {ratio_stats['mean']:.1f}")
        print(f"   Max Toast variants for one Aloha item: {ratio_stats['max']:.0f}")
        
        # Show examples of high-ratio items
        high_ratio = mapped.groupby('AlohaItemName').size().sort_values(ascending=False).head(10)
        print("\n   Top 10 Aloha items with most Toast variants:")
        for aloha_name, count in high_ratio.items():
            print(f"   - {aloha_name}: {count} Toast variants")
            
        return category_stats
    
    def normalize_name(self, name: str) -> str:
        """Normalize item name for matching."""
        if pd.isna(name):
            return ""
        
        # Convert to lowercase
        name = str(name).lower()
        
        # Remove common modifiers/context
        remove_patterns = [
            r'\(olo.*?\)',
            r'\(pos.*?\)',
            r'\(entree\)',
            r'\(pairing.*?\)',
            r'\(meal deal.*?\)',
            r'\(pick [0-9].*?\)',
            r'\(full.*?\)',
            r'\(half.*?\)',
        ]
        for pattern in remove_patterns:
            name = re.sub(pattern, '', name, flags=re.IGNORECASE)
        
        # Standardize separators
        name = name.replace('&', 'and')
        name = name.replace('1/2', 'half')
        name = name.replace('/', ' ')
        
        # Remove extra whitespace
        name = ' '.join(name.split())
        
        return name.strip()
    
    def calculate_similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity score between two strings (0-100)."""
        return SequenceMatcher(None, str1, str2).ratio() * 100
    
    def suggest_mappings(self, min_confidence: float = 60.0) -> pd.DataFrame:
        """Suggest mappings for unmapped Toast items."""
        print("\n" + "="*80)
        print("AUTOMATIC MAPPING SUGGESTIONS")
        print("="*80)
        
        # Get unmapped Toast items
        unmapped = self.df[self.df['AlohaItemName'].isna()].copy()
        print(f"\nAnalyzing {len(unmapped):,} unmapped Toast items...")
        
        # Get all unique Aloha items as potential matches
        aloha_items = self.df[self.df['AlohaItemName'].notna()]['AlohaItemName'].unique()
        
        suggestions = []
        
        for idx, row in unmapped.iterrows():
            toast_name = row['Toast_Item_Name']
            toast_normalized = self.normalize_name(toast_name)
            
            if not toast_normalized:
                continue
            
            # Find best match from Aloha items
            best_match = None
            best_score = 0
            
            for aloha_name in aloha_items:
                aloha_normalized = self.normalize_name(aloha_name)
                score = self.calculate_similarity(toast_normalized, aloha_normalized)
                
                if score > best_score:
                    best_score = score
                    best_match = aloha_name
            
            if best_score >= min_confidence:
                suggestions.append({
                    'Toast_GUID': row['item_guid'],
                    'Toast_Item_Name': toast_name,
                    'Suggested_Aloha_Item': best_match,
                    'Confidence_Score': round(best_score, 1),
                    'ConsolidatedCat': row['ConsolidatedCat'],
                    'Status': 'High Confidence' if best_score >= 80 else 'Review Needed'
                })
        
        suggestions_df = pd.DataFrame(suggestions)
        
        if len(suggestions_df) > 0:
            print(f"\n✓ Found {len(suggestions_df)} potential matches")
            print(f"  - High confidence (≥80%): {(suggestions_df['Confidence_Score'] >= 80).sum()}")
            print(f"  - Medium confidence (60-80%): {((suggestions_df['Confidence_Score'] >= 60) & (suggestions_df['Confidence_Score'] < 80)).sum()}")
            
            # Show sample suggestions
            print("\nSample suggestions (top 10 by confidence):")
            print(suggestions_df.nlargest(10, 'Confidence_Score')[
                ['Toast_Item_Name', 'Suggested_Aloha_Item', 'Confidence_Score', 'Status']
            ].to_string(index=False))
        else:
            print("\n✗ No high-confidence matches found")
            
        return suggestions_df
    
    def create_consolidation_map(self) -> Dict[str, List[str]]:
        """Create a map of Aloha items to Toast items (for aggregation)."""
        print("\n" + "="*80)
        print("CONSOLIDATION MAP")
        print("="*80)
        
        mapped = self.df[self.df['AlohaItemName'].notna()].copy()
        
        # Group Toast items by Aloha item
        consolidation = {}
        for aloha_item in mapped['AlohaItemName'].unique():
            toast_items = mapped[mapped['AlohaItemName'] == aloha_item]['Toast_Item_Name'].tolist()
            toast_guids = mapped[mapped['AlohaItemName'] == aloha_item]['item_guid'].tolist()
            
            consolidation[aloha_item] = {
                'toast_names': toast_items,
                'toast_guids': toast_guids,
                'count': len(toast_items),
                'category': mapped[mapped['AlohaItemName'] == aloha_item]['ConsolidatedCat'].iloc[0]
            }
        
        print(f"\n✓ Created consolidation map for {len(consolidation)} Aloha items")
        
        # Show examples
        print("\nExamples of consolidation (items with 5+ Toast variants):")
        high_variant_items = {k: v for k, v in consolidation.items() if v['count'] >= 5}
        for aloha_item, data in sorted(high_variant_items.items(), key=lambda x: x[1]['count'], reverse=True)[:5]:
            print(f"\n{aloha_item} ({data['count']} Toast variants):")
            for toast_name in data['toast_names'][:3]:
                print(f"  - {toast_name}")
            if data['count'] > 3:
                print(f"  ... and {data['count'] - 3} more")
        
        return consolidation
    
    def export_enhanced_mapping(self, suggestions_df: pd.DataFrame, output_file: str = "enhanced_mapping.xlsx"):
        """Export enhanced mapping with suggestions."""
        print("\n" + "="*80)
        print("EXPORTING RESULTS")
        print("="*80)
        
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            # Sheet 1: Original mapping
            self.df.to_excel(writer, sheet_name='Original_Mapping', index=False)
            
            # Sheet 2: Suggestions for review
            if len(suggestions_df) > 0:
                suggestions_df.to_excel(writer, sheet_name='Suggested_Mappings', index=False)
            
            # Sheet 3: Category coverage summary
            category_stats = self.df.groupby('ConsolidatedCat').agg({
                'Toast_Item_Name': 'count',
                'AlohaItemName': lambda x: x.notna().sum()
            }).rename(columns={
                'Toast_Item_Name': 'Total_Items',
                'AlohaItemName': 'Mapped_Items'
            })
            category_stats['Unmapped_Items'] = category_stats['Total_Items'] - category_stats['Mapped_Items']
            category_stats['Coverage_Percent'] = (category_stats['Mapped_Items'] / category_stats['Total_Items'] * 100).round(1)
            category_stats.to_excel(writer, sheet_name='Category_Coverage')
            
            # Sheet 4: Unmapped items needing manual review
            unmapped_low_conf = self.df[
                (self.df['AlohaItemName'].isna()) & 
                (~self.df['item_guid'].isin(suggestions_df['Toast_GUID'].tolist() if len(suggestions_df) > 0 else []))
            ][['item_guid', 'Toast_Item_Name', 'ConsolidatedCat']]
            unmapped_low_conf.to_excel(writer, sheet_name='Manual_Review_Needed', index=False)
        
        print(f"\n✓ Exported enhanced mapping to: {output_file}")
        print(f"  Sheets created:")
        print(f"  1. Original_Mapping - Your current mapping")
        print(f"  2. Suggested_Mappings - Auto-generated suggestions")
        print(f"  3. Category_Coverage - Coverage statistics")
        print(f"  4. Manual_Review_Needed - Items needing manual review")
    
    def generate_report(self) -> str:
        """Generate a comprehensive text report."""
        report = []
        report.append("="*80)
        report.append("POS MAPPING ANALYSIS REPORT")
        report.append("="*80)
        report.append(f"\nGenerated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        report.append("\n" + "-"*80)
        report.append("OVERALL STATISTICS")
        report.append("-"*80)
        report.append(f"Total Toast items: {self.total_items:,}")
        report.append(f"Mapped to Aloha: {self.mapped_items:,} ({self.mapped_items/self.total_items*100:.1f}%)")
        report.append(f"Unmapped (Toast-only): {self.unmapped_items:,} ({self.unmapped_items/self.total_items*100:.1f}%)")
        
        # Category breakdown
        report.append("\n" + "-"*80)
        report.append("CATEGORY BREAKDOWN")
        report.append("-"*80)
        cat_counts = self.df['ConsolidatedCat'].value_counts()
        for cat, count in cat_counts.items():
            mapped_in_cat = self.df[self.df['ConsolidatedCat'] == cat]['AlohaItemName'].notna().sum()
            report.append(f"{cat}: {count} items ({mapped_in_cat} mapped, {count-mapped_in_cat} unmapped)")
        
        return "\n".join(report)


def main():
    """Main execution function."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Analyze and enhance POS mapping table')
    parser.add_argument('--mapping', type=str, 
                       default='Items_with_MLI_GUID_PLU_for_gsheet_6.xlsx',
                       help='Path to mapping Excel file')
    parser.add_argument('--min-confidence', type=float, default=60.0,
                       help='Minimum confidence score for suggestions (0-100)')
    parser.add_argument('--output', type=str, default='enhanced_mapping.xlsx',
                       help='Output file for enhanced mapping')
    
    args = parser.parse_args()
    
    # Initialize analyzer
    analyzer = POSMappingAnalyzer(args.mapping)
    
    # Run analysis
    category_stats = analyzer.analyze_mapping_quality()
    
    # Generate suggestions
    suggestions = analyzer.suggest_mappings(min_confidence=args.min_confidence)
    
    # Create consolidation map
    consolidation_map = analyzer.create_consolidation_map()
    
    # Export results
    analyzer.export_enhanced_mapping(suggestions, args.output)
    
    # Generate report
    report = analyzer.generate_report()
    print("\n" + report)
    
    # Save report to file
    report_file = args.output.replace('.xlsx', '_report.txt')
    with open(report_file, 'w') as f:
        f.write(report)
    print(f"\n✓ Text report saved to: {report_file}")
    
    print("\n" + "="*80)
    print("ANALYSIS COMPLETE!")
    print("="*80)
    print("\nNext steps:")
    print("1. Review the suggested mappings in the Excel file")
    print("2. Manually review items flagged for manual review")
    print("3. Update your mapping table with accepted suggestions")
    print("4. Re-run this script to verify improvements")


if __name__ == "__main__":
    main()
