<#
.SYNOPSIS
    Run mitmproxy with the RallyHere token-capture addon as a plain local
    listener, and (by default) set the HTTP(S)_PROXY env vars that a
    libcurl-based client like Smite 2 reads, so the game routes through it.

.DESCRIPTION
    Smite 2's TLS fingerprint looks like libcurl/OpenSSL, which ignores the
    Windows WinINET system proxy but DOES honor the HTTP_PROXY/HTTPS_PROXY
    environment variables. This script sets those at User scope so a
    freshly-launched game inherits them -- and restores them on exit.

    It does NOT touch the WinINET system proxy, so your browser is unaffected.
    But those env vars are also read by git, curl, pip and other libcurl tools:
    while this runs they route through mitmproxy (fine -- it forwards them), and
    they'd fail if mitmproxy stopped without the vars being restored. So the
    script snapshots the originals, restores them in a finally block, and
    self-heals from a state file if a prior run ever died uncleanly. There is
    also a printed one-liner to revert by hand.

    AFTER it sets the vars you must RESTART STEAM (fully quit from the tray, not
    just close the window) so Steam and the game it launches inherit them. An
    already-running Steam will not pick them up.

    If the env-var route doesn't capture the game, pass -NoProxyEnv and route
    Smite2.exe through 127.0.0.1:<Port> with Proxifier/ProxyCap instead.

    Sanity-check the listener, independent of the game:
        curl.exe -x http://127.0.0.1:<Port> http://example.com

    HEADS UP -- TLS PINNING. If Smite 2 pins its certificates, mitmproxy cannot
    decrypt however you route it: the connection just fails. Try Find-RHToken.ps1
    first; it may pull the token from a log with no interception at all.

.PARAMETER Port
    Listen port. Default 8080.

.PARAMETER Out
    Capture JSON path. Default rh_capture.json next to this script.

.PARAMETER NoProxyEnv
    Don't set the HTTP(S)_PROXY env vars; just run the listener (use when you're
    routing the game with Proxifier/ProxyCap instead).
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$Out = "$PSScriptRoot\rh_capture.json",
    [switch]$NoProxyEnv
)

$ErrorActionPreference = "Stop"
$addon = Join-Path $PSScriptRoot "capture_rh_token.py"
$proxyUrl = "http://127.0.0.1:$Port"
$stateFile = Join-Path $PSScriptRoot ".rhproxy-env-state.json"

# --- mitmdump discovery -----------------------------------------------------
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

# --- env-var proxy: set safely, always restorable ---------------------------
if (-not ("Native.Env" -as [type])) {
    Add-Type -Namespace Native -Name Env -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.IntPtr wParam, string lParam, uint flags, uint timeout, out System.IntPtr result);
'@
}
function Send-EnvBroadcast {
    # Tell explorer-launched processes to reload the environment. Already-running
    # Steam won't heed it -- hence the "restart Steam" instruction.
    $out = [IntPtr]::Zero
    [Native.Env]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [IntPtr]::Zero, "Environment", 2, 5000, [ref]$out) | Out-Null
}
function Set-ProxyEnv {
    # Snapshot the current User values BEFORE changing them, so restore is exact.
    $snapshot = @{
        HTTP_PROXY  = [Environment]::GetEnvironmentVariable("HTTP_PROXY", "User")
        HTTPS_PROXY = [Environment]::GetEnvironmentVariable("HTTPS_PROXY", "User")
    }
    $snapshot | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
    [Environment]::SetEnvironmentVariable("HTTP_PROXY", $proxyUrl, "User")
    [Environment]::SetEnvironmentVariable("HTTPS_PROXY", $proxyUrl, "User")
    $env:HTTP_PROXY = $proxyUrl   # this window too, so the curl sanity check works
    $env:HTTPS_PROXY = $proxyUrl
    Send-EnvBroadcast
}
function Restore-ProxyEnv {
    if (-not (Test-Path $stateFile)) { return }
    $snapshot = Get-Content -Path $stateFile -Raw | ConvertFrom-Json
    foreach ($name in "HTTP_PROXY", "HTTPS_PROXY") {
        # A JSON null restores to "unset"; SetEnvironmentVariable($null) removes it.
        [Environment]::SetEnvironmentVariable($name, $snapshot.$name, "User")
    }
    Remove-Item -Path $stateFile -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\HTTP_PROXY, Env:\HTTPS_PROXY -ErrorAction SilentlyContinue
    Send-EnvBroadcast
    Write-Host "Reverted HTTP(S)_PROXY. Restart Steam again to drop the proxy from the game." -ForegroundColor Yellow
}

# --- go ---------------------------------------------------------------------
$mitm = Resolve-Mitmdump
if (-not $mitm) {
    Write-Error "mitmdump not found even after refreshing PATH and searching the usual install dirs. Install it (pip install mitmproxy, or winget install mitmproxy.mitmproxy) and, if you just did, open a new PowerShell window and re-run."
    return
}
Write-Host "Using mitmdump: $mitm" -ForegroundColor DarkGray

# Self-heal: a previous run may have died before restoring the env vars.
if (Test-Path $stateFile) {
    Write-Warning "Found leftover proxy env state from a previous run -- restoring it before continuing."
    Restore-ProxyEnv
}

# One-time: generate + trust the mitmproxy CA, in a HIDDEN separate window so
# force-killing it can't disturb the console the real mitmdump then runs in.
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
    $trusted = Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Subject -like "*mitmproxy*" }
    if (-not $trusted) {
        Write-Host "Trusting mitmproxy CA in your user store (confirm the dialog)..." -ForegroundColor Cyan
        Import-Certificate -FilePath $cert -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    }
} else {
    Write-Warning "mitmproxy CA cert not found at $cert. If capture fails on TLS, run 'mitmdump' once, then trust that file."
}

$env:RH_CAPTURE_OUT = $Out

try {
    if (-not $NoProxyEnv) {
        Set-ProxyEnv
        Write-Host ""
        Write-Host "Set User HTTP(S)_PROXY -> $proxyUrl (your WinINET/browser proxy is untouched)." -ForegroundColor Green
        Write-Host ">> RESTART STEAM NOW (quit from the tray, not just the window), then launch Smite 2." -ForegroundColor Green
        Write-Host "   If this window ever dies without reverting, undo by hand with:" -ForegroundColor DarkGray
        Write-Host '   [Environment]::SetEnvironmentVariable("HTTP_PROXY",$null,"User"); [Environment]::SetEnvironmentVariable("HTTPS_PROXY",$null,"User")' -ForegroundColor DarkGray
    } else {
        Write-Host ""
        Write-Host "mitmproxy listening on 127.0.0.1:$Port. -NoProxyEnv set: route Smite2.exe here with Proxifier/ProxyCap." -ForegroundColor Green
    }
    Write-Host "Sanity check:  curl.exe -x http://127.0.0.1:$Port http://example.com" -ForegroundColor DarkGray
    Write-Host "Ctrl+C to stop." -ForegroundColor Green
    Write-Host ""

    # Bind loopback explicitly -- no IPv4/IPv6 '*' ambiguity for 127.0.0.1 clients.
    & $mitm --listen-host 127.0.0.1 --listen-port $Port -s $addon
}
finally {
    if (-not $NoProxyEnv) { Restore-ProxyEnv }
}
