# Stage 3.3 Design Note — `--max-count` / `--max-runtime` clean self-exit

Status: DRAFT for Pete review (review-first workflow). Implements analysis.txt
§3.3. No code until reviewed.

## 1. Why

`--continuous` currently runs FOREVER (loops until the pod is killed). Stage 3.3
adds two OPTIONAL bounds so a continuous run can cleanly exit ITSELF after a
defined window, for scheduler-friendly windowed capture:

  - `--max-count N`   : exit after N captures
  - `--max-runtime S` : exit after S wall-clock seconds

Default BOTH = 0 (unbounded) — the current "run forever" behavior is preserved
exactly unless the user opts in.

Use cases (compose with the Stage 6 cron job pattern):
  - Duty-cycled producer: scheduler fires the producer on a cron; `--max-runtime
    600` captures for 10 min, then cleanly exits and frees the slot (parity with
    the yolo `--continuous Y --max-runtime 600` cron pattern).
  - Bounded fill/test/backfill: `--max-count 5` grabs exactly 5 frames and stops,
    no manual pod kill.

Honest scope: §3.3 marks this "less critical here" — the sampler is CPU-only, so
there is no GPU to free (unlike yolo/bioclip). Value is fleet PARITY (same knobs)
+ scheduler COMPOSABILITY, not resource contention. Small, orthogonal, low-risk.

## 2. Locked spec (§3.3)

- `--max-count N` exits after N captures; `--max-runtime S` after S wall seconds.
- Default 0 = unbounded (preserves `--continuous` forever).
- CHECK AT THE LOOP TAIL — before sleeping / adding the interval — so exit lands
  on a WINDOW EDGE (a completed capture), not mid-interval.

## 3. Design

### 3.1 Where the check goes (dual-grid loop, Stage 5)

`run_dual_grid_loop` already has `max_iters` / `max_captures` params — but those
are TEST bounds (they `return` the loop early with no cleanup semantics). Stage
3.3 is a PRODUCTION bound with the same shape, so we reuse the mechanism cleanly:

  - `--max-count N`  -> the loop stops after N do_capture() calls. This maps
    directly onto the existing `max_captures` counter (currently test-only). We
    promote it to a first-class production bound.
  - `--max-runtime S` -> a NEW wall-clock bound: stop once (now - start) >= S ns.
    Checked at the loop tail, AFTER a capture, BEFORE the next sleep — so we exit
    on a completed-capture edge (§3.3), never mid-interval.

Both are checked at the tail; whichever trips FIRST ends the loop. The loop
returns normally, so the existing `finally:` in `_continuous_to_cache` runs — the
pywaggle Plugin is torn down cleanly (no orphaned connection). Clean exit 0.

### 3.2 Interaction with captures vs heartbeats (OPEN Q #1)

`--max-count` counts CAPTURES, not heartbeats or wake-iterations (a heartbeat is
liveness telemetry, not work product). RECOMMENDATION: max-count counts
do_capture() calls only. `--max-runtime` is wall-clock and mode-agnostic (it
bounds the whole run regardless of what fired). I lean this way; flag for Pete.

### 3.3 Final heartbeat on exit? (OPEN Q #2)

When a bounded run exits, should it emit ONE final heartbeat first (so the data
plane sees the terminal cache state + a clean "last beat")? Options:
  (a) No final beat — just exit; the last scheduled beat already went out.
  (b) Emit a final heartbeat at exit summarizing the terminal ring state.
RECOMMENDATION: (a) for v1 — simpler, and a bounded run is short enough that the
last grid beat is recent. (b) is a nice-to-have but adds an exit-path publish that
must also be fail-soft. I lean (a); flag for Pete.

### 3.4 CLI + validation

New flags (continuous-only, like the cache flags):
  - `--max-count N`   (int, default 0, >= 0; 0 = unbounded)
  - `--max-runtime S` (int seconds, default 0, >= 0; 0 = unbounded)
Fail-fast (exit 2) on negative values; rejected in one-shot mode (meaningless —
one-shot is already a single bounded capture), matching the cache-flag one-shot
rejection. `summarize()` shows them when set.

INTERACTION with the test-only `max_ticks`: `_continuous_to_cache(max_ticks=...)`
is a test injection that currently maps to `max_captures`. To avoid collision
with the production `--max-count`, the production bound is applied as: effective
max_captures = the FIRST of (test max_ticks, --max-count) that is set (tests never
set --max-count; production never sets max_ticks). Clean separation.

### 3.5 Exit code

Bounded self-exit is SUCCESS -> exit 0 (the window completed as asked). Not an
error condition.

## 4. Testing plan

Pure/unit (fake clock + fake camera + fake plugin):
  - `--max-count 3` -> exactly 3 captures then clean return; ring reflects 3 (or
    the cap, whichever smaller); Plugin torn down.
  - `--max-runtime 25` with capture interval 10s -> exits after the capture at/just
    past 25s (on a capture edge, not mid-interval); count matches elapsed grid.
  - both set: whichever trips first wins (max-count small vs max-runtime small).
  - neither set (0/0) -> unbounded (bounded only by the test max_ticks harness) —
    proves default preserves forever-behavior.
  - `--max-runtime` exit lands AFTER a capture, never between the capture and its
    grid edge (edge-alignment assertion).
  - CLI: negative -> ConfigError; one-shot + --max-count/--max-runtime -> rejected;
    summarize() shows them.
On-node (H00F, brief): a `--continuous --max-runtime 30` run captures a few frames
then exits 0 on its own (pod completes, not killed); confirm clean pod completion
+ cache intact.

## 5. Staged implementation

- s3.3a: promote max_captures to a production bound + add wall-clock max_runtime to
  run_dual_grid_loop (tail check, edge-aligned) + unit tests.
- s3.3b: `--max-count` / `--max-runtime` CLI + validate_args + wire into
  _continuous_to_cache; summarize(); + tests.
- s3.3c: on-node self-exit verification on H00F; CHANGELOG; (0.5.0 when you say).

## 6. Open questions for Pete — RESOLVED 2026-07-06

1. `--max-count` counts CAPTURES only (not heartbeats/iterations): **RESOLVED →
   YES, captures only.** A heartbeat is liveness telemetry, not work product.
2. Final heartbeat on bounded exit: **RESOLVED → JUST EXIT for v1.** A bounded run
   is short; the last scheduled beat is recent. Avoids an extra exit-path publish.
   A terminal-beat is a possible later enhancement.
