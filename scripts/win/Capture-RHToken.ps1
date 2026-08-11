<#
.SYNOPSIS
    Run mitmproxy with the RallyHere token-capture addon as a plain local
    listener. It does NOT touch your system proxy -- nothing else on the machine
    is affected and your browser can never be stranded.

.DESCRIPTION
    mitmproxy listens on 127.0.0.1:<Port> and captures nothing until you *route*
    an app through it. Game clients (Unreal/RallyHere) generally ignore the
    Windows system proxy, so the reliable way to route ONLY the game is a
    socket-level forwarder like Proxifier or ProxyCap:

        proxy:  127.0.0.1:<Port>   type HTTPS
        rule:   Smite2.exe (and any EOS / RallyHere helper .exe)  ->  that proxy

    Sanity-check the listener itself, without the game:

        curl.exe -x http://127.0.0.1:<Port> http://example.com

    A flow line should appear in this window. If it does, mitmproxy is fine and
    the only question is routing the game to it.

    HEADS UP -- TLS PINNING. If Smite 2 pins its certificates, mitmproxy cannot
    decrypt even when the traffic is routed correctly: you will see the
    connection attempt fail rather than a clean flow. Try Find-RHToken.ps1
    FIRST -- it may pull the token from a log with no interception at all.

.PARAMETER Port
    Listen port. Default 8080.

.PARAMETER Out
    Capture JSON path. Default rh_capture.json next to this script.
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$Out = "$PSScriptRoot\rh_capture.json"
)

$ErrorActionPreference = "Stop"
$addon = Join-Path $PSScriptRoot "capture_rh_token.py"

# winget/pip drop mitmdump somewhere an already-open shell hasn't picked up in
# its PATH. Refresh from the registry, then search the dirs these installers use.
function Resolve-Mitmdump {
    $cmd = Get-Command mitmdump -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") +
        ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $cmd = Get-Command mitmdump -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $roots = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:APPDATA\Python",
        "$env:ProgramFiles\mitmproxy"
    ) | Where-Object { Test-Path $_ }
    foreach ($root in $roots) {
        $hit = Get-ChildItem -Path $root -Filter mitmdump.exe -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

$mitm = Resolve-Mitmdump
if (-not $mitm) {
    Write-Error "mitmdump not found even after refreshing PATH and searching the usual install dirs. Install it (pip install mitmproxy, or winget install mitmproxy.mitmproxy) and, if you just did, open a new PowerShell window and re-run."
    return
}
Write-Host "Using mitmdump: $mitm" -ForegroundColor DarkGray

# One-time: generate + trust the mitmproxy CA. Generate it in a HIDDEN, separate
# window (not -NoNewWindow) so force-killing it can't disturb the console the
# real mitmdump then runs in. '--version' won't do it: the CA is written when
# the certstore initialises at proxy startup.
$cert = "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
if (-not (Test-Path $cert)) {
    Write-Host "Generating mitmproxy's CA cert (brief hidden launch)..." -ForegroundColor Cyan
    $gen = Start-Process -FilePath $mitm `
        -ArgumentList "--listen-host", "127.0.0.1", "--listen-port", "$Port" `
        -PassThru -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Path $cert); $i++) { Start-Sleep -Milliseconds 500 }
    Stop-Process -Id $gen.Id -Force -ErrorAction SilentlyContinue
}
if (Test-Path $cert) {
    $trusted = Get-ChildItem Cert:\CurrentUser\Root |
        Where-Object { $_.Subject -like "*mitmproxy*" }
    if (-not $trusted) {
        Write-Host "Trusting mitmproxy CA in your user store (confirm the dialog)..." -ForegroundColor Cyan
        Import-Certificate -FilePath $cert -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    }
} else {
    Write-Warning "mitmproxy CA cert not found at $cert. If capture fails on TLS, run 'mitmdump' once, then trust that file."
}

$env:RH_CAPTURE_OUT = $Out

Write-Host ""
Write-Host "mitmproxy is listening on 127.0.0.1:$Port. Your system proxy is UNCHANGED." -ForegroundColor Green
Write-Host "It stays silent until you route an app to it:" -ForegroundColor Green
Write-Host "  - Route Smite2.exe through 127.0.0.1:$Port with Proxifier/ProxyCap, then start the game." -ForegroundColor Green
Write-Host "  - Sanity check now:  curl.exe -x http://127.0.0.1:$Port http://example.com" -ForegroundColor DarkGray
Write-Host "Ctrl+C to stop (nothing to restore -- no system settings were changed)." -ForegroundColor Green
Write-Host ""

# Bind loopback explicitly so there is no IPv4/IPv6 '*' ambiguity for clients
# connecting to 127.0.0.1.
& $mitm --listen-host 127.0.0.1 --listen-port $Port -s $addon
