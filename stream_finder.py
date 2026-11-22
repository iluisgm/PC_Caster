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

A playlist is recognised by ANY of: ".m3u8" in the URL, an HLS content-type
(application/vnd.apple.mpegurl …) on the response, or a body that starts with
"#EXTM3U". Many sites hide the playlist behind an extension-less URL such as
https://host/web87 — and those usually autoplay, so there's no click to wait for.

Public entry point:  find_streams(page_url, on_log=None, wait=20) -> list[dict]
Each result dict: {"url", "referer", "origin", "label", "hls"}
"""

import re
import time
from urllib.parse import urlparse as _urlparse

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


def _clean_url(url: str) -> str:
    """Drop jQuery-style '_=<timestamp>' cache-busters from the query.

    Live players re-request the playlist every few seconds with a fresh
    '&_=…', which would otherwise show up as a brand-new stream each time.
    Other params are kept byte-for-byte so signed URLs stay valid.
    """
    base, sep, query = url.partition("?")
    if not sep:
        return url
    query = query.split("#", 1)[0]
    kept = [p for p in query.split("&") if p and p.split("=", 1)[0] != "_"]
    return base + ("?" + "&".join(kept) if kept else "")


# ── Network watcher shared by both browser scans ──────────────────────────────

# Body-sniff only small text-ish XHR/fetch responses whose type doesn't already
# say what they are — that's where disguised playlists hide.
_SNIFF_CTYPES = ("", "text/plain", "application/octet-stream",
                 "binary/octet-stream")
_SEGMENT_EXTS = (".ts", ".m4s", ".mp4", ".m4a", ".m4v", ".aac", ".key",
                 ".vtt", ".webm")
_SNIFF_MAX_BYTES = 256 * 1024
_SNIFF_MAX_COUNT = 80


def _referer_for(req) -> tuple[str, str]:
    """(referer, origin) the request was sent with.

    Falls back to the requesting frame's origin when Chromium doesn't report a
    Referer (some hosts check only Origin, and the proxy derives Origin from
    the Referer we hand it — an empty one means a guaranteed 403)."""
    h = {}
    try:
        h = req.headers or {}
    except Exception:
        pass
    referer = h.get("referer", "")
    origin = h.get("origin", "")
    if not referer and origin and origin != "null":
        referer = origin.rstrip("/") + "/"
    if not referer:
        try:
            fu = _urlparse(req.frame.url)
            if fu.scheme in ("http", "https") and fu.netloc:
                referer = f"{fu.scheme}://{fu.netloc}/"
        except Exception:
            pass
    return referer, origin


class _StreamCollector:
    """Watches every request/response in a browser context and records each
    HLS playlist once — whether or not its URL says '.m3u8'.

    on_new(item) fires for each newly-seen stream. Call drain() regularly from
    the thread that owns the browser to run the (deferred) body sniffs.
    """

    def __init__(self, on_new):
        self.found: dict[str, dict] = {}
        self._on_new = on_new
        self._checked: set[str] = set()
        self._pending: list = []
        self._sniffs = 0

    def attach(self, ctx):
        # Context-level events cover the page, every iframe and any popup tab.
        ctx.on("request", self._on_request)
        ctx.on("response", self._on_response)

    def _add(self, url: str, req):
        key = _clean_url(url)
        if key in self.found:
            return
        referer, origin = _referer_for(req)
        item = {"url": key, "referer": referer, "origin": origin,
                "label": _label_for(key), "hls": True}
        self.found[key] = item
        try:
            self._on_new(item)
        except Exception:
            pass

    def _on_request(self, req):
        try:
            url = req.url
        except Exception:
            return
        if _is_m3u8(url):
            self._add(url, req)

    def _on_response(self, resp):
        try:
            url = resp.url
            if _is_m3u8(url) or not resp.ok:
                return              # .m3u8 URLs are already caught on request
            hdrs = resp.headers
            ctype = (hdrs.get("content-type") or "").lower()
            req = resp.request
            rtype = req.resource_type
        except Exception:
            return

        if "mpegurl" in ctype:      # application/vnd.apple.mpegurl, x-mpegurl…
            self._add(url, req)
            return

        key = _clean_url(url)
        if (key in self.found or key in self._checked
                or rtype not in ("xhr", "fetch")
                or ctype.split(";", 1)[0].strip() not in _SNIFF_CTYPES
                or url.split("?", 1)[0].lower().endswith(_SEGMENT_EXTS)):
            return
        try:
            if int(hdrs.get("content-length") or 0) > _SNIFF_MAX_BYTES:
                return
        except ValueError:
            pass
        self._checked.add(key)
        self._pending.append(resp)

    def drain(self):
        """Read queued response bodies and keep the ones that are playlists."""
        while self._pending:
            resp = self._pending.pop(0)
            if self._sniffs >= _SNIFF_MAX_COUNT:
                self._pending.clear()
                return
            self._sniffs += 1
            try:
                head = resp.body()[:64]
            except Exception:
                continue
            if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
                self._add(resp.url, resp.request)


# ── Stream reliability ranking ────────────────────────────────────────────────
# Some hosts hand out short-lived, single-session tokens (the URL contains a
# "/secure/" gate and an opaque token). Streams from these die seconds after
# capture — they play for ~1s then 403, so we rank them BELOW open CDNs. Open
# CDNs (Rumble, CloudFront, Akamai…) serve plain segments that keep working, so
# we prefer them automatically when a scan returns several candidates.

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
                    "hls": True,
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
    col = _StreamCollector(
        lambda item: on_log(f"  ✓ found stream: {item['label']}"))
    found = col.found

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--autoplay-policy=no-user-gesture-required",
                          "--mute-audio"],
                )
            except Exception as e:
                # Browser binary not installed yet
                on_log("PLAYWRIGHT_NO_BROWSER")
                return []

            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )

            # Watch the main page, its iframes AND any ad/popup tabs.
            col.attach(ctx)
            page = ctx.new_page()

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
                col.drain()
                if found:
                    break           # autoplay already fired the playlist
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
            col.drain()

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

    def _new(item):
        on_log(f"  ✓ captured: {item['label']}")
        on_found(item)

    col = _StreamCollector(_new)
    found = col.found

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
            # Attach BEFORE navigating: autoplaying players request their
            # playlist within a second of load, with no click to wait for.
            col.attach(ctx)
            main_page = ctx.new_page()

            on_log("Browser open — if the video doesn't start by itself, "
                   "click the server / play button.")
            try:
                main_page.goto(page_url, wait_until="domcontentloaded",
                               timeout=40000)
            except Exception as e:
                on_log(f"Navigation warning: {e}")

            deadline = time.time() + max_seconds
            while time.time() < deadline and not stop_event.is_set():
                col.drain()
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
            "label": _label_for(page_url), "hls": True,
        }]

    for item in _scrape_html(page_url, on_log):
        results[item["url"]] = item

    # Always also run the deep scan — it catches what the scrape can't, and
    # picks up the all-important Referer header.
    for item in _sniff_browser(page_url, on_log, wait):
        results.setdefault(item["url"], item)

    return list(results.values())
