#!/usr/bin/env python3
"""
hls_proxy.py
A tiny local HLS proxy so header-locked .m3u8 streams play on devices (Roku,
Fire TV) that cannot send a custom Referer/Origin.

How it works
------------
The Roku plays  http://<PC-LAN-IP>:<port>/p?u=<b64 url>&r=<b64 referer>
For every request the proxy re-fetches the real resource from the CDN WITH the
Referer header injected. If the resource is an .m3u8 playlist, it rewrites every
child URI (variant playlists, segments, keys) to point back through the proxy so
those requests carry the Referer too. Binary segments are streamed straight
through. The Roku therefore never needs to send any header itself.

Public API
----------
    proxy = HlsProxy(); base = proxy.start(target_ip="192.168.1.20")
    url   = proxy.url_for(real_m3u8, referer)   # cast THIS to the device
"""

import base64
import logging
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse, parse_qs, quote, unquote

# curl_cffi impersonates a real Chrome TLS (JA3) fingerprint. The stream CDNs
# block plain-Python TLS with 403, so this is required — not optional.
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IMPERSONATE = "chrome"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode()).decode()


RULE_NAME = "PC Caster HLS Proxy"


def ensure_firewall_rule(port: int) -> bool:
    """
    Make sure inbound TCP on `port` is allowed so the TV can reach the proxy.
    Returns True if a rule already exists. If not, triggers ONE UAC prompt to
    add it (best-effort). Windows-only; no-op elsewhere.
    """
    import subprocess
    try:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={RULE_NAME}"],
            capture_output=True, text=True, timeout=8,
        )
        if RULE_NAME in (check.stdout or ""):
            return True
    except Exception:
        return False

    # Add the rule with an elevated one-shot (pops a UAC dialog).
    add_args = (
        f"advfirewall firewall add rule name='{RULE_NAME}' "
        f"dir=in action=allow protocol=TCP localport={port} profile=private"
    )
    ps = f"Start-Process netsh -ArgumentList \"{add_args}\" -Verb RunAs -WindowStyle Hidden"
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    return False


