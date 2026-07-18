# image-sampler2

`image-sampler2` samples still images from a camera stream. It is an enhanced fork
of the Sage/Waggle `imagesampler`, redesigned around a **producer/consumer** model:
the sampler is a cheap, CPU-only **producer** that either uploads a single frame or
maintains a bounded local **ring cache** that other plugins (inference, triggered
uploaders) consume on their own schedule.

Collecting still images is one of the fundamental ways to gather training data and
to show the visual context in which an inference was made.

> Status: under staged implementation. **Stages 0-3 shipped**: the CLI contract +
> fail-fast validation (Stage 0), single real capture (Stage 1), capture-time v2
> naming + self-describing EXIF/JSON embed (Stage 2), and the one-shot upload path
> to Beehive with fleet-portable node identity (Stage 3). The `--continuous` ring
> cache and the heartbeat land in later stages. Full design:
> `docs/imagesampler.flint.analysis.txt`.

---

## Modes (exactly one is required)

`image-sampler2` runs in **one** of two mutually-exclusive modes. You must pass
exactly one; passing both, or neither, is a fail-fast error.

| Mode | What it does |
|------|--------------|
| `--one-shot` | Capture **one** frame, queue it for cloud upload, exit. Upload-only — never writes to the cache. Cadence is external (the scheduler relaunches the pod). Best for "grab one now" and periodic cloud snapshots. |
| `--continuous SECONDS` | Run forever, capturing on a fixed period of `SECONDS`. **Local-only** — writes each frame into the ring cache and **never uploads**. This is the producer that fills the shared cache for consumers. |

---

## Command-line flags

### Mode (required, choose one)

- **`--one-shot`**
  Capture exactly one frame, queue it for cloud upload, then exit `0`.
  Upload-only: it does **not** write to `--cache-dir`. Combine with `--from-cache`
  to upload a cached frame instead of hitting the camera.

- **`--continuous SECONDS`**
  Run forever, capturing on a **fixed period** of `SECONDS` (must be a positive
  integer). Local-only: writes into the ring cache and never uploads. Requires
  `--cache-dir`, `--cache-name`, and at least one cache cap.

### Source

- **`--stream STREAM`** *(required, repeatable)*
  A named camera stream (e.g. `top_camera`, `bottom_camera`) or a raw URL
  (`rtsp://IP:PORT/...`). Repeat `--stream` to sample multiple streams; each runs
  in its own worker process. At least one is required.

- **`--name NAME`** *(optional, repeatable)*
  Human label to report for a stream. If given, the number and order of `--name`
  values **must match** the `--stream` values. If omitted, the stream id is used
  as the name.

- **`--from-cache DIR`** *(one-shot only)*
  Instead of hitting the camera, upload the **newest** image already present in
  the cache directory `DIR` (populated by a `--continuous` producer). Does not
  touch the camera, does not write, does not evict. This is the composable
  "periodic uploader": pair a continuous producer with a scheduled
  `--one-shot --from-cache` uploader. Only valid with `--one-shot`.

### Camera connection (native-still fetch)

The camera address may be given by flag or environment variable. **Credentials
are environment-only** (`CAMERA_USER` / `CAMERA_PASSWORD`) and are never accepted
as flags, so they do not appear in process arguments, shell history, or logs (the
password is redacted in log output).

- **`--camera-host HOST`** — camera IP/host for the native-still fetch. Defaults to
  env `CAMERA_HOST`. Required for a from-camera capture.
- **`--camera-port PORT`** — camera HTTP port (default env `CAMERA_PORT` or `80`).
- **`--camera-channel N`** — camera channel (default env `CAMERA_CHANNEL` or `0`).
- **`--capture-timeout SECONDS`** — hard timeout for a single capture (default `10`).
- **`CAMERA_USER` / `CAMERA_PASSWORD`** *(environment only)* — camera credentials.

### Node / provenance identity

