# Deploying the media-sampler3 media stack on a new Thor node

A student-facing guide to install four custom Sage/Waggle components onto a fresh
Jetson **Thor (AGX, ARM64)** node and validate them with the **sage-yolo2 →
sage-bioclip2** inference cascade.

This is the single place to pick up all the software, understand each piece, and
apply it to a new node. Every command here was **ground-truthed on live Thor
nodes** (H038, then H041 with two real cameras) in Sep 2026, and the **full
cascade was proven end-to-end on H041** — two Reolink cameras → media-sampler3 →
yolo2 (detect + crop) → bioclip2 (species) → Beehive, with both test birds
classified correctly (*Cardinalis cardinalis* and *Eopsaltria australis*, 100%).
Findings from those runs are folded in below.

---

## The four components (with links)

| # | Component | What it is | Repo | Key docs |
|---|-----------|-----------|------|----------|
| 1 | **wes-local-cache-manager** | DaemonSet that bounds the shared `/local-cache` hostPath (per-unit + per-node byte caps, oldest-first eviction). The producer/consumer companion to `/uploads`. | https://github.com/flint-pete/wes-local-cache-manager | [README](https://github.com/flint-pete/wes-local-cache-manager/blob/master/README.md) · [DESIGN-AND-PURPOSE](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md) · [HANDOFF](https://github.com/flint-pete/wes-local-cache-manager/blob/master/HANDOFF.md) |
| 2 | **wes-nodeinfo-injection** | WES change that injects 5 node-identity env vars (VSN, node_id, GPS lat/lon, mobility) into every plugin pod. Two tiers: a ConfigMap regen (Tier 1) and a patched edge-scheduler (Tier 2). | https://github.com/flint-pete/wes-nodeinfo-injection | [README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/README.md) · [TESTING](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/TESTING.md) · [node-test/README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/node-test/README.md) · [HANDOFF](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/HANDOFF.md) |
| 3 | **pywaggle2-nodeinfo** | The **library** reader side of #2 — `read_node_info()` normalizes the injected env into a clean `NodeInfo`. Ships inside plugin images; nothing to deploy on the node. | https://github.com/flint-pete/pywaggle2-nodeinfo | [README](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/README.md) · [DESIGN](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md) · [HANDOFF](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/HANDOFF.md) |
| 4 | **media-sampler3** | The **producer**. Captures JPEG stills (and optionally FLAC audio) from a camera into `/local-cache` as a bounded ring, self-describing per the v2 cache-frame contract. | https://github.com/flint-pete/media-sampler3 | [README](https://github.com/flint-pete/media-sampler3/blob/master/README.md) · [RESUME-HERE](https://github.com/flint-pete/media-sampler3/blob/master/RESUME-HERE.md) · [AUDIO-EXTENSION-DESIGN](https://github.com/flint-pete/media-sampler3/blob/master/docs/AUDIO-EXTENSION-DESIGN.md) · [jobs/README](https://github.com/flint-pete/media-sampler3/blob/master/jobs/README.md) |

**Test consumers** (the validation cascade — read the cache, run inference, publish):

| Consumer | What it does | Repo | Key docs |
|----------|-------------|------|----------|
| **sage-yolo2** | Detector + crop producer: reads cached frames, detects (birds), writes crops back to the cache. | https://github.com/flint-pete/sage-yolo2 | [README](https://github.com/flint-pete/sage-yolo2/blob/master/README.md) · [DOCKER-BUILD](https://github.com/flint-pete/sage-yolo2/blob/master/DOCKER-BUILD.md) · [THOR-TESTING](https://github.com/flint-pete/sage-yolo2/blob/master/THOR-TESTING.md) · [overview](https://github.com/flint-pete/sage-yolo2/blob/master/overview.md) |
| **sage-bioclip2** | Species classifier: reads yolo2's crops, classifies. | https://github.com/flint-pete/sage-bioclip2 | [README](https://github.com/flint-pete/sage-bioclip2/blob/master/README.md) · [HANDOFF](https://github.com/flint-pete/sage-bioclip2/blob/master/HANDOFF.md) |

