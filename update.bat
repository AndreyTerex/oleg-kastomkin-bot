@echo off
rem Stops the old version of the bot and builds the new one from the files in this folder.
cd /d "%~dp0"

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
