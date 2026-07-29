#!/usr/bin/env python3
"""
stream_finder.py
Scan a web page for .m3u8 (HLS) streams the same way mobile "video detector"
apps do: load the page in a headless browser and watch the network traffic for
any request whose URL contains ".m3u8".

Two strategies, tried in order:
  1. Fast HTML/JS scrape (requests + regex)  — works only when the link is
     literally in the page source. Cheap, instant, often misses dynamic sites.
  2. Headless-browser sniff (Playwright)      — loads the page, follows iframes,
     clicks play, and captures every .m3u8 the player actually requests.
     This is what catches sites that build the stream URL dynamically.

Public entry point:  find_streams(page_url, on_log=None, wait=20) -> list[dict]
Each result dict: {"url", "referer", "origin", "label"}
"""

import re
import time

import requests

M3U8_RE = re.compile(r"https?://[^\s'\"<>()]+\.m3u8[^\s'\"<>()]*", re.IGNORECASE)

# A normal-looking desktop UA. Many stream hosts reject "python-requests".
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _is_m3u8(url: str) -> bool:
    # match ".m3u8" in the path portion, ignoring the query string/token
    return ".m3u8" in url.split("?", 1)[0].lower()


def _label_for(url: str) -> str:
    """Make a short human label, e.g. 'index.m3u8' or 'mono.ts.m3u8'."""
    path = url.split("?", 1)[0]
    name = path.rsplit("/", 1)[-1] or path
    return name[:48]


# ── Stream reliability ranking ────────────────────────────────────────────────
# Some hosts hand out short-lived, single-session tokens (the URL contains a
# "/secure/" gate and an opaque token). Streams from these die seconds after
# capture — they play for ~1s then 403, so we rank them BELOW open CDNs. Open
# CDNs (Rumble, CloudFront, Akamai…) serve plain segments that keep working, so
# we prefer them automatically when a scan returns several candidates.

from urllib.parse import urlparse as _urlparse

_TOKEN_HOSTS = ("strmd.st", "indianservers.st")
_OPEN_HOSTS = ("rumble.cloud", "cloudfront.net", "akamaized.net",
               "akamaihd.net", "akamai.net")


def stream_reliability(item: dict) -> tuple[int, str]:
    """Score a captured stream by how likely it is to KEEP playing.

    Higher score = more reliable. Returns (score, short_label) where the label
    is one of 'open' / 'token' / 'expiring' for display in the picker.
    """
    p = _urlparse(item.get("url", ""))
    host = p.netloc.lower()
    path = p.path.lower()

    score = 0
    token = False
    if "/secure/" in path:
        score -= 5
        token = True
    if any(host == h or host.endswith("." + h) for h in _TOKEN_HOSTS):
        score -= 3
        token = True
    if any(o in host for o in _OPEN_HOSTS):
        score += 3

    label = "token" if token else "open"
    return score, label


def rank_streams(items: list[dict]) -> list[dict]:
    """Return items sorted best-first (open CDNs above token-locked ones).

    Stable: streams with equal reliability keep their original capture order.
    """
    return sorted(items, key=lambda it: -stream_reliability(it)[0])


# ── Strategy 1: quick scrape ──────────────────────────────────────────────────

def _scrape_html(page_url: str, on_log) -> list[dict]:
    on_log("Quick scan: fetching page source…")
    found = {}
    try:
        r = requests.get(
            page_url,
            headers={"User-Agent": USER_AGENT, "Referer": page_url},
            timeout=12,
        )
        for m in M3U8_RE.findall(r.text):
            if m not in found:
                found[m] = {
                    "url": m,
                    "referer": page_url,
                    "origin": "",
                    "label": _label_for(m),
                }
    except Exception as e:
        on_log(f"Quick scan failed: {e}")
    if found:
        on_log(f"Quick scan found {len(found)} link(s) in the page source.")
    else:
        on_log("Quick scan found nothing — the stream is loaded dynamically.")
    return list(found.values())


# ── Strategy 2: headless-browser network sniff ────────────────────────────────