Verified versions (Sep 2026): cache-manager `v0.2.0`, nodeinfo-injection `v1.0.0`,
pywaggle2-nodeinfo `v0.1.0`, media-sampler3 `fb38d82`, sage-yolo2 `v2.1.0`,
sage-bioclip2 `v2.0.0`.

---

## How the pieces fit together

```
        ┌─────────────────┐   writes JPEG    ┌──────────────────────────────┐
camera ─▶│  media-sampler3 │─── ring cache ──▶│  /local-cache/camera/top │
        │   (PRODUCER)    │                  └──────────────┬───────────────┘
        └─────────────────┘                                 │ reads (non-destructive)
                                                             ▼
                                                   ┌───────────────────┐  writes crops
                                                   │     sage-yolo2    │──────────────┐
                                                   │  detect + crop    │              │
                                                   └─────────┬─────────┘              ▼
                                                             │  publishes   /local-cache/camera-crops
                                                             │  env.count.* to Beehive        │
                                                             ▼                                 │ reads
                                                   ┌───────────────────┐                       │
                                                   │    sage-bioclip2  │◀──────────────────────┘
                                                   │  classify species │  publishes to Beehive
                                                   └───────────────────┘

wes-local-cache-manager  ── bounds /local-cache (byte caps, oldest-first eviction)
wes-nodeinfo-injection   ── puts VSN + GPS into every pod's env
pywaggle2-nodeinfo       ── the library each plugin uses to READ that env
```

Two data flows, do not conflate them:
- **Cache producer/consumer**: media-sampler3 fills `/local-cache`; yolo2/bioclip2
  are non-destructive readers. Only the producer bounds its own ring.
- **Cloud publish**: consumers publish results (`env.count.total`, classifications)
  to Beehive via WES (rabbitmq → upload-agent), independent of the cache.

---

## PREREQUISITE — the target node must be a fully WES-provisioned Sage node

**This is the single most important thing this guide learned from the H038 run.**
These four components are **add-ons on top of the Waggle Edge Stack (WES)**, not a
substitute for it. `pluginctl` (how we launch plugins) requires WES-provided
cluster objects — without them it fails with:

```
Error: pods "<name>" is forbidden: error looking up service account
default/wes-plugin-account: serviceaccount "wes-plugin-account" not found
```

A correctly provisioned node has the full WES stack running. Verify **before you
start** — every line below must return results:

```bash
ssh beckman@node-<VSN>.sage
sudo k3s kubectl get sa -n default | grep -E 'wes-plugin-account|wes-plugin-scheduler'
sudo k3s kubectl get pods -n default | grep -E 'wes-rabbitmq|wes-plugin-scheduler|wes-upload-agent|wes-scoreboard'
sudo k3s kubectl get clusterrole | grep -E 'wes-plugin-role|wes-plugin-scheduler'
```

If these are **missing**, the node has not been WES-provisioned (or WES was
removed). Stop and have it (re)provisioned by the Sage node-setup / Beekeeper
process — that is out of scope for this guide and not something to hand-assemble.
On H038 the WES stack was absent at first; a proper reinstall restored
`wes-plugin-account`, the scheduler, rabbitmq, and the upload-agent, after which
everything below worked.

> **Note — WES install/regeneration resets `wes-identity`.** When WES is installed
> or re-provisioned it (re)creates the `wes-identity` ConfigMap with only the 2
> stock vars (`WAGGLE_NODE_ID`, `WAGGLE_NODE_VSN`). This **overwrites** the 5-var
> version from Component 2. Therefore **apply Component 2 AFTER WES provisioning**,
> and re-apply it any time the node is re-provisioned or rebooted (see "Durability").

Also required on the node (all present on a standard Thor; verify): `docker`,
`podman`, `k3s`, `pluginctl`, `jq`, `git`, `make`, `ffmpeg`, and **working DNS**
(image builds pull base images from Docker Hub and `nvcr.io`). `go` is only needed
for Component 2 **Tier 2** (patched scheduler) and is often absent — install from
https://go.dev/dl/ (1.22+) to `/usr/local/go` only if you do Tier 2.

---

## Conventions used below

- `$VSN` = this node's VSN. Set it once: `VSN=$(cat /etc/waggle/vsn); echo $VSN`
- **Always `sudo k3s kubectl`**, never bare `kubectl` — these nodes have no
  standalone kubectl on PATH. The nodeinfo scripts default to `sudo kubectl`; we
  override with `KUBECTL="sudo k3s kubectl"` (learned on H038 — the scripts fail
  otherwise).
