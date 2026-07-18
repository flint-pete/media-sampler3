# Changelog

All notable changes to **image-sampler2** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project (once it diverges from the upstream baseline) will aim to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Convention: after each improvement or design change to the code, add a short
entry under `[Unreleased]`. When a set of changes is cut into a plugin version,
move them under a new `[x.y.z] - YYYY-MM-DD` heading and bump `sage.yaml`.
Group entries as Added / Changed / Fixed / Removed / Deprecated / Security.

## [Unreleased]

### Added
- **Runtime node identity from the WES-injected env vars.** `nodemeta._runtime_identity()`
  now reads the five `WAGGLE_NODE_*` env vars that the WES `wes-nodeinfo-injection`
  change projects into every plugin pod via `envFrom: wes-identity`
  (`WAGGLE_NODE_{ID,VSN,GPS_LAT,GPS_LON,MOBILITY}`), with sentinel→None normalization
  mirroring `pywaggle2 node_info_env.py` (vsn `0`/empty→None; coords by range,
  `999`→None; never fabricated). This is the runtime identity source the earlier
  `sage-ci` placeholder was written to await; the change is confined to
  `_runtime_identity()` and the return-dict shape is unchanged, so `resolve_identity`,
  the v2 filename, EXIF GPS, and upload meta consume it unchanged. Precedence is
  still explicit CLI arg > runtime env > manifest/`/etc/waggle` (host-only fallback).
  - **Verified live on H00F (2026-07-12):** image-sampler2 side-loaded with the 5
    identity vars injected produced `1783…-v2-H00F-top_camera.jpg` whose EXIF carried
    `Model=H00F` and GPS `lat=41.7179852778, lon=-87.9827151389` (H00F's real surveyed
    location), with upload meta `vsn=H00F, node_id=00004cbb4701d16c` — every value
    sourced from the injected env. (The final Beehive object-store round-trip was
    completed on 2026-07-13 once the H00F upload-agent was fixed: the object appears
    in the public Sage data API with `meta.vsn=H00F, node_id=00004cbb4701d16c,
    filename=…-v2-H00F-top_camera.jpg` — full end-to-end proof to the cloud.)

### Changed
- **The `/tmp` cache fallback is removed.** Continuous mode now targets the shared
  `/local-cache` node cache unconditionally (`--cache-root` > `$IS2_CACHE_ROOT` >
  `/local-cache`), and **fails fast** if the resolved cache root is not an existing,
  writable directory. Previously it silently fell back to a pod-ephemeral `/tmp`
  when `/local-cache` was absent — which "worked" but produced frames no consumer
  plugin could ever read. That scaffolding was useful during early development but
  is a footgun in teaching/production, so it is gone: a missing cache is now a clean
  error explaining that the node needs the `wes-local-cache-manager` WES component
  and the `-v <host>:/local-cache` volume mount.
  - `--cache-root <dir>` remains as an explicit escape hatch for off-node local
    development (point it at any existing writable dir).
  - Removed the interim `--require-local-cache` flag / `IS2_REQUIRE_LOCAL_CACHE`
    env twin — the shared cache is now always required, so the opt-in is redundant.
  - `cache.py`: `TMP_CACHE_DIR`, `require_local_cache_requested()` and
    `assert_shared_cache_available()` removed; `resolve_cache_root()` simplified to
    a pure default (no filesystem probe); new `assert_cache_root_available()`
    enforces presence/writability at startup. Tests updated (233 pass).

## [0.5.1] - 2026-07-06

### Fixed
- ECR build (Jenkins/buildkit) failed on every `RUN` step with
  `runc ... can't mask dir "/proc/acpi"` — a buildkitd/runc sandbox bug triggered
  by the `waggle/plugin-base:1.1.1-base` container config (the base also shipped
  Python 3.8.5). Switched the base image to `python:3.12-slim` (same family the
  birdnet plugin builds cleanly with on the ECR builder), which avoids the sandbox
  bug and modernizes the runtime. No app code changed; 229 tests unaffected.
- Slimmed `requirements.txt`: `pywaggle[vision]` -> `pywaggle` (we only use the
  core `waggle.plugin.Plugin`; no cv2/OpenCV anywhere — the OpenCV fallback is a
  stub and frames are fetched via stdlib urllib), and dropped the unused
  `croniter` and bare `pip` entries. Removes the OpenCV/numpy dependency chain and
  the need for apt system libs -> smaller image, faster build, fewer failure
  surfaces. Verified on-node: image builds on python:3.12-slim and
  `from waggle.plugin import Plugin` + `piexif` resolve; `--help` runs.

## [0.5.0] - 2026-07-06

Stage 3.3: `--max-count` / `--max-runtime` clean self-exit for `--continuous`
(design §3.3). A producer can now run as a bounded, scheduler-friendly burst
(exits cleanly on a completed-capture edge after N captures or S wall seconds)
instead of a forever-daemon — pairs with the Stage 6 cron job pattern. Both
default 0 = unbounded (forever behavior preserved). Verified on-node against H00F
(pod self-completed at the edge past the runtime bound).

