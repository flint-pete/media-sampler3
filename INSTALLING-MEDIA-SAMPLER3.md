# Installing media-sampler3 and the full media stack on a Sage Thor node

This guide takes a clean, WES-provisioned Sage **Thor (AGX, ARM64)** node and
installs every component of the media stack, then proves it works end to end with
two bird cascades: images (camera → frames → detector → crops → species
classifier → Beehive) and audio (camera microphone → clips → BirdNET → Beehive).

Every command here was run on live Thor nodes:

- **H041 (Sep 2026):** the full cascade ran with two real Reolink cameras, and
  seeded test birds were classified correctly (*Cardinalis cardinalis* and
  *Eopsaltria australis*).
- **H039 (Oct 2026):** a fresh install, following this guide from top to bottom
  with no cameras attached. Steps 0–5 and 6a–6g all passed, including the seeded
  bird image and the seeded bird-song clip (*Sialia sialis*), with results reaching
  Beehive. The live-camera step (6h) is next.
- H038 was the first attempt (see the prerequisite note).

> **GPU.** Thor nodes run pods with NVIDIA's container runtime by default: the
> Sage CI team set k3s `default-runtime: nvidia` across the fleet in Oct 2026. So a
> CUDA plugin launched with `pluginctl` or the scheduler sees the Thor GPU with no
> extra flags. Verified on H039: sage-yolo2 logs `on cuda`, and `/dev/nvidia*` and
> `torch.cuda.is_available()` = True are visible inside the pod, and sage-bioclip2
> logs `on cuda` too. To check a consumer, see Step 6c, "Is it using the GPU?".

**Three companion documents in this repo:**

| Document | Read it when |
|----------|--------------|
| [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) | **Before you start.** What each component does, why, and how data and identity flow between them. |
| [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md) | **After any node reboot or power cut.** The stack is side-loaded, not part of the WES base image, so parts of it must be restarted by hand. |
| [DESIGN-PATH.md](DESIGN-PATH.md) | When you want to know *why* a component looks the way it does. It summarizes how each design evolved and links each repo's `docs/history/`. |

---

## The components (with links)

**Four components to install:**

| # | Component | What it is | Repo |
|---|-----------|-----------|------|
| 1 | **wes-local-cache-manager** | A DaemonSet that keeps the shared `/local-cache` directory bounded: per-unit and per-node byte caps, oldest files evicted first. | https://github.com/flint-pete/wes-local-cache-manager |
| 2 | **wes-nodeinfo-injection** | A WES change that publishes 5 node-identity values (VSN, node id, GPS lat/lon, mobility) in the `wes-identity` ConfigMap, plus a patched `pluginctl` (`pluginctl-nodeinfo`) whose pods receive them. (Its optional patched scheduler does the same for SES jobs; not needed here.) | https://github.com/flint-pete/wes-nodeinfo-injection |
| 3 | **pywaggle2-nodeinfo** | The **reader** side of #2. `read_node_info()` turns those env vars into a clean `NodeInfo`. It is a library that gets copied ("vendored") into plugin images; nothing is deployed on the node. | https://github.com/flint-pete/pywaggle2-nodeinfo |
| 4 | **media-sampler3** | The **producer**. Captures JPEG stills (and optionally FLAC audio) from a camera into `/local-cache` as a bounded ring of self-describing files. | https://github.com/flint-pete/media-sampler3 |

**Three test consumers.** They exist to prove the stack works, and they are also
working examples of how to write a cache consumer, two for images and one for
audio:

| Consumer | What it does | Repo |
|----------|-------------|------|
| **sage-yolo2** | Reads cached frames, detects objects (here, birds), publishes counts, and writes crops of each detection back into the cache. | https://github.com/flint-pete/sage-yolo2 |
| **sage-bioclip2** | Reads yolo2's crops, classifies the species with BioCLIP-2.5, and publishes the result. | https://github.com/flint-pete/sage-bioclip2 |
| **sage-birdnet2** | Reads media-sampler3's audio clips, identifies bird (and other) sounds with BirdNET V2.4, and publishes detections. | https://github.com/flint-pete/sage-birdnet2 |

Each repo's README explains its code and has a "where this fits" section that
links back here.

Step 0 clones `master` of all seven repos; that is what this guide was tested
against. The images it builds are `localhost/media-sampler3:0.1.1`,
`.../sage-yolo2:2.1.1`, `.../sage-bioclip2:2.1.0` and `.../sage-birdnet2:2.0.1`.

