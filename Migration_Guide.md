# Migration Guide - Streamlined Reporting Platform

## What Changed?

### ✅ Removed Features:
- **Custom category management** (report categories, item assignments)
- **4-step workflow pages** (menu-sync, report-categories, report-builder, home)
- **Menu sync interface** (manual menu syncing UI)
- All category-related backend endpoints

### ✅ New Features:
- **Single-page reporting interface** - Everything in one place
- **Saved reports system** - Save report configurations for easy reuse
- **Creator tracking** - Reports are tagged with creator name
- **Dynamic report builder** - Use Toast's native groupBy options
- **Net Sales report** → Now a pre-saved default report

## Migration Steps

### 1. **Backend Changes**

Replace your `backend/main.py` with the streamlined version. The new backend:
- Removes all category-related code
- Adds saved reports management (`/api/saved-reports`)
- Adds dynamic report execution (`/api/reports/run`)
- Keeps all Toast API integrations intact

### 2. **Frontend Changes**

Replace your `frontend/index.html` with the streamlined version. The new frontend:
- Single-page interface (no more multi-page workflow)
- Sidebar with saved reports list
- Report builder with Toast's native options
- Results display with CSV export

**Delete these old files** (they're no longer used):
- `frontend/home.html`
- `frontend/menu-sync.html`
- `frontend/report-categories.html`
- `frontend/report-builder.html`
- `frontend/configuration.html`
- `frontend/menu-performance.html`

### 3. **Data Migration**

Your existing data files:
- ✅ **Keep**: Nothing to migrate - fresh start!
- ❌ **Delete** (optional cleanup):
  - `backend/report_categories.json` (no longer used)
  - `backend/item_category_assignments.json` (no longer used)

### 4. **Restart the Server**

```bash
cd backend
uvicorn main:app --reload
```

Visit `http://localhost:8000/` - You'll see the new single-page interface!

## New Workflow

### Old Workflow (4 Steps):
1. Sync Menu → 2. Define Categories → 3. Map Items → 4. Run Reports

### New Workflow (Streamlined):
1. **Build Report** - Configure report type, date range, locations, groupBy
2. **Run** - See results immediately
3. **Save** - Save configuration for future use
4. **Export** - Download CSV

## Using the New Interface

### Creating Your First Report:

1. **Click "New Report"** in the sidebar
2. **Fill in details**:
   - Name: "Net Sales by Location - Weekly"
   - Report Type: Metrics Report
   - Date Range: Last 7 Days
   - Locations: Select all or specific ones
   - Created By: Your name
3. **Click "Run Report"** - See results
4. **Click "Save Report"** - Save for future use

### Using Saved Reports:

1. **Click a saved report** in the sidebar
2. **Modify if needed** (dates, locations, etc.)
3. **Click "Run Report"**
4. **Click "Update Report"** to save changes
5. **Click "Download CSV"** to export

### Available Report Types:

**Metrics Report** (Sales data):
- Group by: Revenue Center, Dining Option, Employee, Job
- Metrics: Net Sales, Guest Count, Item Count, etc.

**Menu Performance** (Item-level sales):
- Group by: Menu Item
- Shows: Item name, quantity sold, sales by item

## Toast API Integration

The streamlined version still uses:
- ✅ **Analytics API** - For sales metrics and menu performance
- ✅ **Standard API** - For menus (available if needed for future features)
- ✅ **OAuth2 authentication** - Automatic token management

## Support Tickets Feature (Future)

Ready to add! The streamlined architecture makes it easy to add:
- User profiles
- Authentication
- Support ticket submission

Just add these as new sections without disrupting the reporting flow.

## Benefits of the New System

1. **Simpler** - One page instead of four
2. **Faster** - No multi-step workflow
3. **Native** - Uses Toast's built-in options (no custom categories to maintain)
4. **Organized** - Saved reports make it easy to run common reports
5. **Scalable** - Easy to add auth, profiles, support tickets later

## Questions?

The new system is designed to be intuitive, but if you have questions:
- Check the inline documentation in the code
- Review Toast API docs for available groupBy options
- Test with the default "Net Sales" report to see how it works

---

**Need help?** The code is well-commented and follows best practices for easy customization!