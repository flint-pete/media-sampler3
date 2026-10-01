# Stage 4 Design Note — `--continuous` loop + ring cache (the producer)

Status: DRAFT for review (Pete). Do NOT implement until approved.
Date: 2026-07-06.
Design refs: analysis.txt 2.1 (producer role), 2.2 (scheduler), 2.6 (ring),
2.7 (cache EXIF/naming), 2.8 (local-only), 4.1 (cache home — RESOLVED → B),
4.2 (cross-user perms — OPEN, deferred). Plan: IMPLEMENTATION-PLAN.md Stage 4.

---

## 1. Goal

Add the `--continuous` producer: a drift-free periodic loop that captures a frame
each tick and writes it into a per-stream, bounded ring-buffer cache on local
disk. **Never uploads** (local-only, 2.8). Reuses the Stage 1–2 capture + name +
EXIF body verbatim as the per-tick action. This is the "producer" half of the
producer/consumer design; a later consumer (or Stage 6 `--one-shot --from-cache`)
reads the ring.

Out of scope for Stage 4 (explicitly deferred): the heartbeat (Stage 5),
`--from-cache` + self-exit (Stage 6), cross-user read permissions (4.2),
resize/quality (4.5).

---

## 2. Decisions already locked (from analysis.txt — not reopening)

These are settled; the note records them so implementation is unambiguous.

- **Scheduler (2.2):** monotonic-grid with skip-on-overrun. `tick` recomputed from
  elapsed monotonic time each cycle → O(1) jump past missed slots, no backlog, no
  busy-loop. Monotonic clock for *scheduling*; wall-clock for the *stamp*.
- **Bounded capture (2.2):** hard timeout on the grab; on timeout/error WARN + SKIP
  the sample, NO inline retry; one bad frame never kills the loop (fail-soft).
- **Ring (2.6):** per-stream subdir `<cache-dir>/<camera>/`, independent ring, caps
  applied per stream (no shared state, no locks). Two independent caps
  (`--cache-max-count`, `--cache-max-mb`), evict-on-EITHER. Evict-BEFORE-write.
  Atomic write (`.tmp` → fsync → `os.replace`). Oldest = capture-ts prefix in the
  v2 name (no stat). Stateless: each capture re-scans the subdir. Startup adoption
  of existing v2 files; never wipe. E3 guard: a single new image bigger than the
  size cap is dropped with a WARNING (keeps the ring valid).
