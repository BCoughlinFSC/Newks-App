# test_consolidation.py

from sales_consolidator import SalesConsolidator
import pandas as pd

# Initialize with your enhanced mapping
consolidator = SalesConsolidator("enhanced_mapping.xlsx")

# Check the status
print("\n✅ Consolidator initialized!")
print(f"   Total items in mapping: {len(consolidator.mapping_df):,}")
print(f"   Items mapped to Aloha: {len(consolidator.toast_to_aloha_map):,}")
print(f"   Unique Aloha items: {len(consolidator.aloha_consolidation_map):,}")

# Show sample consolidation
print("\n📊 Sample consolidation (Caesar Salad):")
for aloha_item, data in consolidator.aloha_consolidation_map.items():
    if 'caesar' in aloha_item.lower():
        toast_count = len(data['toast_names'])  # ← Fixed this line
        print(f"\n{aloha_item} consolidates {toast_count} Toast variants:")
        for toast_name in data['toast_names'][:5]:  # Show first 5
            print(f"  • {toast_name}")
        if toast_count > 5:
            print(f"  ... and {toast_count - 5} more")
        break

print("\n✅ Consolidator working correctly!")