---

## How the pieces fit together

```
        ┌─────────────────┐  writes JPEG   ┌──────────────────────────┐
camera ─▶│  media-sampler3 │──── ring ─────▶│  /local-cache/camera/top │
 (HTTP)  │   (PRODUCER)    │                └────────────┬─────────────┘
        └─────────────────┘                             │ reads (never deletes)
                                                        ▼
                                              ┌───────────────────┐ writes crops
                                              │    sage-yolo2     │─────────────┐
                                              │  detect + crop    │             ▼
                                              └─────────┬─────────┘  /local-cache/camera-crops/
                                                        │ env.count.*      top-crop-0/
                                                        ▼ to Beehive            │ reads
                                              ┌───────────────────┐             │
                                              │   sage-bioclip2   │◀────────────┘
                                              │ classify species  │ env.species.* to Beehive
                                              └───────────────────┘

        ┌─────────────────┐  writes FLAC   ┌───────────────────────────────┐
cam mic ▶│  media-sampler3 │─(+ .json)─────▶│ /local-cache/camera-audio/mic │
         │  --media audio  │                └───────────────┬───────────────┘
        └─────────────────┘                                 ▼ reads
                                              ┌───────────────────┐
                                              │   sage-birdnet2   │ env.detection.* to Beehive
                                              │  BirdNET (audio)  │
                                              └───────────────────┘

wes-local-cache-manager ── bounds /local-cache (byte caps, oldest-first eviction)
wes-nodeinfo-injection  ── publishes VSN + GPS in the wes-identity ConfigMap
pywaggle2-nodeinfo      ── the library a plugin uses to READ those values
```

There are two separate data flows; keep them apart in your head:

- **Cache (on the node).** media-sampler3 fills `/local-cache`. The consumers
  only read it and never delete (yolo2 also writes its crops there). Each producer bounds its own ring, and the cache
  manager is the safety net for the whole directory.
- **Cloud (Beehive).** The consumers publish results (`env.count.*`,
  `env.species.*`, `env.detection.*`) through WES (rabbitmq → upload-agent → Beehive). The producer
  publishes only liveness heartbeats (`env.mediasampler.cache.*`).

[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) covers paths, file naming, identity,
and the seen-store in detail.

---

## PREREQUISITE: the node must be a fully WES-provisioned Sage node

These components are **add-ons to the Waggle Edge Stack (WES)**, not a replacement
for it. `pluginctl` (which launches every plugin below) needs WES-provided cluster
objects. Without them it fails with:

```
Error: pods "<name>" is forbidden: error looking up service account
default/wes-plugin-account: serviceaccount "wes-plugin-account" not found
```

Before you start, check that every line below returns results:

```bash
ssh <user>@node-<VSN>.sage
sudo k3s kubectl get sa -n default | grep -E 'wes-plugin-account|wes-plugin-scheduler'
sudo k3s kubectl get pods -n default | grep -E 'wes-rabbitmq|wes-plugin-scheduler|wes-upload-agent|wes-scoreboard'
sudo k3s kubectl get clusterrole | grep -E 'wes-plugin-role|wes-plugin-scheduler'
```

If anything is missing, the node has not been WES-provisioned. Have it
(re)provisioned through the Sage node-setup / Beekeeper process; that is out of
scope here. (H038 started without WES. After a proper reinstall everything below
worked.)

**You need passwordless `sudo` on the node.** Installing, recovering after a reboot,
and launching every plugin all use it. Only root's kubeconfig may create pods in
the `default` namespace, and the image builds and imports need root.

The node also needs (a standard Thor has them all, but check): `podman`,
`docker`, `k3s`, `pluginctl`, `git`, `make`, `curl`, and **working DNS**, because
image builds pull base images from Docker Hub and `nvcr.io`. Host Go is **not**
needed (Step 3 compiles Go inside a container).

---

## Conventions used below

- `$VSN` is this node's VSN. Set it once per shell:
  `VSN=$(cat /etc/waggle/vsn); echo $VSN`
- `sudo k3s kubectl` is used for every cluster command. (`sudo kubectl` works
  too; on these nodes `kubectl` is a link to `k3s`.)
- **Build every image from source on the node.** Don't copy prebuilt images from
  another node, and don't `pull` from the Sage registry: these images have only
  ever been side-loaded, so a registry pull returns `not found`. (The Sage ECR
  portal can build Thor images, GPU/CUDA ones included; these just haven't been
  published yet.)
