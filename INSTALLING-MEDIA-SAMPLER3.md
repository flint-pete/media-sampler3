# Installing media-sampler3 and the full media stack on a Sage Thor node

This guide takes a clean, WES-provisioned Sage **Thor (AGX, ARM64)** node and
installs every component of the media stack, then proves it works end to end with
a bird-detection cascade (camera → frames → detector → crops → species classifier
→ Beehive).

Every command here was run on live Thor nodes (H038 and H041, Sep 2026). On H041
the full cascade ran with two real Reolink cameras, and seeded test birds were
classified correctly (*Cardinalis cardinalis* and *Eopsaltria australis*).

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
| 2 | **wes-nodeinfo-injection** | A WES change that publishes 5 node-identity values (VSN, node id, GPS lat/lon, mobility) in the `wes-identity` ConfigMap (Tier 1). An optional patched edge-scheduler adds them to every scheduled plugin pod's environment (Tier 2). | https://github.com/flint-pete/wes-nodeinfo-injection |
| 3 | **pywaggle2-nodeinfo** | The **reader** side of #2. `read_node_info()` turns those env vars into a clean `NodeInfo`. It is a library that gets copied ("vendored") into plugin images; nothing is deployed on the node. | https://github.com/flint-pete/pywaggle2-nodeinfo |
| 4 | **media-sampler3** | The **producer**. Captures JPEG stills (and optionally FLAC audio) from a camera into `/local-cache` as a bounded ring of self-describing files. | https://github.com/flint-pete/media-sampler3 |

**Two test consumers.** They exist to prove the stack works, and they are also
working examples of how to write a cache consumer:

| Consumer | What it does | Repo |
|----------|-------------|------|
| **sage-yolo2** | Reads cached frames, detects objects (here, birds), publishes counts, and writes crops of each detection back into the cache. | https://github.com/flint-pete/sage-yolo2 |
| **sage-bioclip2** | Reads yolo2's crops, classifies the species with BioCLIP-2.5, and publishes the result. | https://github.com/flint-pete/sage-bioclip2 |

Each repo's README explains its code and has a "where this fits" section that
links back here.

### Verified set

| Repo | Version | Notes |
|------|---------|-------|
| wes-local-cache-manager | tag `v0.2.1` | its script builds the image from source as `:test` |
| wes-nodeinfo-injection | tag `v1.0.1` | |
| pywaggle2-nodeinfo | tag `v0.1.1` | copied into yolo2/bioclip2 as `node_info.py` (v0.1.1) |
| media-sampler3 | tag `v0.1.0` | image `localhost/media-sampler3:0.1.0` |
| sage-yolo2 | image `2.1.0` (`master`) | |
| sage-bioclip2 | image `2.0.0` (`master`) | |

Step 0 clones `master`, which is the reference branch for all six repos. To
reproduce exactly this set, `git checkout <tag>` in the tagged repos.

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

wes-local-cache-manager ── bounds /local-cache (byte caps, oldest-first eviction)
wes-nodeinfo-injection  ── publishes VSN + GPS in the wes-identity ConfigMap
pywaggle2-nodeinfo      ── the library a plugin uses to READ those values
```

There are two separate data flows; keep them apart in your head:

- **Cache (on the node).** media-sampler3 fills `/local-cache`. yolo2 and bioclip2
  only read it and never delete. Each producer bounds its own ring, and the cache
  manager is the safety net for the whole directory.
- **Cloud (Beehive).** The consumers publish results (`env.count.*`,
  `env.species.*`) through WES (rabbitmq → upload-agent → Beehive). The producer
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

> **Installing WES resets `wes-identity`.** Installing WES, re-provisioning, or
> running its `update-stack.sh` regenerates the `wes-identity` ConfigMap with only
> its 2 stock variables, which overwrites Component 2. So **apply Component 2
> after WES provisioning**, and re-apply it after any WES (re)install or update.
> [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md) checks for this after a reboot.

The node also needs (a standard Thor has them all, but check): `podman` (media-sampler3
and cache-manager builds), `docker` (the yolo2/bioclip2 `deploy-sideload.sh` builds),
`k3s`, `pluginctl`, `git`, `make`, `curl`, and **working DNS**, because image builds pull
base images from Docker Hub and `nvcr.io`. Host Go is **not** needed. Tier 2 below
compiles Go inside a container; Go is only needed if you want to run
wes-nodeinfo-injection's offline unit tests.

---

## Conventions used below

- `$VSN` is this node's VSN. Set it once per shell:
  `VSN=$(cat /etc/waggle/vsn); echo $VSN`
- **Always use `sudo k3s kubectl`**, never bare `kubectl`. These nodes have no
  standalone kubectl on PATH. The wes-nodeinfo-injection scripts take a `KUBECTL`
  override for this (Step 3).
- **Build every image from source on the node.** Don't copy prebuilt images from
  another node, and don't `pull` from the Sage registry: these images have only
  ever been side-loaded, so a registry pull returns `not found`. A native build on
  the Thor is the path this guide uses and verifies. (The Sage ECR portal can now
  build Thor/arm64 images, GPU/CUDA ones included, so publishing these images
  through ECR is possible; they just haven't been published yet.)
- **Side-loaded means not reboot-durable.** Pods started with `pluginctl run` do
  not come back after a reboot. See [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md).

---

## Step 0: Get the source onto the node

```bash
mkdir -p ~/AI-projects && cd ~/AI-projects
for r in wes-local-cache-manager wes-nodeinfo-injection pywaggle2-nodeinfo \
         media-sampler3 sage-yolo2 sage-bioclip2; do
  [ -d "$r" ] || git clone https://github.com/flint-pete/$r.git
