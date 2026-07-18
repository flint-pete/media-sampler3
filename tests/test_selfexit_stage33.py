# Stage 3.3b tests: --max-count / --max-runtime CLI + validation + wiring into
# _continuous_to_cache. Fake clock + fake camera; verify a bounded --continuous
# run self-exits cleanly (exit 0) after the bound, and validation rejects bad/
# misplaced flags.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire   # noqa: E402
import app       # noqa: E402
import cache     # noqa: E402
import metadata  # noqa: E402

piexif = pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (5, 6, 7)).save(buf, "jpeg")
    return buf.getvalue()


class FakeClock:
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(round(secs * 1e9))


def _cont_args(tmp_path, **over):
    ns = app.build_parser().parse_args([
        "--continuous", "10", "--stream", "top",
        "--cache-root", str(tmp_path), "--cache-name", "b", "--cache-max-count", "50",
        "--camera-host", "10.0.0.1",
    ])
    app.validate_args(ns)                     # sets heartbeat default etc.
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


# --- CLI / validation ------------------------------------------------------

def _validate(argv):
    ns = app.build_parser().parse_args(argv)
    app.validate_args(ns)
    return ns


def test_bounds_default_zero_unbounded():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3"])
    assert ns.max_count == 0 and ns.max_runtime == 0


def test_bounds_custom_values():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3",
                    "--max-count", "5", "--max-runtime", "600"])
    assert ns.max_count == 5 and ns.max_runtime == 600


@pytest.mark.parametrize("flag,val", [("--max-count", "-1"), ("--max-runtime", "-5")])
def test_negative_bounds_rejected(flag, val):
    with pytest.raises(app.ConfigError, match=">= 0"):
        _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3",
                   flag, val])


@pytest.mark.parametrize("flag", ["--max-count", "--max-runtime"])
def test_bounds_rejected_in_one_shot(flag):
    with pytest.raises(app.ConfigError, match="only valid with --continuous"):
        _validate(["--one-shot", "--stream", "a", flag, "5"])


def test_summarize_shows_bounds():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3",
                    "--max-count", "5", "--max-runtime", "600"])
    s = app.summarize(ns)
    assert "max_count=5" in s and "max_runtime=600s" in s


def test_summarize_bounds_none_when_unset():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3"])
    assert "bounds=none" in app.summarize(ns)


# --- wiring: bounded --continuous self-exits cleanly -----------------------

def test_continuous_max_count_self_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 9000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    clk = FakeClock()
    # --max-count 3 -> exactly 3 frames land, clean exit 0 (no max_ticks harness)
    ns = _cont_args(tmp_path, max_count=3)
    rc = app._continuous_to_cache(ns, plugin=None,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    sdir = os.path.join(str(tmp_path), "b", "top")
    assert cache.scan_ring(sdir).count == 3


def test_continuous_max_runtime_self_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 9000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    clk = FakeClock()
    # interval 10s, runtime 25s -> captures at 0,10,20,30 then exit (edge-aligned)
    ns = _cont_args(tmp_path, max_runtime=25)
    rc = app._continuous_to_cache(ns, plugin=None,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    sdir = os.path.join(str(tmp_path), "b", "top")
    # 4 captures (0,10,20,30s); exits at first edge at/after 25s
    assert cache.scan_ring(sdir).count == 4


def test_continuous_max_ticks_harness_still_works(tmp_path, monkeypatch):
    # the test-only max_ticks bound must still work when --max-count is 0
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 9000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    clk = FakeClock()
    ns = _cont_args(tmp_path)                  # max_count/max_runtime default 0
    rc = app._continuous_to_cache(ns, max_ticks=2, plugin=None,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    assert cache.scan_ring(os.path.join(str(tmp_path), "b", "top")).count == 2
