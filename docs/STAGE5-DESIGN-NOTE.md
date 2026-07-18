# Stage 5 Design Note — Continuous cache heartbeat (liveness)

Status: DRAFT for Pete review (review-first workflow). Implements analysis.txt
§3.2 (heartbeat / liveness). No code until this is reviewed.

## 1. Why

`--continuous` is LOCAL-ONLY: it never uploads, so there is NO upload record in
the data plane to imply "this plugin is alive." Without a heartbeat, the fleet
cannot distinguish:
  - "running fine, just not uploading (by design)"  from
  - "crashed / wedged / camera dead."
Stage 5 adds the sole liveness signal for continuous mode: a PERIODIC CACHE
HEARTBEAT published to the data plane, decoupled from the capture cadence.

One-shot mode needs NO heartbeat (§3.2): it is short-lived and externally
scheduled; its upload record + `plugin.duration.*` already signal liveness. Stage
5 touches ONLY the continuous path.

## 2. Locked spec being implemented (analysis.txt §3.2)

- CADENCE: fixed heartbeat interval, default ~60s, via `--heartbeat-secs`,
  INDEPENDENT of `--continuous <SECONDS>`. Emit on a monotonic grid (like 2.2).
- If the sample interval is LONGER than the heartbeat interval, emit at most one
  heartbeat per sample — "never more often than there is news."
- The heartbeat FIRES EVEN IF recent captures were skipped/failed — that "running
  but silent" case is exactly what it exists to reveal.
- PAYLOAD (per cache-name / stream): current image count in the ring; total bytes
  used by those images. Optional cheap extras: images written / evicted since the
  last heartbeat; last capture status (ok/skip/fail); cache-name + camera so
  multi-stream nodes disaggregate.
- TOPICS: `env.imagesampler.cache.count` and `env.imagesampler.cache.bytes`
  (final names confirmable at impl). Keep distinct from `plugin.*` reserved names.
- `plugin.duration.*` does NOT apply in continuous mode.

pywaggle confirms (docs): `plugin.publish(name, value, timestamp=ns, meta={...})`
accepts arbitrary dotted names, ns timestamps, and a string-valued meta dict —
so `env.imagesampler.cache.*` with `meta={cache_name, camera, ...}` is valid.

## 3. Design

### 3.1 Cadence model — one loop, two grids

STARTUP BEAT: slot 0 of the heartbeat grid is `[start, start+I)`, so the first
heartbeat is available immediately at loop start (count=0/bytes=0 before the first
capture). This is deliberate — an immediate "I came up" liveness signal. A long
stall emits exactly ONE catch-up beat, never a burst.

The capture loop (`run_capture_loop`, 2.2) already fires on a monotonic grid.
The heartbeat is a SECOND monotonic grid on the SAME single-threaded loop — no
new thread. After each capture tick we check whether the heartbeat grid is due
and, if so, publish once. This satisfies:
  - "decoupled interval" — heartbeat grid uses `heartbeat_secs`, capture grid uses
    `--continuous SECONDS`.
  - "at most one heartbeat per sample" — we only get a chance to emit once per
    capture tick, so if `SECONDS > heartbeat_secs` we naturally emit ≤1 per sample.
  - "fires even if captures fail" — the heartbeat check runs every tick regardless
    of whether the capture succeeded (the capture wrapper is fail-soft and always
    returns), and it reads the ring from disk (`scan_ring`), so it reports true
    state even after a run of failures.

EDGE CASE — sample interval MUCH longer than heartbeat interval (e.g. capture
every 300s, heartbeat every 60s): with a single loop that only wakes per capture,
we would emit a heartbeat only every 300s, violating "~once a minute." Options:

  (A) Accept it: heartbeat is bounded BELOW by the capture interval. Simple, but
      breaks the ~60s promise when sampling is slow. Documented caveat.
  (B) Make the loop wake on the FINER of (next capture, next heartbeat): sleep to
      whichever grid edge comes first; on wake, do a capture and/or a heartbeat as
      each grid dictates. Keeps ~60s heartbeat even with slow sampling. Slightly
      more loop logic but still one thread, still fail-soft.

  RECOMMENDATION: (B). It honors the spec's ~once-a-minute liveness promise for
  the realistic case where sampling is slower than 60s (e.g. a 5-min timelapse
  still reports alive every minute). The "≤1 heartbeat per sample" rule only binds
  when sampling is FASTER than the heartbeat — (B) handles both. OPEN Q for Pete.

### 3.2 What gets published

