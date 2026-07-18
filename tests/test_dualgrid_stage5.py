# Stage 5b tests: dual-grid loop (app.run_dual_grid_loop). Fake clock in ns,
# fake capture + heartbeat callbacks recording fire times. Verifies both grids
# fire independently, heartbeat holds its cadence when capture is slower (1B),
# capture skip-on-overrun still holds, and the startup beat lands first.

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app          # noqa: E402
import heartbeat as hb_mod   # noqa: E402

S = 1_000_000_000


class FakeClock:
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        # advance virtual time by the requested sleep
        self.t += int(round(secs * 1e9))


def make(hb_interval, start=0):
    clk = FakeClock()
    clk.t = start
    hb = hb_mod.Heartbeat(hb_interval, start_ns=start)
    return clk, hb


def test_heartbeat_holds_cadence_when_capture_is_slower():
    # capture every 30s, heartbeat every 10s (1B: heartbeat should fire on its own
    # 10s grid between captures, not be bottlenecked to 30s).
    clk, hb = make(10)
    caps, beats = [], []

    def cap():
        caps.append(clk.t)

    def beat(now):
        beats.append(now)
        hb.snapshot_and_reset(0, 0, now)

    # run enough iters to cover ~60s
    app.run_dual_grid_loop(capture_interval_s=30, do_capture=cap, heartbeat=hb,
                           do_heartbeat=beat, max_iters=10,
                           monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # heartbeats on the 10s grid: 0,10,20,30,40,50,60...
    assert beats[:4] == [0, 10 * S, 20 * S, 30 * S]
    # captures on the 30s grid: 0,30,60...
    assert caps[0] == 0
    assert 30 * S in caps


def test_startup_beat_fires_before_first_capture():
    clk, hb = make(60)
    order = []

    def cap():
        order.append(("cap", clk.t))

    def beat(now):
        order.append(("beat", now))
        hb.snapshot_and_reset(0, 0, now)

    app.run_dual_grid_loop(capture_interval_s=10, do_capture=cap, heartbeat=hb,
                           do_heartbeat=beat, max_iters=1,
                           monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # first iteration at t=0: heartbeat (slot 0) fires, and capture (tick 0) fires
    assert order[0] == ("beat", 0)
    assert ("cap", 0) in order


def test_capture_fires_on_its_grid_when_heartbeat_slower():
    # capture every 5s, heartbeat every 60s -> many captures per heartbeat
    clk, hb = make(60)
    caps, beats = [], []

    def cap():
        caps.append(clk.t)

    def beat(now):
        beats.append(now)
        hb.snapshot_and_reset(0, 0, now)

    app.run_dual_grid_loop(capture_interval_s=5, do_capture=cap, heartbeat=hb,
                           do_heartbeat=beat, max_iters=6,
                           monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # captures at 0,5,10,15,20,25
    assert caps == [0, 5 * S, 10 * S, 15 * S, 20 * S, 25 * S]
    # only the startup beat so far (next at 60s, not reached)
    assert beats == [0]


def test_both_fire_together_on_shared_edge():
    # capture 10s, heartbeat 10s -> they share every grid edge
    clk, hb = make(10)
    caps, beats = [], []
    app.run_dual_grid_loop(
        capture_interval_s=10,
        do_capture=lambda: caps.append(clk.t),
        heartbeat=hb,
        do_heartbeat=lambda now: (beats.append(now), hb.snapshot_and_reset(0, 0, now)),
        max_iters=3, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert caps == [0, 10 * S, 20 * S]
    assert beats == [0, 10 * S, 20 * S]


def test_max_iters_bounds_loop():
    clk, hb = make(10)
    n = app.run_dual_grid_loop(
        capture_interval_s=10,
        do_capture=lambda: None,
        heartbeat=hb,
        do_heartbeat=lambda now: hb.snapshot_and_reset(0, 0, now),
        max_iters=4, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert n == 4


# --- Stage 3.3: max_captures / max_runtime_ns production bounds ---------------

def test_max_captures_bounds_captures_not_heartbeats():
    # capture 10s, heartbeat 3s (many beats per capture): max_captures=2 must stop
    # after exactly 2 CAPTURES regardless of how many heartbeats fired.
    clk, hb = make(3)
    caps, beats = [], []
    app.run_dual_grid_loop(
        capture_interval_s=10,
        do_capture=lambda: caps.append(clk.t),
        heartbeat=hb,
        do_heartbeat=lambda now: (beats.append(now), hb.snapshot_and_reset(0, 0, now)),
        max_captures=2, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert len(caps) == 2                     # exactly 2 captures
    assert caps == [0, 10 * S]
    assert len(beats) >= 2                     # heartbeats fired independently


def test_max_runtime_exits_after_capture_edge():
    # capture every 10s, runtime 25s -> captures at 0,10,20; at tail after the 20s
    # capture, elapsed(20s) < 25s so continue; next capture at 30s, elapsed 30>=25
    # -> exit. Exit lands ON a capture edge, never mid-interval.
    clk, hb = make(60)                         # heartbeat won't interfere
    caps = []
    app.run_dual_grid_loop(
        capture_interval_s=10,
        do_capture=lambda: caps.append(clk.t),
        heartbeat=hb,
        do_heartbeat=lambda now: hb.snapshot_and_reset(0, 0, now),
        max_runtime_ns=25 * S, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    # all captures are on the 10s grid (edge-aligned), none mid-interval
    assert all(c % (10 * S) == 0 for c in caps)
    # exits at the first capture edge at/after 25s == 30s
    assert caps[-1] == 30 * S
    assert caps == [0, 10 * S, 20 * S, 30 * S]


def test_max_runtime_guarantees_at_least_one_capture():
    # runtime shorter than the interval: still deliver >=1 frame (captures>=1 gate)
    clk, hb = make(60)
    caps = []
    app.run_dual_grid_loop(
        capture_interval_s=100,
        do_capture=lambda: caps.append(clk.t),
        heartbeat=hb,
        do_heartbeat=lambda now: hb.snapshot_and_reset(0, 0, now),
        max_runtime_ns=5 * S, monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert caps == [0]                         # the startup capture, then exit


def test_max_count_and_runtime_first_to_trip_wins():
    # max_count=2 trips before runtime=1000s
    clk, hb = make(60)
    caps = []
    app.run_dual_grid_loop(
        capture_interval_s=10,
        do_capture=lambda: caps.append(clk.t),
        heartbeat=hb,
        do_heartbeat=lambda now: hb.snapshot_and_reset(0, 0, now),
        max_captures=2, max_runtime_ns=1000 * S,
        monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert len(caps) == 2
