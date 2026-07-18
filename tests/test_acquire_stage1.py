# Stage 1 tests for image-sampler2 acquisition (acquire.py).
#
# PURE tests: no camera, no real network. HTTP is mocked; the filesystem uses
# pytest's tmp_path. Verifies raw-bytes fetch, JPEG validation, atomic save,
# timeout/error mapping, and URL building/redaction. Section refs -> design doc.
#
# Run:  python3 -m pytest tests/ -q

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire  # noqa: E402


# A minimal but valid JPEG: SOI ... EOI.
VALID_JPEG = acquire.JPEG_SOI + b"\x00\x10JFIF fake body bytes" + acquire.JPEG_EOI
NOT_JPEG = b'[{"cmd":"Snap","code":1,"error":{"rspCode":-7}}]'  # reolink auth-fail blob


# ---------------------------------------------------------------------------
# looks_like_jpeg
# ---------------------------------------------------------------------------

def test_looks_like_jpeg_true():
    assert acquire.looks_like_jpeg(VALID_JPEG)


@pytest.mark.parametrize("bad", [b"", b"\xff\xd8", NOT_JPEG, b"\xff\xd8 no eoi", None, "str"])
def test_looks_like_jpeg_false(bad):
    assert not acquire.looks_like_jpeg(bad)


# ---------------------------------------------------------------------------
# build_reolink_snap_url
# ---------------------------------------------------------------------------

def test_build_url_has_snap_and_auth():
    url = acquire.build_reolink_snap_url("10.107.0.221", 10000, "admin", "pw123")
    assert "cmd=Snap" in url
    assert "user=admin" in url
    assert "password=pw123" in url
    assert url.startswith("http://10.107.0.221:10000/cgi-bin/api.cgi")


def test_build_url_does_not_percent_encode_password_punctuation():
    # Reolink compares password literally; '!' must stay '!', not '%21'.
    url = acquire.build_reolink_snap_url("h", 1, "admin", "p@ss!word")
    assert "password=p@ss!word" in url
    assert "%21" not in url and "%40" not in url


def test_build_url_requires_host_and_creds():
    with pytest.raises(ValueError):
        acquire.build_reolink_snap_url("", 1, "admin", "pw")
    with pytest.raises(ValueError):
        acquire.build_reolink_snap_url("h", 1, "", "pw")
    with pytest.raises(ValueError):
        acquire.build_reolink_snap_url("h", 1, "admin", None)


def test_redact_hides_password():
    url = acquire.build_reolink_snap_url("h", 1, "admin", "secretpw")
    red = acquire._redact(url)
    assert "secretpw" not in red
    assert "password=***" in red
    assert "user=admin" in red  # non-secret fields kept


# ---------------------------------------------------------------------------
# fetch_raw_still (HTTP mocked)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status
    def read(self):
        return self._body
    def getcode(self):
        return self.status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_fetch_returns_raw_bytes_untouched():
    with mock.patch("acquire.urllib.request.urlopen", return_value=_FakeResp(VALID_JPEG)):
        data = acquire.fetch_raw_still("http://cam/snap", timeout_s=5)
    assert data == VALID_JPEG  # byte-identical, no decode/re-encode


def test_fetch_rejects_non_jpeg_body():
    with mock.patch("acquire.urllib.request.urlopen", return_value=_FakeResp(NOT_JPEG)):
        with pytest.raises(acquire.CaptureError, match="did not return a JPEG"):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=5)


def test_fetch_rejects_empty_body():
    with mock.patch("acquire.urllib.request.urlopen", return_value=_FakeResp(b"")):
        with pytest.raises(acquire.CaptureError, match="empty body"):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=5)


def test_fetch_non_200_is_error():
    with mock.patch("acquire.urllib.request.urlopen", return_value=_FakeResp(VALID_JPEG, status=401)):
        with pytest.raises(acquire.CaptureError, match="HTTP 401"):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=5)


def test_fetch_timeout_maps_to_capture_timeout():
    with mock.patch("acquire.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(acquire.CaptureTimeout, match="timed out"):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=2)


def test_fetch_oserror_timeout_maps_to_capture_timeout():
    with mock.patch("acquire.urllib.request.urlopen", side_effect=OSError("connection timed out")):
        with pytest.raises(acquire.CaptureTimeout):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=2)


def test_fetch_generic_error_maps_to_capture_error():
    with mock.patch("acquire.urllib.request.urlopen", side_effect=OSError("connection refused")):
        with pytest.raises(acquire.CaptureError, match="capture failed"):
            acquire.fetch_raw_still("http://cam/snap", timeout_s=2)


def test_fetch_rejects_nonpositive_timeout():
    with pytest.raises(ValueError):
        acquire.fetch_raw_still("http://cam/snap", timeout_s=0)


# ---------------------------------------------------------------------------
# save_bytes_atomic
# ---------------------------------------------------------------------------

def test_save_writes_exact_bytes(tmp_path):
    dst = str(tmp_path / "sub" / "out.jpg")
    acquire.save_bytes_atomic(VALID_JPEG, dst)
    assert os.path.isfile(dst)
    with open(dst, "rb") as f:
        assert f.read() == VALID_JPEG  # byte-identical


def test_save_leaves_no_tmp_on_success(tmp_path):
    dst = str(tmp_path / "out.jpg")
    acquire.save_bytes_atomic(VALID_JPEG, dst)
    assert not os.path.exists(dst + ".tmp")


def test_save_refuses_non_jpeg(tmp_path):
    dst = str(tmp_path / "out.jpg")
    with pytest.raises(acquire.CaptureError, match="not a complete JPEG"):
        acquire.save_bytes_atomic(NOT_JPEG, dst)
    assert not os.path.exists(dst)


def test_save_cleans_tmp_on_replace_failure(tmp_path):
    dst = str(tmp_path / "out.jpg")
    with mock.patch("acquire.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            acquire.save_bytes_atomic(VALID_JPEG, dst)
    assert not os.path.exists(dst + ".tmp")  # temp cleaned up
    assert not os.path.exists(dst)


# ---------------------------------------------------------------------------
# capture_still_to_path (end-to-end, HTTP mocked)
# ---------------------------------------------------------------------------

def test_capture_end_to_end_ok(tmp_path):
    dst = str(tmp_path / "cap.jpg")
    with mock.patch("acquire.urllib.request.urlopen", return_value=_FakeResp(VALID_JPEG)):
        out = acquire.capture_still_to_path("http://cam/snap", dst, timeout_s=5)
    assert out == dst
    with open(dst, "rb") as f:
        assert f.read() == VALID_JPEG


def test_capture_end_to_end_timeout_no_file(tmp_path):
    dst = str(tmp_path / "cap.jpg")
    with mock.patch("acquire.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(acquire.CaptureTimeout):
            acquire.capture_still_to_path("http://cam/snap", dst, timeout_s=2)
    assert not os.path.exists(dst)
    assert not os.path.exists(dst + ".tmp")


# ---------------------------------------------------------------------------
# fallback stub
# ---------------------------------------------------------------------------

def test_opencv_fallback_not_implemented():
    with pytest.raises(NotImplementedError):
        acquire.capture_opencv_fallback()