Per heartbeat, publish (timestamp = now ns):
  - `env.imagesampler.cache.count`  = ring image count (int)
  - `env.imagesampler.cache.bytes`  = ring total bytes (int)
  with `meta = {cache_name, camera, vsn}` (all strings) so multi-stream/-config
  nodes disaggregate. Optional extras (lean: INCLUDE, they're cheap and aid
  debugging):
  - `env.imagesampler.cache.written`  = images written since last heartbeat
  - `env.imagesampler.cache.evicted`  = images evicted since last heartbeat
  - `env.imagesampler.cache.last_status` = "ok" | "skip" | "fail" (last capture)
  Counters reset to 0 after each heartbeat (delta semantics).

The ring count/bytes come from `cache.scan_ring(sdir)` (authoritative, already
used by the loop). written/evicted deltas come from accumulating the per-tick
`commit_capture` result (res.written bool, len(res.evicted)) between heartbeats.

### 3.3 Fail-soft

Publishing must NEVER kill the loop. Wrap `plugin.publish` in try/except -> log a
warning and continue (same posture as `plugin.duration.*` in upload.py today).
A node with no rabbitmq (local test) just queues in-memory / drops; that is fine.

### 3.4 CLI

New flag:
  - `--heartbeat-secs SECONDS` (int, default 60). CONTINUOUS ONLY. Positive int;
    fail-fast (exit 2) on <= 0. Rejected in one-shot mode (meaningless), matching
    the existing cache-flag one-shot rejection.
No rename / no change to existing flags.

### 3.5 Plugin handle in continuous mode

Today `_continuous_to_cache` does NOT create a pywaggle Plugin (it never uploads).
The heartbeat needs `plugin.publish`, so continuous mode must now open a Plugin
(context-managed, like one-shot). Keep it injectable (`plugin=None` param already
present) so tests pass a fake. If the Plugin can't be created off-node, log a
warning and run WITHOUT heartbeats (the cache still works) rather than fail —
continuous producing to a local ring is still useful in a bare test. On-node the
Plugin always exists.

## 4. Testing plan

Pure/unit (no camera, no rabbitmq; fake clock + fake plugin capturing publishes):
  - heartbeat fires on its own grid, independent of capture interval.
  - fast sampling (capture 1s, heartbeat 5s): ≤1 heartbeat per heartbeat-grid
    edge; count of heartbeats over N ticks matches the grid.
  - slow sampling (capture 10s, heartbeat 3s) under option (B): heartbeat still
    fires on the 3s grid between captures.
  - heartbeat fires even when every capture FAILS (fake capture raises): liveness
    still emitted, last_status="fail".
  - payload correctness: count/bytes match scan_ring; written/evicted deltas
    reset each heartbeat.
  - publish exception is swallowed (fail-soft), loop continues.
  - CLI: `--heartbeat-secs` continuous-only, positive-int fail-fast.
On-node (H00F, brief): confirm `env.imagesampler.cache.*` records land in the
data plane (query-browser / data API) while `--continuous` runs; confirm they
keep coming when the camera is unplugged (the key liveness case).

## 5. Staged implementation

- s5a: heartbeat helper (pure): a `Heartbeat` object holding interval + last-fire
  grid + accumulators; `.due(now)` and `.snapshot_and_reset()` — unit-tested.
- s5b: extend `run_capture_loop` (or wrap it) to support the dual-grid wake
  (option B) with injected clock; unit tests for scheduling.
- s5c: wire into `_continuous_to_cache`: open Plugin, accumulate written/evicted,
  publish on due; `--heartbeat-secs` CLI + validate_args; fail-soft publish.
- s5d: on-node verification on H00F; CHANGELOG; (bump 0.3.0 when you say so).

## 6. Scope decision for Pete: fold in §3.3 self-exit?

§3.3 (`--max-count` / `--max-runtime` clean self-exit) lives in the SAME loop and
is small. Options:
  (a) Stage 5 = heartbeat ONLY (this note). Do §3.3 as its own tiny stage later.
  (b) Fold §3.3 into Stage 5 since both edit the continuous loop tail.
RECOMMENDATION: (a) — keep Stage 5 focused on liveness; §3.3 is orthogonal
(windowed scheduling) and deserves its own small note + tests. But I'll wire the
loop so §3.3 drops in cleanly. OPEN Q for Pete.

## 7. Open questions for Pete — RESOLVED 2026-07-06

1. Cadence edge case (§3.1): **RESOLVED → (B) dual-grid wake.** Heartbeat holds
   ~60s even when sampling is slower; ≤1-per-sample rule still binds when sampling
   is faster than the heartbeat.
2. Payload extras (§3.2): **RESOLVED → INCLUDE** written/evicted/last_status
   (delta semantics, reset each heartbeat) alongside count+bytes.
3. Scope (§6): **RESOLVED → heartbeat-ONLY Stage 5.** §3.3 self-exit deferred to
   its own later stage; loop structured so it drops in cleanly.
4. Topic names: **RESOLVED → keep `env.imagesampler.cache.{count,bytes,...}`** per
   the locked spec (no fork disambiguation).
