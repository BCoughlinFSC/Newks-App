# GitHub Quick Start Guide

Follow these steps to get your app on GitHub!

## Step 1: Install Git (If Not Already Installed)

1. **Download Git for Windows:**
   - Go to: https://git-scm.com/download/win
   - Download the installer (it will auto-detect 64-bit or 32-bit)

2. **Run the installer:**
   - Click "Next" through most screens (defaults are fine)
   - **Important:** On "Adjusting your PATH environment" screen, choose:
     - ✅ **"Git from the command line and also from 3rd-party software"**
   - Click "Next" through the rest
   - Click "Install"

3. **Verify installation:**
   - Close and reopen PowerShell
   - Run: `git --version`
   - You should see something like: `git version 2.43.0`

## Step 2: Configure Git (First Time Only)

Open PowerShell and run:

```powershell
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

(Replace with your actual name and email - this is just for Git commits)

## Step 3: Create GitHub Repository

1. **Go to GitHub.com** and sign in

2. **Create a new repository:**
   - Click the **"+"** icon in the top right
   - Select **"New repository"**

3. **Fill in the details:**
   - **Repository name:** `toast-net-sales-app` (or whatever you prefer)
   - **Description:** (optional) "Internal tool for Toast Net Sales reporting"
   - **Visibility:** Select **🔒 Private** (important for internal tools!)
   - **⚠️ DO NOT check** "Add a README file" (you already have one)
   - **⚠️ DO NOT check** "Add .gitignore" (you already have one)
   - **⚠️ DO NOT check** "Choose a license"
   - Click **"Create repository"**

4. **GitHub will show you a page with commands** - you'll use the "push an existing repository" section

## Step 4: Push Your Code to GitHub

Open PowerShell in your project folder (`C:\Users\kaylesworth\Downloads\toast_net_sales_app`) and run these commands **one at a time**:

```powershell
# Initialize git repository
git init

# Add all files (gitignore will automatically exclude venv, .env, etc.)
git add .

# Create your first commit
git commit -m "Initial commit: Toast Net Sales App"

# Add GitHub as remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/toast-net-sales-app.git

# Rename branch to main (GitHub's standard)
git branch -M main

# Push to GitHub
git push -u origin main
```

**Note:** When you run `git push`, GitHub will ask for your credentials:
- **Username:** Your GitHub username
- **Password:** You'll need a **Personal Access Token** (not your GitHub password)

### Getting a Personal Access Token:

1. Go to GitHub.com → Click your profile picture → **Settings**
2. Scroll down → **Developer settings** (left sidebar)
3. Click **Personal access tokens** → **Tokens (classic)**
4. Click **Generate new token** → **Generate new token (classic)**
5. **Note:** Give it a name like "Toast App"
6. **Expiration:** Choose how long (90 days, 1 year, etc.)
7. **Select scopes:** Check ✅ **`repo`** (full control of private repositories)
8. Click **Generate token**
9. **⚠️ COPY THE TOKEN IMMEDIATELY** - you won't see it again!
10. Use this token as your password when `git push` asks for credentials

## Step 5: Verify It Worked

1. Go to your GitHub repository page
2. You should see all your files there!
3. Check that `backend/.env` is **NOT** visible (it should be excluded by .gitignore)

## Step 6: Share with Colleagues

### Option A: Invite Collaborators

1. Go to your repository on GitHub
2. Click **Settings** (top menu)
3. Click **Collaborators** (left sidebar)
4. Click **Add people**
5. Enter their GitHub username or email
6. They'll receive an email invitation
7. Once they accept, they can clone and use the repo

### Option B: Share the Repository Link

- Send them: `https://github.com/YOUR_USERNAME/toast-net-sales-app`
- They can request access if it's private, or you can add them as collaborators

## Step 7: What Your Colleagues Do

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
   - The app opens automatically!

## Updating the Code

When you make changes:

```powershell
git add .
git commit -m "Description of what changed"
git push
```

Colleagues get updates with:

```powershell
git pull
```

## Troubleshooting

**"git: command not found"**
- Git isn't installed or not in PATH
- Install Git (Step 1) and restart PowerShell

**"Authentication failed"**
- Use a Personal Access Token, not your GitHub password
- Make sure the token has `repo` scope

**"Repository not found"**
- Check the repository name matches
- Make sure you're using the correct GitHub username
- Verify the repository exists and you have access

**"Permission denied"**
- Make sure you're using HTTPS (not SSH) for the remote URL
- Check your Personal Access Token has `repo` permissions

---

**Need help?** Check `GITHUB_SETUP.md` for more detailed information.

