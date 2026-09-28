@echo off
rem Pulls the latest code from GitHub (if this folder is a git clone), then rebuilds and restarts the bot.
rem Your .env and the data folder are never touched: they are not in git.
cd /d "%~dp0"

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo .env was not found - created it from .env.example.
    echo Open .env in Notepad, fill in DISCORD_TOKEN (and the other keys you have), save, and run this file again.
    pause
    exit /b 1
)

if exist ".git" (
    where git >nul 2>&1
    if errorlevel 1 (
        echo Git is not installed - skipping the code update, rebuilding the local files.
    ) else (
        echo Downloading the latest code from GitHub...
        git pull --ff-only
        if errorlevel 1 (
            echo Could not update the code - see the messages above. Local changes? Run: git status
            pause
            exit /b 1
        )
    )
)

docker version >nul 2>&1
if errorlevel 1 (
    echo Docker is not running. Start Docker Desktop, wait until it says "running", then run this file again.
    pause
    exit /b 1
)

echo Stopping the old version...
docker compose down

echo Building and starting the new version (first build takes a couple of minutes)...
docker compose up -d --build
if errorlevel 1 (
    echo Something went wrong - see the messages above.
    pause
    exit /b 1
)

echo.
echo Done. Bot logs (close the window to exit, the bot keeps running):
echo.
docker compose logs -f --tail 20
