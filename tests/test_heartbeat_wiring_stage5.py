# Stage 5c tests: heartbeat WIRED into _continuous_to_cache. Fake plugin captures
# publishes; fake clock drives both grids; fake camera (tiny jpeg). Verifies the
# env.imagesampler.cache.* topics, meta, delta payloads, fires-when-captures-fail,
# fail-soft publish, and the --heartbeat-secs CLI validation.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire   # noqa: E402
import app       # noqa: E402
import metadata  # noqa: E402

pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")

S = 1_000_000_000


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (7, 8, 9)).save(buf, "jpeg")
    return buf.getvalue()


class FakePlugin:
    """Captures publish() calls; context-manager no-op."""
    def __init__(self):
        self.published = []

    def publish(self, name, value, timestamp=None, meta=None):
        self.published.append({"name": name, "value": value,
                               "timestamp": timestamp, "meta": meta})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeClock:
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(round(secs * 1e9))


def _args(tmp_path, **over):
    ns = app.build_parser().parse_args([
        "--continuous", "10", "--stream", "top",
        "--cache-root", str(tmp_path), "--cache-name", "hbjob",
        "--cache-max-count", "3", "--camera-host", "10.0.0.1",
    ])
    ns.heartbeat_secs = 10          # heartbeat every 10s (== capture here)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _byname(pub, topic):
    return [p for p in pub.published if p["name"] == topic]


def test_heartbeat_publishes_cache_topics(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 5000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    clk = FakeClock()
    pl = FakePlugin()

    # 3 captures; heartbeat grid == 10s == capture grid -> a beat each edge + the
    # startup beat.
    app._continuous_to_cache(_args(tmp_path), max_ticks=3, plugin=pl,
                             monotonic=clk.monotonic_ns, sleep=clk.sleep)

    counts = _byname(pl, "env.imagesampler.cache.count")
    bytez = _byname(pl, "env.imagesampler.cache.bytes")
    assert len(counts) >= 1 and len(bytez) >= 1
    # Heartbeat fires BEFORE the capture on a shared grid edge, so the last beat
    # reflects the ring state at its edge (before that edge's capture). Counts are
    # monotonic up to the cap (3). Assert the ring is bounded and reported growing.
    assert max(c["value"] for c in counts) <= 3
    assert counts[-1]["value"] >= 1
    assert bytez[-1]["value"] > 0
    # the ring on disk holds the bounded set
    import cache
    assert cache.scan_ring(os.path.join(str(tmp_path), "hbjob", "top")).count == 3
    # meta carries disaggregation keys, all strings
    m = counts[-1]["meta"]
    assert m["cache_name"] == "hbjob" and m["camera"] == "top" and m["vsn"] == "NODE"
    assert all(isinstance(v, str) for v in m.values())


def test_heartbeat_extras_written_evicted_status(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 5000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    clk = FakeClock()
    pl = FakePlugin()
    app._continuous_to_cache(_args(tmp_path), max_ticks=4, plugin=pl,
                             monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert _byname(pl, "env.imagesampler.cache.written")
    assert _byname(pl, "env.imagesampler.cache.evicted")
    stat = _byname(pl, "env.imagesampler.cache.last_status")
    assert stat[-1]["value"] in ("ok", "skip", "fail", "none")


def test_heartbeat_fires_even_when_all_captures_fail(tmp_path, monkeypatch):
    # every capture raises -> ring stays empty, but heartbeats MUST still publish
    def boom(url, t):
        raise acquire.CaptureTimeout("dead camera")
    monkeypatch.setattr(acquire, "fetch_raw_still", boom)
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: 12345)
    clk = FakeClock()
    pl = FakePlugin()
    app._continuous_to_cache(_args(tmp_path), max_ticks=3, plugin=pl,
                             monotonic=clk.monotonic_ns, sleep=clk.sleep)
    counts = _byname(pl, "env.imagesampler.cache.count")
    assert len(counts) >= 1                 # liveness emitted despite dead camera
    assert counts[-1]["value"] == 0         # empty ring
    stat = _byname(pl, "env.imagesampler.cache.last_status")
    assert stat[-1]["value"] == "skip"      # last capture was a skip


def test_heartbeat_publish_failure_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 5000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))

    class BrokenPlugin(FakePlugin):
        def publish(self, *a, **k):
            raise RuntimeError("rabbitmq down")

    clk = FakeClock()
    # must NOT raise; loop completes despite publish exceptions
    rc = app._continuous_to_cache(_args(tmp_path), max_ticks=2, plugin=BrokenPlugin(),
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    sdir = os.path.join(str(tmp_path), "hbjob", "top")
    import cache
    assert cache.scan_ring(sdir).count == 2    # captures still landed


def test_heartbeat_runs_without_plugin(tmp_path, monkeypatch):
    # plugin=None sentinel + pywaggle unavailable -> cache still works, no crash
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    monkeypatch.setenv("CAMERA_USER", "u"); monkeypatch.setenv("CAMERA_PASSWORD", "p")
    seq = iter(range(1000, 5000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))
    # force the "pywaggle import fails" branch by ensuring no waggle module
    clk = FakeClock()
    rc = app._continuous_to_cache(_args(tmp_path), max_ticks=2, plugin=None,
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK
    import cache
    assert cache.scan_ring(os.path.join(str(tmp_path), "hbjob", "top")).count == 2


# --- CLI validation for --heartbeat-secs ----------------------------------

def _validate(argv):
    ns = app.build_parser().parse_args(argv)
    app.validate_args(ns)
    return ns


def test_heartbeat_secs_defaults_to_60():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3"])
    assert ns.heartbeat_secs == 60


def test_heartbeat_secs_custom():
    ns = _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3",
                    "--heartbeat-secs", "30"])
    assert ns.heartbeat_secs == 30


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_heartbeat_secs_non_positive_rejected(bad):
    with pytest.raises(app.ConfigError, match="heartbeat-secs must be a positive"):
        _validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "3",
                   "--heartbeat-secs", bad])


def test_heartbeat_secs_rejected_in_one_shot():
    with pytest.raises(app.ConfigError, match="only valid with --continuous"):
        _validate(["--one-shot", "--stream", "a", "--heartbeat-secs", "30"])
