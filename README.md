# PC Caster

**Cast live HLS / `.m3u8` video streams from your Windows PC to a Roku TV — no phone required.**

PC Caster finds the real stream behind a web page, works around the header/TLS protections those streams use, and plays them on your TV through a tiny Roku channel that the app fully controls.

---

## Table of contents
- [What it is](#what-it-is)
- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [First-time TV setup (one time)](#first-time-tv-setup-one-time)
- [Daily use](#daily-use)
- [Running it without a console window](#running-it-without-a-console-window)
- [Project layout](#project-layout)
- [Configuration & data](#configuration--data)
- [Troubleshooting](#troubleshooting)
- [Legal / responsible use](#legal--responsible-use)

---

## What it is

A small **Windows desktop app** (Python + Tkinter) that casts video — especially **HLS live streams** (`.m3u8`) — from your PC to a **Roku TV** on the same network. It bundles:

- a **stream finder** that detects the `.m3u8` behind a web page (like the "video detector" apps on phones),
- a **local proxy** that makes header/TLS-locked streams playable by a TV,
- a **custom Roku channel** it installs and drives directly,
- quality-of-life extras: PC playback test, live proxy indicator, branded icon, and a log file.

## What it does

- **Find streams** – Open a page (e.g. a sports stream page), click the server you want, and PC Caster watches the network and captures the real `.m3u8` URL **plus the `Referer` header** the player used.
- **Unlock streams** – Many streams reject anything that isn't a real browser (they check the HTTP `Referer` **and** the TLS fingerprint). PC Caster's proxy re-requests the stream **with a real Chrome TLS fingerprint and the right Referer**, so a device that can't send those (like a Roku) can still play it.
- **Cast to TV** – It plays through a custom **"PC Caster"** Roku channel that the app installs once and then controls every time.
- **Test on PC** – Play the (proxied) stream in VLC first to confirm it's good before sending to the TV.
- **(Legacy)** Basic Fire TV casting via ADB, and an experimental "use the stock Castify channel" mode.

## How it works

```
  Web page ──(1) Find .m3u8──▶  PC Caster captures stream URL + Referer
                                        │
                                        ▼
  Roku TV ◀─(4) plays─ "PC Caster" channel ◀─(3) launch with proxy URL
                                        ▲
                                        │ (2) re-fetch with Chrome TLS + Referer
                                  Local HLS proxy  ──────▶  Stream CDN
                                  (http://<PC-IP>:8011)
```

1. **Find .m3u8** – A real browser opens; you click the server. PC Caster sniffs all network requests and grabs the `.m3u8` (the adaptive `index.m3u8`) and the `Referer`.
2. **Local proxy** – PC Caster runs a small HTTP server on your PC (port **8011**). For every request it re-fetches the real resource from the CDN using **`curl_cffi` (impersonating Chrome's TLS/JA3 fingerprint)** and injects the captured **`Referer`/`Origin`** headers. It rewrites the playlist so the video segments also flow through the proxy.
3. **Cast** – PC Caster launches its Roku channel via the Roku ECP API and hands it the **proxy URL** (e.g. `http://192.168.1.50:8011/p.m3u8?...`).
4. **Playback** – The Roku plays from your PC over plain HTTP — it never has to send any special headers, so it just works. A Windows Firewall rule (added automatically) lets the Roku reach your PC.

> **Why the proxy is necessary:** the stream URL itself returns **403 Forbidden** to anything that isn't a real browser — wrong/missing Referer = 403, non-browser TLS fingerprint = 403. That's why pasting the raw `.m3u8` into VLC or a Roku fails. Only the proxied URL works.

## Requirements

| Requirement | Notes |
|---|---|
| **Windows 10/11** | Uses Windows-specific bits (taskbar icon, firewall, `pythonw`). |
| **Python 3.9+** | Install from <https://www.python.org/downloads/> and check **"Add Python to PATH"**. |
| **Python packages** | `requests`, `playwright`, `curl_cffi`, `Pillow` — installed automatically (see below). |
| **Playwright Chromium** | One-time browser download for the stream finder. Installed automatically. |
| **Roku in Developer Mode** | Needed for the custom "PC Caster" channel. One-time setup (below). |
| **Same network** | PC and Roku must be on the same LAN / Wi-Fi. |
| **VLC** *(optional)* | Only for the "Test on PC" button. <https://www.videolan.org/> |

## Installation

1. Install **Python 3.9+** (tick *Add Python to PATH*).
2. Put the `PCCaster` folder anywhere (e.g. your Downloads).
3. Double-click **`run.bat`**. On first run it will:
   - `pip install` the dependencies from `requirements.txt`,
   - download the Playwright **Chromium** engine,
   - generate the brand icons,
   - launch the app (windowless).

That's it for the PC side. (You can re-run `run.bat` anytime to update dependencies.)

## First-time TV setup (one time)

The custom TV channel needs Roku **Developer Mode**:

1. **On the Roku remote**, press in order: **Home ×3, Up ×2, Right, Left, Right, Left, Right**.
2. The *Developer Settings* screen appears → **Enable installer**, **set a password**, accept the agreement. The TV reboots.
3. In PC Caster, click **📺 TV App** → enter the Roku **IP** and the **dev password** → **Install / Update channel**.
4. Make sure the receiver mode is **"My TV App (recommended)"**.

The "PC Caster" channel now appears on your Roku home screen, and the app can drive it.

## Daily use

1. Launch PC Caster.
2. **Scan** for devices (or **＋ Add IP** and type the Roku's IP).
3. Select your Roku in the list.
4. Click **🔍 Find .m3u8**. A browser opens — click the server/quality you want and press play if needed. The stream appears in the scanner.
5. Select the **`index.m3u8` (adaptive)** entry → **Use selected**.
6. *(Optional)* **🖥 Test on PC** to preview in VLC.
7. **▶ Cast to TV**. The match plays through your channel.

**Keep the app open while watching** — the proxy runs inside it. The status bar shows **⚡ Streaming to TV · N reqs** while the Roku is actively pulling. If playback stops, the stream's token likely expired — just **Find .m3u8** again.

## Running it without a console window

- **`PC Caster.vbs`** – double-click to launch with **no console window** (portable; survives folder moves).
- **`Create Desktop Shortcut.bat`** – run once to put a branded **PC Caster** icon on your Desktop (you can pin it to the taskbar). Re-run if you move the folder.
- **`run.bat`** – use when dependencies need installing/updating (shows a console only during setup, then launches windowless).

## Project layout

| File / folder | Purpose |
|---|---|
| `pc_caster.py` | Main app (GUI, casting, device discovery). |
| `stream_finder.py` | Finds `.m3u8` streams by sniffing browser network traffic (Playwright). |
| `hls_proxy.py` | Local HLS proxy: Chrome-TLS re-fetch, Referer injection, playlist rewriting, firewall rule. |
| `roku_deploy.py` | Builds, sideloads, and launches the Roku channel. |
| `roku_receiver/` | Source of the **PC Caster** Roku channel (BrightScript / SceneGraph). |
| `make_icons.py` | Generates the app + channel icons from one design. |
| `logger.py` | Rotating log file setup. |
| `assets/` | Generated icons (`app_icon.ico`, `app_icon.png`, …). |
| `requirements.txt` | Python dependencies. |
| `run.bat` | Setup + launch. |
| `PC Caster.vbs` | Windowless launcher. |
| `Create Desktop Shortcut.bat` | Makes a branded desktop shortcut. |
| `pc_caster.log` | Runtime log (auto-created, self-trimming). |

## Configuration & data

- **Settings:** `~/.pc_caster.json` (your home folder) — saved devices, last device, Roku dev password, receiver mode. Migrated automatically from the old name if present.
- **Log:** `pc_caster.log` next to the app — capped at ~256 KB with 2 rollovers. Open it anytime with the **📄 Log** button.
- **Proxy port:** `8011` (TCP, inbound). The app adds a Windows Firewall rule named **"PC Caster HLS Proxy"** automatically (one UAC prompt).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| **TV blinks then nothing plays** | Firewall blocking the Roku → allow it (the app prompts via UAC), or run as admin once: `New-NetFirewallRule -DisplayName "PC Caster HLS Proxy" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8011 -Profile Private` |
| **Played fine, then stopped** | Stream **token expired** (you'll see `proxy upstream HTTP 403` in the log). Click **🔍 Find .m3u8** again. |
| **Raw `.m3u8` won't open in VLC** | Expected — raw links are header/TLS-locked. Always use **Test on PC** / the proxied URL, never the raw stream link. |
| **"Find .m3u8" finds nothing** | The stream only starts after you click a **server** and press play. Do that in the browser window the scanner opens. First run also needs Playwright: `pip install playwright` then `python -m playwright install chromium`. |
| **Scan doesn't list the Roku** | Use **＋ Add IP** (Roku IP: *Settings → Network → About*). |
| **Channel won't install** | Re-check Developer Mode is fully enabled and the **dev password** is correct (📺 TV App). |
| **App won't start (windowless)** | Run `run.bat` once to see the error in a console, or open `pc_caster.log`. |
| **Roku Media Player just shows a file browser** | That mode can't play these streams. Switch receiver mode to **My TV App** (📺 TV App). |

## Legal / responsible use

PC Caster is a general-purpose casting/relay tool. **You are responsible for only using it with streams you are legally entitled to access**, and for complying with the terms of service of any website and with copyright law in your jurisdiction. The author provides this for personal, lawful use.

---

*Built for Luis · June 2026 · v1.1*
