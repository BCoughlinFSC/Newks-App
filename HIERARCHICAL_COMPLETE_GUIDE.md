# 🎯 YOUR HIERARCHICAL REPORTING SOLUTION
## Complete Setup Guide for Option 3: Both (Hierarchical)

---

## ✅ Perfect Choice!

**Option 3: Hierarchical** gives you:
- 📊 **Category-level summaries** (for executives)
- 🔍 **Item-level drill-down** (for operations)
- 🏪 **Location breakdown** (for multi-unit analysis)
- 📑 **Beautiful formatted reports** (console + Excel)

---

## 📦 What You Have Now

### **Core Files:**
1. ✅ `mapping_enhancement.py` - Analyzes and improves your mapping
2. ✅ `sales_consolidator.py` - Consolidates Toast → Aloha
3. ✅ `hierarchical_reports.py` - **NEW! Generates hierarchical reports**
4. ✅ `yoy_integration.py` - FastAPI endpoints for your web app
5. ✅ `enhanced_mapping.xlsx` - **Already analyzed with 492 suggestions!**

### **Documentation:**
1. ✅ `IMPLEMENTATION_GUIDE.md` - Step-by-step setup
2. ✅ `HIERARCHICAL_EXAMPLES.md` - **NEW! Shows what reports look like**
3. ✅ `YOUR_CUSTOM_SETUP.md` - Customized for your 97 locations
4. ✅ `pos_mapping_solution.md` - Strategy overview

---

## 🚀 Quick Start: Generate Your First Hierarchical Report

### **Step 1: Install (30 seconds)**

```bash
# Copy files to your project
cp sales_consolidator.py your_project/
cp hierarchical_reports.py your_project/
cp enhanced_mapping.xlsx your_project/

# Install dependencies (if needed)
pip install pandas openpyxl
```

### **Step 2: Run First Report (2 minutes)**

```python
from sales_consolidator import SalesConsolidator
from hierarchical_reports import HierarchicalReportGenerator
import pandas as pd

# 1. Initialize
consolidator = SalesConsolidator("enhanced_mapping.xlsx")
hierarchy_gen = HierarchicalReportGenerator(consolidator)

# 2. Load your November data
aloha_df = consolidator.load_aloha_data("aloha_november_2024.xls")

# Get Toast data (from your existing API or export)
toast_df = pd.read_excel("toast_november_2025.xlsx")

# 3. Consolidate Toast → Aloha
toast_consolidated = consolidator.consolidate_toast_data(
    toast_df,
    guid_col='menuItemGuid',
    qty_col='quantitySold',
    sales_col='netSalesAmount'
)

# 4. Compare
comparison = consolidator.compare_yoy(
    aloha_df, 
    toast_consolidated,
    aloha_period="Nov 2024",
    toast_period="Nov 2025"
)

# 5. Generate hierarchical report
hierarchy = hierarchy_gen.generate_hierarchical_comparison(
    comparison,
    period1_label="Nov 2024",
    period2_label="Nov 2025"
)

# 6. Print to console (beautiful formatted output!)
hierarchy_gen.print_hierarchical_report(hierarchy)

# 7. Export to Excel
hierarchy_gen.export_hierarchical_excel(
    hierarchy,
    "november_2024_vs_2025_hierarchical.xlsx"
)

print("\n✅ Done! Check november_2024_vs_2025_hierarchical.xlsx")
```

**That's it!** You now have a hierarchical report.

---

## 📊 What You Get: Three Report Levels

### **Level 1: Category Summary (Executive View)**

```
Category                      Nov 2024     Nov 2025      Change
────────────────────────────────────────────────────────────────
* Consolidated Catering       $892,150    $1,023,400    +14.7% 📈
* Consolidated Soups          $321,450      $354,300    +10.2% 📈
* Consolidated Pizza          $286,400      $307,900     +7.5% 📈
* Consolidated Salads         $445,230      $462,150     +3.8% 📈
* Consolidated Sandwiches     $628,940      $615,280     -2.2% 📉
────────────────────────────────────────────────────────────────
TOTAL                       $3,012,400    $3,199,890     +6.2% 📈
```

