# Stage 4c tests: the continuous scheduler (run_capture_loop) and the
# _continuous_to_cache loop wiring. No camera/network: acquire.fetch_raw_still is
# monkeypatched to a tiny JPEG, and the scheduler clock/sleep are injected.
#
# Covers (design 2.2 + 2.6): fixed-grid scheduling, skip-on-overrun, fail-soft
# capture skip, evict-before-write in the loop, local-only (no upload), and the
# cache-name job-id default.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire   # noqa: E402
import app       # noqa: E402
import cache     # noqa: E402
import metadata  # noqa: E402

pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "jpeg")
    return buf.getvalue()


# --------------------------------------------------------------------------
# run_capture_loop: fixed-grid scheduling + skip-on-overrun (2.2)
# --------------------------------------------------------------------------

class FakeClock:
    """Deterministic monotonic clock (ns). advance() by the test; sleep() jumps."""
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(secs * 1e9)


def test_loop_runs_exactly_max_ticks():
    clk = FakeClock()
    calls = []
    app.run_capture_loop(interval_s=10, do_capture=lambda: calls.append(clk.t),
                         max_ticks=3, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert len(calls) == 3


def test_loop_fires_on_fixed_grid_when_capture_is_instant():
    clk = FakeClock()
    fire_times = []

    def cap():
        fire_times.append(clk.t)      # instant capture (no time advance)

    app.run_capture_loop(interval_s=10, do_capture=cap, max_ticks=4,
                         monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # grid at 0,10,20,30 s -> ns
    assert fire_times == [0, 10_000_000_000, 20_000_000_000, 30_000_000_000]


def test_loop_skips_missed_ticks_on_overrun():
    clk = FakeClock()
    fire_times = []

    def slow_cap():
        fire_times.append(clk.t)
        # capture takes 25s -> overruns two 10s slots
        clk.t += 25_000_000_000

    app.run_capture_loop(interval_s=10, do_capture=slow_cap, max_ticks=3,
                         monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # tick0 @0; after 25s next FUTURE slot is 30s (skips 10,20); after 25s->55s
    # next slot is 60s. No backlog, no negative sleeps.
    assert fire_times == [0, 30_000_000_000, 60_000_000_000]


def test_loop_capture_exception_would_propagate_is_callers_job():
    # run_capture_loop does NOT swallow exceptions; the do_capture wrapper in
    # _continuous_to_cache is responsible for fail-soft. Verify the contract.
    clk = FakeClock()

    def boom():
        raise RuntimeError("capture blew up")

    with pytest.raises(RuntimeError):
        app.run_capture_loop(interval_s=1, do_capture=boom, max_ticks=2,
                             monotonic=clk.monotonic_ns, sleep=clk.sleep)


# --------------------------------------------------------------------------
# _continuous_to_cache loop wiring
# --------------------------------------------------------------------------

def _args(tmp_path, **over):
    ns = app.build_parser().parse_args([
        "--continuous", "10", "--stream", "top",
        "--cache-root", str(tmp_path), "--cache-name", "job-1",
        "--cache-max-count", "3",
        "--camera-host", "10.0.0.1",
    ])
    ns.heartbeat_secs = 60          # validate_args normally sets this; set it here
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class _FakeClock:
    """Virtual monotonic clock (ns); sleep() advances time so the dual-grid loop
    makes progress without real waiting."""
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(round(secs * 1e9))


@pytest.fixture
def clk():
    return _FakeClock()


def test_continuous_writes_into_ring_and_bounds(tmp_path, monkeypatch, clk):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    # distinct capture timestamps so filenames don't collide across ticks
    seq = iter(range(1000, 2000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))

    rc = app._continuous_to_cache(_args(tmp_path), max_ticks=5,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK

    sdir = os.path.join(str(tmp_path), "job-1", "top")
    ring = cache.scan_ring(sdir)
    # cap is 3 -> ring holds exactly 3 after 5 ticks
    assert ring.count == 3
    # newest 3 kept (ts 1004,1003,1002 written last); oldest evicted
    kept = sorted(m.capture_ts_ns for m in ring.members)
    assert kept == [1002, 1003, 1004]
    # no .tmp litter
    assert not any(n.endswith(".tmp") for n in os.listdir(sdir))


def test_continuous_fail_soft_capture_skips_but_keeps_looping(tmp_path, monkeypatch, clk):
    monkeypatch.setenv("CAMERA_USER", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    calls = {"n": 0}

    def flaky(url, t):
        calls["n"] += 1
        if calls["n"] == 2:
            raise acquire.CaptureTimeout("slow")   # 2nd tick fails
        return tiny_jpeg()

    monkeypatch.setattr(acquire, "fetch_raw_still", flaky)
    seq = iter(range(1000, 2000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))

    rc = app._continuous_to_cache(_args(tmp_path), max_ticks=3,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    sdir = os.path.join(str(tmp_path), "job-1", "top")
    ring = cache.scan_ring(sdir)
    # 3 ticks, 1 failed -> 2 images written; loop didn't die
    assert ring.count == 2


def test_continuous_cache_name_defaults_to_job(tmp_path, monkeypatch, clk):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    ns = _args(tmp_path, cache_name=None, job="my-job")
    rc = app._continuous_to_cache(ns, max_ticks=1,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    # subtree uses the job name as <cache-name>
    assert os.path.isdir(os.path.join(str(tmp_path), "my-job", "top"))


def test_continuous_missing_camera_host_fail_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMERA_USER", "u")
    monkeypatch.setenv("CAMERA_PASSWORD", "p")
    ns = _args(tmp_path, camera_host=None)
    rc = app._continuous_to_cache(ns, max_ticks=1)
    assert rc == app.EXIT_CONFIG_ERROR


def test_continuous_missing_creds_fail_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMERA_USER", raising=False)
    monkeypatch.delenv("CAMERA_PASSWORD", raising=False)
    rc = app._continuous_to_cache(_args(tmp_path), max_ticks=1)
    assert rc == app.EXIT_CONFIG_ERROR


def test_safe_job_name_coerces_and_falls_back():
    assert app._safe_job_name("my job/x") == "my-job-x"
    assert app._safe_job_name("") == "imagesampler2"
    assert app._safe_job_name(None) == "imagesampler2"
    assert app._safe_job_name("ok-name_1.2") == "ok-name_1.2"
