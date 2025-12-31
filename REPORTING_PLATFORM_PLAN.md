# Newk's Customer Reporting Platform - Implementation Plan

## Vision
Transform the current Net Sales app into a full-featured reporting platform with:
1. **Saved Reports** - Save and re-run common reports
2. **Report Builder** - Visual menu tree for building custom reports
3. **Multiple Report Types** - Expand beyond just net sales

## Current State ✅
- Working Toast Analytics API integration
- Net Sales report (by location, by date)
- FastAPI backend with OAuth2
- Single-page frontend
- CSV export

## Proposed Architecture

### Phase 1: Multi-Page Structure (No Breaking Changes)
**Goal:** Add navigation without breaking existing functionality

**Changes:**
1. **Add routing to frontend**
   - Convert to multi-page app (or use hash routing)
   - Home page: List of saved reports + "Create New Report"
   - Net Sales page: Current functionality (moved to `/reports/net-sales`)
   - Report Builder page: New menu tree interface

2. **Backend endpoints:**
   - Keep all existing endpoints working
   - Add `/reports/saved` - CRUD for saved reports
   - Add `/menus` - Fetch menu hierarchy from Toast
   - Add `/menu-items` - Get menu items with properties

### Phase 2: Toast Menus API Integration
**Goal:** Fetch and display menu hierarchy

**Questions to Answer:**
1. **Which Toast API for menus?**
   - Toast has multiple APIs: Analytics API (for reporting), Menus API v3 (for menu structure)
   - Do your current credentials work for Menus API, or do you need separate credentials?
   - Base URL might be different (e.g., `https://ws-api.toasttab.com/menus/v3`)

2. **Menu Structure:**
   - Categories (e.g., "The Italian")
   - Items (e.g., "Italian Sandwich (Entree)")
   - Modifier Groups (e.g., "Sandwich Meats")
   - Properties: prep station, sales category, etc.

3. **API Endpoints Needed:**
   - Get all menus
   - Get menu categories
   - Get items in category
   - Get modifier groups
   - Get item properties

### Phase 3: Report Builder UI
**Goal:** Create the tree/hierarchy view

**Features:**
- Expandable/collapsible tree (like Toast's interface)
- Checkboxes to select items for reporting
- Show properties columns (prep station, sales category, etc.)
- Save selections as a report configuration

### Phase 4: Saved Reports
**Goal:** Save and re-run reports

**Features:**
- Save report configurations (date range, locations, menu items)
- List saved reports
- Run saved report with one click
- Edit/delete saved reports

## Technical Questions

### 1. Toast Menus API Access
- **Do you have Toast Menus API v3 credentials?**
  - Same as Analytics API, or different?
  - What's the base URL? (might be `https://ws-api.toasttab.com/menus/v3`)
  
- **Can you test an API call?**
  - Try: `GET https://ws-api.toasttab.com/menus/v3/menus` (or similar)
  - Share the response structure if possible

### 2. Menu Data Structure
- **What properties do you want to see?**
  - Prep station
  - Sales category
  - Item tags
  - Others?

- **Do you want to filter by properties?**
  - "Show only items with prep station X"
  - "Show only items in sales category Y"

### 3. Report Types
- **What reports do you want to build?**
  - Sales by menu item
  - Sales by category
  - Sales by modifier group
  - Custom combinations?

### 4. Saved Reports Storage
- **Where to store saved reports?**
  - Option A: JSON file (simple, no database needed)
  - Option B: SQLite database (more robust)
  - Option C: In-memory (lost on restart, but simplest)

## Implementation Approach

### Step 1: Research Toast Menus API
**Action Items:**
1. Check Toast API documentation for Menus API v3
2. Test API access with your credentials
3. Understand the response structure

**Deliverable:** Working API call that fetches menu data

### Step 2: Create Menu Tree Backend
**Action Items:**
1. Add `/menus` endpoint to fetch menu hierarchy
2. Transform Toast API response into tree structure
3. Add caching (menus don't change often)

**Deliverable:** Backend endpoint returning menu tree

### Step 3: Add Navigation (No Breaking Changes)
**Action Items:**
1. Create multi-page structure
2. Move current net sales to `/reports/net-sales`
3. Add navigation menu
4. Test that existing functionality still works

**Deliverable:** Multi-page app with working navigation

### Step 4: Build Report Builder Page
**Action Items:**
1. Create menu tree component (expandable/collapsible)
2. Add property columns
3. Add selection checkboxes
4. Style to match Toast's interface

**Deliverable:** Working Report Builder page

### Step 5: Add Saved Reports
**Action Items:**
1. Create saved reports storage
2. Add save/load/delete endpoints
3. Add saved reports list page
4. Add "Run Saved Report" functionality

**Deliverable:** Full saved reports feature

## Risk Assessment

### ✅ Low Risk
- Adding new pages (doesn't break existing)
- Adding new endpoints (doesn't break existing)
- Menu tree UI (separate feature)

### ⚠️ Medium Risk
- Toast Menus API might need different credentials
- Menu API structure might be complex
- Large menu trees might be slow to load

### 🔴 High Risk
- Changing existing net sales functionality (we won't do this)
- Breaking current API endpoints (we'll keep them)

## Next Steps

**Immediate Actions:**
1. **You:** Test Toast Menus API access
   - Try calling the API with your current credentials
   - Share the base URL and any documentation you have
   - Share a sample response if possible

2. **Me:** Create a simple menu tree endpoint
   - Start with mock data if API isn't ready
   - Build the tree structure
   - Test the UI component

3. **Together:** Iterate on the design
   - Show you the menu tree
   - Get feedback
   - Refine the interface

## Questions for You

1. **Toast Menus API:**
   - Do you have access? What's the base URL?
   - Do your current credentials work, or do you need new ones?
   - Can you share API documentation or a sample response?

2. **Menu Properties:**
   - Which properties are most important? (prep station, sales category, etc.)
   - Do you want to filter by properties?
   - Do you want to see properties in the tree view?

3. **Saved Reports:**
   - How many saved reports do you expect? (affects storage choice)
   - Do you want to share reports between users?
   - Do you need report scheduling/emailing?

4. **Report Types:**
   - What's the first custom report you want to build?
   - Sales by menu item? Sales by category? Something else?

---

**Ready to start?** Let's begin with Step 1: Testing Toast Menus API access!