**Use this when:**
- Presenting to executives
- Board meetings
- Quick performance overview
- High-level trends

---

### **Level 2: Item Drill-Down (Operations View)**

```
┏━━ * Consolidated Soups
┃   Total: $321,450 → $354,300 (+10.2%)
┃   Items in category: 15
┃
┃   🏆 Top 5 Items:
┃   Item                          Nov 2024     Nov 2025     Change
┃   ──────────────────────────────────────────────────────────────
┃   Bowl Lobster Bisque            $78,920      $95,460  ⬆️ +21.0%
┃   Bowl Chicken Tortilla          $56,780      $67,920  ⬆️ +19.6%
┃   Cup Lobster Bisque             $45,230      $52,890  ⬆️ +16.9%
┃   Bowl Loaded Potato             $38,450      $44,680  ⬆️ +16.2%
┃   Cup Chicken Tortilla           $32,150      $36,920  ⬆️ +14.8%
┃   ...
┃   ⚠️  Bottom 3 Items:
┃   Bowl Maryland Crab Soup         $8,920       $7,450  ⬇️ -16.5%
┃   Cup Maryland Crab Soup          $6,780       $5,920  ⬇️ -12.7%
┃   Bowl Tomato Basil              $12,450      $11,230  ⬇️  -9.8%
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Use this when:**
- Menu optimization
- Pricing decisions
- Identifying winners/losers
- Recipe changes

---

### **Level 3: Location Breakdown (Multi-Unit View)**

```python
# Add location dimension (just one parameter!)
toast_by_location = consolidator.consolidate_toast_data(
    toast_df,
    guid_col='menuItemGuid',
    qty_col='quantitySold',
    sales_col='netSalesAmount',
    location_col='restaurantName'  # ← This adds location!
)

comparison_by_location = consolidator.compare_yoy(
    aloha_df, 
    toast_by_location
)

location_hierarchy = hierarchy_gen.generate_location_hierarchy(
    comparison_by_location
)

hierarchy_gen.print_location_hierarchy(location_hierarchy)
```

**Output:**
```
📂 * Consolidated Soups
   Total (All Locations): $321,450 → $354,300 (+10.2%)
   Locations: 97

🏆 Top 3 Locations:
Location                 Nov 2024     Nov 2025     Change
──────────────────────────────────────────────────────────
Mobile, AL                $8,450      $10,920  📈 +29.2%
   Top items:
   • Bowl Lobster Bisque              +35.8%
   • Bowl Chicken Tortilla            +28.4%
   • Cup Lobster Bisque               +24.2%

Fort Collins, CO          $6,780       $8,560  📈 +26.3%
   Top items:
   • Bowl Lobster Bisque              +32.1%
   • Bowl Loaded Potato               +25.9%
   • Cup Chicken Tortilla             +22.5%

⚠️  Bottom 2 Locations:
Flowood, MS               $4,230       $3,890  📉  -8.0%
Location X                $3,560       $3,120  📉 -12.4%
```

**Use this when:**
- Store performance reviews
- Training decisions
- Identifying best practices
- Regional marketing

---

## 📑 Excel Export Structure

The Excel file has multiple worksheets:

### **Sheet 1: Category_Summary**
High-level rollup:
```
Category               Nov2024_Sales  Nov2025_Sales  Sales_Change  Sales_Change_Pct
Catering                  892,150      1,023,400       131,250         14.7
Soups                     321,450        354,300        32,850         10.2
Pizza                     286,400        307,900        21,500          7.5
```

### **Sheet 2: Full_Detail**
All items with categories:
```
Category    Item                      Nov2024_Sales  Nov2025_Sales  Change_Pct
Catering    Catering Caesar Salad        245,230        290,460       18.5
Catering    Catering Sandwich Platter    198,450        235,680       18.8
Soups       Bowl Lobster Bisque           78,920         95,460       21.0
```

### **Sheets 3-12: Individual Categories**
One sheet per category (Catering, Soups, Salads, etc.) with just that category's items.

---

## 💡 Real-World Use Cases

### **Use Case 1: Executive Meeting**

**Question:** "How did we perform in November?"

**Answer (30 seconds):**
```python
# Generate hierarchical report
hierarchy = hierarchy_gen.generate_hierarchical_comparison(comparison)