### Added
- Stage 3.3 (s3.3a–b): `--max-count` / `--max-runtime` clean self-exit for
  `--continuous` (design §3.3). Lets a producer run as a bounded, scheduler-
  friendly burst instead of a forever-daemon; pairs with the Stage 6 cron job
  pattern. Note in docs/STAGE3.3-DESIGN-NOTE.md.
  - `--max-count N`: exit after N CAPTURES (heartbeats/iterations don't count).
  - `--max-runtime S`: exit after S wall-clock seconds, checked at the loop TAIL
    on a completed-capture edge so exit never lands mid-interval and >=1 frame is
    always delivered (a sub-interval runtime still yields the startup capture).
  - Both default 0 = unbounded (the forever behavior is preserved exactly).
    Whichever bound trips first ends the loop; it returns normally so the pywaggle
    Plugin tears down cleanly. Bounded exit is a SUCCESS (exit 0).
  - `run_dual_grid_loop` gains a `max_runtime_ns` bound and promotes `max_captures`
    to a first-class production bound (test `max_ticks` still works — the two are
    kept separate). +4 loop tests.
  - CLI + validate_args: continuous-only (rejected in one-shot), non-negative ints
    (negative -> fail-fast exit 2); `summarize()` shows `bounds=[...]`. +11 tests.
  - Verified with a real clock/sleep: `--max-count 3` captured exactly 3 frames on
    the 1s grid and self-exited 0 in ~2s. 229 tests pass.
- Stage 3.3c: ON-NODE self-exit verification on H00F (Thor), built/imported as
  image-sampler2:0.5.0-rc. `--continuous 8 --max-runtime 30` captured 5 real
  hummingcam frames on the 8s grid (t=0,8,16,24,32s) then CLEANLY SELF-EXITED at
  the first capture edge past the 30s bound — the pod reached a terminal completed
  state and was reaped (not killed, not crash-looped). Producer ran as a bounded,
  self-terminating burst on real hardware. Creds env-only (shredded); scheduler
  unharmed; node cleaned.

## [0.4.0] - 2026-07-06

Stage 6: `--one-shot --from-cache <dir>` cache uploader (design §2.8) — the
consumer/uploader half of the producer/consumer split. Uploads the NEWEST v2 image
already in a cache dir, preserving its original capture-ts end to end, without
touching the camera, writing, or evicting. Completes the produce→cache→upload
loop. Ships a turnkey producer+uploader job pair. Verified on-node against H00F
(capture-ts preserved in Beehive, cache untouched).

### Added
- Stage 6 (s6a–s6c): `--one-shot --from-cache <dir>` cache uploader — the
  consumer/uploader half of the producer/consumer split (design §2.8). Takes the
  NEWEST v2 image already in a cache dir and uploads it, WITHOUT touching the
  camera, writing, or evicting. Note in docs/STAGE6-DESIGN-NOTE.md.
  - `upload.cache_upload(path, plugin=None)`: reads the cached file, recovers its
    capture-ts from the v2 name, reads back embedded EXIF for the meta block, and
    uploads a COPY with the RECORD timestamp = the ORIGINAL capture ts (preserved
    end to end, §2.10 — never re-stamped to now). `upload_timestamp` = real send
    time; `source=from-cache`. The cached original is never moved/mutated/evicted.
    Publishes `plugin.duration.upload` only (no grab/embed phases). +7 tests.
  - `app._one_shot_from_cache`: resolves the STREAM dir, selects newest via
    `cache.scan_ring` (same "valid v2 file" rule as the producer; ignores
    .tmp/non-v2), maps outcomes to exit codes — fail-fast EXIT_CONFIG_ERROR on a
    missing dir or an EMPTY cache (surfaces a broken/absent producer), runtime
    upload failure -> EXIT_CAPTURE_ERROR. Wired into main() dispatch. +6 tests.
  - `jobs/`: turnkey producer+uploader example pair (`producer-continuous.yaml`,
    `uploader-from-cache.yaml`) + `jobs/README.md` documenting the composed
    periodic-snapshot pattern. Producer reads camera creds from env/Secret (never
    argv, per Infra #10); uploader needs no creds (never hits the camera).
  - Verified end-to-end: producer-filled cache -> from-cache upload selects the
    newest, preserves the original capture-ts, carries embedded unique_id, and
    leaves the cache untouched. 214 tests pass.
- Stage 6d: ON-NODE verification on H00F (Thor), built/imported as
  image-sampler2:0.4.0-rc. Ran the composed loop via pluginctl:
  - PRODUCER (--continuous, live hummingcam) filled a host-mounted ring with 3
    real ~1.3MB frames (newest capture_ts 1783384382979952981).
  - UPLOADER (--one-shot --from-cache /cache/h00f-s6/top) selected the NEWEST,
    uploaded it with NO camera contact, exit 0.
  - DATA-PLANE: the `upload` record landed in Beehive with RECORD timestamp =
    2026-07-07T00:33:02.979952981Z (the ORIGINAL capture time, NOT the ~00:34:18
    send time), meta.source=from-cache, upload_timestamp=1783384458... (real
    send), unique_id from the embedded EXIF; object stored at
    storage.sagecontinuum.org under the original capture-ts name. Capture-ts
    preserved end to end (§2.10) — confirmed live.
  - CACHE UNTOUCHED: all 3 files identical after the upload (no evict/mutate, §2.8).
  - Beehive attached vsn=H00F/node downstream to the in-pod placeholder vsn=NODE.
    WES scheduler stack unharmed; node cleaned (pods removed, creds shredded,
    scratch/build deleted).

## [0.3.0] - 2026-07-06

Stage 5: continuous-mode cache HEARTBEAT (design §3.2) — the sole liveness signal
for `--continuous` (local-only, so no upload record implies "alive"). Fires on its
own monotonic grid decoupled from the capture cadence, even when captures fail
(the "running but silent" case). Verified on-node against H00F, including the
dead-camera liveness case and data-plane delivery of env.imagesampler.cache.*.

### Added
- Stage 5 (s5a–s5c): continuous-mode cache HEARTBEAT — the sole liveness signal
  for `--continuous` (local-only, so no upload record implies "alive"). Design
  §3.2; note in docs/STAGE5-DESIGN-NOTE.md.
  - `heartbeat.py`: pure `Heartbeat` helper (monotonic grid + between-beat
    accumulators). `due()`/`next_due_ns()`/`record_capture()`/
    `snapshot_and_reset()`. Startup beat at slot 0 ("I came up"); a long stall
    emits exactly ONE catch-up beat, never a burst. +8 tests.
  - `app.run_dual_grid_loop`: two monotonic grids on one thread (resolution 1B) —
    sleeps to the nearest of (next capture edge, next heartbeat edge) so the
    heartbeat holds its ~60s cadence even when sampling is slower, while never
    emitting >1 beat per slot. Fail-soft callbacks. +5 tests.
  - Wired into `_continuous_to_cache`: opens a pywaggle Plugin (fail-SOFT — if
    unavailable, the cache still runs without heartbeats), accumulates
    written/evicted/last_status per beat, and publishes on the heartbeat grid:
      * `env.imagesampler.cache.count`   (ring image count)
      * `env.imagesampler.cache.bytes`   (ring total bytes)
      * `env.imagesampler.cache.written` (images written since last beat, delta)
      * `env.imagesampler.cache.evicted` (images evicted since last beat, delta)
      * `env.imagesampler.cache.last_status` ("ok"/"skip"/"fail"/"none")
    with `meta={cache_name, camera, vsn}` (all strings). Fires even when every
    capture fails (the "running but silent" case). Publish is fail-soft (a broken
    broker never kills the loop). +10 wiring/CLI tests.
  - New CLI `--heartbeat-secs SECONDS` (continuous-only, default 60, positive-int
    fail-fast; rejected in one-shot). `summarize()` shows it.
  - Verified end-to-end vs a mock HTTP camera: startup beat (count=0), then beats
    on the independent grid with correct delta reset; ring stays bounded; password
    redacted. 201 tests pass.
- Stage 5d: ON-NODE verification on H00F (Thor), built/imported as
  image-sampler2:0.3.0-rc (Dockerfile now COPYs heartbeat.py). Two runs via
  `sudo pluginctl run --selector zone=core --env-from <creds> -v <host>:/cache`:
  - HAPPY PATH (live hummingcam, interval=10s, --heartbeat-secs 15, cap=3):
    STAGE 5 beats fired on the independent 15s grid (not the 10s capture grid),
    startup beat count=0/status=none, then count climbed 0→2→3 and held at the
    cap; written/evicted deltas reset each beat. Dual-grid 1B confirmed on real
    hardware.
  - DEAD CAMERA (unreachable IP, --capture-timeout 3, heartbeat 10s): every
    capture failed ("Connection refused") yet heartbeats KEPT FIRING on the 10s
    grid with count=0/status=skip — the "running but silent" liveness case the
    heartbeat exists to reveal, proven live.
  - DATA-PLANE: `env.imagesampler.cache.*` records are queryable from the Sage
    data API (data.sagecontinuum.org). Beehive attached identity downstream
    (vsn=H00F, node=00004cbb...) to our in-pod placeholder vsn=NODE, exactly as
    designed; meta carries cache_name/camera + Beehive host/job/plugin/task/zone.
  - Creds env-only (--env-from a mode-600 file, shredded after; pw never on argv).
    WES scheduler stack unharmed; node cleaned (pods removed, scratch/build/creds
    deleted).

## [0.2.0] - 2026-07-06

Stage 4: `--continuous` local ring-cache producer (design 2.2 + 2.6). Adds a
drift-free periodic capture loop that writes v2-named, EXIF-embedded frames into
a bounded, per-stream ring on local disk. Local-only (never uploads); one plugin
instance per camera stream. Verified on-node against the live H00F hummingcam.

### Added
- Stage 4d: ON-NODE verification of the `--continuous` producer on H00F (Thor)
  against the live hummingcam (Reolink RLC-811A, 10.107.0.221:10000), via
  `sudo pluginctl run --selector zone=core --env-from <creds> -v <host>:/cache`
  with a host-mounted cache so the ring was observable from the host over SSH.
  Params: interval=10s, --cache-max-count 3. Results (real ~1.5MB frames):
  - Ring bounded at exactly 3; steady-state `evicted=1 ring_count=3` per tick
    (evict-before-write holds the cap). Observed both in host `ls` and plugin log.
  - Fixed-grid scheduling: writes on a clean 10s cadence (2.2 scheduler).
  - EXIF correct on real frames: schema_version, camera, acquisition_path=
    native-raw, upload_timestamp_ns=None (local-only), lat/lon=None (GPS omitted,
    not faked), unique_id=sha256. vsn=NODE placeholder (in-pod, expected).
  - Crash-safety / startup adoption: killed the pod and restarted with the same
    cache-root; first post-restart capture logged `evicted=1 ring_count=3`,
    proving it adopted the 3 pre-existing files (no wipe, no double-count).
  - Local-only: `/run/waggle/uploads` stayed EMPTY in-pod after 20+ captures;
    cache tree is separate from the uploads mount (4.1 validated in practice).
  - Credentials env-only (--env-from a mode-600 file, shredded after); password
    redacted as `password=***` in the fetch log. No creds on argv.
  - WES stack (scheduler, sciencerule-checker, upload-agent, rabbitmq) unharmed;
    test ran as a side-loaded pod in `default`, never touched `ses`. Node cleaned
    (pod removed, scratch dir + creds file + build checkout deleted).
  - Dockerfile fix: COPY now includes capture.py + cache.py (Stage-4 modules) —
    verified imports load in-container. Built/imported as image-sampler2:0.2.0-rc.
- Stage 4c: `--continuous` producer loop wired end-to-end (design 2.2 + 2.6).
  - `app.run_capture_loop()`: monotonic-grid scheduler with skip-on-overrun; clock
    + sleep injectable for deterministic tests (fake clock).
  - `app._continuous_to_cache()`: resolves camera/identity, resolves cache location
    (root auto-detect, name defaults to job id), then loops capture ->
    scan_ring -> plan_evictions -> commit_capture. LOCAL-ONLY (never uploads);
    fail-fast on config (camera host/creds, unwritable cache dir); fail-soft at
    runtime (bad capture warns + skips, loop continues). One log line per write:
    `wrote <name> size=.. evicted=.. ring_count=.. ring_mb=..`.
  - Verified end-to-end against a mock HTTP camera: 6 ticks, ring bounded at 3,
    evict-before-write steady, EXIF round-trips, no `.tmp` litter, password
    redacted in logs.
- Stage 4c CLI (breaking, approved): renamed `--cache-dir` -> `--cache-root`
  (BASE dir; per-stream ring at `<cache-root>/<cache-name>/<camera>/`). Both
  `--cache-root` and `--cache-name` are now OPTIONAL: root auto-detects
  (`$IS2_CACHE_ROOT` -> `/local-cache` if present -> `/tmp`); name defaults to the
  job id. `--continuous` interval is integer seconds. `--continuous` is ONE stream
  per process (a1): >1 `--stream` is a fail-fast error. Cache dir is created
  (mkdir -p) + writability-checked at run time (was: must pre-exist).
  - Stage-0 CLI tests updated for the rename + new optional/single-stream rules.
  - Tests: +10 (tests/test_continuous_stage4.py), -2 obsolete Stage-0 tests.
    178 total pass.
- Stage 4b: shared capture+embed body `capture.py::capture_and_embed_to_tmp`
  (grab -> embed -> write fsync'd `.tmp`), used by BOTH one-shot and the coming
  continuous loop so bytes/naming/EXIF are IDENTICAL across modes. Raises
  `capture.CaptureError` on grab/embed failure (fail-soft at callers).
  - `upload.py::one_shot_upload` refactored to call the shared body for phases 1-2,
    then rename the staged `.tmp` to the object name for pywaggle upload. Behaviour
    preserving: all Stage-3 tests stay green; return `info` shape unchanged.
  - Tests: +8 (tests/test_capture_stage4.py). 170 total pass.
- Stage 4a: pure ring-cache module `cache.py` (no camera/network/pywaggle; FS-only,
  fully unit-tested). Implements design 2.6:
  - `resolve_cache_root()` auto-detect precedence `$IS2_CACHE_ROOT` -> `/local-cache`
    (if present) -> `/tmp`. `/tmp` is an interim stopgap; all logic assumes a future
    node-persistent `/local-cache` from the Sage CI team.
  - `stream_dir()` -> `<cache-root>/<cache-name>/<camera>/` with cache-name
    validation + mkdir -p + writability check (fail-fast CacheError).
  - `scan_ring()` stateless per-stream scan: v2-named files are ring members
    (ordered oldest-first by capture-ts prefix, no stat); non-v2 files and `.tmp`
    are unknown (uncounted, untouched).
  - `plan_evictions()` PURE planner: two independent caps (count, MB decimal 10^6),
    evict-on-EITHER, oldest-first; E3 guard drops a single new image larger than the
    size budget.
  - `commit_capture()` EVICT-BEFORE-write then atomic `os.replace`; fail-soft on
    eviction-delete errors; cleans up tmp on drop/failed-publish (no `.tmp` litter).
  - `metadata.parse_v2_name()` inverse of `build_v2_name` (recovers capture-ts for
    ring ordering; robust to hyphens in vsn/camera; rejects non-v2 names).
  - Tests: +51 (tests/test_cache_stage4.py) covering root auto-detect, name
    validation, scan ordering + unknown handling, every cap combination, E3,
    evict-before-write ordering, atomic publish, fail-soft eviction. 162 total pass.
- Node identity placeholder + FULL Beehive round-trip verification (2026-07-06).
  - Verified there is NO runtime way for a plugin to learn its own VSN/GPS
    (pywaggle 0.56 source + docs + live ses pod + yolo/bioclip precedent). /etc/waggle
    is a node-HOST path NOT mounted into pods; node identity is attached DOWNSTREAM
    by Beehive via routing.
  - nodemeta.py: added a clearly-marked sage-ci PLACEHOLDER runtime lookup
    (grep "TODO(sage-ci)") to be replaced when the Sage CI team ships runtime
    GPS/VSN calls. Precedence: explicit flag > runtime lookup > manifest/etc-waggle
    (host-only). Unknown vsn -> PLACEHOLDER_VSN ("NODE", env IS2_PLACEHOLDER_VSN),
    flagged vsn_is_placeholder + WARNING. Unknown lat/lon -> OMITTED from EXIF
    (never fabricated). Identity is NO LONGER fatal.
  - app.py: one-shot path no longer fails on unresolved vsn; logs placeholder/GPS
    warnings; always proceeds (Beehive attributes the node).
  - tests: +5 placeholder/runtime tests; 111 total, all pass.
  - VERIFIED end-to-end: built arm64 image (podman, py3.8 + pywaggle 0.56.3 +
    piexif), imported into k3s containerd, ran via `pluginctl run` (creds via
    --env-from, never argv) in a real WES pod on H00F. The upload landed in the
    Beehive data API: record timestamp == capture_timestamp == filename prefix
    (capture-time keying); filename used placeholder vsn "NODE" yet Beehive meta
    correctly shows {"vsn":"H00F","node":"00004cbb4701d16c"}; all string meta
    (unique_id/upload_timestamp/acquisition_path/schema_version=sage-img-1) present.
- Stage 3 (one-shot upload path; first end-to-end Beehive result). Verified on
  H00F 2026-07-06.
  - upload.py one_shot_upload(): grab -> embed -> pywaggle upload_file(path, meta,
    timestamp=capture_ts_ns) [capture-time keying, 2.10]. meta carries
    capture_timestamp/upload_timestamp/unique_id/vsn/node_id/job/task/plugin/
    acquisition_path/schema_version, ALL stringified (pywaggle valid_meta).
    plugin.duration.grab/embed/upload published in NANOSECONDS. Fail-soft on
    runtime capture/upload errors (-> EXIT_CAPTURE_ERROR).
  - nodemeta.py: fleet-portable node identity from /etc/waggle (precedence flag >
    node-manifest-v2.json > /etc/waggle/vsn|node-id). --node-manifest /
    WAGGLE_NODE_MANIFEST override.
  - app.py one-shot path wired: resolve identity -> URL -> upload.
  - tests: test_nodemeta_stage3.py (8) + test_upload_stage3.py (6, fake plugin);
    107 total, all pass.
  - On-node: manifest auto-resolved identity with ZERO flags (vsn=H00F,
    lat=41.7179852752395, lon=-87.98271513806043); pywaggle staged
    <capture_ts>-<sha1>/{data,meta} with record timestamp == capture ts; meta all
    strings; unique_id matches embedded EXIF; durations published.
- Stage 2 (capture-ts + v2 naming + EXIF embed; the self-describing file).
  Verified on H00F 2026-07-06.
  - metadata.py: now_capture_ts_ns (2.9); build_v2_name/object_name_for
    (<capture_ts_ns>-v2-<vsn>-<camera>.jpg, 2.10); build_exif_bytes (2.11 mapping:
    standard tags + full JSON UserComment with 8-byte ASCII prefix; GPS abs-value
    + N/S/E/W ref for negative coords); inject_exif (piexif, no pixel re-encode);
    embed_all (one pass: unique_id -> EXIF -> inject); read_back_fields.
  - app.py: node/provenance flags --vsn/--node-id/--job/--task/--plugin-version/
    --lat/--lon (env fallbacks WAGGLE_NODE_*), feeding the EXIF at upload time.
    EXIT_CAPTURE_ERROR reserved for capture/embed failures.
  - requirements.txt: piexif == 1.1.*.
  - tests/test_metadata_stage2.py (16) + CLI stub/regression tests; all pass.
  - Verified on-node via a THROWAWAY script (spikes/verify_stage2_oneshot.py), not
    a CLI flag: real 4K frame, PIXEL SCAN (SOS..EOI) byte-identical to raw (no
    re-encode, +984 bytes EXIF); unique_id == SHA256(raw); JSON unique_id ==
    ImageUniqueID tag; all fields round-trip; APP1/EXIF now present.
- Stage 1 (single real capture -> save raw bytes; the acquisition spine).
  Verified on H00F 2026-07-06.
  - acquire.py: native-still HTTP fetch. build_reolink_snap_url (query-param auth,
    no %-encode of password punctuation, rs cache-buster); fetch_raw_still with a
    hard timeout, returns RAW bytes untouched (no decode/re-encode); JPEG validated
    by SOI/EOI so non-JPEG auth-error blobs are rejected not saved; save_bytes_atomic
    (temp -> fsync -> os.replace, cleans .tmp on failure). CaptureTimeout vs
    CaptureError distinguished; passwords redacted in logs. OpenCV/RTSP fallback
    stubbed (NotImplementedError).
  - app.py: one-shot-from-camera wired. --camera-host/--camera-port/--camera-channel
    (env fallbacks CAMERA_HOST/PORT/CHANNEL); credentials ENV-ONLY (CAMERA_USER/
    CAMERA_PASSWORD), never a flag. --capture-timeout; --out-path (Stage-1 temporary
    sink; replaced by v2 naming in Stage 2). New exit code EXIT_CAPTURE_ERROR=3.
  - tests/test_acquire_stage1.py (26 tests) + Stage-1 dispatch tests; 74 total, all
    mocked (no camera/network), all pass.
  - On-node verification: valid JPEG 1,226,354 bytes 3840x2160, SOI/EOI intact, no
    .tmp litter. Design 2.4 CONFIRMED (Reolink cmd=Snap = bare JPEG, no APP0/APP1/
    COM). Timeout path clean (rc=3, no file).
- docs/IMPLEMENTATION-PLAN.md: staged, verify-as-you-go build plan (Stages 0-7 +
  1.5 spike), each ending in a verifiable artifact; dependency spine and deferred
  items documented. Stage 0 and Stage 1.5 marked DONE.
- Stage 1.5 EXIF-injection spike (spikes/exif_spike.py): resolves design 4.4.
  Proves piexif.insert embeds our EXIF/UserComment WITHOUT re-encoding pixels and
  WITHOUT stripping foreign camera segments (Mobotix M1IMG COM survived; SOS..EOI
  pixel scan byte-identical). 13-field UserComment JSON + SHA256 round-trip; H00F
  negative-lon GPS handled via abs+ref. 6/6 checks pass.
- Stage 0 (CLI contract + fail-fast validation). Rewrote app.py's command-line
  interface to the new two-mode design (docs 2.2/2.6/2.8/2.12):
  - Required, mutually-exclusive mode group: --one-shot | --continuous SECONDS.
  - Source flags: --stream (repeatable, required), --name (repeatable, optional,
    count must match --stream), --from-cache DIR (one-shot only).
  - Ring-cache flags (continuous only): --cache-dir, --cache-name,
    --cache-max-count, --cache-max-mb.
  - Pure, unit-testable validate_args() enforcing every fail-fast rule (both/
    neither mode, non-positive interval, missing stream, name/stream mismatch,
    cache flags in one-shot, --from-cache in continuous, continuous missing
    cache-dir/cache-name/cap, cache-dir not existing/writable, unsafe cache-name,
    non-positive caps). Config errors exit with code 2 (distinct from runtime).
  - Stage 0 performs NO capture: it validates and prints the config, exits 0.
  - tests/test_cli_stage0.py: 45 pure tests (no camera/network) covering every
    bad and good flag combination; all pass. Verified real subprocess exit codes.
  - Added .gitignore (venv, __pycache__, pytest cache, sample.jpg).

### Removed
- --out-dir and --out-path CLI flags (introduced in an earlier Stage-2 draft).
  Neither belongs in the locked two-mode design: --out-dir collided with the
  upstream flag the design renamed to --cache-dir (local ring cache, no upload),
  and --one-shot is upload-only, so a local one-shot sink contradicts 2.2/2.8.
  Stage 2's deliverable is the embed LOGIC (metadata.py); on-node verification now
  uses a throwaway script (spikes/verify_stage2_oneshot.py) instead of a scaffold
  flag. A regression test guards against the flags returning. The one-shot camera
  path is a clean "arrives in Stage 3" stub.

### Changed
- Node identity sourcing corrected (design 2.11b) after verifying against pywaggle
  source + a live H00F pod: node VSN/geo are NOT in pod env vars (pywaggle reads
  only WAGGLE_PLUGIN_*/WAGGLE_APP_ID). The real source is /etc/waggle/
  node-manifest-v2.json (world-readable). REMOVED the bogus WAGGLE_NODE_VSN/
  WAGGLE_NODE_LAT/WAGGLE_NODE_LON env fallbacks (those vars do not exist) and the
  --lat/--lon default env lookups; --vsn/--node-id/--lat/--lon now default None and
  OVERRIDE the manifest. Result: fleet-portable self-identification with no
  per-node config.
- Design 2.11/4.4 unique_id semantics superseded by new 4.6 [RESOLVED]: unique_id
  = SHA256 of the ORIGINAL captured frame (before injection), not the final saved
  bytes. A hash of the final file cannot live inside that file (self-reference
  paradox); the source-frame hash is stable and written to BOTH UserComment JSON
  and ImageUniqueID. Object-integrity hash of final bytes, if ever wanted, goes in
  upload meta (Stage 3), not embedded. Mirror re-synced.
- Design doc 4.4: moved OPEN -> RESOLVED (piexif). Recorded the decision and the
  empirically-learned API quirks (BytesIO sink for in-memory bytes; UserComment
  8-byte charset prefix; GPS abs value + N/S/E/W ref, no signed values). Mirror
  re-synced.
- ecr-meta/ecr-science-description.md: rewritten to document the new two-mode CLI
  and every flag (what each does, which mode it belongs to) plus the fail-fast
  rules and usage examples (one-shot, multi-stream, continuous producer,
  --from-cache composition).
- Analysis doc 2.10: added a "HOW THE CAPTURE-TS SWITCH WORKS" note answering how
  we move the filename prefix from upload-time to capture-time given that pywaggle
  assembles the name. Grounded in the current pywaggle source: upload_file() does
  `timestamp = timestamp or get_timestamp()`, so pywaggle only DEFAULTS the
  timestamp — passing an explicit timestamp=capture_ts short-circuits the fallback
  and pywaggle uses our value verbatim for both the filename prefix and the upload
  record timestamp. The whole switch is one argument; we don't bypass pywaggle's
  naming. Noted the MIN_TIMESTAMP_NS constraint (ns int >= 2000-01-01) and that the
  cache path builds the full name itself with no pywaggle call.
- Analysis doc: final cleanup pass. Removed superseded-draft archaeology now that
  the design is settled — stripped the rejected-alternatives list and "Option C"
  labels from 2.11 (EXIF), the "Opt-4" tag from 2.10, and the internal draft
  question IDs (Q0/Q2) from the 2.8/2.13 headings and body cross-references.
  Trimmed the intro's self-referential "this is a rewrite of earlier drafts"
  meta-commentary to a concise reading guide. Kept legitimate design rationale
  (why the naive scheduling loops are rejected), upstream->new flag mappings, and
  the Part 1 defect->where-addressed tags. Verified all section cross-references
  resolve and no dangling Q-number/Option-letter references remain.
- Analysis doc 3.2 (heartbeat/liveness): sharpened and split by mode. Corrected the
  old "every cycle" wording (which would flood the data plane at up-to-1 Hz in
  continuous mode). --one-shot: emit plugin.duration.* for the single run (upload
  record is the primary liveness signal). --continuous (local-only cache producer):
  a PERIODIC CACHE HEARTBEAT ~once a minute (configurable --heartbeat-secs,
  decoupled from the sample interval), NOT per cycle, carrying simple cache stats —
  total image count and total bytes in --cache-dir (optionally images written/
  evicted since last, last capture status, cache-name+camera). It is the sole
  liveness signal in continuous mode and fires even when captures are skipped/
  failed. Documented that plugin.duration.* does NOT apply in continuous mode
  (no discrete run to time; per-capture timing would be noise).
- Analysis doc 2.10 (+ 2.1/2.7): clarified that the FULL v2 name INCLUDING the
  capture-timestamp prefix (<capture_ts_ns>-v2-<vsn>-<camera>.jpg) is applied AT
  CAPTURE TIME, identically in both paths — it is NOT added only at upload. The
  continuous/cache path: the sampler builds the full name itself and writes that
  exact filename into the ring, so cache files on disk already carry the capture-ts
  prefix (which is what makes 2.6 oldest-by-capture-ts eviction and 2.8/5.2
  --from-cache selection work off the filename). The one-shot/upload path: the
  sampler owns the basename and passes timestamp=capture_ts to pywaggle, which
  prepends the same prefix, producing the identical object name. Result: a cached
  file and a one-shot-uploaded file share the same naming structure and capture-ts
  prefix; a --from-cache upload preserves the cached file's original name end to
  end. Replaces the old upload-centric "pywaggle owns the prefix" framing that left
  the cache-file naming implicit.
- Analysis doc: clarified the end-to-end logic/flow of the two modes. Added a
  MODE-AND-FLOW OVERVIEW to 2.1 laying out the full decision tree for all three
  invocations: (1) --one-shot = grab one camera frame, queue for upload, exit
  ("get a sample now" / periodic snapshot); (2) --one-shot --from-cache = upload
  the NEWEST already-cached image (no camera, no write, no evict); (3) --continuous
  = fixed-period producer that writes the local ring and never uploads. Made
  explicit (per user question) that --one-shot is UPLOAD-ONLY and never writes a
  persistent local copy: --cache-dir/--cache-max-* are rejected in one-shot, and
  the staged file is ephemeral upload staging on the host-backed uploads mount (the
  async WES upload-agent completes the transfer after the pod exits). Reinforced
  the "upload always / cache never" vs "cache always / upload never" split and that
  the upload+persist case is met by composition, not a do-everything invocation.
  Reiterated that --from-cache time-window selectors remain a deferred enhancement.
- Analysis doc: renumbered all sections and cross-references from Roman to Arabic
  numerals (PART I->PART 1, II.6->2.6, IV.5->4.5, etc.) so sub-references read as
  3.1 / 4.2 instead of III.1 / IV.2. Content unchanged.
- Analysis doc: complete end-to-end scrub and reorganization (1405 -> 812 lines,
  no loss of verified findings or locked decisions). Restructured into 5 status-
  tagged parts with a legend ([LOCKED]/[VERIFIED]/[REQUIREMENT]/[OPEN]/[DEFERRED]):
  Part I upstream code study (all VERIFIED findings preserved: pywaggle naming,
  RTSP timestamp semantics, metadata evidence, uniqueness + coarse-clock analysis),
  Part II image-sampler2 design (13 locked subsections incl. producer/consumer
  architecture, modes, acquisition mandate, ring cache, Q0/from-cache, timestamps,
  v2 naming, EXIF, shared cache, back-dated-ts verification), Part III requirements
  carried forward (fail-soft loop, heartbeat, self-exit, format/quality, deps,
  tests — promoted from stale "ideas" to REQUIREMENTs), Part IV open items, Part V
  deferred enhancements. Reconciled contradictions from the accreted draft: the
  old "add trigger-based sampling" idea is now DECIDED-against (sampler is a pure
  producer; triggering is a consumer concern); the sample.jpg race is FIXED-in-
  design; the BOTTOM LINE rewritten to the producer/consumer framing; all resolved
  open questions (Q0/Q1/Q2) read as resolved. Surfaced one new OPEN item (IV.5):
  --resize/--jpeg-quality conflicts with the never-re-encode mandate and needs a
  reconciliation decision.

### Fixed
- Analysis Section 3: corrected a dangling "see pitfalls" cross-reference (no such
  section ever existed) to point at Section 4 (Shortcomings), the shared sample.jpg
  race. Added an image-sampler2 RESOLUTION note documenting that the upstream save
  pitfalls are already designed out: the sample.jpg race is eliminated by per-stream
  v2 filenames (Sec 13) + per-stream cache subdirs (Sec 15) + atomic temp->rename
  (Sec 15), and the upload-vs-local mutual exclusivity is replaced by the one-shot/
  continuous mode split (Sec 2b/15).

### Added
- Analysis: Q0 RESOLVED + --from-cache source (LOCKED). --continuous is STRICTLY
  LOCAL-ONLY (never uploads); the cache is its sole sink and uploading is a
  consumer concern. This unlocks the producer/consumer architecture: image-sampler2
  (CPU) fills the shared cache continuously; consumer plugins load a model ONCE,
  batch-infer N cached images, upload the interesting ones, unload, and free the
  GPU (BioClip amortization ~11.4s/img -> ~2.04s/img at N=10, GPU freed after) —
  fixing the single-GPU contention trap. Periodic-snapshot case solved by
  COMPOSITION, not an upload flag: a second SES-cron one-shot job with the new
  --one-shot --from-cache <dir> uploads the NEWEST cached image via the existing
  one-shot upload path (no new upload logic, no camera hit). --from-cache is a
  one-shot-only option (fail-fast if combined with --continuous or empty cache).
  Section 17.2 defers time-window selectors (--closest-before/after-timestamp) for
  cloud-side "pull the cached image nearest time T" use.
- Analysis Sections 16 & 17 (design in progress): shared cache for cross-plugin
  triggers (mode #3) + Planned Enhancements. #3 = continuous ring cache is a
  SHARED buffer other plugins read (e.g. audio lightning trigger uploads the ~10s
  of pre-event frames; remote wildfire alert pulls a matching-moment view). LOCKED:
  uniqueness via a stable user-supplied --cache-name (required with --continuous,
  fs-safe, validated) -> <SHARED_ROOT>/<cache-name>/<camera>/; consumers use
  convention for now. Documented ring<->trigger behavior (2C): size cache >=
  longest trigger lookback; concurrent read+evict is POSIX-safe (open fd survives
  unlink; atomic writes; consumers tolerate ENOENT/TOCTOU); eviction keeps the
  most-recent window. ON-NODE FINDINGS (H00F + edge-scheduler 0.28.0 source):
  CONFIRMED a host-backed, restart-persistent, cross-pod shared mount exists —
  every plugin auto-gets hostPath /media/plugin-data/uploads/<JOB>/<NAME>/<TAG> ->
  /run/waggle/uploads (rw, on 937G NVMe, per-instance); user volumes also exist
  but need a nodeSelector. Open: cache home A (under uploads, needs upload-agent
  scoping check) vs B (dedicated subtree; leaning B); cross-user read perms. Sec 17
  defers the discovery/announcement mechanism (2B option ii) to post-testing.
- Analysis Section 15 (design in progress): continuous-mode local cache / ring
  buffer. --continuous writes to a local --cache-dir (renamed from --out-dir),
  bounded as a ring buffer. LOCKED decisions: --cache-dir required with
  --continuous and rejected with --one-shot (fail-fast); two independent caps
  --cache-max-count / --cache-max-mb (decimal MB), evict-on-either, at least one
  required; per-stream subdirs (<cache-dir>/<camera>/) as independent race-free
  rings; oldest-by-capture-ts-prefix eviction; stateless (scan-each-capture);
  startup adoption of v2-matching files (never wipe, ignore unknowns); per-capture
  algorithm evicts BEFORE the atomic temp->rename so the ring never transiently
  exceeds caps and no torn file joins it; oversized-new-image dropped with warning
  (keep cache valid); fail-soft on delete/disk-full, fail-fast on config. OPEN:
  Q0 — whether --continuous also uploads or is strictly local-only.
- Analysis Section 2b (LOCKED): replace the poorly-named --cronjob flag with two
  descriptive, mutually-exclusive, REQUIRED mode flags — --one-shot (capture once,
  exit; external SES cadence) and --continuous <SECONDS> (run forever on a fixed
  period; interval is the flag's required positive-integer argument). Both/neither
  or interval<=0 are fail-fast parse errors; croniter dependency dropped. Includes
  the LOCKED fixed-period scheduling design: a ~12-line monotonic-grid-with-skip
  loop that never drifts, skips missed ticks on overrun (no backlog), and is
  clock-jump proof; requires a bounded capture (hard timeout -> warn+skip, no
  inline retry; warn at startup if timeout>=interval). Breaking CLI change vs
  upstream.
- Analysis Section 1 design note: camera source selection should prefer the
  acquisition method that yields the best metadata in the resulting JPG — pull the
  original encoded image from the camera's native still endpoint and save raw
  bytes where possible; avoid decode+re-encode paths (which strip camera
  metadata); cross-references Sections 11 and 12.
- Analysis Section 9: what a downloaded JPG reveals (cv2.imwrite embeds no EXIF),
  the object-store URL structure, the verified filename->event-log reverse-lookup
  recipe, and the ns-as-key uniqueness problem.
- Analysis Section 10: coarse-clock/coarse-timestamp finding. Diagnosed W096's
  whole-second upload timestamps to file-forager stamping by source-file mtime
  (not time_ns()) and reusing one second across multiple artifacts — a
  plugin-level provenance issue outside image-sampler2/pywaggle. Flagged as an
  upstream action item (raise with file-forager author / Sage data conventions).
- Analysis Section 11: camera-metadata vendor-interface study + live evidence.
  Marker-scanned three real frames: Reolink snapshot (no metadata; camera authors
  none), Mobotix via mobotix-scan (stripped — libav re-encode, only a Lavc tag),
  and Mobotix via imagesampler-mobotix (FULL M1IMG fingerprint + MXF block
  preserved). Documents Reolink/Hanwha/Mobotix acquisition interfaces and decodes
  the Mobotix fingerprint (manufacturer, ms capture time+TZ, per-sensor geometry/
  exposure telemetry).
- Analysis Section 11 update: added Hanwha data point (W08D bottom camera,
  2560x1920, raw-preserved) — JFIF only, NO EXIF. 2 of 3 tested cameras author no
  metadata; confirms metadata presence is model-specific/unpredictable, which is
  exactly why the vendor-agnostic preserve-if-present design is correct (no
  per-camera special-casing; future EXIF-bearing cameras work with no code
  change). Corrected the earlier "Hanwha likely carries EXIF" assumption and the
  capture-time note (capture_timestamp is ALWAYS node clock per Section 13, never
  camera). Hanwha open item resolved.

- Analysis Section 14: OPEN Q2 RESOLVED. (Check 1) Data-service honors
  client-supplied timestamps — across 18,873 fleet upload records the API record
  timestamp equals the supplied ns prefix 100% (gap <1ms), and genuinely
  back-dated file-forager records (~5.5h behind source; oldest 23.7h back) are
  stored and retrievable at their back-dated time. So a capture-time record
  timestamp is safe at the data layer. (Check 2) slack-hummingbird watcher uses a
  FIXED 120s relative lookback (safe pattern, not a max-ts cursor), so it won't
  permanently drop back-dated records; the image-sampler2 capture-time switch
  affects image-upload records (already tolerated via the 240s deferred image
  queue), not the near-real-time detection records the watcher polls. Follow-up
  only if inference plugins later adopt capture-time: widen detection lookback to
  >=300s. Section 13 production prerequisite CLEARED for image-sampler2.

### Changed
- Design (EXIF field set, Section 12 — LOCKED): Option C hybrid — standard EXIF
  tags where a real one fits (Model=vsn, Software=plugin+version, DateTimeOriginal
  =capture, ImageUniqueID=SHA256, GPS=lat/lon) PLUS a complete JSON blob in
  UserComment for lossless round-trip of all 13 fields. unique_id = SHA256 of the
  saved JPEG bytes (also an integrity check).
- Design (timestamp source + filename, Section 13 — LOCKED): the ONE authoritative
  time is the NODE Linux clock (RTC/GPS-disciplined), never the camera. Filename
  prefix switches from send-time (upstream) to CAPTURE time (node time_ns() at
  grab); upload time (node time_ns() at send) is stored in meta+EXIF. Filename
  scheme Opt-4: `<node_capture_ts_ns>-v2-<vsn>-<camera>.jpg`. The "v2" marker
  flags the changed timestamp meaning + new machinery; vsn fixes cross-node ns
  collisions; camera fixes same-node batch collisions; the guaranteed 1:1 key is
  the EXIF SHA256. PREREQUISITE before production: resolve back-dated-timestamp
  handling (open Q2) and audit the watcher polling window.

- Design mandate (acquisition/metadata): where possible, acquire the ORIGINAL
  ENCODED image from the camera's native still endpoint and save raw JPEG bytes
  UNTOUCHED so high-quality-camera metadata (e.g. Mobotix M1IMG, Hanwha EXIF) is
  preserved; INJECT our Sage fields as an added EXIF/COM segment without
  re-encoding. OpenCV/RTSP (pixel decode + re-encode) demoted to a fallback for
  stream-only cameras and clearly labeled as re-encoded. Native-still HTTP fetch
  becomes a first-class acquisition source. Prefer camera-side capture time
  (Mobotix TIM/TZN/TIT) when present, else node grab-time.
- Design decision (linking/uniqueness): object name = `<ns>-<camera>.jpg`
  (per-stream, not constant `sample.jpg`); embed immutable provenance as EXIF
  (vsn, camera, capture_ts, plugin+version, per-capture unique id); mirror vsn +
  both timestamps + unique id into the upload record meta for a
  construction-guaranteed 1:1 key. Empirically, `(vsn, ns)` is NOT unique (760
  same-node + 695 cross-node ns collisions in 24h of fleet data, incl.
  coarse/whole-second clocks); `(vsn, ns, filename)` is unique in practice but
  not by construction — hence the per-capture unique id.

## [0.3.8-baseline] - 2026-07-03

Starting point. Not a code change — recorded for provenance.

### Added
- Exact copy of upstream `waggle-sensor/plugin-imagesampler` @ `main`
  (version 0.3.8) as the baseline. Text files sha256-verified against upstream;
  `ecr-meta` binaries size-verified.
- `docs/imagesampler.flint.analysis.txt` — full code study (camera source,
  sampling frequency, save behavior, shortcomings, pywaggle naming + upload
  metadata mechanism, two-timestamp design, verified RTSP timestamp semantics).
- `README-image-sampler2.md` — fork README recording baseline provenance.