def _sniff_browser(page_url: str, on_log, wait: int) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        on_log("PLAYWRIGHT_MISSING")
        return []

    on_log("Deep scan: launching headless browser…")
    found = {}

    def _record(req):
        try:
            url = req.url
        except Exception:
            return
        if _is_m3u8(url) and url not in found:
            h = {}
            try:
                h = req.headers
            except Exception:
                pass
            found[url] = {
                "url": url,
                "referer": h.get("referer", ""),
                "origin": h.get("origin", ""),
                "label": _label_for(url),
            }
            on_log(f"  ✓ found stream: {_label_for(url)}")

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                # Browser binary not installed yet
                on_log("PLAYWRIGHT_NO_BROWSER")
                return []

            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )

            # Watch requests on the main page AND any ad/popup tabs the site opens.
            def _attach(page):
                page.on("request", _record)

            ctx.on("page", _attach)
            page = ctx.new_page()
            _attach(page)

            on_log("Loading page…")
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                on_log(f"Navigation warning: {e}")

            # These sites need a 'play' interaction. Try clicking likely targets
            # in the main page and inside every iframe, then keep waiting for the
            # player to fire off its .m3u8 request.
            deadline = time.time() + max(5, wait)
            click_selectors = [
                "video", ".play", ".vjs-big-play-button", "#play",
                "button[aria-label*='play' i]", ".jw-icon-display", "body",
            ]
            while time.time() < deadline and not found:
                for fr in page.frames:
                    for sel in click_selectors:
                        try:
                            el = fr.query_selector(sel)
                            if el:
                                el.click(timeout=800, force=True)
                        except Exception:
                            pass
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    break

            # Once we have at least one hit, linger briefly to collect variants
            # (e.g. separate quality renditions).
            if found:
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass

            browser.close()
    except Exception as e:
        on_log(f"Deep scan error: {e}")

    return list(found.values())


# ── Interactive sniff: you click, it watches (mirrors the mobile app) ─────────

def find_streams_interactive(page_url, on_log, on_found, stop_event,
                             max_seconds: int = 180) -> list[dict]:
    """
    Open a VISIBLE browser at page_url and watch the network for .m3u8 while the
    user clicks a server link themselves. Exactly how the mobile detector works.

      on_log(str)        progress messages
      on_found(dict)     called once per newly-seen stream (from worker thread)
      stop_event         threading.Event — set it to close the browser early
      max_seconds        hard cap so the window can't hang forever

    Returns the full de-duplicated list when the browser closes.
    Special log tokens: 'PLAYWRIGHT_MISSING', 'PLAYWRIGHT_NO_BROWSER'.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        on_log("PLAYWRIGHT_MISSING")
        return []

    if not page_url.startswith(("http://", "https://")):
        page_url = "https://" + page_url

    found = {}

    def _record(req):
        try:
            url = req.url
        except Exception:
            return
        if _is_m3u8(url) and url not in found:
            h = {}
            try:
                h = req.headers
            except Exception:
                pass
            item = {
                "url": url,
                "referer": h.get("referer", ""),
                "origin": h.get("origin", ""),
                "label": _label_for(url),
            }
            found[url] = item
            on_log(f"  ✓ captured: {_label_for(url)}")
            try:
                on_found(item)
            except Exception:
                pass

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=False,
                    args=["--autoplay-policy=no-user-gesture-required",
                          "--mute-audio"],
                )
            except Exception:
                on_log("PLAYWRIGHT_NO_BROWSER")
                return []

            ctx = browser.new_context(user_agent=USER_AGENT, viewport=None)
            ctx.on("page", lambda pg: pg.on("request", _record))
            main_page = ctx.new_page()
            main_page.on("request", _record)

            on_log("Browser open — click the server you want (TSN 4, FOX…).")
            try:
                main_page.goto(page_url, wait_until="domcontentloaded",
                               timeout=40000)
            except Exception as e:
                on_log(f"Navigation warning: {e}")

            deadline = time.time() + max_seconds
            while time.time() < deadline and not stop_event.is_set():
                # Reap ad-popup tabs so they don't bury the real window.
                for pg in list(ctx.pages):
                    if pg is not main_page:
                        try:
                            pg.close()
                        except Exception:
                            pass
                try:
                    main_page.wait_for_timeout(500)
                except Exception:
                    # main window was closed by the user
                    break

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        on_log(f"Scanner error: {e}")

    return list(found.values())


# ── Public API ────────────────────────────────────────────────────────────────

def find_streams(page_url: str, on_log=None, wait: int = 20) -> list[dict]:
    """
    Return a de-duplicated list of .m3u8 stream descriptors found on page_url.
    on_log(str) is an optional progress callback (safe to pass a GUI logger).
    """
    if on_log is None:
        on_log = lambda *_: None

    page_url = page_url.strip()
    if not page_url.startswith(("http://", "https://")):
        page_url = "https://" + page_url

    results = {}

    # If the user pasted a direct .m3u8, just return it.
    if _is_m3u8(page_url):
        return [{
            "url": page_url, "referer": "", "origin": "",
            "label": _label_for(page_url),
        }]

    for item in _scrape_html(page_url, on_log):
        results[item["url"]] = item

    # Always also run the deep scan — it catches what the scrape can't, and
    # picks up the all-important Referer header.
    for item in _sniff_browser(page_url, on_log, wait):
        results.setdefault(item["url"], item)

    return list(results.values())