# Show category summary
print(hierarchy['category_summary'])
```

**Output:**
```
Company-wide +6.2%
- Catering leading (+14.7%)
- Soups strong (+10.2%)
- Sandwiches declining (-2.2%)
```

---

### **Use Case 2: Menu Optimization**

**Question:** "What soup items should we promote?"

**Answer (1 minute):**
```python
# Print full hierarchy
hierarchy_gen.print_hierarchical_report(hierarchy)

# Drill into Soups category
```

**Output:**
```
Top performers:
- Bowl Lobster Bisque (+21.0%)
- Bowl Chicken Tortilla (+19.6%)

Declining:
- Bowl Maryland Crab Soup (-16.5%)
- Bowl Tomato Basil (-9.8%)

Action: Feature Lobster Bisque, consider removing Maryland Crab
```

---

### **Use Case 3: Store Performance**

**Question:** "Why is Mobile, AL crushing it?"

**Answer (2 minutes):**
```python
# Generate location hierarchy
location_hierarchy = hierarchy_gen.generate_location_hierarchy(
    comparison_by_location
)

# Print location breakdown
hierarchy_gen.print_location_hierarchy(location_hierarchy)
```

**Output:**
```
Mobile, AL top performer:
- Soups +29.2% (vs company avg +10.2%)
- Salads +25.9% (vs company avg +3.8%)

What they're doing right:
- Bowl Lobster Bisque +35.8% (training?)
- Strong upselling (bowl vs cup ratios)

Action: Send Mobile's GM to train other locations
```

---

## 🎨 Customization Options

### **Adjust Detail Level:**

```python
# Show more items per category
hierarchy_gen.print_hierarchical_report(
    hierarchy,
    show_top_items=10,      # Show top 10 instead of 5
    show_bottom_items=5     # Show bottom 5 instead of 3
)

# Show more locations
hierarchy_gen.print_location_hierarchy(
    location_hierarchy,
    show_top_locations=5,    # Top 5 instead of 3
    show_bottom_locations=5  # Bottom 5 instead of 2
)
```

### **Filter to Specific Categories:**

```python
# Just soups
soup_only = {
    'hierarchy': [
        h for h in hierarchy['hierarchy'] 
        if 'Soup' in h['category']
    ],
    'category_summary': hierarchy['category_summary'][
        hierarchy['category_summary']['Category'].str.contains('Soup')
    ],
    'period1_label': hierarchy['period1_label'],
    'period2_label': hierarchy['period2_label']
}

hierarchy_gen.print_hierarchical_report(soup_only)
```

### **Custom Time Periods:**

```python
# Compare any periods
hierarchy = hierarchy_gen.generate_hierarchical_comparison(
    comparison,
    period1_label="Q4 2024",    # Custom labels
    period2_label="Q4 2025"
)
```

---

## 🔧 Integration with Your FastAPI App

Add hierarchical endpoints to your web app:

```python
# In yoy_integration.py, add:

@yoy_router.post("/compare/hierarchical")
async def compare_hierarchical(request: YoYComparisonRequest = Body(...)):
    """Generate hierarchical comparison report."""
    
    # ... (load data using your existing code)
    
    # Generate hierarchy
    hierarchy_gen = HierarchicalReportGenerator(consolidator)
    hierarchy = hierarchy_gen.generate_hierarchical_comparison(
        comparison,
        period1_label=request.aloha_period,
        period2_label=request.toast_period
    )
    
    return hierarchy  # Returns JSON with nested structure

@yoy_router.post("/compare/hierarchical/export")
async def export_hierarchical(request: YoYComparisonRequest = Body(...)):
    """Export hierarchical report to Excel."""
    
    # ... (generate hierarchy)
    
    # Export to Excel
    output_file = f"hierarchical_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    hierarchy_gen.export_hierarchical_excel(hierarchy, output_file)
    
    return {"file": output_file, "download_url": f"/downloads/{output_file}"}
