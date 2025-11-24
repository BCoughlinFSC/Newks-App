# Helper script to set up Git and push to GitHub
# Run this AFTER you've created the GitHub repository

param(
    [Parameter(Mandatory=$true)]
    [string]$GitHubUsername,
    
    [Parameter(Mandatory=$true)]
    [string]$RepositoryName
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "GitHub Setup Helper" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Git is installed
try {
    $gitVersion = git --version 2>&1
    Write-Host "✓ Git found: $gitVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ Git is not installed!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Git first:" -ForegroundColor Yellow
    Write-Host "  1. Go to: https://git-scm.com/download/win" -ForegroundColor White
    Write-Host "  2. Download and install Git" -ForegroundColor White
    Write-Host "  3. Restart PowerShell" -ForegroundColor White
    Write-Host "  4. Run this script again" -ForegroundColor White
    exit 1
}

# Check if git is configured
$userName = git config --global user.name 2>&1
$userEmail = git config --global user.email 2>&1

if (-not $userName -or $userName -match "error") {
    Write-Host "⚠ Git user name not configured" -ForegroundColor Yellow
    $name = Read-Host "Enter your name (for Git commits)"
    git config --global user.name $name
    Write-Host "✓ Git user name set" -ForegroundColor Green
}

if (-not $userEmail -or $userEmail -match "error") {
    Write-Host "⚠ Git user email not configured" -ForegroundColor Yellow
    $email = Read-Host "Enter your email (for Git commits)"
    git config --global user.email $email
    Write-Host "✓ Git user email set" -ForegroundColor Green
}

Write-Host ""
Write-Host "Initializing Git repository..." -ForegroundColor Yellow

# Check if already a git repo
if (Test-Path .git) {
    Write-Host "✓ Git repository already initialized" -ForegroundColor Green
} else {
    git init
    Write-Host "✓ Git repository initialized" -ForegroundColor Green
}

Write-Host ""
Write-Host "Adding files..." -ForegroundColor Yellow
git add .
Write-Host "✓ Files added" -ForegroundColor Green

Write-Host ""
Write-Host "Creating initial commit..." -ForegroundColor Yellow
git commit -m "Initial commit: Toast Net Sales App"
Write-Host "✓ Initial commit created" -ForegroundColor Green

Write-Host ""
Write-Host "Setting up remote repository..." -ForegroundColor Yellow
$remoteUrl = "https://github.com/$GitHubUsername/$RepositoryName.git"

# Check if remote already exists
$existingRemote = git remote get-url origin 2>&1
if ($existingRemote -and -not ($existingRemote -match "error")) {
    Write-Host "⚠ Remote 'origin' already exists: $existingRemote" -ForegroundColor Yellow
    $overwrite = Read-Host "Overwrite? (y/n)"
    if ($overwrite -eq "y" -or $overwrite -eq "Y") {
        git remote set-url origin $remoteUrl
        Write-Host "✓ Remote updated" -ForegroundColor Green
    }
} else {
    git remote add origin $remoteUrl
    Write-Host "✓ Remote added: $remoteUrl" -ForegroundColor Green
}

Write-Host ""
Write-Host "Renaming branch to 'main'..." -ForegroundColor Yellow
git branch -M main
Write-Host "✓ Branch renamed to 'main'" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Ready to push!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next step: Push to GitHub" -ForegroundColor Yellow
Write-Host "  Run: git push -u origin main" -ForegroundColor White
Write-Host ""
Write-Host "Note: GitHub will ask for credentials:" -ForegroundColor Yellow
Write-Host "  - Username: Your GitHub username" -ForegroundColor White
Write-Host "  - Password: Use a Personal Access Token (not your password)" -ForegroundColor White
Write-Host ""
Write-Host "To create a token:" -ForegroundColor Yellow
Write-Host "  1. GitHub.com → Settings → Developer settings" -ForegroundColor White
Write-Host "  2. Personal access tokens → Tokens (classic)" -ForegroundColor White
Write-Host "  3. Generate new token (classic)" -ForegroundColor White
Write-Host "  4. Check 'repo' scope" -ForegroundColor White
Write-Host "  5. Copy the token and use it as your password" -ForegroundColor White
Write-Host ""

