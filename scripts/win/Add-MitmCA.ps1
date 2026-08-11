<#
.SYNOPSIS
    Find the CA bundle(s) Smite 2's libcurl/OpenSSL client validates against and
    append mitmproxy's CA, so MITM capture is trusted without relying on the
    Windows cert store. Backs up each file first; -List previews; -Revert undoes.

.DESCRIPTION
    The client is OpenSSL (per its JA4 fingerprint), which validates against a
    bundled cacert.pem shipped in the game rather than the Windows trust store --
    so trusting mitmproxy's CA in Windows isn't enough. This locates the bundle(s)
    in the install (any .pem/.crt containing 2+ certificates) and appends
    mitmproxy's CA cert, once, keeping a .rhbak backup of each original.

    Restart Steam + Smite 2 afterward so the client reloads its bundle, then
    re-run Capture-RHToken.ps1. If capture works now, it was a bundled-CA client.
    If it STILL fails after this, that's genuine certificate pinning -- the wall.

.PARAMETER GamePath
    Smite 2 install dir. Default is the standard Steam location.

.PARAMETER MitmCert
    mitmproxy's CA in PEM form. Default is where mitmproxy writes it.

.PARAMETER List
    Only report the bundles found; make no changes.

.PARAMETER Revert
    Restore every .rhbak backup under GamePath and remove the backups.
#>
[CmdletBinding()]
param(
    [string]$GamePath = "C:\Program Files (x86)\Steam\steamapps\common\SMITE 2",
    [string]$MitmCert = "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.pem",
    [switch]$List,
    [switch]$Revert
)

$ErrorActionPreference = "Stop"
$marker = "# rh-probe: mitmproxy CA appended"

if (-not (Test-Path $GamePath)) {
    Write-Error "Game path not found: $GamePath  (pass -GamePath)"
    return
}

# --- Revert: restore originals from the .rhbak backups -----------------------
if ($Revert) {
    $baks = Get-ChildItem -Path $GamePath -Recurse -Filter *.rhbak -File -ErrorAction SilentlyContinue
    if (-not $baks) {
        Write-Host "No .rhbak backups found under $GamePath -- nothing to revert." -ForegroundColor Yellow
        return
    }
    foreach ($bak in $baks) {
        $orig = $bak.FullName -replace '\.rhbak$', ''
        Copy-Item -Path $bak.FullName -Destination $orig -Force
        Remove-Item -Path $bak.FullName -Force
        Write-Host "Reverted $orig" -ForegroundColor Green
    }
    return
}

# --- Find CA bundles: .pem/.crt/.cer text holding 2+ certificates ------------
Write-Host "Scanning $GamePath for CA bundles (this can take a moment)..." -ForegroundColor Cyan
$candidates = Get-ChildItem -Path $GamePath -Recurse -File -Include *.pem, *.crt, *.cer -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -lt 5MB } |
    ForEach-Object {
        $text = Get-Content -Path $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($text) {
            $count = ([regex]::Matches($text, "BEGIN CERTIFICATE")).Count
            if ($count -ge 2) {
                [pscustomobject]@{ Path = $_.FullName; Certs = $count; Text = $text }
            }
        }
    }

if (-not $candidates) {
    Write-Host "No multi-certificate bundle found under the game dir." -ForegroundColor Yellow
    Write-Host "The client may use the Windows store (already trusted by Capture-RHToken.ps1)"
    Write-Host "or SSL_CERT_FILE/CURL_CA_BUNDLE. If capture still fails, that or pinning is why."
    return
}

Write-Host "Found $($candidates.Count) CA bundle(s):" -ForegroundColor Green
$candidates | ForEach-Object { Write-Host ("  {0}  ({1} certs)" -f $_.Path, $_.Certs) }

if ($List) {
    Write-Host "`n-List given: no changes made." -ForegroundColor DarkGray
    return
}

# --- Append mitmproxy's CA (idempotent, backed up) ---------------------------
if (-not (Test-Path $MitmCert)) {
    Write-Error "mitmproxy CA not found at $MitmCert. Run Capture-RHToken.ps1 once to generate it, or pass -MitmCert."
    return
}
$caText = Get-Content -Path $MitmCert -Raw

$patched = 0
foreach ($c in $candidates) {
    if ($c.Text -match [regex]::Escape($marker)) {
        Write-Host "Already patched: $($c.Path)" -ForegroundColor DarkGray
        continue
    }
    $bak = "$($c.Path).rhbak"
    if (-not (Test-Path $bak)) { Copy-Item -Path $c.Path -Destination $bak -Force }
    Add-Content -Path $c.Path -Value "`n$marker`n$caText"
    Write-Host "Appended mitmproxy CA to $($c.Path)  (backup: $bak)" -ForegroundColor Green
    $patched++
}

if ($patched -gt 0) {
    Write-Host "`nRestart Steam + Smite 2 so the client reloads its CA bundle, then re-run Capture-RHToken.ps1." -ForegroundColor Green
    Write-Host "Undo any time with:  .\Add-MitmCA.ps1 -Revert" -ForegroundColor DarkGray
} else {
    Write-Host "`nAll bundles already patched. If capture still fails, that points at pinning." -ForegroundColor DarkGray
}