```

**Frontend integration:**
```javascript
// Fetch hierarchical report
const response = await fetch('/api/yoy/compare/hierarchical', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    toast_start_date: '2025-11-01',
    toast_end_date: '2025-11-30',
    aloha_start_date: '2024-11-01',
    aloha_end_date: '2024-11-30'
  })
});

const hierarchy = await response.json();

// Display in your UI:
// hierarchy.category_summary - for summary table
// hierarchy.hierarchy - for expandable tree view
```

---

## ✅ Complete Workflow

### **Weekly Reports:**

```python
# weekly_report.py
from datetime import date, timedelta
from sales_consolidator import SalesConsolidator
from hierarchical_reports import HierarchicalReportGenerator

def generate_weekly_report():
    """Run every Monday for previous week's performance."""
    
    # Calculate date ranges
    end_date = date.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=6)
    
    # Previous year same week
    prev_year_end = end_date - timedelta(days=365)
    prev_year_start = start_date - timedelta(days=365)
    
    # Initialize
    consolidator = SalesConsolidator("enhanced_mapping.xlsx")
    hierarchy_gen = HierarchicalReportGenerator(consolidator)
    
    # Load data
    aloha_df = consolidator.load_aloha_data(f"aloha_{prev_year_start}_{prev_year_end}.xls")
    toast_df = fetch_toast_data(start_date, end_date)  # Your API call
    
    # Consolidate & compare
    toast_consolidated = consolidator.consolidate_toast_data(toast_df)
    comparison = consolidator.compare_yoy(aloha_df, toast_consolidated)
    
    # Generate hierarchy
    hierarchy = hierarchy_gen.generate_hierarchical_comparison(comparison)
    
    # Export
    filename = f"weekly_report_{start_date}_{end_date}.xlsx"
    hierarchy_gen.export_hierarchical_excel(hierarchy, filename)
    
    # Email to team
    send_email_with_attachment("team@company.com", filename)
    
    print(f"✅ Weekly report generated: {filename}")

# Run weekly
if __name__ == "__main__":
    generate_weekly_report()
```

---

## 🎯 Success Checklist

After setup, you should be able to:

✅ **Generate company-wide hierarchical reports** (5 minutes)
✅ **Drill down from categories to items** (instant)
✅ **Add location breakdown** (1 parameter)
✅ **Export to Excel** (ready to share)
✅ **Answer executive questions** (< 1 minute)
✅ **Identify operational issues** (< 5 minutes)
✅ **Compare any time periods** (flexible)

---

## 📚 All Your Files

### **Analysis & Setup:**
- `IMPLEMENTATION_GUIDE.md` - **Start here!**
- `HIERARCHICAL_EXAMPLES.md` - See what reports look like
- `YOUR_CUSTOM_SETUP.md` - Your 97-location setup
- `pos_mapping_solution.md` - Strategy overview

### **Python Modules:**
- `mapping_enhancement.py` - Improve mapping table
- `sales_consolidator.py` - Core consolidation engine
- `hierarchical_reports.py` - **Hierarchical report generator**
- `yoy_integration.py` - FastAPI endpoints

### **Data:**
- `enhanced_mapping.xlsx` - Your mapping with 492 suggestions

---

## 🚀 Next Steps

1. **Review suggested mappings** → `enhanced_mapping.xlsx` Sheet 2
2. **Run first report** → Use code from Step 2 above
3. **Test with real data** → November 2024 vs 2025
4. **Share with team** → Excel export ready to present
5. **Integrate into app** → Add to your FastAPI

---

## ❓ Questions?

**"How do I get Toast data?"**
→ Use your existing Toast API integration in `main.py`

**"What if my Aloha export format is different?"**
→ The tool auto-detects headers, or you can specify column names

**"Can I compare more than 2 periods?"**
→ Yes! Run multiple comparisons and combine results

**"What about items not in the mapping?"**
→ They're tracked separately as "Toast-only items"

Ready to generate your first hierarchical report? 🎯
