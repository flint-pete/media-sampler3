# Stage 5a tests: pure Heartbeat helper (heartbeat.py). No I/O, no pywaggle.
# Fake clock in ns; verify grid due() timing, one-beat-per-slot (catch-up not
# burst), delta accumulation + reset, and payload correctness.

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import heartbeat as hb_mod   # noqa: E402

S = 1_000_000_000  # ns per second


def test_rejects_bad_interval():
    for bad in (0, -5, 1.5, "60"):
        with pytest.raises(ValueError):
            hb_mod.Heartbeat(bad, start_ns=0)


def test_first_beat_available_at_start():
    # slot 0 is [start, start+I); a heartbeat is available as soon as now >= start
    # (an immediate startup "I'm alive" beat). Before start, not due.
    hb = hb_mod.Heartbeat(60, start_ns=1000)
    assert hb.due(999) is False                  # before start
    assert hb.due(1000) is True                  # slot 0 available at start
    assert hb.due(1000 + 59 * S) is True         # still slot 0, not yet emitted


def test_first_slot_fires_at_start():
    # slot 0 is [start, start+I); due() true as soon as now >= start
    hb = hb_mod.Heartbeat(60, start_ns=0)
    assert hb.due(0) is True                      # slot 0 available immediately
    hb.snapshot_and_reset(0, 0, 0)
    assert hb.due(0) is False                     # emitted slot 0
    assert hb.due(60 * S) is True                 # slot 1


def test_one_beat_per_slot_no_burst_on_stall():
    hb = hb_mod.Heartbeat(10, start_ns=0)
    hb.snapshot_and_reset(0, 0, 0)               # emit slot 0
    # jump 35s ahead (slots 1,2,3 elapsed) -> only ONE catch-up beat owed
    assert hb.due(35 * S) is True
    hb.snapshot_and_reset(0, 0, 35 * S)
    assert hb.due(35 * S) is False               # no burst
    assert hb.due(40 * S) is True                # next slot after 35s (slot 4 @40s)


def test_next_due_ns_returns_next_grid_edge():
    hb = hb_mod.Heartbeat(60, start_ns=1000)
    hb.snapshot_and_reset(0, 0, 1000)            # emit slot 0
    # next edge is start + 60s
    assert hb.next_due_ns(1000 + 5 * S) == 1000 + 60 * S
    # if already due, returns now
    assert hb.next_due_ns(1000 + 61 * S) == 1000 + 61 * S


def test_delta_accumulate_and_reset():
    hb = hb_mod.Heartbeat(60, start_ns=0)
    hb.record_capture(written=True, evicted=1, status=hb_mod.STATUS_OK)
    hb.record_capture(written=True, evicted=1, status=hb_mod.STATUS_OK)
    hb.record_capture(written=False, evicted=0, status=hb_mod.STATUS_SKIP)
    p = hb.snapshot_and_reset(ring_count=3, ring_bytes=4500, now_ns=60 * S)
    assert p["count"] == 3 and p["bytes"] == 4500
    assert p["written"] == 2          # two writes
    assert p["evicted"] == 2          # 1+1
    assert p["last_status"] == hb_mod.STATUS_SKIP
    assert p["ts"] == 60 * S
    # after reset, deltas are zero
    p2 = hb.snapshot_and_reset(ring_count=3, ring_bytes=4500, now_ns=120 * S)
    assert p2["written"] == 0 and p2["evicted"] == 0
    # last_status persists (it is a level, not a delta)
    assert p2["last_status"] == hb_mod.STATUS_SKIP


def test_fires_even_when_all_captures_fail():
    hb = hb_mod.Heartbeat(10, start_ns=0)
    for _ in range(3):
        hb.record_capture(written=False, evicted=0, status=hb_mod.STATUS_SKIP)
    assert hb.due(10 * S) is True
    p = hb.snapshot_and_reset(ring_count=0, ring_bytes=0, now_ns=10 * S)
    assert p["count"] == 0 and p["written"] == 0
    assert p["last_status"] == hb_mod.STATUS_SKIP   # liveness still emitted


def test_status_none_until_first_capture():
    hb = hb_mod.Heartbeat(60, start_ns=0)
    p = hb.snapshot_and_reset(0, 0, 0)
    assert p["last_status"] == hb_mod.STATUS_NONE
