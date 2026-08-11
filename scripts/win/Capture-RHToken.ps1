<#
.SYNOPSIS
    Point Windows' proxy at mitmproxy, run the RallyHere token-capture addon,
    and put the proxy back when you Ctrl+C.

.DESCRIPTION
    Turns the manual "sniff a warm token off the client" step into one command.
    It sets the WinINET system proxy to a local mitmproxy, runs mitmdump with
    capture_rh_token.py, and restores the previous proxy on exit no matter how
    you leave (Ctrl+C included).

    Run this, then start (or restart) Smite 2. Each time the client mints a
    fresh token the console prints it, the env host, and a paste-ready probe
    command; everything also lands in rh_capture.json.

    ONE-TIME SETUP: mitmproxy's CA cert must be trusted or the game's TLS will
    reject the interception. This script installs it into your user Trusted Root
    store on first run (a Windows dialog will ask you to confirm).

    IF NO rally-here.io FLOWS APPEAR but other hosts do, the game is ignoring
    the WinINET proxy (some Unreal/libcurl clients do). See README.md for the
    env-var and transparent-proxy fallbacks.

.PARAMETER Port
    Local port for mitmproxy. Default 8080.

.PARAMETER Out
    Where the addon writes the capture JSON. Default rh_capture.json next to
    this script.
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$Out = "$PSScriptRoot\rh_capture.json"
)

$ErrorActionPreference = "Stop"
$addon = Join-Path $PSScriptRoot "capture_rh_token.py"
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# winget/pip drop mitmdump somewhere that an already-open shell hasn't picked up
# in its PATH. Refresh PATH from the registry, then, failing that, look in the
# handful of places these installers actually use — so "it's installed but not
# found" stops being a reason to reopen the terminal.
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

# Nudge WinINET so running apps notice the proxy change without a reboot.
if (-not ("Native.WinInet" -as [type])) {
    Add-Type -Namespace Native -Name WinInet -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("wininet.dll", SetLastError = true)]
public static extern bool InternetSetOption(System.IntPtr h, int opt, System.IntPtr buf, int len);
'@
}
function Invoke-ProxyRefresh {
    # 39 = INTERNET_OPTION_SETTINGS_CHANGED, 37 = INTERNET_OPTION_REFRESH
    [Native.WinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null
    [Native.WinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null
}

# One-time: generate + trust the mitmproxy CA. The CA is written when mitmdump
# starts up, so force it by launching briefly and stopping — '--version' exits
# before the certstore initialises and won't create it.
$cert = "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
if (-not (Test-Path $cert)) {
    Write-Host "Generating mitmproxy's CA cert (brief mitmdump launch)..." -ForegroundColor Cyan
    $gen = Start-Process -FilePath $mitm -ArgumentList "--listen-port", "$Port" -PassThru -NoNewWindow
    for ($i = 0; $i -lt 10 -and -not (Test-Path $cert); $i++) { Start-Sleep -Milliseconds 500 }
    Stop-Process -Id $gen.Id -Force -ErrorAction SilentlyContinue
}
if (Test-Path $cert) {
    $trusted = Get-ChildItem Cert:\CurrentUser\Root |
        Where-Object { $_.Subject -like "*mitmproxy*" }
    if (-not $trusted) {
        Write-Host "Installing mitmproxy CA into your Trusted Root store (confirm the dialog)..." -ForegroundColor Cyan
        Import-Certificate -FilePath $cert -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    }
} else {
    Write-Warning "mitmproxy CA cert not found at $cert. If capture fails on TLS, run 'mitmdump' once, then trust that file."
}

# Snapshot the current proxy so we can put it back exactly.
$prev = Get-ItemProperty -Path $regPath
$prevEnable = if ($null -ne $prev.ProxyEnable) { $prev.ProxyEnable } else { 0 }
$prevServer = $prev.ProxyServer

try {
    Set-ItemProperty -Path $regPath -Name ProxyServer -Value "127.0.0.1:$Port"
    Set-ItemProperty -Path $regPath -Name ProxyEnable -Value 1
    Invoke-ProxyRefresh
    # Also export for libcurl-based children launched from THIS shell.
    $env:HTTP_PROXY = "http://127.0.0.1:$Port"
    $env:HTTPS_PROXY = $env:HTTP_PROXY
    $env:RH_CAPTURE_OUT = $Out

    Write-Host ""
    Write-Host "Proxy is on 127.0.0.1:$Port. Now start (or restart) Smite 2." -ForegroundColor Green
    Write-Host "Watching for RallyHere tokens -- Ctrl+C to stop and restore the proxy." -ForegroundColor Green
    Write-Host ""

    & $mitm --listen-port $Port -s $addon
}
finally {
    Set-ItemProperty -Path $regPath -Name ProxyEnable -Value $prevEnable
    if ($null -ne $prevServer) {
        Set-ItemProperty -Path $regPath -Name ProxyServer -Value $prevServer
    }
    Remove-Item Env:\HTTP_PROXY, Env:\HTTPS_PROXY -ErrorAction SilentlyContinue
    Invoke-ProxyRefresh
    Write-Host "`nProxy restored." -ForegroundColor Yellow
}
