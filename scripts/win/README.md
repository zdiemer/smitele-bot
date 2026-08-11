# Capturing a RallyHere token off the Windows Smite 2 client

`scripts/probe_rallyhere.py` needs a bearer **token**, the **env host**
(`https://<env-id>.rally-here.io`), and — to stop re-sniffing hourly — a
**refresh token**, all of which only the running game knows. Getting them off a
game client on Windows is fiddly, so there are two routes here, cheapest and
safest first.

> **Do not hijack the system proxy for this.** An earlier version set the
> Windows (WinINET) proxy system-wide. That breaks your browser, can strand your
> connection if the tool exits badly, and *still* usually misses the game, which
> doesn't use WinINET. None of the tools here touch your system proxy.

## Plan A — read the token off disk (no proxy, nothing to break)

`Find-RHToken.ps1` searches the client's own data dirs for a JWT-shaped token
(`eyJ…`), decodes each candidate, and prints the ones that look like RallyHere
(they carry `player_uuid` and `session:` perms).

```powershell
cd scripts\win
.\Find-RHToken.ps1
```

If it finds one, you're done with zero interception — it writes the freshest
candidate to `rh_capture.json`. The token's payload doesn't include the env
host, so grep the source file it names for `rally-here.io` to get the
`--base-url`. If it finds nothing, the client doesn't persist its token; go to
Plan B.

## Plan B — MITM the game only, never the system

`Capture-RHToken.ps1` runs mitmproxy as a **plain local listener on
127.0.0.1:8080**. It does not touch the WinINET system proxy (so your browser is
safe). The client's TLS fingerprint (JA4 `t13d3113h1…`, no GREASE,
OpenSSL-style cipher list, ALPN http/1.1) identifies it as **libcurl + OpenSSL**
— which ignores the WinINET proxy but **honors the `HTTP_PROXY`/`HTTPS_PROXY`
env vars**. So by default the script sets those (at User scope, restored on
exit) and you just restart Steam:

```powershell
pip install mitmproxy            # or: winget install mitmproxy.mitmproxy
.\Capture-RHToken.ps1            # sets HTTP(S)_PROXY, trusts the CA, listens
# -> then FULLY quit Steam (tray -> Exit) and relaunch it, then start Smite 2.
```

Confirm the listener works, independently of the game:

```powershell
curl.exe -x http://127.0.0.1:8080 http://example.com
```

Then watch for:

```
[capture] first flow to <host>...rally-here.io  <-- RallyHere
```

The addon logs the first flow to **every** host, so if you route the game and
still see nothing, it isn't inheriting the env vars — confirm you restarted
Steam *after* the script set them. If the env-var route just doesn't take, run
`.\Capture-RHToken.ps1 -NoProxyEnv` and force `Smite2.exe` through
`127.0.0.1:8080` with **Proxifier** (30-day trial) or **ProxyCap** at the socket
layer instead — that works regardless of what the client honors.

### The cert step is the real obstacle, and it's usually not pinning

Because the client is **OpenSSL**, it does **not** read the Windows cert store by
default — it validates against a **bundled `cacert.pem`**. So trusting
mitmproxy's CA in Windows (which the script does) may not be enough on its own.
Work it in this order; only the last is genuine pinning:

1. **Trust in Windows** — already done by the script; some curl builds do use the
   native store (`CURLSSLOPT_NATIVE_CA`), so try it first.
2. **Point OpenSSL's env override at mitmproxy's CA.** OpenSSL/libcurl honor
   `SSL_CERT_FILE` / `CURL_CA_BUNDLE`. Set one to a bundle that includes
   `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem` and restart Steam. Works
   unless the app hardcodes its CA path.
3. **Append to the game's bundle** — `Add-MitmCA.ps1` does this for you. It finds
   every multi-cert bundle in the Smite 2 install, backs each up, and appends
   mitmproxy's CA:

   ```powershell
   .\Add-MitmCA.ps1 -List     # preview what it found, change nothing
   .\Add-MitmCA.ps1           # append (keeps a .rhbak of each original)
   .\Add-MitmCA.ps1 -Revert   # undo
   ```

   Restart Steam + Smite 2 afterward so the client reloads the bundle. (Pass
   `-GamePath` if your install isn't at the default Steam location.)
4. **Still rejected after all of that → true pinning** (`CURLOPT_PINNEDPUBLICKEY`
   or a custom verify callback). That's the wall; see below.

## What Wireshark can and can't do here

Wireshark captures packets but **cannot read the token** — it's inside the TLS
stream, and you have no key to decrypt it (the game won't emit an
`SSLKEYLOGFILE`). What it *can* do, passively and without any interception, is
read the **`server_name` (SNI)** out of the TLS ClientHello. That's how the env
host was found: the API is `api-smite2.titanforgegames.com` (Titan Forge fronts
its RallyHere env behind that CNAME, on Azure — the `rally-here.io` DNS lookups
are the SDK resolving sibling services). Wireshark gets you the env host and
proves the traffic exists; it is not a way to get the token. Filter:
`tls.handshake.extensions_server_name contains "titanforgegames"`.

## The pinning wall

If Smite 2 pins its certificates, mitmproxy's cert is rejected and the game's
connection fails no matter how perfectly you route it — Plan B is dead and only
Plan A (or the game not pinning) can work. That's not a bug in these tools; it's
the client refusing to be MITM'd. If both plans fail, the coarse Steam
"is-playing" signal already shipped in the bot is the ceiling, exactly as
`docs/smite2-live-data.md` concludes.

## Then probe (from anywhere)

`rh_capture.json` holds `base_url`, `token`, `self_uuid`, and (Plan B)
`refresh_token`:

```sh
python scripts/probe_rallyhere.py \
    --base-url "$(jq -r .base_url rh_capture.json)" \
    --token    "$(jq -r .token rh_capture.json)" \
    --self-uuid "$(jq -r .self_uuid rh_capture.json)" \
    --other-uuid <a-friends-uuid>
```

Run it **directly first** (the token is the wall — a 403 is a 403 from any
address), then re-run the same warm token out the egress VPS lane as a control:

```sh
export RH_PROXY_AUTH="homecluster:<vps password from selfhosted values.local.yaml>"
python scripts/probe_rallyhere.py ... --proxy http://100.121.204.109:3129
```

A `200` direct but `401` through the VPS means RallyHere pins the bearer to an
IP/geo — worth knowing before any repeated probing.

## The refresh token is the point (Plan B only)

The access token dies in minutes to ~an hour; the **refresh token** the addon
grabs from the auth response lives far longer. Capture it once and you can mint
fresh access tokens yourself — the game needn't be running — until it expires.
That's fine for a one-shot investigation on your own account; it's the exact
loop `docs/smite2-live-data.md` warns off a *deployed* bot for ToS reasons.
