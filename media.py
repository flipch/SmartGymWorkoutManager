"""Safe media caching: only vendor CDN media, hashed cache names, no redirects, size-capped, atomic writes.

Replaces a proxy that fetched ANY url (SSRF into the LAN / cloud metadata), keyed its cache by the URL's basename
(so a request for http://evil/x/poison.mp4 poisoned https://cdn.speediance.com/real/poison.mp4), followed redirects
and wrote partial files, and redirected the browser to the caller's URL on failure (open redirect).
"""
import hashlib
import os
import re
from urllib.parse import urlparse

import requests
from urllib3.util import parse_url

_AUTHORITY = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::443)?", re.IGNORECASE)

# Hosts we are willing to fetch media from (exact names, or a leading-dot suffix). Override with GYM_MEDIA_HOSTS.
DEFAULT_HOSTS = (".speediance.com", "d1rwzqjevxdih5.cloudfront.net")
MAX_BYTES = 150 * 1024 * 1024
SUBFOLDERS = {
    ".jpg": "images", ".jpeg": "images", ".png": "images", ".gif": "images", ".webp": "images",
    ".mp4": "videos", ".mov": "videos", ".webm": "videos",
    ".mp3": "audio", ".wav": "audio", ".aac": "audio",
}
CONTENT_PREFIX = {"images": "image/", "videos": "video/", "audio": "audio/"}


def allowed_hosts() -> tuple[str, ...]:
    extra = os.environ.get("GYM_MEDIA_HOSTS")
    return tuple(h.strip().lower() for h in extra.split(",") if h.strip()) if extra else DEFAULT_HOSTS


def is_allowed_url(url) -> bool:
    """https only, a vendor media host, a known media extension, no credentials in the URL."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in url):
        return False  # a backslash, space or control character is read differently by different URL parsers
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        other = parse_url(url)  # the parser requests/urllib3 will actually use to connect
    except Exception:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in (None, 443):
        return False
    if not _AUTHORITY.fullmatch(parsed.netloc) or (other.host or "").lower() != host or other.port not in (None, 443) or other.auth:
        return False
    if not any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in allowed_hosts()):
        return False
    return os.path.splitext(parsed.path)[1].lower() in SUBFOLDERS


def cache_path(cache_root: str, url: str):
    """(absolute path, subfolder) for an allowed url, keyed by a hash of the WHOLE url; (None, None) otherwise."""
    if not is_allowed_url(url):
        return None, None
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    sub = SUBFOLDERS[ext]
    return os.path.join(cache_root, sub, hashlib.sha256(url.encode()).hexdigest()[:32] + ext), sub


class MediaError(Exception):
    """A media download was refused or failed; the message is safe to show (no URL query, no headers)."""


def download(url: str, local_path: str, sub: str, *, timeout: float = 15, get=None) -> None:
    """Fetch `url` into `local_path` atomically. Raises MediaError; never leaves a partial file behind."""
    if not is_allowed_url(url):
        raise MediaError("url is not an allowed media url")
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    tmp = local_path + ".part"
    try:
        resp = (get or requests.get)(url, stream=True, timeout=timeout, allow_redirects=False)  # resolved at call time
        try:
            if resp.status_code != 200:
                raise MediaError(f"upstream status {resp.status_code}")
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith(CONTENT_PREFIX[sub]) and not ctype.startswith("application/octet-stream"):
                raise MediaError(f"unexpected content type {ctype.split(';')[0]}")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                raise MediaError("file too large")
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise MediaError("file too large")
                    fh.write(chunk)
            if written == 0:
                raise MediaError("empty response")
            os.replace(tmp, local_path)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except MediaError:
        raise
    except requests.RequestException as err:
        raise MediaError(f"download failed ({type(err).__name__})") from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
