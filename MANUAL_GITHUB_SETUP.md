# Manual GitHub Setup (Step-by-Step)

Since you're having trouble with the script, here's a manual approach that will definitely work!

**Your details:**
- GitHub Username: `aylesworthkim`
- Repository Name: `newks-net-sales`
- Repository URL: `https://github.com/aylesworthkim/newks-net-sales.git`

---

## Step 1: Install Git (If Not Already Installed)

1. **Download Git:**
   - Go to: https://git-scm.com/download/win
   - Download the installer

2. **Install Git:**
   - Run the installer
   - Click "Next" through most screens
   - **Important:** On "Adjusting your PATH environment" screen, choose:
     - ✅ **"Git from the command line and also from 3rd-party software"**
   - Finish the installation

3. **Restart PowerShell:**
   - Close this PowerShell window
   - Open a NEW PowerShell window
   - Navigate back to your project:
     ```powershell
     cd C:\Users\kaylesworth\Downloads\toast_net_sales_app
     ```

4. **Verify Git is installed:**
   ```powershell
   git --version
   ```
   You should see something like: `git version 2.43.0`

---

## Step 2: Configure Git (First Time Only)

Run these commands (replace with your actual name and email):

```powershell
git config --global user.name "Kim Aylesworth"
git config --global user.email "your.email@example.com"
```

---

## Step 3: Initialize Git Repository

Run these commands **one at a time**:

```powershell
# Initialize git
git init

# Add all files (gitignore will exclude venv, .env automatically)
git add .

# Create first commit
git commit -m "Initial commit: Newk's Net Sales App"
```

---

## Step 4: Connect to GitHub

```powershell
# Add GitHub as remote
git remote add origin https://github.com/aylesworthkim/newks-net-sales.git

# Rename branch to main
git branch -M main
```

---

## Step 5: Push to GitHub

```powershell
git push -u origin main
```

**When prompted for credentials:**
- **Username:** `aylesworthkim`
- **Password:** Use a **Personal Access Token** (NOT your GitHub password)

### How to Create a Personal Access Token:

1. Go to: https://github.com/settings/tokens
2. Click **"Generate new token"** → **"Generate new token (classic)"**
3. **Note:** Give it a name like "Newk's App"
4. **Expiration:** Choose how long (90 days, 1 year, etc.)
5. **Select scopes:** Check ✅ **`repo`** (full control of private repositories)
6. Click **"Generate token"**
7. **⚠️ COPY THE TOKEN IMMEDIATELY** - it looks like: `ghp_xxxxxxxxxxxxxxxxxxxx`
8. Use this token as your password when `git push` asks for credentials

---

## Step 6: Verify It Worked

1. Go to: https://github.com/aylesworthkim/newks-net-sales
2. You should see all your files!
3. Check that `backend/.env` is **NOT** visible (it should be excluded)

---

## Troubleshooting

**"git: command not found"**
- Git isn't installed or PowerShell wasn't restarted
- Install Git and restart PowerShell

**"Authentication failed"**
- Make sure you're using a Personal Access Token, not your password
- Make sure the token has `repo` scope

**"Repository not found"**
- Make sure the repository exists at: https://github.com/aylesworthkim/newks-net-sales
- Make sure you're logged into GitHub

**"Permission denied"**
- Check your Personal Access Token has `repo` permissions
- Make sure the repository name matches exactly: `newks-net-sales`

---

## Quick Copy-Paste Commands

Once Git is installed and you've restarted PowerShell, run these in order:

```powershell
cd C:\Users\kaylesworth\Downloads\toast_net_sales_app
git init
git add .
git commit -m "Initial commit: Newk's Net Sales App"
git remote add origin https://github.com/aylesworthkim/newks-net-sales.git
git branch -M main
git push -u origin main
```

Then use your Personal Access Token when prompted for password.