Embedded into the EXIF and attached to the upload meta. On a real Sage node these
are read automatically from `/etc/waggle/node-manifest-v2.json` (and the
`/etc/waggle/vsn` / `node-id` files) — so the plugin self-identifies on any node
with no per-node configuration. The flags below OVERRIDE the manifest.

- **`--vsn VSN`** — node VSN (e.g. `H00F`). Overrides the manifest. Used in the v2
  filename and EXIF. Fatal if it cannot be resolved (flag or manifest).
- **`--node-id ID`** — node hardware id. Overrides manifest `.name` / `/etc/waggle/node-id`.
- **`--lat DEG` / `--lon DEG`** — node latitude/longitude (decimal degrees).
  Override manifest `.gps_lat` / `.gps_lon`. Omitted from EXIF GPS if unresolved;
  negative values stored correctly (absolute value + N/S/E/W ref).
- **`--node-manifest PATH`** — path to the node manifest JSON (default env
  `WAGGLE_NODE_MANIFEST` or `/etc/waggle/node-manifest-v2.json`). For testing / off-node use.
- **`--job NAME`** — job name for provenance. Default env `WAGGLE_JOB_NAME` or `sage`.
- **`--task NAME`** — task name. Default env `WAGGLE_TASK_NAME` or `image-sampler2`.
- **`--plugin-version REF`** — plugin image `ref:version` recorded in EXIF/meta.

### Ring cache (continuous only)

- **`--cache-dir DIR`** *(required with `--continuous`)*
  Directory that holds the per-stream ring cache. Must already exist and be
  writable (fail-fast otherwise). Each stream writes into its own subdirectory
  `DIR/<camera>/`.

- **`--cache-name NAME`** *(required with `--continuous`)*
  Stable, filesystem-safe identifier for this cache instance, so consumer plugins
  can find it and two different configurations on the same camera do not collide.
  Allowed characters: letters, digits, dot (`.`), dash (`-`), underscore (`_`).
  No path separators or whitespace.

- **`--cache-max-count N`** *(continuous; at least one cap required)*
  Maximum number of images kept **per stream** in the ring. When exceeded, the
  oldest images are evicted first.

- **`--cache-max-mb MB`** *(continuous; at least one cap required)*
  Maximum total size of the per-stream ring, in decimal megabytes (10^6 bytes).
  When exceeded, the oldest images are evicted first.

  You may set one or both caps; eviction triggers when **either** would be
  exceeded. At least one cap is required with `--continuous` — an unbounded cache
  is not allowed.

---

## Fail-fast rules

Invalid flag combinations are rejected **before any work** with a clear message and
a nonzero exit code (config errors exit `2`):

- Not exactly one mode (`--one-shot` XOR `--continuous`).
- `--continuous SECONDS` not a positive integer.
- No `--stream` given.
- `--name` count does not match `--stream` count.
- Cache flags (`--cache-dir`, `--cache-name`, `--cache-max-count`, `--cache-max-mb`)
  used with `--one-shot` (they are continuous-only).
- `--from-cache` used with `--continuous` (it is one-shot-only).
- `--continuous` without `--cache-dir`, without `--cache-name`, or with no cap.
- `--cache-dir` does not exist or is not writable.
- `--cache-name` contains illegal characters (path separators, spaces, etc.).
- A cache cap set to a non-positive value.

---

## Usage examples

Capture one image and upload it:

```bash
python3 app.py --one-shot --stream top_camera
```

Capture one image from each of two streams, with labels:

```bash
python3 app.py --one-shot \
  --stream bottom_camera --name street \
  --stream top_camera    --name sky
```

Run a continuous producer that keeps the last 500 images (or 1 GB) per stream:

```bash
python3 app.py --continuous 60 \
  --stream top_camera \
  --cache-dir /run/waggle/cache \
  --cache-name hummingcam \
  --cache-max-count 500 \
  --cache-max-mb 1000
```

Periodically upload the newest cached frame (the composition pattern — pair with a
continuous producer, schedule this on a cron cadence):

```bash
python3 app.py --one-shot --stream top_camera \
  --from-cache /run/waggle/cache/hummingcam/top_camera
```
