<#
.SYNOPSIS
    Search the Smite 2 / RallyHere client's own files for a JWT-shaped bearer
    token. No proxy, no interception, nothing to break -- if the client logs or
    caches its token, this finds it directly. Try this before Capture-RHToken.

.DESCRIPTION
    A RallyHere access token is a JWT: three base64url segments, and because the
    header is almost always {"alg":...} it begins "eyJ". This scans the game's
    likely data dirs for that shape, decodes each candidate's middle segment,
    and prints the ones that look like RallyHere tokens (they carry player_uuid
    and session: permissions) along with the file they came from.

    The token's own payload does not contain the env host the probe needs
    (https://<env-id>.rally-here.io); grep the same file for "rally-here.io" to
    find it, or read it off SNI in Wireshark. See README.md.

.PARAMETER Roots
    Directories to scan. Defaults to the usual Windows game-data locations.

.PARAMETER Out
    Where to write the best candidate as rh_capture.json. Default next to this
    script.
#>
[CmdletBinding()]
param(
    [string[]]$Roots = @(
        "$env:LOCALAPPDATA",
        "$env:APPDATA",
        "$env:USERPROFILE\Documents\My Games",
        "$env:USERPROFILE\Saved Games"
    ),
    [string]$Out = "$PSScriptRoot\rh_capture.json"
)

$ErrorActionPreference = "SilentlyContinue"
$Roots = $Roots | Where-Object { Test-Path $_ }
Write-Host "Scanning for JWT-shaped tokens under:" -ForegroundColor Cyan
$Roots | ForEach-Object { Write-Host "  $_" }

# eyJ (base64 of '{"') then three dot-separated base64url runs.
$jwtPattern = 'eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}'

function ConvertFrom-JwtPayload([string]$token) {
    $mid = $token.Split('.')[1].Replace('-', '+').Replace('_', '/')
    switch ($mid.Length % 4) { 2 { $mid += '==' } 3 { $mid += '=' } 1 { return $null } }
    try { return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($mid)) }
    catch { return $null }
}

$files = Get-ChildItem -Path $Roots -Recurse -File `
    -Include *.log, *.txt, *.json, *.ini, *.cfg, *.sav, *.dat -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -lt 25MB }

$best = $null
$seen = @{}
foreach ($file in $files) {
    foreach ($match in (Select-String -Path $file.FullName -Pattern $jwtPattern -AllMatches -ErrorAction SilentlyContinue).Matches) {
        $token = $match.Value
        if ($seen.ContainsKey($token)) { continue }
        $seen[$token] = $true
        $payload = ConvertFrom-JwtPayload $token
        if (-not $payload) { continue }
        if ($payload -notmatch 'player_uuid|session:|rally') { continue }

        Write-Host "`n--- RallyHere-looking token in $($file.FullName) ---" -ForegroundColor Green
        Write-Host $payload
        try {
            $claims = $payload | ConvertFrom-Json
            $exp = $claims.exp
            $fresh = if ($exp) { $exp -gt ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) } else { $true }
            if ($fresh -and -not $best) {
                $best = [ordered]@{
                    token      = $token
                    self_uuid  = $claims.player_uuid
                    exp        = $exp
                    source     = $file.FullName
                    base_url   = "https://<env-id>.rally-here.io  # find it: grep this file for rally-here.io"
                }
            }
        } catch { }
    }
}

if (-not $best) {
    Write-Host "`nNo RallyHere-looking token found on disk. The client likely doesn't persist it -- use Capture-RHToken.ps1 + Proxifier instead." -ForegroundColor Yellow
    return
}

$best | ConvertTo-Json | Set-Content -Path $Out -Encoding UTF8
Write-Host "`nWrote the freshest candidate to $Out." -ForegroundColor Green
Write-Host "Fill in base_url (grep the source file for rally-here.io), then run scripts/probe_rallyhere.py." -ForegroundColor Green
