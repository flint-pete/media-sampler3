# Hanwha (SUNAPI) native-still acquisition tests for media-sampler3 (acquire.py).
#
# A real local HTTP server implements RFC 7616 Digest (MD5, qop=auth), the scheme a
# Hanwha XNP-6400RW answers with (verified on H03B: `WWW-Authenticate: Digest
# realm="iPolis_<MAC>", qop="auth"`). No camera needed.
#
# Run:  python3 -m pytest tests/test_acquire_hanwha.py -q

import hashlib
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire  # noqa: E402

VALID_JPEG = acquire.JPEG_SOI + b"\x00\x10JFIF hanwha body" + acquire.JPEG_EOI
REALM = "iPolis_E4-30-22-00-00-00"
NONCE = "389a97de:43a6d57835490301f2e02f6db0791b02"
USER, PASSWORD = "camuser", "S3cr!t&pw"


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class _Camera:
    """State shared with the handler: what to serve and what was seen."""
    def __init__(self):
        self.body = VALID_JPEG
        self.status = 200
        self.hits = 0          # every request, authenticated or not
        self.auth_ok = 0       # requests that passed Digest
        self.paths = []


def _make_handler(cam):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _challenge(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate",
                             f'Digest realm="{REALM}", charset="UTF-8", '
                             f'nonce="{NONCE}", qop="auth"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            cam.hits += 1
            cam.paths.append(self.path)
            h = self.headers.get("Authorization", "")
            if not h.startswith("Digest "):
                return self._challenge()
            f = dict(re.findall(r'(\w+)="?([^",]+)"?', h[7:]))
            ha1 = _md5(f"{USER}:{REALM}:{PASSWORD}")
            ha2 = _md5(f"GET:{f.get('uri', '')}")
            want = _md5(f"{ha1}:{NONCE}:{f.get('nc')}:{f.get('cnonce')}:auth:{ha2}")
            if f.get("username") != USER or f.get("response") != want:
                return self._challenge()
            cam.auth_ok += 1
            self.send_response(cam.status)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(cam.body)))
            self.end_headers()
            self.wfile.write(cam.body)
    return H


@pytest.fixture
def camera():
    cam = _Camera()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cam))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    cam.port = srv.server_address[1]
    yield cam
    srv.shutdown()
    srv.server_close()


def _src(cam, user=USER, password=PASSWORD, **kw):
    return acquire.build_still_source("hanwha", "127.0.0.1", cam.port, user, password, **kw)


# --- happy path ---------------------------------------------------------------

def test_hanwha_digest_fetch_returns_raw_jpeg(camera):
    data = acquire.fetch_raw_still(_src(camera), 5)
    assert data == VALID_JPEG                      # raw bytes, untouched
    assert camera.auth_ok == 1
    assert camera.paths[-1].startswith("/stw-cgi/video.cgi?msubmenu=snapshot&action=view")


def test_hanwha_url_carries_channel_and_optional_profile(camera):
    s = _src(camera, channel=0)
    assert "Channel=0" in s.url and "Profile" not in s.url
    s2 = _src(camera, channel=1, profile=2)
    assert "Channel=1" in s2.url and "Profile=2" in s2.url
    acquire.fetch_raw_still(s2, 5)
    assert "Profile=2" in camera.paths[-1]


def test_hanwha_url_and_repr_never_contain_password(camera):
    s = _src(camera)
    for text in (s.url, repr(s), str(s), s.redacted()):
        assert PASSWORD not in text and "S3cr" not in text


def test_hanwha_password_never_logged(camera, caplog):
    caplog.set_level("DEBUG")
    acquire.fetch_raw_still(_src(camera), 5)
    assert PASSWORD not in caplog.text


# --- failures -----------------------------------------------------------------

def test_hanwha_wrong_password_is_auth_error(camera):
    with pytest.raises(acquire.CaptureAuthError):
        acquire.fetch_raw_still(_src(camera, password="wrong"), 5)
    assert camera.auth_ok == 0
    # exactly one login attempt: the unauthenticated challenge + ONE digest try.
    # (stdlib's handler would retry the bad password 5 more times -> lockout risk)
    assert camera.hits == 2


def test_auth_error_is_a_capture_error_for_fail_soft_callers():
    assert issubclass(acquire.CaptureAuthError, acquire.CaptureError)


def test_hanwha_auth_backoff_stops_hammering_the_camera(camera):
    # Hanwha locks accounts after repeated bad logins: after one auth failure the
    # source must NOT contact the camera again until its backoff expires.
    clock = [1000.0]
    s = _src(camera, password="wrong")
    s._now = lambda: clock[0]
    with pytest.raises(acquire.CaptureAuthError):
        acquire.fetch_raw_still(s, 5)
    hits_after_first = camera.hits
    for _ in range(5):
        with pytest.raises(acquire.CaptureAuthError, match="backing off"):
            acquire.fetch_raw_still(s, 5)
    assert camera.hits == hits_after_first            # no network while backing off
    clock[0] += acquire.HANWHA_AUTH_BACKOFF_START_S + 1
    with pytest.raises(acquire.CaptureAuthError):
        acquire.fetch_raw_still(s, 5)                 # tries again after the window
    assert camera.hits > hits_after_first


def test_hanwha_backoff_doubles_and_caps():
    s = acquire.HanwhaStill("h", 80, "u", "p")
    waits = []
    for _ in range(10):
        s._note_auth_failure()
        waits.append(s._backoff_s)
    assert waits[0] == acquire.HANWHA_AUTH_BACKOFF_START_S
    assert waits[1] == 2 * waits[0]
    assert max(waits) == acquire.HANWHA_AUTH_BACKOFF_MAX_S


def test_hanwha_success_resets_backoff(camera):
    s = _src(camera)
    s._note_auth_failure()
    s._blocked_until = 0           # pretend the window passed
    acquire.fetch_raw_still(s, 5)
    assert s._backoff_s == 0


def test_hanwha_non_jpeg_body_is_capture_error(camera):
    camera.body = b"<html>error</html>"
    with pytest.raises(acquire.CaptureError, match="did not return a JPEG"):
        acquire.fetch_raw_still(_src(camera), 5)


def test_hanwha_http_error_status_is_capture_error(camera):
    camera.status = 500
    with pytest.raises(acquire.CaptureError):
        acquire.fetch_raw_still(_src(camera), 5)


def test_hanwha_unreachable_is_capture_error():
    s = acquire.build_still_source("hanwha", "127.0.0.1", 9, USER, PASSWORD)  # discard port
    with pytest.raises(acquire.CaptureError):
        acquire.fetch_raw_still(s, 2)


# --- builder ------------------------------------------------------------------

def test_build_still_source_reolink_is_unchanged_url_string():
    u = acquire.build_still_source("reolink", "10.0.0.5", 80, "u", "p")
    assert isinstance(u, str) and "cmd=Snap" in u


@pytest.mark.parametrize("args", [
    ("hanwha", "", 80, "u", "p"),        # no host
    ("hanwha", "h", 80, "", "p"),        # no user
    ("hanwha", "h", 80, "u", None),      # no password
    ("mobotix", "h", 80, "u", "p"),      # unknown vendor
])
def test_build_still_source_rejects_bad_config(args):
    with pytest.raises(ValueError):
        acquire.build_still_source(*args)
