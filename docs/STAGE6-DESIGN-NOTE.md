# Stage 6 Design Note — `--one-shot --from-cache <dir>` (cache uploader)

Status: DRAFT for Pete review (review-first workflow). Implements analysis.txt
§2.8 + the flow at §2.2(2). No code until reviewed.

## 1. Why

`--continuous` is a pure LOCAL-ONLY producer; it fills a ring cache and never
uploads (§2.8). The periodic-snapshot need ("one cloud image every ~30 min") is
met by COMPOSITION, not an upload flag on the producer:

  (a) `image-sampler2 --continuous <sec> --cache-root ... --cache-name ...`  (producer)
  (b) `image-sampler2 --one-shot --from-cache <dir>`, scheduled by SES cron
      (`*/30 * * * *`): take the NEWEST cached image and upload it via the EXISTING
      one-shot upload path — no camera hit, no new capture, no eviction.

Stage 6 implements (b): the consumer/uploader half. It is also independently
useful (re-upload, backfill, camera-free testing) and is the reference
"read from cache -> act" pattern other consumers (YOLO/BioClip/BirdNet) follow.

## 2. Locked spec being implemented (§2.8, §2.2(2), §2.10)

- `--one-shot --from-cache <dir>`: read the NEWEST v2 image already in `<dir>`,
  upload it, exit 0. Do NOT touch the camera, write, or evict.
- v1 selection = NEWEST only. Time-window selectors
  (`--closest-before/after-timestamp`) are DEFERRED (§5.2).
- `--from-cache` is one-shot-only: combining with `--continuous` is fail-fast;
  empty/nonexistent cache dir is fail-fast. (Both already enforced in
  validate_args from Stage 0.)
- PRESERVE the original capture-ts end to end (§2.10): the cached file ALREADY has
  its `<capture_ts_ns>-v2-<vsn>-<camera>.jpg` name and embedded EXIF. The upload
  RECORD timestamp must be that original capture ts (via
  `upload_file(timestamp=capture_ts)`), NOT re-stamped to "now". `upload_timestamp`
  is set to the real send time in meta/EXIF.

## 3. Design

### 3.1 Selection — newest v2 file in `<dir>`

The producer writes into `<cache-root>/<cache-name>/<camera>/`. Question: does
`--from-cache <dir>` point at the STREAM dir (…/<camera>/) or a higher level?

