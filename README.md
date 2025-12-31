# Newk's Custom Reporting Platform

A streamlined custom reporting tool for Newk's Restaurant Group built on Toast's read-only APIs.

## 🎯 Core Features

- **Custom Report Builder** - Build reports with Toast's native options (no custom categories needed)
- **Saved Reports** - Save report configurations for easy reuse
- **Creator Tracking** - Reports are tagged by creator for team collaboration
- **Multiple Report Types** - Metrics (sales) and Menu Performance (item-level)
- **Flexible Grouping** - Group by Revenue Center, Dining Option, Employee, Menu Item, etc.
- **CSV Export** - Download any report for analysis in Excel/Google Sheets
- **Date Range Options** - Last 7/30/90 days or custom range

## 🚀 Quick Start

**Just double-click `START_APP.bat`** - handles everything automatically!

The launcher will:
1. ✅ Check for Python
2. ✅ Create virtual environment (first time only)
3. ✅ Install dependencies (first time only)
4. ✅ Check credentials
5. ✅ Start the server and open your browser

## 📋 First-Time Setup

### Option 1: Automated (Recommended)

1. **Run setup:** Double-click `setup.bat`
2. **Add credentials:** Edit `backend\.env` with your Toast API credentials
3. **Launch:** Double-click `START_APP.bat`

### Option 2: Manual

```bash
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Edit `backend\.env`:
```env
TOAST_API_BASE_URL=https://ws-api.toasttab.com/era/v1
TOAST_API_CLIENT_ID=your_analytics_client_id
TOAST_API_CLIENT_SECRET=your_analytics_client_secret

TOAST_STANDARD_API_CLIENT_ID=your_standard_api_client_id
TOAST_STANDARD_API_CLIENT_SECRET=your_standard_api_client_secret
```

Start server:
```bash
uvicorn main:app --reload
```

Visit: `http://localhost:8000/`

## 📊 How to Use

### Creating a Report:

1. **Click "New Report"** in the sidebar
2. **Configure report**:
   - Name your report
   - Choose report type (Metrics or Menu Performance)
   - Select date range
   - Choose locations
   - Add groupBy options (optional)
   - Enter your name
3. **Run Report** - See results immediately
4. **Save Report** - Save configuration for future use
5. **Download CSV** - Export data

### Using Saved Reports:

1. Click any saved report in the sidebar
2. Modify if needed (dates, locations, etc.)
3. Run report
4. Update or delete as needed

## 📈 Report Types

### Metrics Report (Sales Data)
Group by options:
- **Revenue Center** - Sales by revenue center
- **Dining Option** - Dine In, Takeout, Delivery, etc.
- **Employee** - Sales by employee
- **Job** - Sales by job role

Data includes: Net Sales, Guest Count, Item Count, etc.

### Menu Performance (Item-Level Sales)
Group by options:
- **Menu Item** - Sales by individual menu item

Data includes: Item name, quantity sold, sales amount

## 📁 Architecture

### Backend (`backend/main.py`)
- FastAPI server
- Toast Analytics API integration (OAuth2)
- Toast Standard API integration (for menus)
- Saved reports storage (JSON file)
- Dynamic report execution
- CSV export support

### Frontend (`frontend/index.html`)
- Single-page interface
- Saved reports sidebar
- Dynamic report builder
- Results table with export
- Clean, modern dark theme

### Data Storage
- `saved_reports.json` - Saved report configurations
- Each report includes: name, description, creator, dates, config

## 🔧 API Endpoints

### Core
- `GET /health` - Health check
- `GET /locations` - List all active locations

### Saved Reports
- `GET /api/saved-reports` - List all saved reports
- `POST /api/saved-reports` - Create new report
- `GET /api/saved-reports/{id}` - Get specific report
- `PUT /api/saved-reports/{id}` - Update report
- `DELETE /api/saved-reports/{id}` - Delete report

### Report Execution
- `POST /api/reports/run` - Run report with config
- `GET /api/report-types` - Get available report types & options

## 🎨 Default Reports

The system comes with a default "Net Sales by Location - Weekly" report to get you started.

## 🔐 Security

- Never commit `.env` files (contains API credentials)
- `.gitignore` automatically excludes sensitive files
- Share credentials separately via secure channel

## 🔄 Future Features

Ready to add:
- **User Authentication** - Login/profiles
- **Support Tickets** - Submit tickets through the app
- **Report Scheduling** - Auto-run reports on schedule
- **Email Reports** - Email results automatically
- **Dashboard View** - Visual charts and KPIs

## 🛠 Troubleshooting

**"Python is not installed"**
→ Install Python 3.10+ from python.org

**"Port 8000 already in use"**
→ Close other apps using port 8000 or change port in `START_APP.bat`

**"Module not found"**
→ Run `setup.bat` to install dependencies

**"Authentication failed"**
→ Check your Toast API credentials in `.env`

**"No restaurants available"**
→ Verify your Toast API access includes restaurant data

## 📚 Documentation

- **Toast API Docs**: https://doc.toasttab.com/
- **FastAPI Docs**: http://localhost:8000/docs (when server is running)

## 🤝 Sharing with Team

### Via GitHub (Recommended):
1. Create GitHub repository
2. Push code (excluding `.env`)
3. Team clones and adds their own `.env`
4. Everyone runs `START_APP.bat`

### Via Zip:
1. Zip project (excluding `venv/` and `.env`)
2. Share zip file
3. Share credentials separately
4. Recipients run `setup.bat` then `START_APP.bat`

## 📝 Saved Report Example

```json
{
  "id": "uuid-here",
  "name": "Net Sales by Revenue Center",
  "description": "Daily sales broken down by revenue center",
  "createdBy": "Jane Doe",
  "createdAt": "2024-12-17T10:00:00Z",
  "lastModified": "2024-12-17T10:00:00Z",
  "config": {
    "reportType": "metrics",
    "dateRangeType": "last_30_days",
    "restaurantIds": [],
    "groupBy": ["REVENUE_CENTER"]
  }
}
```

## 🎯 Best Practices

1. **Name reports descriptively** - "Net Sales by Location - Weekly" not "Report 1"
2. **Add descriptions** - Help others understand what the report shows
3. **Use date ranges wisely** - Shorter ranges = faster results
4. **Save common reports** - Don't rebuild the same report repeatedly
5. **Export for analysis** - Use CSV exports for deeper analysis in Excel

## 📞 Support

For Toast API issues, contact Toast support. For this app, check:
1. Backend logs (in terminal where server is running)
2. Browser console (F12 → Console tab)
3. Network tab (F12 → Network tab) for API errors

---

**Built for Newk's Restaurant Group** | Powered by Toast APIs