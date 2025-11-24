# Sharing via GitHub (Recommended)

GitHub is the **easiest and best way** to share this with your colleagues. Here's how:

## Why GitHub?

✅ **Easy updates** - Push changes, colleagues pull updates  
✅ **Version control** - Track changes, roll back if needed  
✅ **Issue tracking** - Colleagues can report bugs  
✅ **Professional** - Standard way to share code  
✅ **Free private repos** - Keep it internal to your team  

## Step 1: Create a GitHub Repository

1. **Go to GitHub.com** and sign in (or create a free account)

2. **Create a new repository:**
   - Click the "+" icon → "New repository"
   - Name it: `toast-net-sales-app` (or whatever you prefer)
   - Set to **Private** (important for internal tools)
   - **Don't** initialize with README (you already have one)
   - Click "Create repository"

3. **GitHub will show you commands** - you'll use the "push an existing repository" option

## Step 2: Push Your Code to GitHub

Open PowerShell in your project folder and run:

```powershell
# Initialize git (if not already done)
git init

# Add all files (gitignore will exclude venv, .env, etc.)
git add .

# Create first commit
git commit -m "Initial commit: Toast Net Sales App"

# Add GitHub as remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/toast-net-sales-app.git

# Push to GitHub
git branch -M main
git push -u origin main
```

**Note:** If you haven't used Git before, you might need to:
- Install Git: https://git-scm.com/download/win
- Configure your name/email:
  ```powershell
  git config --global user.name "Your Name"
  git config --global user.email "your.email@example.com"
  ```

## Step 3: Share with Colleagues

1. **Invite collaborators:**
   - Go to your repo on GitHub
   - Click "Settings" → "Collaborators" → "Add people"
   - Enter their GitHub usernames or emails
   - They'll get an email invitation

2. **Or share the repo link:**
   - They can request access if the repo is private
   - Or you can make it public (not recommended for internal tools with API keys)

## Step 4: What Your Colleagues Do

They'll:

1. **Clone the repository:**
   ```powershell
   git clone https://github.com/YOUR_USERNAME/toast-net-sales-app.git
   cd toast-net-sales-app
   ```

2. **Run setup (one time):**
   - Double-click `setup.bat`
   - Or run: `.\setup.bat` in PowerShell

3. **Add credentials:**
   - Edit `backend\.env` with Toast API credentials (you'll share these separately)

4. **Start the app:**
   - Double-click `START_APP.bat`
   - Or run: `.\START_APP.bat`

That's it! The app opens automatically in their browser.

## Step 5: Sharing Updates

When you make changes:

```powershell
git add .
git commit -m "Description of changes"
git push
```

Colleagues get updates with:

```powershell
git pull
```

## Alternative: GitHub Releases (For Non-Technical Users)

If your colleagues aren't comfortable with Git:

1. **Create a release:**
   - Go to your repo → "Releases" → "Create a new release"
   - Tag: `v1.0.0`
   - Upload a zip file of the project (excluding `venv/` and `.env`)
   - Publish release

2. **Colleagues download:**
   - Go to Releases page
   - Download the zip
   - Extract and run `START_APP.bat`

## Security Note

**Never commit `.env` files to GitHub!**

The `.gitignore` file already excludes:
- `backend/.env` (your credentials)
- `backend/venv/` (virtual environment)
- `__pycache__/` (Python cache)

Always share credentials separately via secure channel (encrypted email, Teams, etc.).

