# How to Share This App with Colleagues

You have **two great options** - choose what works best for your team!

---

## 🎯 Option 1: GitHub (Recommended for Teams)

**Best for:** Teams, collaboration, easy updates

### Why GitHub?
- ✅ **Easy updates** - Push changes, colleagues pull updates with one command
- ✅ **Version control** - Track all changes, roll back if needed
- ✅ **Issue tracking** - Colleagues can report bugs and request features
- ✅ **Professional** - Standard way to share code in 2025
- ✅ **Free private repos** - Keep it internal to your team

### Quick Setup:
1. Create a private GitHub repository
2. Push your code (see `GITHUB_SETUP.md` for step-by-step)
3. Invite colleagues as collaborators
4. They clone and run `START_APP.bat`

**See `GITHUB_SETUP.md` for complete instructions.**

---

## 🚀 Option 2: One-Click App Launcher (Easiest for Non-Technical Users)

**Best for:** Non-technical users, quick distribution, "true app" experience

### How It Works:
1. **You share:** Zip file of the project (excluding `venv/` and `.env`)
2. **Colleagues:**
   - Extract the zip
   - Double-click `START_APP.bat`
   - App opens automatically!

### What `START_APP.bat` Does:
- ✅ Checks for Python (prompts to install if missing)
- ✅ Creates virtual environment (first time only)
- ✅ Installs dependencies (first time only)
- ✅ Checks for credentials (prompts if missing)
- ✅ Starts the server
- ✅ Opens browser automatically

### Sharing Steps:
1. **Create a zip** (exclude `venv/` and `.env`):
   ```powershell
   # Option A: Use Windows File Explorer
   # Right-click folder → Send to → Compressed folder
   # Then delete venv/ and .env from the zip
   
   # Option B: Use PowerShell
   Compress-Archive -Path backend\main.py,backend\requirements.txt,backend\.env.example,backend\README.md,frontend\*,START_APP.bat,setup.bat,README.md,GITHUB_SETUP.md -DestinationPath toast-net-sales-app.zip
   ```

2. **Share the zip** (email, Teams, OneDrive, etc.)

3. **Share credentials separately** (secure channel):
   - `TOAST_API_BASE_URL`
   - `TOAST_API_CLIENT_ID`
   - `TOAST_API_CLIENT_SECRET`

4. **Colleagues:**
   - Extract zip
   - Run `setup.bat` (first time only)
   - Edit `backend\.env` with credentials
   - Double-click `START_APP.bat` anytime after

---

## 📊 Comparison

| Feature | GitHub | One-Click Launcher |
|---------|--------|-------------------|
| **Ease of sharing** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **Ease of updates** | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Version control** | ✅ Yes | ❌ No |
| **Best for teams** | ✅ Yes | ⚠️ Small groups |
| **Best for non-technical** | ⚠️ Requires Git | ✅ Yes |
| **Professional** | ✅ Yes | ⚠️ Less formal |

---

## 🔐 Security Reminder

**NEVER include these in what you share:**
- ❌ `backend/.env` (contains your credentials!)
- ❌ `backend/venv/` (virtual environment - let each person create their own)

The `.gitignore` file automatically excludes these for GitHub.

**Always share credentials separately** via secure channel (encrypted email, Teams private message, password manager, or in-person).

---

## 🎬 Quick Start for You

### If Using GitHub:
1. Read `GITHUB_SETUP.md`
2. Create repo and push code
3. Invite colleagues
4. Done!

### If Using Direct Share:
1. Create zip (exclude `venv/` and `.env`)
2. Share zip + credentials separately
3. Colleagues run `START_APP.bat`
4. Done!

---

## 💡 Recommendation

**Use GitHub if:**
- Your colleagues are comfortable with basic Git commands
- You want easy updates and version control
- You're working as a team

**Use One-Click Launcher if:**
- Your colleagues aren't technical
- You want the simplest possible experience
- You're sharing with just a few people

**You can also do both!** Put it on GitHub, but create releases with zip files for non-technical users.

