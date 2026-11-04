# Packs the bot for handing over to a friend: all code, docker files, instructions and YOUR .env with the keys.
# Run pack_for_friend.bat (double-click). Result: oleg-bot-for-friend.zip next to this script.
#
# With DATABASE_URL in .env (shared database) the data folder is NOT packed: the friend's bot takes everything
# from the database. Without DATABASE_URL the current data files are packed (stop your bot first).
# Machine-specific files (.shared-mirror.json, .shared-instance.json, vpn.json, backups) are never packed:
# with them the friend's bot could believe it was the last database owner and start without the database.
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$stage = Join-Path $env:TEMP 'oleg-bot-pack'
$target = Join-Path $stage 'oleg-bot'
$zip = Join-Path $root 'oleg-bot-for-friend.zip'

$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Write-Host '.env not found next to this script - nothing to give the friend. Put your .env here and run again.'
    exit 1
}

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $target | Out-Null

# All bot modules: every .py in the root (except tests) and in cogs/.
Get-ChildItem $root -Filter '*.py' -File | Copy-Item -Destination $target
$cogs = New-Item -ItemType Directory -Path (Join-Path $target 'cogs')
Get-ChildItem (Join-Path $root 'cogs') -Filter '*.py' -File | Copy-Item -Destination $cogs

$files = @(
    'requirements.txt', 'Dockerfile', 'docker-compose.yml', '.dockerignore', '.gitignore', '.gitattributes',
    '.env', '.env.example', 'README.md', 'HANDOVER.md', 'update.bat'
)
foreach ($file in $files) {
    $source = Join-Path $root $file
    if (Test-Path $source) { Copy-Item $source $target }
    else { Write-Host "Missing (skipped): $file" }
}

$data = New-Item -ItemType Directory -Path (Join-Path $target 'data')
$shared = Select-String -Path $envFile -Pattern '^\s*DATABASE_URL\s*=\s*\S' -Quiet
if ($shared) {
    Write-Host 'DATABASE_URL is set: data is not packed, the friend''s bot reads everything from the shared database.'
} else {
    # Same list as datasync.SYNC_FILES: server data only, no caches and no machine-specific files.
    $syncFiles = @(
        'stats.json', 'memory.json', 'oleg.json', 'lobbies.json', 'champion_history.json', 'recap.json', 'invites.json',
        'wallet.json', 'quiz.json', 'patchnotes.json', 'lobby_preferences.json', 'captains.json'
    )
    foreach ($name in $syncFiles) {
        $source = Join-Path (Join-Path $root 'data') $name
        if (Test-Path $source) { Copy-Item $source $data }
    }
    Write-Host 'No DATABASE_URL: current data files packed. Stop your bot before the friend starts theirs.'
}

if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $target -DestinationPath $zip
Remove-Item $stage -Recurse -Force

Write-Host ''
Write-Host "Done: $zip"
Write-Host 'The archive contains your .env with the bot token and keys - send it only privately (not in a public channel).'
Write-Host 'Instructions for the friend: HANDOVER.md inside the archive.'