- **Launch plugins with `sudo pluginctl-nodeinfo run`**, the patched `pluginctl`
  you install in Step 3. It takes exactly the same flags as `pluginctl`; the only
  difference is that its pods receive the node's identity (VSN, GPS, ...).
- **Side-loaded means not reboot-durable.** Pods do not come back after a reboot.
  See [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md).

---

## Step 0: Get the source onto the node

```bash
mkdir -p ~/AI-projects && cd ~/AI-projects
for r in wes-local-cache-manager wes-nodeinfo-injection pywaggle2-nodeinfo \
         media-sampler3 sage-yolo2 sage-bioclip2 sage-birdnet2; do
  [ -d "$r" ] || git clone https://github.com/flint-pete/$r.git
done
```

---

## Step 1: Component 1, wes-local-cache-manager (and the cache directory)

The shared cache is a host directory, `/media/plugin-data/local-cache`, mounted
into pods as `/local-cache`. It is world-writable with the sticky bit, so pods
running as different users can each own their own subtree and still read
everyone else's.

One script creates that directory, builds a small stdlib-Python image
(`python:3.12-slim` base), imports it into k3s, applies the ConfigMap and
DaemonSet, and verifies them. Read the script first; every step is annotated.

```bash
cd ~/AI-projects/wes-local-cache-manager
./test-add-node.sh          # mkdir/chmod 1777 -> podman build -> side-load -> apply -> verify
```

Verify:

```bash
ls -ld /media/plugin-data/local-cache                         # drwxrwxrwt ... root root
sudo k3s kubectl get daemonset wes-local-cache-manager        # DESIRED = READY = 1
sudo k3s kubectl logs -l app.kubernetes.io/name=wes-local-cache-manager --tail=5
```

Expected log line:
`sweep ok: 0 units, node_total=0 bytes (node cap ..., per-unit cap ...)`.

What it enforces. Defaults can be overridden in the `wes-local-cache-manager-env`
ConfigMap:

- A **unit** is any directory 2 levels below the root (`CACHE_UNIT_DEPTH=2`), for
  example `camera/top` (`<cache-name>/<source>`). Each unit is capped at 2 GiB,
  the whole node at 15 GiB, and the sweep runs every 60 s.
- `/local-cache/.state/` is reserved and **never evicted**. Consumers keep their
  "seen-store" there, so they don't reprocess everything after a restart.
  **Never delete `.state/`.**
- The DaemonSet has no nodeSelector. On a single-node field cluster it runs on
  this node only.

Design and caps model:
[DESIGN-AND-PURPOSE](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md).
Teardown: `./test-remove-node.sh` (add `WIPE_CACHE=1` to also empty the cache).

---

## Step 2: Component 2, the node-identity ConfigMap (`wes-identity`)

Steps 2 and 3 both use wes-nodeinfo-injection's `node-test/` directory. The
scripts back up every object they touch, and teardown restores from that backup
([node-test/README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/node-test/README.md)).

WES already keeps a ConfigMap called `wes-identity` with 2 values. This script
regenerates it with 5, taken from this node's manifest, then launches a small
reader pod that runs pywaggle2's `read_node_info()` logic to prove the values are
readable. Look at the stock ConfigMap first, so you can see what changes:

```bash
sudo k3s kubectl get configmap wes-identity -o jsonpath='{.data}'; echo
#   -> {"WAGGLE_NODE_ID":"...","WAGGLE_NODE_VSN":"<VSN>"}

cd ~/AI-projects/wes-nodeinfo-injection/node-test
./test-add-configmap.sh
```