def lan_ip_towards(target_ip: str) -> str:
    """Pick the local IP on the interface that can reach target_ip."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# Roku hardware decoders reject HE-AAC (SBR) audio in TS segments with
# "decoder:pump:Unsupported AAC stream", even though VLC plays it fine.
# When ffmpeg is available we probe the first segment per host and, if the
# audio is HE-AAC, re-encode just the audio to AAC-LC (video is copied).
_FFMPEG  = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
_audio_fix: dict[str, bool] = {}   # netloc -> segment audio needs transcode


def _probe_needs_audio_fix(data: bytes) -> bool:
    if not (_FFMPEG and _FFPROBE):
        return False
    try:
        p = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=profile", "-of", "csv=p=0",
             "-i", "pipe:0"],
            input=data, capture_output=True, timeout=15)
        prof = (p.stdout or b"").decode(errors="ignore").strip().lower()
        return "he-aac" in prof or "sbr" in prof
    except Exception:
        return False


def _transcode_audio_to_lc(data: bytes) -> bytes:
    """Re-encode segment audio to AAC-LC, copy video, keep timestamps."""
    try:
        p = subprocess.run(
            [_FFMPEG, "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?",
             "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "128k",
             "-copyts", "-muxpreload", "0", "-muxdelay", "0",
             "-f", "mpegts", "pipe:1"],
            input=data, capture_output=True, timeout=20)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    return data


# Some hosts (strmd.st) put date-encoded values like 20260706098359 in
# #EXT-X-MEDIA-SEQUENCE. That overflows Roku's 32-bit playlist parser
# ("reader pick stream error: parsing failed"), so we shift it down to a
# small number. The offset is remembered per playlist URL so the sequence
# still advances consistently across live reloads.
_seq_bases: dict[tuple[str, str], int] = {}
_SEQ_MAX = 2**31 - 1


def _safe_seq(tag: str, value: int, playlist_url: str) -> int:
    if value <= _SEQ_MAX:
        return value
    base = _seq_bases.setdefault((tag, playlist_url), value - 1000)
    return max(0, value - base)


def _rewrite_playlist(text: str, base_url: str, referer: str,
                      proxy_origin: str) -> str:
    """Rewrite every URI in an m3u8 so it routes back through this proxy."""
    rb = quote(_b64(referer), safe="")

    def to_proxy(uri: str) -> str:
        absu = urljoin(base_url, uri.strip())
        return f"{proxy_origin}/p.m3u8?u={quote(_b64(absu), safe='')}&r={rb}"

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            for tag in ("#EXT-X-MEDIA-SEQUENCE:",
                        "#EXT-X-DISCONTINUITY-SEQUENCE:"):
                if s.startswith(tag):
                    try:
                        n = int(s[len(tag):].strip())
                        s = line = f"{tag}{_safe_seq(tag, n, base_url)}"
                    except ValueError:
                        pass
                    break
            # Tags that embed a URI="..." attribute (keys, media, maps).
            if 'URI="' in s:
                pre, rest = s.split('URI="', 1)
                inner, post = rest.split('"', 1)
                out.append(f'{pre}URI="{to_proxy(inner)}"{post}')
            else:
                out.append(line)
            continue
        # Bare line = a segment or child playlist URI.
        out.append(to_proxy(s))
    return "\n".join(out) + "\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        # Record activity so the UI can show "serving" in real time.
        try:
            self.server.requests_served += 1          # type: ignore[attr-defined]
            self.server.last_activity = time.time()   # type: ignore[attr-defined]
        except Exception:
            pass
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/p"):
            self.send_error(404)
            return
        q = parse_qs(parsed.query)
        try:
            target = _unb64(q["u"][0])
            referer = _unb64(q["r"][0]) if q.get("r") else ""
        except Exception:
            self.send_error(400, "bad params")
            return

        headers = {"User-Agent": UA, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
            pr = urlparse(referer)
            headers["Origin"] = f"{pr.scheme}://{pr.netloc}"

        try:
            r = creq.get(target, headers=headers, impersonate=IMPERSONATE,
                         timeout=25)
        except Exception as e:
            logging.getLogger("pccaster").warning(
                "proxy upstream error: %s (%s)", e, target[:90])
            self.send_error(502, f"upstream error: {e}")
            return

        if r.status_code != 200:
            # Non-200 from the CDN is the #1 cause of playback stopping
            # (usually token expiry -> 403). Log it so we can see why.
            logging.getLogger("pccaster").warning(
                "proxy upstream HTTP %s for %s", r.status_code, target[:110])
            self.send_response(r.status_code)
            self.end_headers()
            return

        body = r.content
        ctype = r.headers.get("Content-Type", "").lower()
        is_playlist = (body[:7] == b"#EXTM3U"
                       or "mpegurl" in ctype
                       or target.split("?", 1)[0].lower().endswith(".m3u8"))

        proxy_origin = self.server.public_origin  # type: ignore[attr-defined]

        if is_playlist:
            try:
                text = body.decode("utf-8", "ignore")
            except Exception:
                text = body.decode("latin-1", "ignore")
            rewritten = _rewrite_playlist(text, target, referer, proxy_origin)
            data = rewritten.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        else:
            data = body
            path = target.split("?", 1)[0].lower()
            if path.endswith(".key") or "/key" in path:
                out_ctype = "application/octet-stream"   # HLS AES key
            else:
                # Segments are MPEG-TS even when the CDN mislabels them
                # (these hosts often serve .ts as image/png to dodge filters).
                out_ctype = "video/mp2t"
                host = urlparse(target).netloc
                fix = _audio_fix.get(host)
                if fix is None:
                    fix = _probe_needs_audio_fix(data)
                    _audio_fix[host] = fix
                    if fix:
                        logging.getLogger("pccaster").info(
                            "HE-AAC audio detected on %s — re-encoding "
                            "segment audio to AAC-LC for the Roku", host)
                if fix:
                    data = _transcode_audio_to_lc(data)
            self.send_response(200)
            self.send_header("Content-Type", out_ctype)

        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


class HlsProxy:
    def __init__(self, port: int = 8011):
        self.port = port
        self._srv = None
        self._thread = None
        self.public_origin = ""

    def start(self, target_ip: str = "8.8.8.8") -> str:
        """Start the proxy; returns the base origin reachable by target_ip."""
        if self._srv:
            return self.public_origin
        ip = lan_ip_towards(target_ip)
        self._srv = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        self.public_origin = f"http://{ip}:{self.port}"
        self._srv.public_origin = self.public_origin  # type: ignore[attr-defined]
        self._srv.requests_served = 0                 # type: ignore[attr-defined]
        self._srv.last_activity = 0.0                 # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self.public_origin

    @property
    def running(self) -> bool:
        return self._srv is not None

    @property
    def requests_served(self) -> int:
        return getattr(self._srv, "requests_served", 0) if self._srv else 0

    def seconds_since_activity(self) -> float:
        """Seconds since the last request, or a large number if none/idle."""
        if not self._srv:
            return 1e9
        last = getattr(self._srv, "last_activity", 0.0)
        if not last:
            return 1e9
        return time.time() - last

    def url_for(self, real_m3u8: str, referer: str) -> str:
        return (f"{self.public_origin}/p.m3u8"
                f"?u={quote(_b64(real_m3u8), safe='')}"
                f"&r={quote(_b64(referer), safe='')}")

    def stop(self):
        if self._srv:
            try:
                self._srv.shutdown()
            except Exception:
                pass
            self._srv = None
