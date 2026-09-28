@echo off
rem Starts or updates the bot. Normally you only need it once: after that Watchtower
rem updates the bot by itself every time a new version appears on GitHub.
rem Your .env and the data folder are never touched.
cd /d "%~dp0"

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo .env was not found - created it from .env.example.
    echo Open .env in Notepad, fill in DISCORD_TOKEN (and the other keys you have), save, and run this file again.
    pause
    exit /b 1
)

rem If this folder is a git clone, also refresh docker-compose.yml and the other files.
if exist ".git" (
    where git >nul 2>&1
    if not errorlevel 1 (
        echo Downloading the latest files from GitHub...
        git pull --ff-only
        if errorlevel 1 echo Could not update the files - continuing with the local ones.
    )
)

docker version >nul 2>&1
if errorlevel 1 (
    echo Docker is not running. Start Docker Desktop, wait until it says "running", then run this file again.
    pause
    exit /b 1
)

echo Downloading the ready-made bot image from GitHub...
docker compose pull
if errorlevel 1 (
    echo Could not download the image - building it from the files in this folder instead.
    docker compose up -d --build --remove-orphans
) else (
    docker compose up -d --remove-orphans
)
if errorlevel 1 (
    echo Something went wrong - see the messages above.
    pause
    exit /b 1
)

echo.
echo Done. From now on the bot updates itself (Watchtower checks GitHub every 5 minutes).
echo Bot logs (close the window to exit, the bot keeps running):
echo.
docker compose logs -f --tail 20 bot