done
```

---

## Step 1: Create the shared cache directory `/local-cache`

This is a host directory, mounted into pods as `/local-cache`. It is
world-writable with the sticky bit, so pods running as different users can each
own their own subtree and still read everyone else's. (Step 2's script does this
too; running it here first does no harm.)

```bash
sudo mkdir -p /media/plugin-data/local-cache
sudo chmod 1777 /media/plugin-data/local-cache
ls -ld /media/plugin-data/local-cache      # expect: drwxrwxrwt ... root root
```

---

## Step 2: Component 1, wes-local-cache-manager (DaemonSet)

One script builds a small stdlib-Python image (`python:3.12-slim` base), imports
it into k3s, applies the ConfigMap and DaemonSet, and verifies them. It already
uses `sudo k3s kubectl` internally, so no `KUBECTL` override is needed. It runs
`podman` rootless (as your user). Read the script first; every step is
annotated.

```bash
cd ~/AI-projects/wes-local-cache-manager
./test-add-node.sh          # provision -> podman build -> side-load -> apply -> verify
```

Verify:

```bash
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
  this node only. Kubernetes doesn't schedule DaemonSet pods onto `NotReady`
  nodes.

Design and caps model:
[DESIGN-AND-PURPOSE](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md).
Teardown: `./test-remove-node.sh` (add `WIPE_CACHE=1` to also empty the cache).

---

## Step 3: Component 2, wes-nodeinfo-injection (and Component 3, the reader)

The scripts live in `node-test/` and run on the node. They back up every object
they touch, and teardown restores from that backup. See
[node-test/README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/node-test/README.md).

> **Set `KUBECTL` before every run:** `export KUBECTL="sudo k3s kubectl"`. If a
> run failed because KUBECTL was wrong, check
> `node-test/.node-backup/configmap_wes-identity.yaml` before re-running. If it
> contains only `__ABSENT__`, delete it; otherwise teardown would delete
> `wes-identity` instead of restoring it. v1.0.1's `lib.sh` refuses to record such
> a bad backup, but older clones don't.

### Tier 1: the wes-identity ConfigMap (required; no build)

This regenerates `wes-identity` with 5 variables taken from this node's manifest,
then launches a small reader pod. The pod explicitly mounts the ConfigMap
(`envFrom`) and runs an inline copy of pywaggle2's `read_node_info()` logic, to
prove the values are readable.

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
export KUBECTL="sudo k3s kubectl"
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
has no GPS, the raw values show sentinels (e.g. `999` or empty) and `lat`/`lon`
come back `null`. The reader never invents coordinates.

```bash
sudo k3s kubectl get configmap wes-identity -o jsonpath='{.data}'; echo
```

Revert Tier 1 with `./test-remove-configmap.sh`.