DECISION: `<dir>` is the STREAM dir (the leaf that directly contains the v2
`.jpg` files) — the same `sdir` the producer writes to. This is unambiguous
(one camera per stream dir, §2.6) and matches "a consumer pointed at a known
cache" (§2.12). A consumer composing with a producer knows
`<cache-root>/<cache-name>/<camera>/`. (If Pete wants `--from-cache` to accept a
parent and auto-descend, that's an OPEN Q — I lean stream-dir for v1 simplicity.)

Selection reuses the SAME ring scan as the producer: `cache.scan_ring(dir)` lists
valid v2 members sorted by capture_ts_ns; NEWEST = max capture_ts_ns. Reusing
scan_ring means we honor the exact same "what is a valid managed v2 file" rule
(ignores `.tmp`, non-v2 names) — no divergent parsing.

Fail-fast (exit 2) if: dir doesn't exist / isn't a dir; or scan_ring finds ZERO
valid v2 images (empty cache). Runtime read/upload failure -> exit 3
(EXIT_CAPTURE_ERROR reused as the "couldn't deliver" code).

### 3.2 Upload — reuse embedded metadata, do NOT re-embed

The cached file is already a complete v2 artifact: raw camera bytes + embedded
EXIF/UserComment carrying vsn/camera/capture_ts/unique_id/acquisition_path. So
the uploader must NOT re-capture or re-embed (that would change bytes/unique_id).
It:
  1. reads the file bytes,
  2. parses `metadata.parse_v2_name(fname)` -> (capture_ts_ns, vsn, camera),
  3. reads embedded fields via `metadata.read_back_fields(bytes)` for the meta
     block (unique_id, acquisition_path, node_id, etc.) — falls back to the
     name-parsed values if a field is absent,
  4. `plugin.upload_file(path, meta=meta, timestamp=capture_ts_ns)` with
     `meta["upload_timestamp"]=str(now_ns)` at send.

NEW function `upload.cache_upload(*, path, plugin=None)` (sibling to
`one_shot_upload`), so the two share nothing that would let a camera capture leak
in. Emits `plugin.duration.upload` only (no grab/embed phases — there was no
grab/embed in this run). §3.2-consistent: duration.* applies to one-shot uploads.

Because upload_file may move/consume the source, we upload a COPY in a temp dir
(never mutate/evict the cached original — §2.8 "does not touch … or evict").

### 3.3 Dispatch (main)

`main()` currently wires one-shot-from-camera + continuous-to-cache. Add:
  if args.one_shot and args.from_cache: return _one_shot_from_cache(args)
placed before the from-camera branch. `_one_shot_from_cache(args)` resolves the
dir, selects newest, calls `upload.cache_upload`, maps ok->0 / not-found->2 /
upload-fail->3.

Identity note: a `--from-cache` upload does NOT need node identity resolution
(vsn/camera come from the cached file itself). So it skips the nodemeta path —
simpler and correct (the producer already stamped identity at capture time).

### 3.4 Ready-to-copy JOB PAIR (§2.8 "ship a turnkey pair")

Add `jobs/` with two example SES job YAMLs:
  - `jobs/producer-continuous.yaml`  — the `--continuous` producer.
  - `jobs/uploader-from-cache.yaml`  — the `--one-shot --from-cache` uploader on a
    `*/30 * * * *` cron science-rule.
So the simple periodic-snapshot case is copy-paste. (These are examples/docs; the
exact SES schema fields I'll confirm against a known-good job — this is also where
Infra #10 envFrom/secretRef for creds gets exercised.)

## 4. Testing plan

Pure/unit (no camera, no rabbitmq; fake plugin capturing upload_file calls; real
tiny v2 jpegs written to a tmp ring):
  - selects the NEWEST by capture_ts_ns among several cached files.
  - uploads with timestamp == the file's ORIGINAL capture_ts (not now); meta
    upload_timestamp is a distinct, later value.
  - meta faithfully reflects embedded fields (unique_id, camera, vsn, acq path).
  - does NOT evict/modify the cache dir (file count + bytes unchanged after).
  - empty dir -> EXIT_CONFIG_ERROR; missing dir -> EXIT_CONFIG_ERROR.
  - upload exception -> EXIT_CAPTURE_ERROR (fail-soft return, mapped).
  - ignores `.tmp` and non-v2 files when choosing newest.
  - CLI: `--from-cache` one-shot-only + non-empty (already covered Stage 0; add a
    dispatch-level test).
On-node (H00F, brief): run a `--continuous` producer into a host-mounted cache,
then a `--one-shot --from-cache` against that dir; confirm the SAME object
(capture-ts name) lands in Beehive with its original capture timestamp and the
cache dir is untouched.

## 5. Staged implementation

- s6a: `upload.cache_upload` (read file + parse name + read_back_fields -> upload
  copy with preserved capture-ts) + unit tests.
- s6b: `_one_shot_from_cache` dispatch in app.py (select newest via scan_ring,
  exit-code mapping) + tests.
- s6c: `jobs/` example producer+uploader pair; README/docs snippet.
- s6d: on-node verification on H00F (producer -> from-cache upload -> Beehive,
  original capture-ts preserved, cache untouched); CHANGELOG; (0.4.0 when you say).

## 6. Open questions for Pete — RESOLVED 2026-07-06

1. `--from-cache <dir>` = the STREAM dir (…/<cache-name>/<camera>/): **RESOLVED →
   STREAM dir for v1.** Unambiguous (one camera per stream), matches §2.12 known
   cache. Parent auto-descend deferred.
2. Job pair (§3.4): **RESOLVED → INCLUDE the jobs/ producer+uploader pair in
   Stage 6.** Makes the composed periodic-snapshot case turnkey; exercises the
   creds-via-Secret pattern (Infra #10).
3. Empty-cache exit code: **RESOLVED → FAIL-FAST EXIT_CONFIG_ERROR.** A scheduled
   uploader firing against an empty cache is a real misconfig worth surfacing;
   silently-empty would hide a broken producer.
