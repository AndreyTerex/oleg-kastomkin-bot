# Packs the bot for handing over to a friend: code, .env with all keys and fresh data (lobbies, Oleg's memory).
# Run pack_for_friend.bat (double-click) right before the handover so the data is up to date.
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$stage = Join-Path $env:TEMP 'oleg-bot-pack'
$target = Join-Path $stage 'oleg-bot'
$zip = Join-Path $root 'oleg-bot-for-friend.zip'

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $target | Out-Null

$files = @(
    'bot.py', 'config.py', 'utils.py', 'storage.py', 'champions.py', 'modes.py', 'llm.py', 'memory.py',
    'persona.py', 'role_requests.py', 'stats.py', 'timeparse.py', 'portraits.py',
    'requirements.txt', 'Dockerfile', 'docker-compose.yml',
    '.dockerignore', '.gitignore', '.env', '.env.example', 'README.md', 'HANDOVER.md', 'update.bat'
)
foreach ($file in $files) {
    $source = Join-Path $root $file
    if (Test-Path $source) { Copy-Item $source $target }
    else { Write-Host "Missing (skipped): $file" }
}

$cogs = New-Item -ItemType Directory -Path (Join-Path $target 'cogs')
Get-ChildItem (Join-Path $root 'cogs') -Filter '*.py' | Copy-Item -Destination $cogs

$data = New-Item -ItemType Directory -Path (Join-Path $target 'data')
Get-ChildItem (Join-Path $root 'data') -Filter '*.json' -ErrorAction SilentlyContinue | Copy-Item -Destination $data

if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $target -DestinationPath $zip
Remove-Item $stage -Recurse -Force

Write-Host ''
Write-Host "Done: $zip"
Write-Host 'Send this file to your friend. Stop your own bot first: docker compose down'
