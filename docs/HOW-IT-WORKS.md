# How the media stack works

This page is for someone new to the project. It explains what each of the six
components does, why it exists, and how they connect. To install, see
[INSTALLING-MEDIA-SAMPLER3.md](../INSTALLING-MEDIA-SAMPLER3.md). To restart after
a reboot, see [REBOOT-RECOVERY.md](../REBOOT-RECOVERY.md). For why the designs
ended up this way, see [DESIGN-PATH.md](../DESIGN-PATH.md).

---

## 1. The problem being solved

A Sage node has cameras and microphones, and several AI plugins want to analyze
them: a detector, a species classifier, eventually an audio classifier. Before
this stack, **every plugin opened the camera itself** on its own schedule. That
caused three problems:

- Load on the camera grew with every plugin added.
- Plugins saw different frames, so their results couldn't be linked to each other.
- Every plugin carried its own camera code and credentials.

The stack splits the work into a **producer** and **consumers**:

```
          one producer                 shared, bounded cache               many consumers
camera ──▶ media-sampler3 ──writes──▶ /local-cache/<cache>/<source>/ ──reads──▶ sage-yolo2, sage-bioclip2, sage-birdnet2, ...
```

- **One producer per camera or mic** captures on a fixed schedule into a ring on
  the node's disk.
- **Consumers read that ring** on their own schedule, at their own rate, and
  never delete what they read.
- Each file **describes itself**: who captured it, when, on which node, plus a
  hash. So any consumer's result can be traced back to the exact frame it came
  from.

Two platform pieces make this safe to run on a real node:

- **A disk bound.** A full disk would break the node.
  **wes-local-cache-manager** caps `/local-cache`.
- **Node identity.** Results need a VSN and location. **wes-nodeinfo-injection**
  publishes them, and **pywaggle2-nodeinfo** is the library that reads them.

---

## 2. The seven components and who owns what

