#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# image-sampler2 -- continuous-mode cache heartbeat (Stage 5, design §3.2).
#
# The heartbeat is the SOLE liveness signal for --continuous mode (local-only, so
# there is no upload record to imply "alive"). This module is PURE: it owns the
# heartbeat's monotonic grid and the between-heartbeat accumulators (written /
# evicted / last capture status). It performs NO I/O and knows nothing about
# pywaggle -- the caller reads a payload snapshot and publishes it. That keeps the
# scheduling + payload logic fully unit-testable with a fake clock.
#
# Cadence (§3.2, resolved 1B): the heartbeat fires on its own fixed grid
# (default ~60s), decoupled from the capture interval. The dual-grid WAKE lives in
# the loop (Stage 5b); this object only answers "is a heartbeat due at time now?"
# and "give me the payload + reset the deltas." The heartbeat fires even when
# captures fail -- that "running but silent" case is exactly what it reveals.

# Delta semantics: written/evicted count events SINCE the last heartbeat, then
# reset to 0 when the heartbeat is emitted.

# Last capture status values.
STATUS_OK = "ok"
STATUS_SKIP = "skip"     # capture failed fail-soft (timeout / capture error)
STATUS_FAIL = "fail"     # commit/publish problem after a capture
STATUS_NONE = "none"     # no capture attempted yet since start


class Heartbeat:
    """Owns the heartbeat grid + between-beat accumulators. Pure, no I/O.

    interval_s: heartbeat cadence in seconds (int, > 0).
    monotonic():  injected clock returning nanoseconds (default set by caller).

    Usage per loop tick:
        hb.record_capture(written=bool, evicted=int, status=...)   # per capture
        if hb.due(now_ns):
            payload = hb.snapshot_and_reset(ring_count, ring_bytes, now_ns)
            <publish payload>            # caller does the I/O, fail-soft
    """

    def __init__(self, interval_s, start_ns):
        if not isinstance(interval_s, int) or interval_s <= 0:
            raise ValueError("heartbeat interval_s must be a positive int seconds")
        self.interval_ns = interval_s * 1_000_000_000
        self.start_ns = start_ns
        # index of the last heartbeat grid slot we have already emitted. -1 means
        # none emitted yet; the first due() at/after start fires slot 0.
        self._last_emitted_slot = -1
        # between-beat accumulators
        self.written_since = 0
        self.evicted_since = 0
        self.last_status = STATUS_NONE

    def record_capture(self, *, written, evicted, status):
        """Accumulate one capture's outcome since the last heartbeat."""
        if written:
            self.written_since += 1
        self.evicted_since += int(evicted)
        self.last_status = status

    def _current_slot(self, now_ns):
        if now_ns < self.start_ns:
            return -1
        return (now_ns - self.start_ns) // self.interval_ns

    def due(self, now_ns):
        """True if a heartbeat grid slot has arrived that we have not emitted yet.

        Grid slots are start, start+I, start+2I ... At most ONE heartbeat is owed
        per due() call even if several slots elapsed (a long stall emits one
        catch-up beat, not a burst)."""
        return self._current_slot(now_ns) > self._last_emitted_slot

    def next_due_ns(self, now_ns):
        """Absolute ns of the next heartbeat grid edge at/after now (for the
        dual-grid wake in the loop). If a beat is already due, returns now_ns."""
        slot = self._current_slot(now_ns)
        if slot > self._last_emitted_slot:
            return now_ns
        next_slot = self._last_emitted_slot + 1
        return self.start_ns + next_slot * self.interval_ns

    def snapshot_and_reset(self, ring_count, ring_bytes, now_ns):
        """Mark the current slot emitted, return the publish payload, reset deltas.

        Returns a dict of {topic_suffix: value} plus a 'meta_status'/'ts' the
        caller maps to pywaggle publishes. Counters reset AFTER snapshot."""
        slot = self._current_slot(now_ns)
        # advance to the current slot so a stall emits exactly one catch-up beat
        self._last_emitted_slot = max(slot, self._last_emitted_slot + 1)
        payload = {
            "count": int(ring_count),
            "bytes": int(ring_bytes),
            "written": int(self.written_since),
            "evicted": int(self.evicted_since),
            "last_status": self.last_status,
            "ts": int(now_ns),
        }
        self.written_since = 0
        self.evicted_since = 0
        return payload