Expected output, indented by the script (values are this node's):

```
    === pywaggle2 NodeInfo (from wes-identity ConfigMap) ===
    {
      "vsn": "<VSN>",
      "node_id": "...",
      "lat": 41.7,
      "lon": -87.9,
      "mobility": "unknown",
      "vsn_is_placeholder": false
    }
    === raw WAGGLE_NODE_* env seen by the pod ===
      WAGGLE_NODE_ID=...
      WAGGLE_NODE_VSN=<VSN>
      WAGGLE_NODE_GPS_LAT=...
      ...
```

`mobility` is `"unknown"` because no fleet manifest sets it yet. If the manifest
has no GPS, `lat`/`lon` come back `null`; the reader never invents coordinates.
Re-run the `get configmap` command: it now shows all 5 variables. Revert with
`./test-remove-configmap.sh`.

---

## Step 3: Component 2, the patched `pluginctl` (`pluginctl-nodeinfo`), and Component 3

A ConfigMap does nothing on its own: a pod only sees these variables if its spec
says `envFrom: wes-identity`. The stock `/usr/bin/pluginctl` builds pods itself,
without that line, so its pods get nothing. The fix is a one-function patch
(0002) to the pod-building code that `pluginctl` and the WES scheduler share.
This step builds the patched code and installs the resulting `pluginctl` as
`/usr/local/bin/pluginctl-nodeinfo`. The stock `pluginctl` is never touched, and
no WES object changes:

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
mkdir -p ../.upstream
[ -d ../.upstream/edge-scheduler ] || {
  git clone https://github.com/waggle-sensor/edge-scheduler.git ../.upstream/edge-scheduler
  git -C ../.upstream/edge-scheduler checkout 5391a00          # the tested base commit
  git -C ../.upstream/edge-scheduler apply \
      "$(realpath ../patches/0002-edge-scheduler-envfrom-wes-identity.patch)"; }
./install-pluginctl-nodeinfo.sh     # ~3 min the first time (podman build)
```

Every pod it launches carries the node's identity. You can check any of them:

```bash
sudo k3s kubectl get pod <name> -o jsonpath='{.spec.containers[0].envFrom}'; echo
#   -> [{"configMapRef":{"name":"wes-identity","optional":true}}]
```

What that buys you in this cascade (verified on H039):

- **media-sampler3** fills VSN and GPS into every frame's EXIF by itself; no
  `--vsn`/`--lat`/`--lon` flags.
- **yolo2/bioclip2** attribute results with each frame's EXIF identity, and use
  the pod's identity as a cross-check and as a GPS fallback. On H039 a frame with
  no GPS still produced records with the node's lat/lon (`location_source: node`).
- **sage-birdnet2** uses the node's GPS to restrict BirdNET to the species eBird
  expects here and now.
- **Beehive** attaches the VSN to every record during routing in any case.

Good to know:

- A value you pass explicitly (`-e WAGGLE_NODE_VSN=...`, or a plugin flag like
  `--vsn`) wins over the ConfigMap.
- `--env-from <file>` (used for camera credentials) works alongside it.
- If the ConfigMap is missing, pods still start (`optional: true`), just without
  the variables.
- Uninstall: `sudo rm /usr/local/bin/pluginctl-nodeinfo`.

**Component 3 (pywaggle2-nodeinfo)** has no install step. Its `read_node_info()`
is copied into the plugin images (sage-yolo2 and sage-bioclip2 `node_info.py`; see
[VENDORED.md](https://github.com/flint-pete/sage-yolo2/blob/master/VENDORED.md)).
media-sampler3's `nodemeta.py` implements the same rules. Contract:
[DESIGN](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md).

> **Installing WES resets `wes-identity`.** Re-provisioning a node, or running
> WES's `update-stack.sh`, regenerates the ConfigMap with only its 2 stock values.
> Re-run Step 2 (`./test-add-configmap.sh`) after any WES (re)install or update;
> [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md) checks for this.

> **SES jobs (not needed here).** Pods that the WES *scheduler* launches get the
> same identity only if the scheduler itself is replaced with the patched one
> ("Tier 2"). That swaps a control-plane component, so it lives in
> wes-nodeinfo-injection's
> [node-test/README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/node-test/README.md),
> not in this guide.

---

## Step 4: Component 4, build media-sampler3 (the producer)

This uses a CPU base image (`python:3.12-slim`) and builds in about a minute:

```bash
cd ~/AI-projects/media-sampler3
make sideload       # = sudo podman build -t localhost/media-sampler3:0.1.1 .
                    #   + podman save | sudo k3s ctr images import -
                    #   + confirm the image is listed
```

Flags and file contract: [README](README.md).

---

## Step 5: Build the test consumers (sage-yolo2, sage-bioclip2, sage-birdnet2)

These use a GPU/CUDA base image (`nvcr.io/nvidia/pytorch:25.08-py3`) and are
**large builds**: yolo2 is about 10 GiB and bioclip2 about 17 GiB. Check `df -h /`
first. Each took 5–30 minutes, mostly downloading the CUDA base image and
importing into k3s (on H039, with the base cached: yolo2 6 min, bioclip2 10 min).
Each repo's `scripts/deploy-sideload.sh` reads name, namespace and version from
its `sage.yaml`, then builds and imports (add `--dry-run` to only print the plan):

```bash
cd ~/AI-projects/sage-yolo2    && scripts/deploy-sideload.sh --skip-register
cd ~/AI-projects/sage-bioclip2 && scripts/deploy-sideload.sh --skip-register
cd ~/AI-projects/sage-birdnet2 && scripts/deploy-sideload.sh --skip-register   # CPU image, a few minutes
```

Notes:

- `--skip-register` skips the ECR catalog record, which `pluginctl` doesn't need,
  so no `SAGE_TOKEN` is required.
- **The import step is slow but safe.** Importing the 10 GiB yolo2 image took
  about 7 minutes on H041 and loads the node's disk I/O (SSH may feel sluggish).
  Build bioclip2 right after yolo2 so it reuses the cached CUDA base layer.
- sage-birdnet2 is small and CPU-only (`python:3.12-slim` + BirdNET/TensorFlow).
- The resulting names are `registry.sagecontinuum.org/beckman/sage-yolo2:2.1.1`,
  `.../sage-bioclip2:2.1.0` and `.../sage-birdnet2:2.0.1`. That's only the side-loaded image's *name*;
  nothing is pulled from the registry.
- More detail:
  [sage-yolo2 DOCKER-BUILD](https://github.com/flint-pete/sage-yolo2/blob/master/DOCKER-BUILD.md).

---

## Step 6: Run and validate the cascade

One sequence, whether or not a camera is attached yet: check the producer (6a),
prove both chains with a known bird image and a known bird-song clip (6b–6g),
then attach the live camera (6h).

Every launch is `sudo pluginctl-nodeinfo run`, and two flags appear on every one:

- `--selector zone=core` is required because the pods mount a host directory (`-v`).
- `-v /media/plugin-data/local-cache:/local-cache` is what gives the pod the
  shared cache.

`pluginctl run` stays attached to the pod's log, so each launch ends with `&`
(or use separate shells). Define the cloud query helper once:

```bash
q() { curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
        -H 'Content-Type: application/json' \
        -d "{\"start\":\"-30m\",\"filter\":{\"vsn\":\"$VSN\",\"name\":\"$1\"}}" | tail -2; }
```

### 6a. Check the producer (no camera needed)

Point media-sampler3 at an address that never answers (`192.0.2.10` is reserved
for documentation) with dummy credentials. This proves the image, the cache
mount, the node identity and the heartbeat path to Beehive all work:

```bash
umask 077; printf 'CAMERA_USER=dummy\nCAMERA_PASSWORD=dummy\n' > ~/ms3-dummy-creds.env
sudo pluginctl-nodeinfo run --name ms3-smoke --selector zone=core \
  --env-from ~/ms3-dummy-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.1 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name smoke \
  --cache-max-count 20 --heartbeat-secs 20 \
  --camera-host 192.0.2.10 &
sleep 60; sudo k3s kubectl logs ms3-smoke | tail -4
#   "capture skipped: capture timeout ..." every 10 s, and
#   "heartbeat: ... status=skip" every 20 s. The pod stays Running.
#   There must be NO "node VSN not resolvable" warning (Step 3 working).
q env.mediasampler.cache.written     # rows with "task":"ms3-smoke"
```

Clean up:

```bash
sudo pluginctl rm ms3-smoke; rm -f ~/ms3-dummy-creds.env
sudo rmdir /media/plugin-data/local-cache/smoke/top /media/plugin-data/local-cache/smoke
```

**If the producer exits right away,** it found a configuration problem. It exits
with code 2 and a one-line reason (`sudo k3s kubectl logs <name>`):

| Message | Fix |
|---|---|
| `at least one --stream is required` | add `--stream <label>` |
| `--continuous requires at least one of --cache-max-count / --cache-max-mb` | add a cap; an unbounded ring is refused |
| `set CAMERA_USER and CAMERA_PASSWORD in the environment` | pass `--env-from <file>` (never put credentials in flags) |
| a message about `/local-cache` not existing | add `-v /media/plugin-data/local-cache:/local-cache` |

### 6b. Seed a known bird (image and sound)

Real frames and clips may contain no birds, and then the consumers correctly find
nothing. To prove both chains deterministically, put a known bird image and a
known bird-song clip where the producer would write them, named like real
captures. The image is sage-yolo2's cardinal test fixture. The clip is 15 s of an
Eastern Bluebird in the camera-mic format, from sage-birdnet2's `tests/test-audio/`
(CC BY-SA; attribution in that folder's README):

```bash
sudo mkdir -p /media/plugin-data/local-cache/camera/top /media/plugin-data/local-cache/camera-audio/mic
SEED=/media/plugin-data/local-cache/camera/top/$(date +%s%N)-v2-$VSN-top.jpg
sudo cp ~/AI-projects/sage-yolo2/tests/test-images/bird-cardinal-sample.jpg "$SEED"
SEED_AUDIO=/media/plugin-data/local-cache/camera-audio/mic/$(date +%s%N)-v2-$VSN-mic.flac
sudo cp ~/AI-projects/sage-birdnet2/tests/test-audio/eastern-bluebird-XC179669.flac "$SEED_AUDIO"
```

### 6c. Run sage-yolo2 (detector and crop producer)

GPU consumers **must** set a memory limit, or the kernel kills them (OOMKilled,
exit 137):

```bash
sudo pluginctl-nodeinfo run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.1 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops &
```

It processes the seeded bird on its first wake, right after it starts. What the
flags mean:

- `--every 5m --all-unseen --max-frames 0`: wake every 5 minutes and process
  every frame not yet seen.
- `-e WAGGLE_JOB_NAME/-e WAGGLE_TASK_NAME`: these name the consumer's seen-store
  (`/local-cache/.state/sage-yolo2/camera-sage-yolo2/...`). Keep them the same
  every time you relaunch. Without them, seen-memory is tied to the pod and lost
  on every restart.
- `--conf-thres 0.25` vs `--crop-match bird:0.4`: birds at confidence 0.25 or
  higher are counted (`env.count.bird`), but only those at 0.4 or higher are
  cropped. So counts and crops can legitimately differ.
- `--crop-cache-name camera-crops`: crops go to
  `/local-cache/camera-crops/<name>-crop-<i>/`, where `<i>` is the detection's
  position in the frame. The first bird goes to `top-crop-0`, a second bird in the
  same frame to `top-crop-1`, and so on.

**Is it using the GPU?** The startup log says which device was chosen:

```bash
sudo k3s kubectl logs sage-yolo2-consumer | grep 'Loading yolo11x.pt on'
#   "... on cuda"  -> GPU
#   "... on cpu"   -> CPU
```

On H039 it says `on cuda` (Oct 2026, after the fleet-wide NVIDIA-runtime fix).
If it says `on cpu`, the pod can't see the GPU. Check that the node's k3s config
has `default-runtime: nvidia` (`sudo grep default-runtime /etc/rancher/k3s/config.yaml`),
and ask the Sage CI team if it's missing. The results are the same on the CPU,
just slower (YOLO11x took about 2 s per frame).

### 6d. Run sage-bioclip2 (species classifier)

```bash
sudo pluginctl-nodeinfo run --name sage-bioclip2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-bioclip2 \
  registry.sagecontinuum.org/beckman/sage-bioclip2:2.1.0 -- \
  --source cache --input /local-cache/camera-crops/top-crop-0 \
  --every 10m --all-unseen --max-frames 0 --rank Species --min-confidence 0.1 &
```

The `--input` paths chain together: the producer's `--cache-name`/`--name`
determine yolo2's `--input`, and yolo2's `--crop-cache-name` plus the producer's
`--name` determine bioclip2's `--input`. If you change one, fix the next.

Good to know:

- **One bioclip2 reads one crop directory**, so only the first bird in each frame
  (`top-crop-0`) is classified. Run one bioclip2 per `top-crop-N` to cover more;
  teaching bioclip2 to read all of them is an open improvement.
- It runs on the GPU when the pod can see one. Check with
  `sudo k3s kubectl logs sage-bioclip2-consumer | grep 'classifier loaded on'`.
  On H039 a crop took 0.12 s on CUDA vs 1.86 s on the CPU, with the same result.
- It needs no internet access. The model is baked into the image and loaded
  offline (`HF_HUB_OFFLINE=1`; see "Sage adjustments" in sage-bioclip2's README).

### 6e. Run sage-birdnet2 (audio classifier)

```bash
sudo pluginctl-nodeinfo run --name sage-birdnet2-consumer --selector zone=core \
  --resource limit.memory=2Gi,request.memory=1Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-birdnet2 \
  registry.sagecontinuum.org/beckman/sage-birdnet2:2.0.1 -- \
  --source cache --input /local-cache/camera-audio/mic \
  --every 10m --all-unseen --max-frames 0 --min-confidence 0.6 &
```

It classifies the seeded clip on its first wake. Its log shows the steps:

```bash
sudo k3s kubectl logs sage-birdnet2-consumer | grep -E 'Location from|Geo filter|Classified|Sialia'
#   Location from node identity: (<lat>, <lon>)        <- node GPS, from Step 3
#   Geo filter: 131 species expected at this location/time
#   Classified <ts>-v2-<VSN>-mic.flac: 3 detections
#     Sialia sialis (Eastern Bluebird): 0.9996 [0.0-3.0s] ...
```

What's different from the image consumers:

- **Input:** `--input` is the audio producer's `--cache-name`/`--stream`
  (`camera-audio`/`mic`).
- **Metadata:** audio has no EXIF, so each clip's provenance is in a
  `<clip>.flac.json` sidecar. The seeded clip has none, which is why the log warns
  "no metadata sidecar". Like the seeded image, it's then processed using only its
  filename.
- **Location:** BirdNET restricts its species list to what eBird expects at this
  place and week. It takes the place from the pod's node GPS (Step 3). Without it,
  the filter is off, and you get more false positives.
- **Runs on the CPU, with no GPU involved.** It measured about 0.5 GB of memory on
  H039.

### 6f. Verify end to end

```bash
sudo k3s kubectl get pods | grep -iE 'yolo2-consumer|bioclip2-consumer|birdnet2-consumer'   # Running
sudo ls -R /media/plugin-data/local-cache/camera-crops/ | head          # a top-crop-0 crop
q env.count.bird                          # yolo2: value 1, meta has this node's vsn, lat, lon
q env.species.species                     # bioclip2: "Cardinalis cardinalis"
q env.detection.biophony.sialia_sialis    # birdnet2: confidence, meta has common_name, lat, lon
q env.detection.audio.summary             # birdnet2: one per clip, even with no detections
```

On H041, and again on H039, this produced `env.count.bird = 1`, a crop under
`camera-crops/top-crop-0/`, and then bioclip2 published **`Cardinalis
cardinalis`** at 100% confidence, all tagged with the node's VSN (and, on H039,
its lat/lon). A second test on H041 with an Australian robin returned *Eopsaltria
australis*. On H039 the bluebird clip came back as **`Sialia sialis`** in three
3-second windows (0.89–0.9996), and the summary listed 1 species.

Two things that surprise people when re-running this test:

- **Re-seeding the same image doesn't re-trigger bioclip2.** yolo2 crops it again,
  but the crop has the same pixels, so it has the same `unique_id` (a hash of the
  crop), and bioclip2's seen-store skips it, even after a reboot. Appending bytes
  to the file doesn't help. Use a different bird image, or a mirrored copy
  (`ImageOps.mirror`, see REBOOT-RECOVERY.md step 9).
- **Records carry the capture time, not the processing time.** The `q` window
  (`-30m`) must cover the item's timestamp, which is the seeded file's name.

### 6g. Remove the seeded bird and clip

Neither was written by media-sampler3, so they lack the `unique_id` (EXIF for the
image, sidecar for the clip) that consumers use to mark an item seen. yolo2 and
birdnet2 would reprocess them, and republish, on every wake. (A known limitation,
listed in the consumers' READMEs.)

```bash
sudo rm "$SEED" "$SEED_AUDIO"   # just the seeded files; never wildcard here once real data exists
```

Leave the three consumers running; they pick up live frames and clips from 6h.

### 6h. Attach the live camera

**How media-sampler3 talks to a Reolink camera.** It uses the camera's **HTTP
port** (`--camera-port`, default 80), for both images (the snapshot API,
`/cgi-bin/api.cgi?cmd=Snap`) and the optional audio (the HTTP-FLV sub-stream,
recorded by ffmpeg). RTSP is not used. So the one network requirement is that the
node can reach the camera's HTTP port. Check this first; no credentials needed.
(On H038 the cameras were on an unroutable subnet and nothing worked.)

```bash
CAM_IP=<CAM_IP>
curl -s -o /dev/null -w 'http(80): %{http_code}\n' --max-time 5 "http://$CAM_IP/"
```

> **Cold-boot gotcha (seen on H041 after a power cut).** A Reolink can come back
> with its web page up (`http://cam/` returns 200) while `api.cgi` still returns
> **404**, because its media services haven't started yet. The producer retries
> every capture period and recovers on its own once the API answers. If it stays
> 404 for more than 15 minutes, power-cycle the camera.

**Credentials come from a mode-600 env file on the node, never the command line**
(and never into docs, commits or chat). They aren't secret from cluster admins,
though: `pluginctl --env-from` copies them into the pod spec as plain `env`
values, visible to anyone who can `kubectl get pod -o yaml` in `default`. (A
Kubernetes Secret is the proper fix for scheduled jobs.)

```bash
umask 077
cat > ~/ms3-cam-creds.env <<'EOF'
CAMERA_USER=<USER>
CAMERA_PASSWORD=<PASS>
EOF
chmod 600 ~/ms3-cam-creds.env
```

Launch the producer:

```bash
sudo pluginctl-nodeinfo run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.1 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 \
  --camera-host "$CAM_IP" --camera-port 80 &

sleep 30; sudo ls -lt /media/plugin-data/local-cache/camera/top/ | head -3   # new JPEGs every 10 s
q env.mediasampler.cache.count       # producer heartbeat
q env.count.total                    # yolo2, every frame (0 = ran and saw nothing), after its next wake
```

What the naming flags do:

- `--cache-name camera` plus `--name top` puts frames in `/local-cache/camera/top/`,
  which is exactly yolo2's `--input`.
- `--name` also becomes the `<source>` in each filename, `<ts>-v2-<VSN>-top.jpg`,
  and the `camera` field in EXIF; yolo2 uses it to name its crop directories
  (`top-crop-N`).
- `--stream` is the stream label, and it is **required**. It's used for the names
  only when `--name` is omitted.
- Optional: `--plugin-version localhost/media-sampler3:0.1.1` records the image
  version in each frame (otherwise `media-sampler3:dev`).

On H041 the producer wrote 3840×2160 JPEGs (240–370 KB) every 10 s.

**More cameras.** Run one producer per camera with its own `--name` (for example
`side`, giving `camera/side/`), its own pod `--name`, and its own `--camera-host`.
Each new camera also needs its own yolo2 instance (`--input
/local-cache/camera/side`, a different pod name and `WAGGLE_TASK_NAME`), or
nothing reads its frames.

**Audio producer (feeds sage-birdnet2).** A second, independent media-sampler3
pod records a 15 s FLAC clip once a minute from the camera's microphone, each with
a `<clip>.flac.json` metadata sidecar that includes the node's GPS. It was proven
on H00F.

```bash
sudo pluginctl-nodeinfo run --name camera-audio-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.1 -- \
  --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
  --camera-host "$CAM_IP" --camera-port 80 \
  --audio-format flac --bandpass-fmax 8000 \
  --stream mic --cache-name camera-audio \
  --cache-max-count 500 --cache-max-mb 2000 --heartbeat-secs 60 &
# clips land in /local-cache/camera-audio/mic/, exactly birdnet2's --input

sleep 75; sudo ls -lt /media/plugin-data/local-cache/camera-audio/mic/ | head -3   # .flac + .flac.json
q env.detection.audio.summary        # birdnet2, after its next wake (every 10 min)
```

> **Config convention.** Keep the non-secret camera map (IP, port, label) in your
> notes. Keep the username/password only in the mode-600 env file on the node.

---

## After a reboot

Nothing installed here is in the WES base image. A reboot or power cut kills
every plugin pod, and they do **not** restart on their own. Follow
**[REBOOT-RECOVERY.md](REBOOT-RECOVERY.md)**: an ordered, copy-paste checklist
that verifies what survived, re-applies what didn't, and relaunches the producer
and consumers with the same commands as Step 6.

---

## Known limitations and open items

- The stack is side-loaded, so it isn't reboot-durable (see REBOOT-RECOVERY.md).
  The lasting fix is publishing the images to the registry and folding the
  nodeinfo change (ConfigMap generator, patched scheduler and `pluginctl`) into
  the WES base stack. That's CI-team work, tracked in
  [sage-design-planning](https://github.com/flint-pete/sage-design-planning).
- bioclip2 classifies only `top-crop-0` per instance (6d).
- Hand-seeded test frames and clips are reprocessed on every wake (6g).
- bioclip2 keeps its seen-store under `.state/sage-yolo2/...` (a quirk of the
  shared consumer code; see the sage-bioclip2 README).
- The SES job files in `jobs/` are untested templates.
- The audio producer has only been run on H00F. On H039, sage-birdnet2 is verified
  with a seeded clip; the live microphone end to end is still to do.