**Component 3 (pywaggle2-nodeinfo)** has no install step. Its `read_node_info()`
is copied into the plugin images (sage-yolo2 and sage-bioclip2 `node_info.py`; see
[VENDORED.md](https://github.com/flint-pete/sage-yolo2/blob/master/VENDORED.md)).
media-sampler3's `nodemeta.py` implements the same rules. Contract:
[DESIGN](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md).

### What Tier 1 does and does not do in this cascade

A ConfigMap does nothing on its own. A pod only sees these variables if its spec
says `envFrom: wes-identity`. Two things add that: the Tier 2 patched scheduler,
or a hand-written spec like the Tier 1 test pod. **`pluginctl run` builds pods on
the client side, so the plugins launched in Step 6 do not get these variables.**
That's fine for this cascade:

- **media-sampler3** is told its VSN with `--vsn "$VSN"`. Without `--lat/--lon`,
  its frames carry no GPS in EXIF.
- **yolo2/bioclip2** take node identity from each frame's EXIF, which is written
  by the producer. The pod env is only a fallback and a cross-check.
- **Beehive** attaches the node's VSN to every published record during routing,
  regardless.

Tier 1 matters for anything that does get `envFrom`: the scheduler path (Tier 2)
and future SES-scheduled jobs.

### Tier 2: the patched edge-scheduler (optional; not needed for this cascade)

Tier 2 makes scheduler-launched plugins automatically receive
`envFrom: wes-identity`. It builds the real upstream scheduler with a one-function
patch. Go compiles inside the build container, so host Go isn't needed.

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
mkdir -p ../.upstream
git clone https://github.com/waggle-sensor/edge-scheduler.git ../.upstream/edge-scheduler
git -C ../.upstream/edge-scheduler checkout 5391a00          # the tested base commit
git -C ../.upstream/edge-scheduler apply \
    "$(realpath ../patches/0002-edge-scheduler-envfrom-wes-identity.patch)"
export KUBECTL="sudo k3s kubectl"
./test-add-scheduler.sh       # ~3-6 min build + rollout; auto-reverts if the rollout fails
```

To verify, check a scheduler-launched plugin pod (not a `pluginctl` pod):

```bash
sudo k3s kubectl get pod <some-plugin> -o jsonpath='{.spec.containers[0].envFrom}'
#   -> [{"configMapRef":{"name":"wes-identity","optional":true}}]
```

Revert with `./test-remove-scheduler.sh`. The patched scheduler runs a side-loaded
image. If a reboot ever loses that image, scheduling stops until you run
`test-remove-scheduler.sh` or `test-add-scheduler.sh` (see
[REBOOT-RECOVERY.md](REBOOT-RECOVERY.md)).

---

## Step 4: Component 4, build media-sampler3 (the producer)

This uses a CPU base image (`python:3.12-slim`) and builds in a few minutes:

```bash
cd ~/AI-projects/media-sampler3
make sideload       # = sudo podman build -t localhost/media-sampler3:0.1.0 .
                    #   + podman save | sudo k3s ctr images import -
                    #   + confirm the image is listed
```

media-sampler3 **refuses to start** (exit 2) if `/local-cache` isn't mounted, so
Steps 1–2 must come first. Flags and file contract:
[README](README.md).

---

## Step 5: Build the test consumers (sage-yolo2, sage-bioclip2)

These use a GPU/CUDA base image (`nvcr.io/nvidia/pytorch:25.08-py3`) and are
**large, slow builds**: yolo2 is about 10 GiB and bioclip2 about 17 GiB, tens of
minutes each. Check `df -h /` first. Each repo ships
`scripts/deploy-sideload.sh`, which reads name, namespace, and version from its
`sage.yaml` and does build → import (→ optional ECR register).

```bash
cd ~/AI-projects/sage-yolo2
scripts/deploy-sideload.sh --dry-run          # show the plan
scripts/deploy-sideload.sh --skip-register    # build + side-load, skip the ECR catalog

cd ~/AI-projects/sage-bioclip2
scripts/deploy-sideload.sh --dry-run
scripts/deploy-sideload.sh --skip-register
```

Notes:

- `--skip-register` means you don't need a `SAGE_TOKEN`. `pluginctl run` doesn't
  need the catalog record.
- **The import step is slow but safe.** Importing the 10 GiB yolo2 image took
  about 7 minutes on H041 and loads the node's disk I/O (SSH may feel sluggish).
  It doesn't crash the node. Build bioclip2 right after yolo2 so it reuses the
  cached CUDA base layer.
- The resulting tags are `registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0` and
  `.../sage-bioclip2:2.0.0`. That's only the side-loaded image's *name*; nothing is
  pulled from the registry. If you build under your own namespace or version,
  change the tags in Step 6.
- More detail:
  [sage-yolo2 DOCKER-BUILD](https://github.com/flint-pete/sage-yolo2/blob/master/DOCKER-BUILD.md).

---

## Step 6: Run and validate the cascade

With a camera, run the live producers (6b). Without one, seed a synthetic cache
(6a), which exercises everything downstream of the producer.

Every launch uses `sudo pluginctl run`, because only root's kubeconfig can create
pods in the `default` namespace. Two flags appear on every launch:

- `--selector zone=core` is required because the pods mount a host directory (`-v`).
- `-v /media/plugin-data/local-cache:/local-cache` is what gives the pod the
  shared cache.

### 6a. (No camera) seed a synthetic cache

```bash
sudo mkdir -p /media/plugin-data/local-cache/camera/top
# copy in JPEGs named like real frames: <ns-timestamp>-v2-<VSN>-top.jpg
# (6f gives a ready-made bird image)
sudo ls -l /media/plugin-data/local-cache/camera/top/
```

### 6b. (Cameras attached) run the live producers, one per camera

**How media-sampler3 talks to a Reolink camera.** Both the image path and the
optional audio path use the camera's **HTTP port** (`--camera-port`, default 80):

- images: the HTTP snapshot API, `/cgi-bin/api.cgi?cmd=Snap`
- audio: the HTTP-FLV audio sub-stream, `/flv?...channel0_sub`, recorded by ffmpeg

RTSP is not used. So the one network requirement is that the node can reach each
camera's HTTP port. Check this first, with no credentials needed. On H038 the
cameras were on an unroutable subnet and nothing worked; on H041 they were
reachable and everything did.

```bash
for ip in <CAM1_IP> <CAM2_IP>; do
  echo "== $ip =="; curl -s -o /dev/null -w 'http(80): %{http_code}\n' --max-time 5 "http://$ip/"
done
```

> **Cold-boot gotcha (seen on H041 after a power cut).** A Reolink can come back
> with its web page up (`http://cam/` returns 200) while `api.cgi` still returns
> **404**, because its media services haven't started yet. The producers retry
> every capture period and recover on their own once the API answers. If it stays
> 404 for more than 15 minutes, power-cycle the camera.

**Credentials come from an env file, never the command line.** If both cameras
share one account, use a single file with mode 600, on the node only:

```bash
umask 077
cat > ~/ms3-cam-creds.env <<'EOF'
CAMERA_USER=<USER>
CAMERA_PASSWORD=<PASS>
EOF
chmod 600 ~/ms3-cam-creds.env      # never commit it, never paste it into docs or chat
```

Launch **one producer per camera**, each writing to its own subtree (`camera/top`,
`camera/side`). `pluginctl run` stays attached to the pod, so add `&` to
background each launch or use separate shells:

```bash
# Camera 1 -> /local-cache/camera/top
sudo pluginctl run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host <CAM1_IP> --camera-port 80 &

# Camera 2 -> /local-cache/camera/side
sudo pluginctl run --name camera-producer-side --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream side_camera --name side \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host <CAM2_IP> --camera-port 80 &
```

What the naming flags do:

- `--cache-name camera` plus `--name top` puts frames in `/local-cache/camera/top/`.
- `--name` also becomes the `<source>` in each filename, `<ts>-v2-<VSN>-top.jpg`,
  and the `camera` field in EXIF. sage-yolo2 uses that field to name its crop
  directories (`top-crop-N`).
- `--stream` is just the stream label. It's used for naming only when `--name` is
  omitted.

Optional flags: add `--lat <deg> --lon <deg>` to put GPS in EXIF, and
`--plugin-version localhost/media-sampler3:0.1.0` to record the image version
(otherwise it's `media-sampler3:dev`).

Check that fresh 4K frames appear every 10 s:

```bash
sudo ls -lt /media/plugin-data/local-cache/camera/top/  | head
sudo ls -lt /media/plugin-data/local-cache/camera/side/ | head
```

On H041 both producers ran 1/1, writing 3840×2160 JPEGs (240–370 KB) into the
two subtrees.

> **Config convention.** Keep the non-secret camera map (host, port, channel,
> label per camera) in your notes or project dir. Keep the secret
> username/password only in the mode-600 env file on the node.

**(Optional) audio producer.** This is a second, independent media-sampler3 pod
that records a 15 s FLAC clip once a minute from the camera's microphone over the
camera's HTTP port. Each clip gets a `<clip>.json` metadata sidecar. This was
proven on H00F (camera HTTP on port 10000); it hasn't been run on H041. Nothing in
this cascade consumes the audio; a BirdNET consumer is the planned reader.

```bash
sudo pluginctl run --name camera-audio-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
  --camera-host <CAM_IP> --camera-port 80 \
  --audio-format flac --bandpass-fmax 8000 \
  --stream mic --cache-name camera-audio \
  --cache-max-count 500 --cache-max-mb 2000 --heartbeat-secs 60 --vsn "$VSN" &
# clips land in /local-cache/camera-audio/mic/
```

### 6c. Run sage-yolo2 (detector and crop producer)

GPU consumers **must** set a memory limit, or the kernel kills them (OOMKilled,
exit 137):

```bash
sudo pluginctl run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops &
```

What the flags mean:

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
- This instance reads only `camera/top`. To process the side camera too, run a
  second yolo2 with a different `--name` (pod name) and
  `--input /local-cache/camera/side`. That hasn't been run yet.

### 6d. Run sage-bioclip2 (species classifier)

```bash
sudo pluginctl run --name sage-bioclip2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-bioclip2 \
  registry.sagecontinuum.org/beckman/sage-bioclip2:2.0.0 -- \
  --source cache --input /local-cache/camera-crops/top-crop-0 \
  --every 10m --all-unseen --max-frames 0 --rank Species --min-confidence 0.1 &
```

The `--input` paths chain together: the producer's `--cache-name`/`--name`
determine yolo2's `--input`, and yolo2's `--crop-cache-name` plus the producer's
`--name` determine bioclip2's `--input`. If you change one, fix the next.

**Known limitation:** one bioclip2 instance reads one crop directory. Only the
first bird in each frame (`top-crop-0`) is classified. To cover every detection,
run one bioclip2 per `top-crop-N` directory. Teaching bioclip2 to read all of
them is an open improvement.

### 6e. Verify end to end

```bash
sudo k3s kubectl get pods | grep -iE 'camera-producer|yolo2-consumer|bioclip2-consumer'
sudo ls -lt /media/plugin-data/local-cache/camera/top/ | head        # frames
sudo ls -R  /media/plugin-data/local-cache/camera-crops/ | head      # crops (after a bird)

# Cloud proof: query the Sage data API for this node's records
q() { curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
        -H 'Content-Type: application/json' \
        -d "{\"start\":\"-30m\",\"filter\":{\"vsn\":\"$VSN\",\"name\":\"$1\"}}" | tail -2; }
q env.count.total                # yolo2, every frame (0 = ran and saw nothing)
q env.mediasampler.cache.count   # producer heartbeat
q env.species.species            # bioclip2, once a bird crop is classified
```

Success means all of these:

- pods are Running
- `env.count.total` rows are tagged with this node's VSN
- crops appear after a bird is detected
- `env.species.species` rows follow

### 6f. Deterministic test: seed a known bird

Real camera frames may contain no birds. Then yolo2 correctly writes zero crops,
and bioclip2 never fires. That looks like a failure but isn't. To prove the whole
chain, seed the bird image that sage-yolo2 ships as a test fixture:

```bash
sudo cp ~/AI-projects/sage-yolo2/tests/test-images/bird-cardinal-sample.jpg \
   /media/plugin-data/local-cache/camera/top/$(date +%s%N)-v2-$VSN-top.jpg
# yolo2 picks it up on its next wake (or relaunch the consumer to process it now)
```

On H041 this produced `env.count.bird = 1`, a crop under
`camera-crops/top-crop-0/`, and then bioclip2 published **`Cardinalis
cardinalis`** at 100% confidence. A second test with an Australian robin returned
*Eopsaltria australis*.

**Delete the seeded file after the test.** It wasn't written by media-sampler3,
so it lacks the EXIF `unique_id` that yolo2 uses to mark a frame seen. yolo2 will
reprocess it, and republish `env.count.bird = 1`, on every wake until the ring
evicts it. (A known limitation, listed in sage-yolo2's README.)

---

## After a reboot

Nothing installed here is in the WES base image. A reboot or power cut kills
every `pluginctl` pod, and they do **not** restart on their own. Follow
**[REBOOT-RECOVERY.md](REBOOT-RECOVERY.md)**: it is an ordered, copy-paste
checklist that verifies what survived, re-applies what didn't, and relaunches the
producers and consumers with the same commands as Step 6.

---

## Known limitations and open items

- The stack is side-loaded, so it isn't reboot-durable (see REBOOT-RECOVERY.md).
  The lasting fix is publishing the images to the registry and folding the
  nodeinfo change into the WES base stack. That's CI-team work, tracked in
  [sage-design-planning](https://github.com/flint-pete/sage-design-planning).
- `pluginctl` pods don't receive `wes-identity`. The producer uses `--vsn`.
- bioclip2 classifies only `top-crop-0` per instance (6d).
- Hand-seeded test frames are reprocessed on every wake (6f).
- bioclip2 keeps its seen-store under `.state/sage-yolo2/...` (a quirk of the
  shared consumer code; see the sage-bioclip2 README).
- The SES job files in `jobs/` are untested templates.
- The audio producer hasn't been run on H041 (proven on H00F only).
- Tier 2 (patched scheduler) wasn't exercised on H041 and hasn't been tested
  across a reboot.
