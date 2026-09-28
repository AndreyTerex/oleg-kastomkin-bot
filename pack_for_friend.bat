@echo off
rem Builds oleg-bot-for-friend.zip with the current code, keys and data.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack_for_friend.ps1"
pause