- **Cache file naming/EXIF (2.7):** identical to what `--one-shot` would upload —
  `<capture_ts_ns>-v2-<vsn>-<camera>.jpg` with full Sage EXIF embedded. No
  `upload_timestamp` (that's a consumer concern).
- **4.1 cache home → Option B (RESOLVED 2026-07-06, empirically):** the WES
  upload-agent (`waggle-sensor/wes-upload-agent`, `main.sh`) rsyncs any
  `/uploads/[<job>/]<plugin>/<version>/<ts>-<sha1>/` dir to beehive **and deletes
  the source** (`rsync --remove-source-files`). So the ring must live in a
  dedicated subtree OUTSIDE the upload mount, or the agent would both upload our
  local-only files and delete them out from under our eviction. `--cache-root`
  points at that subtree; it is never `/run/waggle/uploads`.
- **Cache location model (DECIDED 2026-07-06 with Pete):** the flag is
  **`--cache-root`** (renamed from `--cache-dir`), a BASE dir. The per-instance
  subtree is `<cache-root>/<cache-name>/<camera>/`. `--cache-name` overrides the
  middle segment (default = job id).
  > **SUPERSEDED (see CHANGELOG [Unreleased]):** the `/tmp` fallback described in
  > the rest of this bullet has been REMOVED. `--cache-root` now defaults to
  > `$IS2_CACHE_ROOT` → `/local-cache` and the plugin **fails fast** if that dir
  > is not present/writable — no silent `/tmp`. The `wes-local-cache-manager` WES
  > component now provides `/local-cache`. The historical text below is kept for
  > design-record context only.

  **Default root was auto-detected:**
  `$IS2_CACHE_ROOT` → `/local-cache` (if it exists) → `/tmp`. Today that lands in
  `/tmp` (pod-local, ephemeral) which fully supports the PRODUCER + all Stage-4
  verification; the day CI ships a persistent, cross-consumer `/local-cache` mount
  the plugin uses it with zero flag/code change. **All cache code is written
  ASSUMING a real persistent `/local-cache`; `/tmp` is an interim stopgap.**
- **Interval (DECIDED):** `--continuous SECONDS` is INTEGER seconds only (positive
  int; fractional not supported — keeps scheduling simple).
- **Interim /tmp caveat (important scope boundary):** `/tmp` is pod-local and NOT
  visible to other consumer pods, and is wiped on pod restart. So under `/tmp` the
  PRODUCER is fully functional but the cross-pod CONSUMER story does NOT work yet —
  that waits for CI's `/local-cache` + the 4.2 cross-user-read design. Stage 4 is
  therefore PRODUCER-ONLY verification. (Startup-adoption/crash-safety logic is
  still built and correct — it just only demonstrably matters within one pod
  lifetime until `/local-cache` gives persistence across restarts.)

---

## 3. Module plan (small, testable, reuses Stages 1–3)

New module **`cache.py`** — the ring, pure and stateless. No camera, no network,
no pywaggle → fully unit-testable with tmp dirs. Proposed API:

```
class CacheError(Exception): ...            # config-time (fail-fast) issues only

def resolve_cache_root(explicit=None) -> str
    # precedence: explicit --cache-root > $IS2_CACHE_ROOT > /local-cache (if isdir)
    # > /tmp. Returns the base dir. Does NOT create it (caller mkdir -p + writable
    # check for fail-fast). Pure-ish (only os.path.isdir probe) -> unit-testable
    # via monkeypatching the /local-cache probe.

def stream_dir(cache_root, cache_name, camera) -> str
    # <cache-root>/<cache-name>/<camera>; mkdir -p; fail-fast (CacheError) if not
    # writable. cache_name defaults (in the caller) to the job id; --cache-name
    # overrides. Validates cache_name is filesystem-safe (no path separators).

def scan_ring(stream_dir) -> RingState
    # RingState = {count, total_bytes, members:[(capture_ts_ns, path, size)] oldest-first,
    #              unknown_files:[...]}  -- members are v2-name matches only;
    # ordering by capture-ts prefix, fallback mtime for odd names; unknown files
    # counted separately and never touched/sized.

def plan_evictions(ring, new_bytes, max_count, max_mb) -> EvictPlan
    # returns {drop_new: bool, evict:[paths oldest-first]} implementing the E3 guard
    # + evict-loop (2.6 steps 3–4). Pure function of its inputs (no I/O) -> trivially
    # unit-testable for all cap combinations.

def commit_capture(stream_dir, tmp_path, final_name, plan) -> CommitResult
    # apply evictions (delete oldest), then os.replace(tmp -> final). Fail-soft:
    # eviction-delete errors WARN + continue; returns {written, evicted, warnings}.
```

New module **`capture.py`** (or a shared function in `acquire`/`upload`) — the
per-tick body factored out of `_one_shot_from_camera` so continuous and one-shot
share ONE capture+embed path (DRY; guarantees identical bytes/naming/EXIF):

```
def capture_and_embed_to_tmp(*, url, capture_timeout, ident, job, task,
                             plugin, camera, cache_stream_dir) -> (tmp_path, final_name, size, capture_ts_ns)
    # grab (bounded) -> embed_all (Stage 2) -> write .tmp in the stream dir.
    # Raises CaptureError/CaptureTimeout on grab failure (caller warns+skips).
```

`app.py` — new **`_continuous_to_cache(args)`** dispatch:
```
1. Resolve identity once (nodemeta, Stage 3 placeholder-aware).           # cheap, reused per tick
2. Build the Reolink URL once (creds from env, Stage 3).
3. stream = args.stream[0]; camera = args.name[0] if args.name else stream
4. root = cache.resolve_cache_root(args.cache_root)                        # -> /local-cache (fail-fast if absent)
   cname = args.cache_name or job_id                                       # --cache-name overrides
   sdir = cache.stream_dir(root, cname, camera)                            # mkdir -p; fail-fast if unwritable
5. Run the 2.2 monotonic-grid loop:
     each tick:
       try: tmp,final,size,ts = capture.capture_and_embed_to_tmp(...)
       except (CaptureError, CaptureTimeout): WARN; continue          # fail-soft skip
       ring = cache.scan_ring(sdir)
       plan = cache.plan_evictions(ring, size, args.cache_max_count, args.cache_max_mb)
       if plan.drop_new: WARN(E3); delete tmp; continue
       res = cache.commit_capture(sdir, tmp, final, plan)
       log one line: wrote <final> size=.. evicted=.. ring_count=.. ring_mb=..
   (No exit condition in Stage 4 — runs until killed; --max-count/--max-runtime
    self-exit is Stage 6.)
```

Existing `_one_shot_from_camera` is refactored to call the SAME
`capture_and_embed_to_tmp` for its grab+embed, then hand the tmp to the upload
path instead of the ring. (Behaviour-preserving refactor; Stage-3 tests must stay
green.)

---

## 4. Flags (Stage 0 defined most; Stage 4 gives them behaviour + one RENAME)

**RENAME (breaking, approved 2026-07-06): `--cache-dir` → `--cache-root`.** Meaning
changes from "full cache dir" to a BASE root; the per-instance subtree
`<cache-root>/<cache-name>/<camera>/` is derived. This is a Stage-4 CLI edit (the
LOCKED CLI in analysis.txt is updated to match). No external users yet (pre-1.0),
so the break is acceptable and net-clearer.

Flag rules (`validate_args`, fail-fast, exit 2):
- `--continuous SECONDS` mutually exclusive with `--one-shot` (argparse group);
  SECONDS is a positive INTEGER (fractional rejected) → fail-fast on <= 0.
- `--cache-root` OPTIONAL with `--continuous`; if omitted, auto-detect
  (`$IS2_CACHE_ROOT` → `/local-cache` if isdir → `/tmp`). If given, must resolve to
  a writable dir (mkdir -p, then writability check) → else fail-fast.
- `--cache-name` OPTIONAL; default = job id (from `WAGGLE_APP_ID`/env; safe
  fallback if unset). Must be filesystem-safe (letters/digits/dot/dash/underscore,
  no path separators) → else fail-fast.
- At least one of `--cache-max-count` / `--cache-max-mb` REQUIRED with
  `--continuous` (else unbounded) → fail-fast.
- `--cache-root` / `--cache-name` / `--cache-max-*` with `--one-shot` → fail-fast
  (meaningless).
- `--cache-max-mb` is decimal MB (10^6 bytes) per 2.6 — explicit in code.

**Verify in code during 4a/4c:** the current Stage-0 `validate_args` was written
against the OLD `--cache-dir` (REQUIRED, must pre-exist) and `--cache-name`
(REQUIRED, orthogonal). Stage 4 must: rename the flag, make root OPTIONAL with
auto-detect, make name OPTIONAL with job-id default, and drop the "must already
exist" rule in favor of mkdir -p. Update the Stage-0 CLI tests accordingly.

---

## 5. Deployment / mount (Option B concretely)

- **Now (interim, /tmp default):** no mount needed. With no `--cache-root`, the
  ring lands in `/tmp/<cache-name or job>/<camera>/` inside the pod. Works for the
  producer and all Stage-4 verification. `pluginctl run ... -- --continuous <sec>
  --cache-max-count N` (optionally `--cache-root <hostdir>` + `-v <hostdir>:<hostdir>`
  to inspect files from the host during testing).
- **Future (CI `/local-cache`):** when the Sage CI team provides a node-persistent,
  cross-consumer-readable `/local-cache` mount, the auto-detect default picks it up
  with ZERO flag/code change. The scheduled job just needs that mount; the consumer
  reads the same `<cache-name>/<camera>/` subtree ro.
- **Never** point `--cache-root` at `/run/waggle/uploads` (4.1: the upload-agent
  would upload + delete our files).
- **4.2 (cross-user read perms) stays OPEN** and is on the post-initial-code list:
  it genuinely cannot be solved until a real shared mount exists (under `/tmp` there
  is no cross-pod visibility to permission). Once `/local-cache` lands, confirm the
  uid the ring writes as and whether a different-user consumer pod can read it
  (likely world-readable files + traversable dirs; add `chmod` on write if needed).
  Does NOT block Stage 4 producer work.

---

## 6. Verification plan (on-node, real evidence — per Pete's "verify in data plane")

Ring mechanics are local, so verify ON DISK on H00F (not the data API):

1. **Ring holds at cap.** Run with tiny caps (e.g. `--cache-max-count 5`,
   short interval); after >5 ticks confirm exactly 5 files, oldest evicted first
   (compare capture-ts prefixes), count + bytes correct.
2. **No torn/partial files.** No `.tmp` left under a final name at any point; every
   file a valid JPEG.
3. **Crash-safety.** `kill -9` mid-run, restart → re-scans cleanly, adopts existing
   files, no wipe, no double-count.
4. **Overrun.** Set interval shorter than a slow capture → schedule stays on grid,
   missed ticks skipped (no backlog / busy-loop). Check log timestamps land on the
   grid.
5. **E3 guard.** Set `--cache-max-mb` below a single frame size → new image dropped
   with WARNING, ring stays valid, loop continues.
6. **Local-only.** Confirm NOTHING is uploaded: no `upload` records for this
   instance in the data API over the run window; and the cache dir is NOT under the
   uploads mount (4.1).
7. **Fail-soft.** Point at an unreachable camera → WARN+skip each tick, loop keeps
   running, no crash.

---

## 7. Staged implementation order (small commits, tests each step)

Per Pete's staged workflow — each is a reviewable commit with tests + CHANGELOG.

- **4a — `cache.py` pure ring** (scan_ring, plan_evictions, commit_capture,
  stream_dir) with exhaustive unit tests (all cap combos, E3, adoption, unknown
  files, ordering, atomic replace, eviction fail-soft). No app wiring yet.
- **4b — factor `capture_and_embed_to_tmp`** shared body; refactor
  `_one_shot_from_camera` to use it (Stage-3 tests stay green — behaviour
  preserving).
- **4c — `_continuous_to_cache` loop** in app.py wiring 4a+4b + the 2.2 scheduler;
  unit-test the loop with a fake clock + fake capture (no camera): assert grid
  scheduling, skip-on-overrun, evict-before-write ordering, fail-soft skip.
- **4d — on-node verification** (section 6) on H00F via pluginctl with a mounted
  cache dir; record results in CHANGELOG + this note.
- Update IMPLEMENTATION-PLAN Stage 4 → done; mirror analysis.txt if it changed.

---

## 8. Open questions for Pete

RESOLVED 2026-07-06:
- **#1 one stream per `--continuous` process (a1).** One plugin instance = one
  camera stream. Top+bottom cameras = two separate plugin processes / SES jobs,
  each with its own cache subtree, sample rate, and caps. ENFORCEMENT: `--stream`
  stays a repeatable list (CLI shape unchanged, preserves old semantics), but
  `--continuous` with >1 `--stream` is a fail-fast config error ("--continuous
  supports exactly one --stream; run a separate plugin per camera"). One-shot is
  LEFT UNCHANGED (it already uses stream[0]); the single-stream rule is confined to
  the new continuous mode so shipped Stage-3 behavior is undisturbed. This removes
  all intra-process concurrency: one scheduler, one ring, one fail-soft path.
- **#2 cache location / naming** → `--cache-dir` renamed to `--cache-root` (base
  dir), subtree `<cache-root>/<cache-name>/<camera>/`, `--cache-name` overrides the
  middle segment (default = job id). Default root auto-detected
  `$IS2_CACHE_ROOT` → `/local-cache` (if present) → `/tmp`. Interim `/tmp`, all code
  assumes a future persistent `/local-cache`. (See §2, §4, §5.)
- **#3 interval** → INTEGER seconds only (no fractional). (See §4.)

STILL OPEN (Pete to decide before 4c):
1. **Single- vs multi-stream per `--continuous` process.** 2.6 says "each `--stream`
   (own process) writes its own subdir" with per-stream caps. Current CLI takes
   `--stream` as a repeatable list. Options:
     (a) ONE stream per `--continuous` process — fail-fast if >1 `--stream` given.
         Matches the locked per-process model + Pete's one-plugin-per-model
         preference; multi-stream = run multiple jobs. (My lean — simpler, no
         intra-process concurrency.)
     (b) Loop over multiple streams in one process (round-robin within the tick, or
         a worker thread/subprocess per stream). More moving parts (scheduling,
         fail-soft isolation, per-stream rings in one proc).
   Awaiting Pete's call.

MINOR (implementer's discretion unless Pete objects):
- Per-tick log line: `wrote <final> size=<b> evicted=<n> ring_count=<c> ring_mb=<m>`
  (plus a WARN line on skip/drop/evict-failure). OK?
