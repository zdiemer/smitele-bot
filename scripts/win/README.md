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
127.0.0.1:8080** and changes nothing else. It captures nothing until you route
an app to it.

```powershell
pip install mitmproxy            # or: winget install mitmproxy.mitmproxy
.\Capture-RHToken.ps1            # trusts the mitmproxy CA on first run
```

Confirm the listener works, independently of the game:

```powershell
curl.exe -x http://127.0.0.1:8080 http://example.com
```

A flow line should appear in the mitmproxy window. Once it does, the only task
left is routing the **game** to the proxy — and because Smite 2 ignores the
Windows system proxy, use a socket-level forwarder:

- **Proxifier** (30-day trial) or **ProxyCap**: add proxy `127.0.0.1:8080` type
  HTTPS, then a rule sending `Smite2.exe` (and any EOS / RallyHere helper `.exe`)
  to it. This forces the app's TCP through the proxy whether or not it honors
  any proxy setting.

Then start Smite 2 and watch for:

```
[capture] first flow to <host>...rally-here.io  <-- RallyHere
```

The addon logs the first flow to **every** host (not just RallyHere), so if you
route the game and still see nothing, the game isn't going through the forwarder
— fix the Proxifier rule. If you see a `rally-here.io` attempt that fails on a
TLS/certificate error, that's **certificate pinning** (below).

## What Wireshark can and can't do here

Wireshark captures packets but **cannot read the token** — it's inside the TLS
stream, and you have no key to decrypt it (the game won't emit an
`SSLKEYLOGFILE`). What it *can* do, passively and without any interception, is
read the **`server_name` (SNI)** out of the TLS ClientHello — which is exactly
the `<env-id>.rally-here.io` env host, and confirms the game is talking to
RallyHere at all. So Wireshark is a fine way to get the env host and prove the
traffic exists; it is not a way to get the token. Filter: `tls.handshake.extensions_server_name contains "rally-here"`.

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
