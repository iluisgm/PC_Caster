#!/usr/bin/env python3
"""
roku_deploy.py
Build the PC Caster channel into a .zip and sideload it onto a
Developer-Mode Roku, then launch it with a stream URL.

Sideload uses the Roku dev web server (http://<roku-ip>) with HTTP digest auth
(user 'rokudev', password = the one you set when enabling Developer Mode).
"""

import io
import os
import zipfile
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_DIR = os.path.join(HERE, "roku_receiver")
DEV_APP_ID = "dev"   # sideloaded channels always launch as 'dev'


def build_zip() -> bytes:
    """Zip the roku_receiver folder (manifest must be at the archive root)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(CHANNEL_DIR):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, CHANNEL_DIR)
                z.write(full, arc.replace(os.sep, "/"))
    return buf.getvalue()


def sideload(ip: str, password: str, zip_bytes: bytes | None = None) -> tuple[bool, str]:
    """
    Upload + install the channel to a Developer-Mode Roku.
    Returns (ok, message). 'user' is always 'rokudev'.
    """
    if zip_bytes is None:
        zip_bytes = build_zip()

    url = f"http://{ip}/plugin_install"
    auth = HTTPDigestAuth("rokudev", password)
    files = {"archive": ("channel.zip", zip_bytes, "application/zip")}
    data = {"mysubmit": "Install"}

    try:
        r = requests.post(url, auth=auth, files=files, data=data, timeout=30)
    except Exception as e:
        return False, f"Could not reach the Roku dev server at http://{ip} ({e}). " \
                      "Is Developer Mode enabled and the IP correct?"

    if r.status_code == 401:
        return False, "Dev password rejected (HTTP 401). Check the password you " \
                      "set when enabling Developer Mode."
    if r.status_code != 200:
        return False, f"Sideload failed (HTTP {r.status_code})."

    txt = r.text or ""
    low = txt.lower()
    if "success" in low or "received" in low or "identical" in low or "replac" in low:
        return True, "Channel installed on the Roku."
    if "failed" in low or "error" in low:
        # Pull a short hint out of the HTML if present.
        return False, "Roku reported an install error. " \
                      "Make sure Developer Mode is fully set up (SDK agreement accepted)."
    return True, "Upload accepted by the Roku."


def is_installed(ip: str, password: str) -> bool:
    """Best-effort check that a dev channel is present."""
    try:
        r = requests.get(f"http://{ip}:8060/query/apps", timeout=4)
        return 'id="dev"' in (r.text or "")
    except Exception:
        return False


def launch(ip: str, stream_url: str, fmt: str = "hls") -> tuple[bool, str]:
    """Launch the sideloaded channel and tell it what to play."""
    enc = quote(stream_url, safe="")
    ecp = (f"http://{ip}:8060/launch/{DEV_APP_ID}"
           f"?contentId={enc}&mediaType={fmt}")
    try:
        r = requests.post(ecp, timeout=6)
        if r.status_code == 200:
            return True, "Launched PC Caster on the TV."
        return False, f"Roku ECP returned HTTP {r.status_code}."
    except Exception as e:
        return False, str(e)


def input_play(ip: str, stream_url: str, fmt: str = "hls") -> tuple[bool, str]:
    """Send a new URL to the channel while it's already running (via /input)."""
    enc = quote(stream_url, safe="")
    ecp = f"http://{ip}:8060/input?contentId={enc}&mediaType={fmt}"
    try:
        r = requests.post(ecp, timeout=6)
        return (r.status_code == 200), f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)