| Component | Kind | Owns | Talks to |
|-----------|------|------|----------|
| [wes-local-cache-manager](https://github.com/flint-pete/wes-local-cache-manager) | WES DaemonSet (node service) | keeping `/media/plugin-data/local-cache` under its byte caps; never touches `.state/` | the filesystem only |
| [wes-nodeinfo-injection](https://github.com/flint-pete/wes-nodeinfo-injection) | WES change: a ConfigMap generator plus a patched `pluginctl` (and an optional patched scheduler) | the `wes-identity` ConfigMap (5 `WAGGLE_NODE_*` vars); `pluginctl-nodeinfo`, whose pods get `envFrom: wes-identity` | k3s |
| [pywaggle2-nodeinfo](https://github.com/flint-pete/pywaggle2-nodeinfo) | library (copied into plugins) | the rules for turning raw env values into a clean `NodeInfo` (sentinels become `None`, no invented GPS) | the env of whatever pod imports it |
| **media-sampler3** (this repo) | plugin (producer) | capturing frames and clips, naming them, embedding provenance, bounding its own ring, heartbeats | camera (HTTP), `/local-cache`, Beehive (heartbeats only) |
| [sage-yolo2](https://github.com/flint-pete/sage-yolo2) | plugin (consumer, and also a crop producer) | detection, counts, crop files, its seen-store | `/local-cache` (read frames, write crops), Beehive |
| [sage-bioclip2](https://github.com/flint-pete/sage-bioclip2) | plugin (consumer) | species classification, its seen-store | `/local-cache` (read crops), Beehive |
| [sage-birdnet2](https://github.com/flint-pete/sage-birdnet2) | plugin (audio consumer) | BirdNET sound classification, eBird geo filter, its seen-store | `/local-cache` (read clips + sidecars), Beehive |

---

## 3. The cache: paths, names, and rules

**Location.** On the host the cache is `/media/plugin-data/local-cache`. Every
plugin pod mounts it as `/local-cache` (`pluginctl run -v …:/local-cache`). The
directory is mode `1777` (world-writable, sticky bit), so pods running as
different users can each write their own subtree and read everyone else's.

**Layout used in this stack:**

```
/local-cache/
├── camera/                    <- --cache-name of the image producers
│   ├── top/                   <- --name of producer #1 (one "unit" for the cache manager)
│   │   └── 1790000000000000000-v2-H041-top.jpg
│   └── side/                  <- --name of a second camera's producer, if any
├── camera-audio/mic/          <- audio producer (clip.flac + clip.flac.json pairs), read by sage-birdnet2
├── camera-crops/              <- written by sage-yolo2 (--crop-cache-name)
│   ├── top-crop-0/            <- first detection in each frame of camera "top"
│   └── top-crop-1/            <- second detection, and so on
└── .state/                    <- consumers' seen-stores; never evicted, never delete
    └── sage-yolo2/<consumer-id>/<cache-name>/<source>/seen   (all three consumers use this
                                   prefix; a quirk of the shared consumer code)
```

**The file contract** (shared by everything that reads the cache):

```
<capture_ts_ns>-v2-<VSN>-<source>.<ext>        e.g. 1790000000000000000-v2-H041-top.jpg
```

- `capture_ts_ns` is the node's clock in nanoseconds at capture time. Sorting
  names sorts by time, with no `stat` needed.
- `<source>` is media-sampler3's `--name` (or `--stream` if `--name` isn't given).
- **Images** carry their provenance inside the JPEG as **EXIF**: standard tags,
  plus a JSON blob in `UserComment` (`schema_version`, `vsn`, `node_id`, `job`,
  `task`, `plugin`, `camera`, `capture_timestamp_ns`, `unique_id` = SHA-256 of the
  original bytes, `lat`/`lon`, …). The pixels are never re-encoded.
- **Audio** has no EXIF, so each clip gets a JSON sidecar with the same fields.
  The sidecar is written first and the clip second, so if a consumer can see a
  clip, its sidecar already exists.
- Files appear **atomically**. They're written as `.tmp`, then renamed. A
  consumer never sees a half-written file.

**Who deletes what:**

| Layer | Who | Rule |
|-------|-----|------|
| 1 | the producer (media-sampler3) | bounds **its own** ring with `--cache-max-count` / `--cache-max-mb`, oldest first, at write time |
| 1 | sage-yolo2 as crop producer | bounds its crop rings the same way (`--crop-max-count`) |
| 2 | wes-local-cache-manager | safety net: every 60 s, caps each unit (2 GiB) and the whole node (15 GiB), oldest first; skips `.state/` |
| — | consumers | **never delete** anything they read |

**Seen-stores.** A consumer records which frames it has already processed (by
the frame's `unique_id`) under `/local-cache/.state/`. So a restart or reboot
resumes instead of reprocessing everything. The directory name comes from the
consumer id, which is `<WAGGLE_JOB_NAME>-<WAGGLE_TASK_NAME>`. That's why the
launch commands set those two env vars and must keep them the same across
relaunches.

---

## 4. Node identity: where VSN and GPS come from

Three possible sources, from strongest to weakest:

1. **Explicit flags.** media-sampler3 `--vsn`, `--lat`, `--lon`. These always win.
2. **The pod environment.** `WAGGLE_NODE_VSN`, `_ID`, `_GPS_LAT`, `_GPS_LON`,
   `_MOBILITY`, from the `wes-identity` ConfigMap. They're present only when the
   pod spec has `envFrom: wes-identity`. Two things add that:
   - `pluginctl-nodeinfo` (install Step 3), for hand-launched pods; this is what
     the stack uses;
   - the patched WES scheduler (wes-nodeinfo-injection "Tier 2"), for SES jobs.

   **Pods from the stock `pluginctl run` don't get them**, because the stock
   binary builds pods itself, with unpatched code.
3. **Files on the node** (`/etc/waggle/node-manifest-v2.json`, `/etc/waggle/vsn`).
   These are host files and usually aren't visible inside a pod.

If none of these is available, the producer records the placeholder VSN `NODE`.
Missing GPS is left out; it is never made up.

How each component uses identity:

- **media-sampler3** (`nodemeta.py`) resolves identity once at startup and writes
  it into every frame's EXIF. In this stack it gets identity from the pod env;
  no flags.
- **sage-yolo2 / sage-bioclip2** trust the **frame's EXIF identity** first, so a
  result is attributed to the node that captured the frame. They use the pod env
  (through the copied-in pywaggle2 `node_info.py`) only for missing fields and to
  warn on a mismatch.
- **Beehive** attaches the node's VSN to every published record during routing,
  whatever the plugin says.

The sentinel rules live in pywaggle2-nodeinfo and are reproduced in each copy:

- empty or `"0"` VSN becomes `None`
- a coordinate outside ±90 / ±180 (e.g. `999`) becomes `None`
- missing or unknown mobility becomes `"unknown"`

Copies that must stay in sync with the canonical code:

- `wes-nodeinfo-injection/pywaggle2/` (a byte-identical mirror)
- the inline reader in its test pod
- `media-sampler3/nodemeta.py` (a re-implementation)
- `sage-yolo2/node_info.py` and `sage-bioclip2/node_info.py` (vendored v0.1.1)

---

## 5. What goes to the cloud (Beehive)

| Topic | Published by | When |
|-------|--------------|------|
| `env.mediasampler.cache.count/.bytes/.written/.evicted/.last_status` | media-sampler3 | every `--heartbeat-secs`; liveness of the ring |
| `env.count.total` | sage-yolo2 | every processed frame (a value of 0 still proves it ran) |
| `env.count.<class>` (e.g. `env.count.bird`) | sage-yolo2 | only when that class was detected |
| `env.crop.count` | sage-yolo2 | when crops were written |
| annotated image upload | sage-yolo2 | frames with detections (`--upload-image`) |
| `env.species.<rank>` (+ `.confidence`), `env.species.top5`, `.summary` | sage-bioclip2 | per crop; the top result only if ≥ `--min-confidence` |
| `env.detection.biophony.<species>` (also `.anthrophony.*`, `.geophony.*`) | sage-birdnet2 | each 3-second window with a detection ≥ `--min-confidence`; value = confidence |
| `env.detection.audio.summary` | sage-birdnet2 | every clip, even with no detections |

Every consumer record is **frame-anchored**: its timestamp is the frame's capture
time, and its metadata links back to the source. bioclip2's `source_unique_id` is
the parent frame's SHA-256, so a species result traces through the yolo2
detection to the exact producer frame.

Publishing goes through pywaggle → WES rabbitmq → upload-agent → Beehive. Query
results with the data API (`https://data.sagecontinuum.org/api/v1/query`); see
install Step 6f.

---

## 6. media-sampler3 code map

All modules live at the repo root and are copied into the image by the
`Dockerfile`. The entrypoint is `python3 -u /app/app.py`.

| File | Purpose | Key functions |
|------|---------|---------------|
| `app.py` | CLI and orchestration. Parses and validates flags, then runs one mode. Exit codes: 0 ok, 2 config error, 3 capture error. | `build_parser`, `validate_args`, `main`, `_continuous_to_cache` (producer loop), `run_dual_grid_loop` (capture and heartbeat timing), `_one_shot_from_camera`, `_one_shot_from_cache` |
| `acquire.py` | Fetches one native JPEG from a Reolink over HTTP (`cmd=Snap`). No decode or re-encode. Redacts passwords in logs. | `build_reolink_snap_url`, `fetch_raw_still` |
| `audio_acquire.py` | Records one bounded clip by running ffmpeg. The camera mic uses the HTTP-FLV sub-stream; a USB mic uses ALSA. Timeout = clip length + 15 s. | `build_reolink_flv_url`, `build_ffmpeg_cmd`, `capture_clip` |
| `metadata.py` | The v2 naming contract and the image EXIF provenance. | `build_v2_name`, `parse_v2_name`, `build_field_dict`, `embed_all` |
| `audio_metadata.py` | The audio versions: clip name and JSON sidecar. | `build_audio_name`, `build_audio_field_dict`, `write_sidecar` |
| `capture.py` | Shared image step: fetch, embed EXIF, write `<final>.tmp`. | `capture_and_embed_to_tmp` |
| `cache.py` | The bounded ring: find the cache root (refuses to start if it's missing), scan, plan evictions, commit atomically. | `resolve_cache_root`, `assert_cache_root_available`, `stream_dir`, `scan_ring`, `plan_evictions`, `commit_capture`, `commit_capture_pair` |
| `heartbeat.py` | Pure state machine for liveness counters and the heartbeat schedule. | `Heartbeat` |
| `nodemeta.py` | Node identity resolution (section 4). | `resolve_identity` |
| `upload.py` | Beehive uploads. Used by `--one-shot` only; `--continuous` never uploads frames. | `one_shot_upload`, `cache_upload` |

Tests are in `tests/`; run them with `make test` (no camera or node needed). Some
names are inherited from the image-sampler2 lineage and kept for compatibility:
the `IS2_CACHE_ROOT` env var (also read by the consumers), `IS2_PLUGIN_VERSION`,
and `IS2_PLACEHOLDER_VSN`.

---

## 7. Suggested order for studying the stack

1. Read this page, then [README.md](../README.md) (flags and modes).
2. Read `cache.py` and `metadata.py`: the cache contract that everything else
   depends on.
3. Read `app.py` `_continuous_to_cache` to see one capture tick from start to
   finish.
4. Read sage-yolo2's README and `consumer.py` / `selection.py` / `seenstore.py`:
   the consumer side of the same contract. Then sage-birdnet2's `VENDORED.md`: the
   same consumer code plus one sidecar reader, for audio.
5. Read wes-local-cache-manager `manager/sweeper.py` (short): the Layer-2 safety
   net.
6. Read wes-nodeinfo-injection's README and `gen-wes-identity.sh`, then
   pywaggle2-nodeinfo `waggle/data/node_info_env.py`: the identity path.
7. Do a full install on a test node with the install guide, then practice
   [REBOOT-RECOVERY.md](../REBOOT-RECOVERY.md).

Good first improvements are the known limitations at the end of the install
guide, for example letting bioclip2 read every `*-crop-N` directory, or having
yolo2 mark hand-seeded frames as seen.
