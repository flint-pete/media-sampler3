# media-sampler3 -- app.py wiring for Hanwha cameras (--camera-vendor hanwha).
#
# CLI/env defaults, validation, config resolution, and an END-TO-END continuous run
# against a real local HTTP Digest server (the fake Hanwha in test_acquire_hanwha):
# fetch -> EXIF embed -> ring cache, with no mocking of the HTTP path.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import acquire  # noqa: E402
import app      # noqa: E402
import cache    # noqa: E402
import metadata  # noqa: E402
from test_acquire_hanwha import PASSWORD, USER, camera  # noqa: E402,F401  (fixture)

def _tiny_jpeg():
    pytest.importorskip("piexif")
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "jpeg")
    return buf.getvalue()


def _parse(argv):
    return app.build_parser().parse_args(argv)


# --- CLI / env ----------------------------------------------------------------

def test_vendor_defaults_to_reolink(monkeypatch):
    monkeypatch.delenv("CAMERA_VENDOR", raising=False)
    a = _parse(["--one-shot", "--stream", "cam", "--camera-host", "h"])
    assert a.camera_vendor == "reolink" and a.camera_profile is None


def test_vendor_and_profile_flags():
    a = _parse(["--one-shot", "--stream", "ptz", "--camera-host", "h",
                "--camera-vendor", "hanwha", "--camera-profile", "2"])
    assert a.camera_vendor == "hanwha" and a.camera_profile == 2


def test_vendor_and_profile_from_env(monkeypatch):
    monkeypatch.setenv("CAMERA_VENDOR", "hanwha")
    monkeypatch.setenv("CAMERA_PROFILE", "3")
    a = _parse(["--one-shot", "--stream", "ptz", "--camera-host", "h"])
    assert a.camera_vendor == "hanwha" and a.camera_profile == 3


def test_unknown_vendor_rejected_by_argparse():
    with pytest.raises(SystemExit):
        _parse(["--one-shot", "--stream", "c", "--camera-host", "h",
                "--camera-vendor", "mobotix"])


def test_camera_mic_with_hanwha_is_config_error():
    a = _parse(["--continuous", "10", "--stream", "mic", "--cache-max-count", "3",
                "--media", "audio", "--source-type", "camera_mic",
                "--camera-host", "h", "--camera-vendor", "hanwha"])
    with pytest.raises(app.ConfigError, match="Reolink-only"):
        app.validate_args(a)


def test_resolve_camera_config_builds_hanwha_source(monkeypatch):
    monkeypatch.setenv("CAMERA_USER", USER)
    monkeypatch.setenv("CAMERA_PASSWORD", PASSWORD)
    a = _parse(["--continuous", "10", "--stream", "ptz", "--cache-max-count", "3",
                "--camera-host", "10.31.81.32", "--camera-vendor", "hanwha",
                "--vsn", "H03B"])
    src, ident, name = app._resolve_camera_config(a)
    assert isinstance(src, acquire.HanwhaStill)
    assert src.url.startswith("http://10.31.81.32:80/stw-cgi/video.cgi?")
    assert PASSWORD not in repr(src) and name == "ptz"


# --- end to end ---------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(secs * 1e9)


class _RecPlugin:
    def publish(self, *a, **k):
        pass


def _run(tmp_path, cam, monkeypatch, password, ticks=5):
    monkeypatch.setenv("CAMERA_USER", USER)
    monkeypatch.setenv("CAMERA_PASSWORD", password)
    seq = iter(range(1000, 2000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    a = _parse(["--continuous", "10", "--stream", "ptz", "--cache-max-count", "3",
                "--cache-root", str(tmp_path), "--cache-name", "job-1",
                "--camera-host", "127.0.0.1", "--camera-port", str(cam.port),
                "--camera-vendor", "hanwha", "--vsn", "H03B"])
    a.heartbeat_secs = 60
    clk = _Clock()
    return app._continuous_to_cache(a, max_ticks=ticks, plugin=_RecPlugin(),
                                    monotonic=clk.monotonic_ns, sleep=clk.sleep)


def test_continuous_hanwha_end_to_end_into_ring(tmp_path, camera, monkeypatch):
    camera.body = _tiny_jpeg()
    assert _run(tmp_path, camera, monkeypatch, PASSWORD) == app.EXIT_OK
    ring = cache.scan_ring(os.path.join(str(tmp_path), "job-1", "ptz"))
    assert ring.count == 3                     # 5 captures, cap 3
    assert camera.auth_ok == 5                 # every tick fetched via Digest
    import piexif
    exif = piexif.load(ring.members[-1].path)  # metadata embedded on the JPEG
    assert exif["0th"] or exif["Exif"]


def test_continuous_hanwha_bad_password_backs_off_and_keeps_running(
        tmp_path, camera, monkeypatch):
    camera.body = _tiny_jpeg()
    assert _run(tmp_path, camera, monkeypatch, "wrong", ticks=5) == app.EXIT_OK
    # tick 1 contacts the camera (challenge + one bad attempt); ticks 2-5 fall in
    # the 60 s auth backoff -> no further logins that could lock the account.
    assert camera.hits == 2 and camera.auth_ok == 0
    sdir = os.path.join(str(tmp_path), "job-1", "ptz")
    assert not os.path.isdir(sdir) or cache.scan_ring(sdir).count == 0
