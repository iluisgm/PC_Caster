═══════════════════════════════════════════════════════
  PC Caster — Cast .m3u8 streams from your PC to your TV
═══════════════════════════════════════════════════════

  (Quick start. Full docs are in README.md)


WHAT IT DOES
────────────
  Finds the live HLS (.m3u8) stream behind a web page, works
  around the Referer/TLS blocks those streams use, and plays
  it on your Roku TV through a small channel the app controls.


REQUIREMENTS
────────────
  • Windows 10/11
  • Python 3.9+   (https://www.python.org/downloads/)
      ✓ tick "Add Python to PATH" during install
  • PC and Roku on the SAME network
  • VLC (optional, for the "Test on PC" button)
  Python packages (auto-installed): requests, playwright,
  curl_cffi, Pillow  + the Playwright Chromium browser.


INSTALL / RUN
─────────────
  Double-click  run.bat
    → installs everything the first time, then launches.

  For no console window, use one of these instead:
    • PC Caster.vbs                 (double-click)
    • Create Desktop Shortcut.bat   (run once for a desktop icon)


ONE-TIME TV SETUP (custom channel)
──────────────────────────────────
  1. On the Roku remote press, in order:
       Home x3, Up x2, Right, Left, Right, Left, Right
     → enable Developer Mode, set a password (TV reboots).
  2. In the app click "📺 TV App", enter the Roku IP + that
     password, and click "Install / Update channel".
  3. Keep receiver mode on "My TV App (recommended)".


HOW TO USE
──────────
  1. Scan (or "＋ Add IP") and select your Roku.
  2. Click "🔍 Find .m3u8" → in the browser that opens, click
     the server you want and press play.
  3. Pick the "index.m3u8 (adaptive)" entry → "Use selected".
  4. (optional) "🖥 Test on PC" to preview in VLC.
  5. "▶ Cast to TV".

  Keep the app open while watching (the proxy runs inside it).
  The status bar shows "⚡ Streaming to TV" while it's serving.


IF SOMETHING BREAKS
───────────────────
  • Click "📄 Log" (or open pc_caster.log) to see what happened.
  • TV blinks but won't play → firewall; let the UAC prompt add
    the "PC Caster HLS Proxy" rule (port 8011).
  • Played then stopped → stream token expired; click
    "🔍 Find .m3u8" again to get a fresh link.
  • Never use the raw stream link directly — it is Referer/TLS-
    locked and only works through the app's proxy.


RESPONSIBLE USE
───────────────
  Only use PC Caster with streams you're legally allowed to
  access. You are responsible for complying with site terms
  and copyright law.


VERSION
───────
  1.1 — Built for Luis | June 2026
