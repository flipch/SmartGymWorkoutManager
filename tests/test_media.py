"""/media_proxy and the media cache: only vendor CDN media, hashed cache names, no redirects, capped, atomic."""
import base64
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gym_app  # noqa: E402
import media  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
GOOD = "https://us-image.speediance.com/exercise/bench.png"
CLOUDFRONT = "https://d1rwzqjevxdih5.cloudfront.net/v/curl.mp4"


class FakeResp:
    def __init__(self, status=200, chunks=(b"x" * 10,), headers=None):
        self.status_code, self._chunks, self.headers, self.closed = status, chunks, headers or {}, False

    def iter_content(self, chunk_size=0):
        yield from self._chunks

    def close(self):
        self.closed = True


class UrlPolicy(unittest.TestCase):
    def test_allowed(self):
        for url in (GOOD, CLOUDFRONT, "https://US-IMAGE.speediance.com/a/B.PNG", "https://us-image.speediance.com:443/a.jpg"):
            self.assertTrue(media.is_allowed_url(url), url)

    def test_refused(self):
        for url in ("http://us-image.speediance.com/a.png", "https://169.254.169.254/latest/meta-data/x.png",
                    "https://127.0.0.1/a.png", "http://smart-gym-mcp:5002/a.png", "https://evil.example/a.png",
                    "https://speediance.com.evil.example/a.png", "https://evilspeediance.com/a.png",
                    "https://user:pw@us-image.speediance.com/a.png", "https://us-image.speediance.com:8443/a.png",
                    "file:///etc/passwd", "gopher://us-image.speediance.com/a.png", "https://us-image.speediance.com/noext",
                    "https://us-image.speediance.com/a.html", "https://us-image.speediance.com/a.svg", "", None, 5,
                    "https://us-image.speediance.com:notaport/a.png", "//us-image.speediance.com/a.png"):
            self.assertFalse(media.is_allowed_url(url), repr(url))

    def test_host_list_is_configurable_and_never_widens_the_scheme(self):
        with mock.patch.dict(os.environ, {"GYM_MEDIA_HOSTS": "media.lan.example"}):
            self.assertTrue(media.is_allowed_url("https://media.lan.example/a.png"))
            self.assertFalse(media.is_allowed_url(GOOD))
            self.assertFalse(media.is_allowed_url("http://media.lan.example/a.png"))


class CachePaths(unittest.TestCase):
    def test_same_basename_on_different_urls_never_share_a_cache_file(self):
        a, _ = media.cache_path("/c", "https://us-image.speediance.com/real/poison.mp4")
        b, _ = media.cache_path("/c", "https://d1rwzqjevxdih5.cloudfront.net/other/poison.mp4")
        c, _ = media.cache_path("/c", "https://us-image.speediance.com/real/poison.mp4?v=2")
        self.assertEqual(len({a, b, c}), 3)
        self.assertEqual(a, media.cache_path("/c", "https://us-image.speediance.com/real/poison.mp4")[0])
        self.assertTrue(a.endswith(".mp4") and "/videos/" in a)

    def test_disallowed_urls_have_no_cache_path(self):
        self.assertEqual(media.cache_path("/c", "https://evil.example/a.png"), (None, None))


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "images", "f.png")

    def files(self):
        return sorted(str(p.relative_to(self.dir)) for p in Path(self.dir).rglob("*") if p.is_file())

    def run_dl(self, resp, url=GOOD):
        return media.download(url, self.path, "images", get=lambda *a, **k: resp)

    def test_success_is_atomic_and_leaves_no_part_file(self):
        resp = FakeResp(headers={"Content-Type": "image/png"})
        self.run_dl(resp)
        self.assertEqual(self.files(), ["images/f.png"])
        self.assertTrue(resp.closed)

    def test_redirects_are_not_followed(self):
        seen = {}

        def get(url, **kw):
            seen.update(kw)
            return FakeResp(status=302, headers={"Location": "http://127.0.0.1/admin"})

        with self.assertRaises(media.MediaError):
            media.download(GOOD, self.path, "images", get=get)
        self.assertIs(seen.get("allow_redirects"), False)
        self.assertEqual(self.files(), [])

    def test_refusals_leave_nothing_behind(self):
        cases = [FakeResp(status=404), FakeResp(headers={"Content-Type": "text/html"}), FakeResp(chunks=()),
                 FakeResp(headers={"Content-Length": str(media.MAX_BYTES + 1)})]
        for resp in cases:
            with self.assertRaises(media.MediaError):
                self.run_dl(resp)
            self.assertEqual(self.files(), [])

    def test_a_body_larger_than_the_cap_is_aborted_without_a_partial_file(self):
        with mock.patch.object(media, "MAX_BYTES", 100):
            with self.assertRaises(media.MediaError):
                self.run_dl(FakeResp(chunks=(b"x" * 60, b"x" * 60)))
        self.assertEqual(self.files(), [])

    def test_a_connection_dropped_mid_stream_leaves_no_partial_file_and_no_url_in_the_error(self):
        def chunks():
            yield b"x" * 10
            raise requests.ConnectionError("reset by peer for https://us-image.speediance.com/exercise/bench.png?sig=SECRET")

        class Dropping(FakeResp):
            def iter_content(self, chunk_size=0):
                return chunks()

        with self.assertRaises(media.MediaError) as ctx:
            self.run_dl(Dropping())
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertEqual(self.files(), [])

    def test_not_allowed_urls_are_refused_before_any_request(self):
        def boom(*a, **k):
            raise AssertionError("network used")

        with self.assertRaises(media.MediaError):
            media.download("http://169.254.169.254/a.png", self.path, "images", get=boom)


