# Capturing a RallyHere token off the Windows Smite 2 client

`scripts/probe_rallyhere.py` needs three things only the running game knows: a
bearer **token**, the **env host** (`https://<env-id>.rally-here.io`), and —
if you want to stop re-sniffing every hour — a **refresh token**. This folder
lifts all three off the live client so you capture once and probe at leisure.

Two files:

- **`capture_rh_token.py`** — a mitmproxy addon. The actual work: it reads the
  token off the `Authorization` header, the host off the URL, and the refresh
  token off any auth response, decodes the JWT to show your permissions and
  player-uuid, writes `rh_capture.json`, and prints a paste-ready probe command.
- **`Capture-RHToken.ps1`** — a launcher. Sets the Windows proxy to a local
  mitmproxy, trusts its CA on first run, runs the addon, and restores the proxy
  when you Ctrl+C.

## Use it

```powershell
# one-time: install mitmproxy
pip install mitmproxy      # or: winget install mitmproxy.mitmproxy

# each capture:
cd scripts\win
.\Capture-RHToken.ps1
# ...then start (or restart) Smite 2. Tokens print as the client mints them.
```

First run pops a Windows dialog to trust mitmproxy's CA — without it the game's
TLS refuses the interception. Say yes. Ctrl+C restores your proxy exactly as it
was.

## Then probe (back on Linux, or anywhere)

`rh_capture.json` holds `base_url`, `token`, `self_uuid`, and `refresh_token`.
The addon prints the command, but by hand:

```sh
python scripts/probe_rallyhere.py \
    --base-url "$(jq -r .base_url rh_capture.json)" \
    --token    "$(jq -r .token rh_capture.json)" \
    --self-uuid "$(jq -r .self_uuid rh_capture.json)" \
    --other-uuid <a-friends-uuid>
```

Run it **directly first** — the token is the wall, and a 403 is a 403 from any
address. Then re-run the *same warm token* out the egress VPS lane as a control:

```sh
export RH_PROXY_AUTH="homecluster:$(the vps password from selfhosted values.local.yaml)"
python scripts/probe_rallyhere.py ... --proxy http://100.121.204.109:3129
```

If direct self-read is `200` but the VPS run is `401`, RallyHere is pinning the
bearer to an IP/geo — worth knowing before any repeated probing, and something a
`403`-vs-`200` reading from home alone would never show you.

## The refresh token is the point

The access token dies in minutes to ~an hour. The **refresh token** the addon
grabs from the auth response lives far longer. Capture it once and you can mint
fresh access tokens yourself — the game doesn't even need to be running — until
the refresh token itself expires. That's what turns "re-sniff every hour" into
"one warm capture." (Minting the token headlessly is fine for a one-shot
investigation on your own account; it is the exact loop
`docs/smite2-live-data.md` warns off a *deployed bot* for token-lifetime and
ToS reasons.)

## If no `rally-here.io` flows show up

The addon logs the first flow to every host. If you see other hosts but never
`rally-here.io`, the game is ignoring the WinINET proxy — some Unreal/libcurl
clients read `HTTP_PROXY`/`HTTPS_PROXY` env vars instead of the system setting,
and some ignore both. In rough order of effort:

1. **Env vars, game inheriting them.** Set `HTTP_PROXY`/`HTTPS_PROXY` to
   `http://127.0.0.1:8080` at the user level (`setx`), fully restart Steam so
   the game inherits them, then launch. libcurl-backed Unreal HTTP usually
   honors these even when it ignores WinINET.
2. **Transparent interception.** Run `mitmdump --mode transparent` and route the
   box's traffic through it (WireGuard/route, or a second NIC). No app
   cooperation needed, but more setup. mitmproxy's transparent-mode docs cover
   the routing.
3. **Intercept off-box.** Run mitmproxy on the gateway/another host and point
   this machine's default route or a firewall redirect at it.

The addon and probe don't care how the traffic arrives — only that it reaches
mitmproxy. Once `rally-here.io` shows in the host log, capture works.
