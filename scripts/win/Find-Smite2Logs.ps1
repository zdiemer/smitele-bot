<#
.SYNOPSIS
    Locate Smite 2's Unreal Engine log/config/replay files and (optionally) scan
    the newest logs for anything that looks like god-select / match-roster data.

.DESCRIPTION
    Smite 2 is Unreal Engine 5. UE writes per-session logs to
    <Project>\Saved\Logs\ -- for a shipping build usually
    %LOCALAPPDATA%\Smite2\Saved\Logs\, but it lands in the install dir instead
    when the Steam library is user-writable. This finds those dirs wherever they
    are, lists the logs newest-first, points at the Config and Demos (replay)
    dirs, and with -Scan greps the newest few logs for gameplay keywords so we
    can see whether the client records picks at all.

    Read-only. It opens no ports and touches no game process -- it only reads
    files the game already wrote, which is the safe, anti-cheat-neutral path (no
    process memory, no injection). Run it any time after a match, before the next
    launch rotates the current log.

.PARAMETER GamePath
    Smite 2 install dir, searched in addition to the user profile. Default is the
    standard Steam location; pass yours if the library is on another drive.

.PARAMETER Scan
    After listing, grep the newest -Newest logs for gameplay keywords and print
    the matching lines.

.PARAMETER Keywords
    Extra strings to grep for -- e.g. a god you just played and your own handle,
    which are the surest way to tell whether the roster is in the log at all.

.PARAMETER Newest
    How many of the newest logs to scan (default 3).

.EXAMPLE
    .\Find-Smite2Logs.ps1

.EXAMPLE
    # Right after a match, look for the gods you saw and your name:
    .\Find-Smite2Logs.ps1 -Scan -Keywords "Loki","Anubis","YourHandle"
#>
[CmdletBinding()]
param(
    [string]$GamePath = "C:\Program Files (x86)\Steam\steamapps\common\SMITE 2",
    [switch]$Scan,
    [string[]]$Keywords = @(),
    [int]$Newest = 3
)

$ErrorActionPreference = 'SilentlyContinue'
function Head($m) { Write-Host "`n$m" -ForegroundColor Cyan }

# --- 1. Find the UE "Saved" dirs, wherever the project lives ----------------
$saved = New-Object System.Collections.Generic.List[string]

# a) From the install: the game exe reveals the project folder.
if (Test-Path $GamePath) {
    $exe = Get-ChildItem -Path $GamePath -Recurse -Filter *.exe -File |
        Where-Object { $_.FullName -match '\\Binaries\\Win64\\' } |
        Sort-Object { $_.Name -notmatch 'smite' } |   # prefer Smite*.exe over EAC/crashpad
        Select-Object -First 1
    if ($exe) {
        # ...\<Project>\Binaries\Win64\Smite2.exe  ->  <Project> is three up.
        $projRoot = Split-Path (Split-Path (Split-Path $exe.FullName))
        $projName = Split-Path $projRoot -Leaf
        Write-Host "Install project: $projRoot  (project '$projName')" -ForegroundColor DarkGray
        $saved.Add((Join-Path $projRoot 'Saved'))
        $saved.Add((Join-Path $env:LOCALAPPDATA "$projName\Saved"))
    }
}

# b) Branded folders in the user profile, project name unknown.
foreach ($root in @($env:LOCALAPPDATA, (Join-Path (Split-Path $env:LOCALAPPDATA) 'LocalLow'))) {
    if (Test-Path $root) {
        Get-ChildItem -Path $root -Directory |
            Where-Object { $_.Name -match 'smite|titan|hirez' } |
            ForEach-Object { $saved.Add((Join-Path $_.FullName 'Saved')) }
    }
}

# c) Brute-force: any *\Saved dir a couple levels into LOCALAPPDATA.
Get-ChildItem -Path $env:LOCALAPPDATA -Directory -Recurse -Depth 2 |
    Where-Object { $_.Name -eq 'Saved' } |
    ForEach-Object { $saved.Add($_.FullName) }

$saved = $saved | Sort-Object -Unique | Where-Object { Test-Path $_ }
if (-not $saved) {
    Write-Warning "No UE Saved\ dir found. Pass -GamePath if Smite 2 isn't at the default Steam location, and make sure you've launched the game at least once."
    return
}

# --- 2. List logs, and point at Config / Demos (replays) --------------------
$logs = foreach ($s in $saved) {
    $ld = Join-Path $s 'Logs'
    if (Test-Path $ld) { Get-ChildItem -Path $ld -Filter *.log -File }
}
$logs = $logs | Sort-Object LastWriteTime -Descending

Head "Log files (newest first):"
if ($logs) {
    $logs | Select-Object -First 12 `
        FullName, @{n = 'SizeKB'; e = { [int]($_.Length / 1KB) } }, LastWriteTime |
        Format-Table -AutoSize
} else {
    Write-Warning "Found Saved dir(s) but no .log files:`n  $($saved -join "`n  ")"
}

foreach ($s in $saved) {
    foreach ($sub in 'Config\Windows', 'Demos', 'Crashes') {
        $p = Join-Path $s $sub
        if (Test-Path $p) {
            $n = (Get-ChildItem $p -File -Recurse | Measure-Object).Count
            Write-Host ("{0,-10} {1}  ({2} files)" -f $sub.Split('\')[0], $p, $n) -ForegroundColor DarkGray
        }
    }
}

# --- 3. Optional: does the log actually record the roster? ------------------
if ($Scan -and $logs) {
    $patterns = @(
        'god', 'character', 'loadout', 'draft', 'pick', 'select', 'roster',
        'matchstart', 'match start', 'playerstate', 'BP_God', 'Character_',
        'TeamId', 'team_id', 'godselect'
    ) + $Keywords
    $regex = ($patterns | ForEach-Object { [regex]::Escape($_) }) -join '|'
    Head "Scanning the newest $Newest log(s) for: $($patterns -join ', ')"
    foreach ($log in ($logs | Select-Object -First $Newest)) {
        Write-Host "`n--- $($log.FullName) ---" -ForegroundColor Yellow
        $hits = Select-String -Path $log.FullName -Pattern $regex -AllMatches |
            Select-Object -First 50
        if ($hits) {
            $hits | ForEach-Object { "{0,6}: {1}" -f $_.LineNumber, $_.Line.Trim() }
        } else {
            Write-Host "  (no keyword hits -- this build likely logs quietly)" -ForegroundColor DarkGray
        }
    }
    Write-Host "`nReview the lines before sharing them, then paste the interesting ones. If your match's gods aren't in here, the next steps are: bump log verbosity via Saved\Config\Windows\Engine.ini (or launch with -log -LogCmds), or read the loading screen with screen-capture + OCR." -ForegroundColor Green
}