class ProxyRoute(unittest.TestCase):
    def setUp(self):
        self.c = gym_app.app.test_client()
        self.root = tempfile.mkdtemp()
        patcher = mock.patch.object(gym_app, "CACHE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_internal_and_foreign_urls_are_refused_without_any_fetch(self):
        with mock.patch("requests.get", side_effect=AssertionError("must not fetch")):
            for url in ("http://169.254.169.254/latest/meta-data/x.png", "https://evil.example/a.png", "file:///etc/passwd",
                        "http://smart-gym-mcp:5002/x.mp4", "https://us-image.speediance.com/page.html"):
                resp = self.c.get("/media_proxy", query_string={"url": url}, headers=BASIC)
                self.assertEqual(resp.status_code, 400, url)
                self.assertNotIn("Location", resp.headers)

    def test_failure_is_a_502_never_a_redirect_to_the_callers_url(self):
        with mock.patch("media.requests.get", side_effect=requests.ConnectionError("down")):
            resp = self.c.get("/media_proxy", query_string={"url": GOOD}, headers=BASIC)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("Location", resp.headers)

    def test_allowed_media_is_cached_served_with_safe_headers_and_not_refetched(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            return FakeResp(chunks=(b"PNGDATA",), headers={"Content-Type": "image/png"})

        with mock.patch("media.requests.get", side_effect=get):
            first = self.c.get("/media_proxy", query_string={"url": GOOD}, headers=BASIC)
            second = self.c.get("/media_proxy", query_string={"url": GOOD}, headers=BASIC)
        self.assertEqual((first.status_code, first.data, second.data), (200, b"PNGDATA", b"PNGDATA"))
        self.assertEqual(calls, [GOOD], "second request must be a cache hit")
        self.assertEqual(first.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", first.headers["Content-Security-Policy"])
        self.assertTrue(first.headers["Content-Type"].startswith("image/"))

    def test_a_poisoned_url_with_the_same_filename_does_not_replace_the_real_media(self):
        real = "https://us-image.speediance.com/real/poison.png"
        fake = "https://d1rwzqjevxdih5.cloudfront.net/other/poison.png"

        def get(url, **kw):
            return FakeResp(chunks=(b"REAL" if url == real else b"OTHER",), headers={"Content-Type": "image/png"})

        with mock.patch("media.requests.get", side_effect=get):
            other = self.c.get("/media_proxy", query_string={"url": fake}, headers=BASIC)
            genuine = self.c.get("/media_proxy", query_string={"url": real}, headers=BASIC)
        self.assertEqual((other.data, genuine.data), (b"OTHER", b"REAL"))

    def test_template_filter_never_routes_a_foreign_url_through_the_proxy(self):
        with gym_app.app.test_request_context():
            self.assertEqual(gym_app.local_cache_filter("https://evil.example/a.png", force=True), "https://evil.example/a.png")
            self.assertTrue(gym_app.local_cache_filter(GOOD, force=True).startswith("/media_proxy?"))


class PreloadAndRefresh(unittest.TestCase):
    def setUp(self):
        self.c = gym_app.app.test_client()

    def test_state_changing_get_routes_are_gone(self):
        for path in ("/settings/preload", "/library/refresh"):
            self.assertEqual(self.c.get(path, headers=BASIC).status_code, 405, path)

    def test_cross_site_post_is_refused(self):
        for path in ("/settings/preload", "/library/refresh"):
            resp = self.c.post(path, headers={**BASIC, "Sec-Fetch-Site": "cross-site"})
            self.assertEqual((path, resp.status_code), (path, 403))

    def test_only_one_preload_runs_at_a_time(self):
        creds = {"token": "x", "user_id": "1", "unit": 0}
        self.assertTrue(gym_app._PRELOAD_LOCK.acquire(blocking=False))
        try:
            with mock.patch.object(gym_app.client, "credentials", creds):
                resp = self.c.post("/settings/preload", headers=BASIC)
            self.assertEqual(resp.status_code, 409)
        finally:
            gym_app._PRELOAD_LOCK.release()

    def test_templates_use_post_forms_not_links(self):
        root = Path(__file__).parent.parent / "templates"
        for name, action in (("library.html", "/library/refresh"), ("settings.html", "/settings/preload")):
            text = (root / name).read_text()
            self.assertIn(f'<form method="post" action="{action}"', text)
            self.assertNotIn(f'href="{action}"', text)


if __name__ == "__main__":
    unittest.main()