- **Build every image from source ON the node.** Do NOT copy prebuilt images from
  another node and do NOT `pull` from the Sage registry — the custom images were
  only ever side-loaded, so a registry pull returns `not found` (verified on H038).
  Native `podman build` on the Thor (arm64, no QEMU) is the working path; the ECR
  portal cannot cross-build the GPU/CUDA plugins (QEMU crash on the NVIDIA base —
  Infra #3, open).

---

## Step 0 — Get the source onto the node

```bash
mkdir -p ~/AI-projects && cd ~/AI-projects
for r in wes-local-cache-manager wes-nodeinfo-injection pywaggle2-nodeinfo \
         media-sampler3 sage-yolo2 sage-bioclip2; do
  [ -d "$r" ] || git clone https://github.com/flint-pete/$r.git
done
```

---

## Step 1 — Provision the shared cache directory `/local-cache`

Host path, world-writable + sticky so pods of differing UIDs each own a subtree and
read across them. (Step 2's script also does this; running it here first is fine.)

```bash
sudo mkdir -p /media/plugin-data/local-cache
sudo chmod 1777 /media/plugin-data/local-cache
ls -ld /media/plugin-data/local-cache      # expect: drwxrwxrwt ... root root
```

---

## Step 2 — Component 1: wes-local-cache-manager (DaemonSet)

Builds a tiny stdlib-Python image (`python:3.12-slim` base), side-loads it, applies
the ConfigMap + DaemonSet. One script does it all; read it first — every step is
annotated as an ansible candidate.

```bash
cd ~/AI-projects/wes-local-cache-manager
./test-add-node.sh          # provision -> podman build -> side-load -> apply -> verify
```

Verify:

```bash
sudo k3s kubectl get daemonset wes-local-cache-manager        # DESIRED=READY=1
sudo k3s kubectl get pods -l app.kubernetes.io/name=wes-local-cache-manager -o wide
sudo k3s kubectl logs -l app.kubernetes.io/name=wes-local-cache-manager --tail=5
```

Expected log line: `sweep ok: 0 units, node_total=0 bytes (node cap ..., per-unit cap ...)`.

Defaults (override via the `wes-local-cache-manager-env` ConfigMap): per-unit 2 GiB,
per-node 15 GiB, sweep 60 s, `CACHE_UNIT_DEPTH=2` (unit = `<cache-name>/<camera>`).
A reserved `/local-cache/.state/` dir is **never evicted** — consumers keep their
"seen-store" there so they don't reprocess everything after a restart. **Never
delete `.state/`.**

Manifest detail: the DaemonSet ships with **no nodeSelector** (`<none>`), so it runs
on every Ready node in this node's cluster. On a single-node field cluster that's
just this node. (Verified on H038: Kubernetes does **not** schedule DaemonSet pods
onto `NotReady` nodes, so a dead sibling node does not get a stray pod.) See
[DESIGN-AND-PURPOSE](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md)
for the caps model and [HANDOFF](https://github.com/flint-pete/wes-local-cache-manager/blob/master/HANDOFF.md)
for the reserved-state area.

Teardown: `./test-remove-node.sh` (add `WIPE_CACHE=1` to also empty the cache).

---

## Step 3 — Component 2: wes-nodeinfo-injection (identity env) + Component 3 (the reader)

Scripts live in `node-test/`, run ON the node, back up the objects they touch, and
are restore-from-backup on teardown. See
[node-test/README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/node-test/README.md)
and [TESTING](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/TESTING.md).

> **Set `KUBECTL` for every call** — the scripts default to `sudo kubectl`, which
> does not exist on these nodes.

### Tier 1 — the wes-identity ConfigMap (required; needs no build, no Go)

Regenerates `wes-identity` with the 5 vars from THIS node's manifest, then launches
a reader pod that runs pywaggle2's `read_node_info()` to prove the chain.

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
export KUBECTL="sudo k3s kubectl"
./test-add-configmap.sh
```

Expected: the reader prints `NodeInfo(vsn=<VSN>, node_id=..., lat=..., lon=...,
mobility="unknown", vsn_is_placeholder=false)` with THIS node's real coords.
(Empty `mobility` in the manifest normalizes to `"unknown"` — correct. The lat/lon
shown are whatever the node's live manifest carries; if the manifest has no GPS,
lat/lon come back `None` by design — the reader never fabricates coordinates.)

Confirm the ConfigMap:

```bash
sudo k3s kubectl get configmap wes-identity -o jsonpath='{.data}'; echo
```

Revert Tier 1: `./test-remove-configmap.sh`.

**Component 3 (pywaggle2-nodeinfo)** needs no install step of its own — its reader
is baked into the plugin images and is exactly what Tier 1's test pod just
exercised. See its [DESIGN](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md)
for the sentinel-normalization contract.

### Tier 2 — the patched edge-scheduler (OPTIONAL for this cascade)

Makes daemon-/SES-scheduled plugins **auto-receive** `envFrom: wes-identity`.
Requires Go (1.22+ at `/usr/local/go`) and builds the real upstream scheduler.

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
mkdir -p ../.upstream
git clone https://github.com/waggle-sensor/edge-scheduler.git ../.upstream/edge-scheduler
git -C ../.upstream/edge-scheduler checkout 5391a00          # the patched base commit
git -C ../.upstream/edge-scheduler apply ../patches/0002-*.patch
export KUBECTL="sudo k3s kubectl"
./test-add-scheduler.sh                                       # ~3-6 min build + rollout; auto-reverts on failure
# verify a scheduled plugin gets it:
sudo k3s kubectl get pod <some-plugin> -o jsonpath='{.spec.containers[0].envFrom}'
#   -> [{"configMapRef":{"name":"wes-identity","optional":true}}]
```

**When to skip Tier 2:** `pluginctl run` builds pods **client-side**, bypassing the
scheduler daemon — so a `pluginctl`-launched plugin does NOT auto-get `envFrom`.
Every launch command in this guide passes `--vsn "$VSN"` explicitly instead, so
**Tier 1 alone is sufficient for this cascade**. Tier 2 matters only for
fleet-wide, scheduler-launched plugins. Revert: `./test-remove-scheduler.sh`.

---

## Step 4 — Component 4: build media-sampler3 (the producer)

CPU base (`python:3.12-slim`), builds fast from source:

```bash
cd ~/AI-projects/media-sampler3
sudo podman build -t localhost/media-sampler3:0.1.0 .
sudo podman save localhost/media-sampler3:0.1.0 | sudo k3s ctr images import -
sudo k3s ctr images ls | grep media-sampler3        # confirm present, linux/arm64
```

media-sampler3 **fails fast** if `/local-cache` is absent, so Steps 1–2 must be done
first. See [README](https://github.com/flint-pete/media-sampler3/blob/master/README.md)
for the full flag set and the v2 cache-frame contract.

---

## Step 5 — build the test consumers (sage-yolo2, sage-bioclip2)

GPU/CUDA base (`nvcr.io/nvidia/pytorch:25.08-py3`) — **large, slow builds** (yolo2
~10 GiB, bioclip2 ~17 GiB image; expect tens of minutes each — check `df -h /`).
Each repo ships `scripts/deploy-sideload.sh`, which reads name/namespace/version
from its `sage.yaml` (nothing hardcoded) and does build → import → register.

```bash
cd ~/AI-projects/sage-yolo2
scripts/deploy-sideload.sh --dry-run          # inspect the plan
scripts/deploy-sideload.sh --skip-register    # build + side-load (skip ECR catalog)

cd ~/AI-projects/sage-bioclip2
scripts/deploy-sideload.sh --dry-run
scripts/deploy-sideload.sh --skip-register
```

Notes:
- `--skip-register` avoids needing `SAGE_TOKEN`; for `pluginctl run` the catalog
  record is not required.
- **The `ctr images import` step is slow but safe.** Importing the ~10 GiB yolo2
  image into containerd took ~7 min on H041 and pins the node's I/O while it runs
  (SSH can get sluggish) — but it does **not** crash or reboot the node. (An H041
  reboot during an earlier build turned out to be a coincidental campus power
  outage, not the import.) bioclip2 builds faster if you build it right after yolo2
  — they share the `nvcr.io` CUDA base layer, which is then cached.
- Confirm the resulting tags — the launch commands below assume
  `registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0` and `.../sage-bioclip2:2.0.0`.
  If you build under your own namespace/version, update the tags accordingly.
- Build background + Thor specifics:
  [sage-yolo2 DOCKER-BUILD](https://github.com/flint-pete/sage-yolo2/blob/master/DOCKER-BUILD.md)
  and [THOR-TESTING](https://github.com/flint-pete/sage-yolo2/blob/master/THOR-TESTING.md).

---

## Step 6 — validate the cascade

The real system captures from a camera. The full chain below was **proven
end-to-end on H041** with two Reolink cameras. If no camera is attached yet, seed
a synthetic cache (6a) to exercise the read → detect → crop → classify → publish
path; with cameras, run the live producers (6b).

`sudo pluginctl` is required — pluginctl targets the `default` namespace and only
root's kubeconfig can create pods there. `--selector zone=core` places the pod on
the core zone.

> **Camera access — read before 6b.** media-sampler3's **image** path uses the
> Reolink **HTTP snapshot API** (`/cgi-bin/api.cgi?cmd=Snap`), default port **80**
> — **not** RTSP. RTSP (port **554**) is used only by the optional **audio** path.
> So the image producer needs the camera's **HTTP port (80)** reachable; the audio
> producer needs **554**. Verify reachability from the node first (below) — on H038
> the cameras were on an unroutable subnet and nothing worked; on H041 they were
> reachable and everything worked. This is a real prerequisite.

### 6a. (No camera) seed a synthetic cache

```bash
sudo mkdir -p /media/plugin-data/local-cache/camera/top
# copy in a few sample JPEGs named like real frames: <ns>-v2-<VSN>-top.jpg
# any JPEGs prove the plumbing; bird images actually exercise the detector (see 6f)
sudo ls -l /media/plugin-data/local-cache/camera/top/
```

### 6b. (Cameras attached) run the live producers — one per camera

**Camera reachability check first** (no creds needed — just that the ports answer):

```bash
for ip in <CAM1_IP> <CAM2_IP>; do
  echo "== $ip =="
  curl -s -o /dev/null -w 'http(80): %{http_code}\n' --max-time 5 "http://$ip/"   # image path
  nc -z -w3 "$ip" 554 && echo 'rtsp(554): open' || echo 'rtsp(554): closed'       # audio path
done
```

> **Cold-boot gotcha (seen on H041 after a power outage):** a Reolink can come back
> with its web front-end up (`http://cam/` → 200) but `api.cgi` returning **404**
> and RTSP closed — the media/API services haven't initialized. The producers keep
> retrying every 10 s and auto-recover once the camera's API returns; if it stays
> 404/closed for >15 min the camera needs a physical power-cycle.

**Credentials come from an env file, never the CLI.** For the common case of two
cameras on **one shared account**, use a single `600` creds file:

```bash
umask 077
cat > ~/ms3-cam-creds.env <<'EOF'
CAMERA_USER=<USER>
CAMERA_PASSWORD=<PASS>
EOF
chmod 600 ~/ms3-cam-creds.env      # node-local only — never commit, never put in the guide
```

Launch **two producers**, one per camera, each writing to its own cache subtree
(`camera/top`, `camera/side`). `pluginctl run --continuous` **blocks**
(stays attached), so background each launch (`&`) or run them in separate shells:

```bash
# Camera 1 -> camera/top
sudo pluginctl run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host <CAM1_IP> --camera-port 80 &

# Camera 2 -> camera/side
sudo pluginctl run --name camera-producer-side --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream side_camera --name side \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host <CAM2_IP> --camera-port 80 &
```

Verify fresh 4K frames (`<ts>-v2-<VSN>-top.jpg` / `-side.jpg`) appear every 10 s:

```bash
sudo ls -lt /media/plugin-data/local-cache/camera/top/  | head
sudo ls -lt /media/plugin-data/local-cache/camera/side/ | head
```

On H041 both producers ran 1/1, capturing 3840×2160 JPEGs (~240–370 KB) to the two
subtrees from the shared `sagestudent` account. If a producer logs HTTP 404 on
`cmd=Snap`, see the cold-boot gotcha above — it's the camera, not the config.

> **Config stash convention.** Keep a **non-secret** camera map in the project dir
> (host/port/channel/stream-label per camera — safe to version) and the **secret**
> user/pass only in the `600` env file on the node (`--env-from`, gitignored,
> never committed). The guide, repos, and chat never hold the password.

(Optional) **audio producer** — a second, independent instance using **RTSP 554**,
15 s FLAC once/min. Only relevant if you want audio and the camera's 554 is open:

```bash
sudo pluginctl run --name camera-audio-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
  --camera-host <CAM_IP> --camera-port 554 \
  --audio-format flac --bandpass-fmax 8000 \
  --stream mic --cache-name camera-audio \
  --cache-max-count 500 --cache-max-mb 2000 --heartbeat-secs 60 --vsn "$VSN" &
```

### 6c. Run yolo2 (detector + crop producer)

GPU consumers **must** set a memory limit (else OOMKilled, exit 137):

```bash
sudo pluginctl run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops
```

### 6d. Run bioclip2 (species classifier)

```bash
sudo pluginctl run --name sage-bioclip2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-bioclip2 \
  registry.sagecontinuum.org/beckman/sage-bioclip2:2.0.0 -- \
  --source cache --input /local-cache/camera-crops/top-crop-0 \
  --every 10m --all-unseen --max-frames 0 --rank Species --min-confidence 0.1
```

The `--input` paths chain off the producer's `--cache-name`/`--name` and yolo2's
`--crop-cache-name`. If you change any of those, fix the downstream `--input`. The
crop subdir suffix (`top-crop-0`) is derived from the source stream name.

### 6e. Verify end-to-end

```bash
sudo k3s kubectl get pods | grep -iE 'camera-producer|yolo2-consumer|bioclip2-consumer'
ls -lt /media/plugin-data/local-cache/camera/top/ | head           # frames
ls -lt /media/plugin-data/local-cache/camera-crops/ 2>/dev/null    # crops

# cloud proof: yolo2 publishes counts to Beehive (needs rabbitmq + upload-agent up)
curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H 'Content-Type: application/json' \
  -d "{\"start\":\"-15m\",\"filter\":{\"vsn\":\"$VSN\",\"name\":\"env.count.total\"}}" | tail -2
```

Success = pods Running, crops appearing after yolo2 runs, classifications from
bioclip2, and `env.count.total` rows in the data API tagged with this node's VSN.

### 6f. Deterministic detection test — seed a known bird

Real camera frames may contain no birds, so yolo2 correctly produces **zero crops**
and the downstream stages never fire — which looks like a failure but isn't. To
prove the full detect → crop → classify chain deterministically, seed a known bird
image. **sage-yolo2 ships one** as a fixture (added after the H041 run):

```bash
# public-domain Northern Cardinal, confirmed to detect as "bird"
cp ~/AI-projects/sage-yolo2/tests/test-images/bird-cardinal-sample.jpg \
   /media/plugin-data/local-cache/camera/top/$(date +%s%N)-v2-$VSN-top.jpg
# yolo2 picks it up on its next --every wake (or restart the consumer to process now)
```

On H041 this produced `env.count.bird = 1`, a crop under
`camera-crops/top-crop-0/`, and bioclip2 then classified it
**`Cardinalis cardinalis` (100%)** — the proof the whole cascade works. (A second
test with an Australian robin classified as *Eopsaltria australis*, 100%.) Note the
repo's generic `tests/test-images/*` are camera-sized photos with no guaranteed
bird; `bird-cardinal-sample.jpg` is the curated detection fixture.

---

## Durability — what to re-apply after a reboot or re-provision

| Piece | Survives reboot? | Survives WES re-provision? | Action |
|---|---|---|---|
| `/local-cache` dir + data | yes (host path) | yes | verify sticky bit |
| wes-local-cache-manager DaemonSet | yes (auto-restarts) | yes | verify 1/1 |
| side-loaded images in containerd | yes (survived a power-cut reboot on H041) | usually | rebuild if gone |
| **wes-identity 5 vars (Component 2 Tier 1)** | **yes** — a plain reboot leaves it intact | **no** — WES provisioning rewrites it to 2 stock vars | re-run `test-add-configmap.sh` **only** after a WES (re)provision |
| patched scheduler (Tier 2) | no | no | re-run `test-add-scheduler.sh` (if used) |
| producer / consumer pods (`pluginctl run`) | no — go to `Unknown` | no | delete stale, re-run Step 6 |

**Confirmed by a real event:** H041 took a campus power outage mid-run. After it
came back, `/local-cache` + all frames, the cache-manager DaemonSet (auto-restarted),
the side-loaded images, **and the wes-identity 5 vars** were all intact — only the
`pluginctl run` producer/consumer pods had to be relaunched. (An earlier draft said
wes-identity "reverts on every reboot" — that was wrong; it reverts on WES
*re-provision*, not on a plain reboot.)

Clear stale pods before relaunching:

```bash
sudo k3s kubectl delete pod camera-producer camera-audio-producer \
  sage-yolo2-consumer sage-bioclip2-consumer \
  --ignore-not-found --grace-period=0 --force
```

Consumers resume from `/local-cache/.state/`, so they won't reprocess the backlog.

---

## What was verified, and where

**Proven end-to-end on H041 (live Thor, two Reolink cameras, Sep 2026):**
- WES-runtime prerequisite satisfied (`wes-plugin-account` present → `pluginctl`
  schedules) ✓
- Step 0 clone (repo URLs, `master` branch, versions), Step 1 `/local-cache`,
  Step 2 cache-manager (1/1 Running, sweeping) ✓
- Step 3 Tier 1 nodeinfo — pywaggle2 resolved H041's real VSN + GPS ✓
- Step 4 media-sampler3 built from source + side-loaded ✓
- Step 5 GPU builds — **both yolo2 (2.1.0) and bioclip2 (2.0.0) built from source on
  the node and side-loaded** ✓
- Step 6b **two live producers, one per camera** (192.168.4.10→top, .11→side),
  capturing 3840×2160 JPEGs to separate cache subtrees from a shared creds file ✓
- Steps 6c–6f **full cascade**: yolo2 detect → `env.count.bird` + annotated upload +
  crop → bioclip2 species classify → Beehive. Seeded birds classified correctly:
  **`Cardinalis cardinalis` (100%)** and **`Eopsaltria australis` (100%)** ✓
- Durability confirmed by a real power-outage reboot (see Durability table) ✓

**Verified on H038 (clean Thor) along the way:**
- The WES-runtime prerequisite itself — H038 started bare (no `wes-plugin-account`),
  which is exactly how the prerequisite was discovered; after a WES reinstall the
  plugin layer worked ✓
- Tier 1 nodeinfo and cache-manager on a node that had **real GPS** in its manifest ✓

**Caveats / still open:**
- `master` is the default branch on all six repos (the doc links reflect this).
- Reolink cold-boot gotcha (6b): after a power cut a camera's `api.cgi` can 404 /
  RTSP close until it fully initializes, occasionally needing a physical power-cycle.
- Tier 2 (patched scheduler) is optional and was not exercised here — `pluginctl`
  with explicit `--vsn` makes Tier 1 sufficient for this cascade.

---

## Deeper reading

- Cache architecture & caps: cache-manager
  [DESIGN-AND-PURPOSE](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md)
- Identity/GPS contract & sentinel rules: nodeinfo
  [README](https://github.com/flint-pete/wes-nodeinfo-injection/blob/master/README.md),
  pywaggle2-nodeinfo [DESIGN](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md)
- Producer contract, audio path, cache-frame naming: media-sampler3
  [README](https://github.com/flint-pete/media-sampler3/blob/master/README.md) +
  [AUDIO-EXTENSION-DESIGN](https://github.com/flint-pete/media-sampler3/blob/master/docs/AUDIO-EXTENSION-DESIGN.md)
- Detector→classifier design: sage-yolo2
  [overview](https://github.com/flint-pete/sage-yolo2/blob/master/overview.md) +
  [CROP-PRODUCER-Design](https://github.com/flint-pete/sage-yolo2/blob/master/CROP-PRODUCER-Design.md)
- CI-handoff status (what's upstream-owned vs. shimmed) — each repo's `HANDOFF.md`.
