# Reboot recovery: restarting the media stack after a node reboot

**Why this exists.** Nothing in the media stack is part of the WES base image:

- The images were built on the node and side-loaded into k3s.
- The plugins run as hand-launched (`pluginctl-nodeinfo run`) pods.
- The node-identity change edits a live WES object (the `wes-identity` ConfigMap).

So a reboot or power cut stops the stack, and the plugin pods do **not** come
back on their own. This page is the one ordered checklist for bringing everything
back. Every step checks first and only re-applies what is actually missing, so
running it again does no harm.

Run it on the node, in order. The launch commands are the same as
[INSTALLING-MEDIA-SAMPLER3.md](INSTALLING-MEDIA-SAMPLER3.md) Step 6. If you
changed a launch there, change it here too. Repo clones are assumed to be in
`~/AI-projects/` (install Step 0).

This page is retired once the CI team publishes the images to the registry and
folds the node-identity change into the WES base stack. Until then it is the
recovery path.

---

## What survives a reboot, and what doesn't

| Piece | After a plain reboot | Action |
|-------|---------------------|--------|
| `/media/plugin-data/local-cache` (frames, crops, `.state/` seen-stores) | survives (it's a host directory) | check it exists with the sticky bit |
| Standard WES services (rabbitmq, upload-agent, scheduler, …) | restart on their own | check that they're Running |
| Side-loaded images in containerd | **usually** survive (they did through H041's power cut), but not guaranteed | check; rebuild if missing |
| wes-local-cache-manager DaemonSet | restarts on its own **if its image survived** | check 1/1; if `ImagePullBackOff`, re-run its add script |
| `wes-identity` 5-var ConfigMap (install Step 2) | survived on H041, but earlier H00F notes saw it revert. Any WES reinstall or `update-stack.sh` run **does** reset it | check; re-apply if the GPS vars are missing |
| `/usr/local/bin/pluginctl-nodeinfo` (install Step 3) | survives (a file on disk) | check it's there; it reads the ConfigMap fresh at every launch |
| Producer and consumer pods | **gone** (the listing shows stale `Unknown`/`Failed` entries) | delete stale entries, relaunch |

**How to recognize a reboot.** Every plugin pod shows `Unknown` or `Failed`, and
the newest files in the cache stopped at the same moment. Confirm with `uptime`.
A low uptime plus stale pods means a reboot. The control plane isn't broken; k3s
just still lists the dead pods.

---

## 0. Set variables, confirm the reboot, and clear stale pods

```bash
ssh <user>@node-<VSN>.sage
VSN=$(cat /etc/waggle/vsn); echo $VSN
CAM_IP=<CAM_IP>          # the camera (non-secret; keep it in your notes)

uptime                                    # low uptime => it was a reboot
sudo k3s kubectl get nodes                # expect Ready
sudo k3s kubectl get pods | grep -iE 'camera-|yolo2|bioclip2|birdnet2'   # expect Unknown/Failed

sudo k3s kubectl delete pod camera-producer camera-audio-producer \
    sage-yolo2-consumer sage-bioclip2-consumer sage-birdnet2-consumer \
    --ignore-not-found --grace-period=0 --force
```

(Add the pod names of any extra cameras' producers and yolo2 instances.)

## 1. Check the WES base services

```bash
sudo k3s kubectl get pods | grep -E 'wes-rabbitmq|wes-plugin-scheduler|wes-upload-agent|wes-scoreboard'
```

All four should be Running, which can take a few minutes after boot. If WES
itself is broken, fix that first (out of scope here). If WES was reinstalled or
updated, expect step 4 to find `wes-identity` reset.

## 2. Check the cache directory and the cache manager

```bash
ls -ld /media/plugin-data/local-cache              # expect drwxrwxrwt
sudo k3s kubectl get daemonset wes-local-cache-manager   # expect DESIRED = READY = 1
sudo k3s kubectl get pods -l app.kubernetes.io/name=wes-local-cache-manager
```

- If the directory lost its sticky bit, run
  `sudo chmod 1777 /media/plugin-data/local-cache`.
- If the manager pod is `ImagePullBackOff`/`ErrImageNeverPull`, its side-loaded
  image is gone. Rebuild and re-apply it:
  `cd ~/AI-projects/wes-local-cache-manager && ./test-add-node.sh`.

## 3. Check the side-loaded plugin images

```bash
sudo k3s ctr images ls -q | grep -E 'media-sampler3|sage-yolo2|sage-bioclip2|sage-birdnet2|wes-local-cache-manager'
```

Expect `localhost/media-sampler3:0.1.0`, `.../sage-yolo2:2.1.0`,
`.../sage-bioclip2:2.0.0`, `.../sage-birdnet2:2.0.0`, and the cache manager.
Rebuild anything missing:

```bash
cd ~/AI-projects/media-sampler3 && make sideload                     # about a minute
cd ~/AI-projects/sage-yolo2     && scripts/deploy-sideload.sh --skip-register   # long
cd ~/AI-projects/sage-bioclip2  && scripts/deploy-sideload.sh --skip-register   # long
cd ~/AI-projects/sage-birdnet2  && scripts/deploy-sideload.sh --skip-register   # a few minutes
```

## 4. Check node identity: the ConfigMap and `pluginctl-nodeinfo`

```bash
sudo k3s kubectl get configmap wes-identity -o jsonpath='{.data}'; echo
command -v pluginctl-nodeinfo             # expect /usr/local/bin/pluginctl-nodeinfo
```

If `WAGGLE_NODE_GPS_LAT`, `WAGGLE_NODE_GPS_LON`, and `WAGGLE_NODE_MOBILITY` are
missing (only the 2 stock vars remain), re-apply (install Step 2):

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
./test-add-configmap.sh          # expect the NodeInfo JSON with this node's vsn/lat/lon
```

Without the 5 variables, plugins still start, but the producer falls back to the
placeholder VSN `NODE` and no GPS, so don't skip this. If `pluginctl-nodeinfo` is
missing, re-run `./install-pluginctl-nodeinfo.sh` from the same directory
(install Step 3).

> **Only if you also installed the patched WES scheduler** (wes-nodeinfo-injection
> "Tier 2", not part of the install guide): check
> `sudo k3s kubectl get pods | grep wes-plugin-scheduler`. If it is
> `ImagePullBackOff`, **plugin scheduling is down for the whole node**; run
> `./test-remove-scheduler.sh` in that directory right away to restore the stock
> scheduler.

## 5. Recreate the camera credentials file (if you deleted it)

```bash
ls -l ~/ms3-cam-creds.env 2>/dev/null || {
  umask 077
  printf 'CAMERA_USER=<USER>\nCAMERA_PASSWORD=<PASS>\n' > ~/ms3-cam-creds.env
  chmod 600 ~/ms3-cam-creds.env; }
```

Get the real values from the camera owner. Never commit them or paste them into
docs or chat.

## 6. Check that the camera is reachable

```bash
curl -s -o /dev/null -w 'http: %{http_code}\n' --max-time 5 "http://$CAM_IP/"
```

After a power cut, the camera rebooted too. A Reolink can serve its web page
(200) while its snapshot API still returns 404 for several minutes. The producer
retries and recovers on its own. If it stays 404 for more than 15 minutes,
power-cycle the camera.

## 7. Relaunch the producers (media-sampler3: images and audio)

Start the producers before the consumers. `pluginctl-nodeinfo run` stays attached,
so each launch ends with `&`.

```bash
sudo pluginctl-nodeinfo run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 \
  --camera-host "$CAM_IP" --camera-port 80 &

sudo pluginctl-nodeinfo run --name camera-audio-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
  --camera-host "$CAM_IP" --camera-port 80 \
  --audio-format flac --bandpass-fmax 8000 \
  --stream mic --cache-name camera-audio \
  --cache-max-count 500 --cache-max-mb 2000 --heartbeat-secs 60 &
```

Keep `--cache-name`/`--name` identical to before. The consumers' `--input` paths
depend on them.

Check that fresh frames arrive every 10 s, and clips every minute:

```bash
sleep 75
sudo ls -lt /media/plugin-data/local-cache/camera/top/ | head -3
sudo ls -lt /media/plugin-data/local-cache/camera-audio/mic/ | head -3
```

## 8. Relaunch the consumers (sage-yolo2, sage-bioclip2, sage-birdnet2)

```bash
sudo pluginctl-nodeinfo run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops &

sudo pluginctl-nodeinfo run --name sage-bioclip2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-bioclip2 \
  registry.sagecontinuum.org/beckman/sage-bioclip2:2.0.0 -- \
  --source cache --input /local-cache/camera-crops/top-crop-0 \
  --every 10m --all-unseen --max-frames 0 --rank Species --min-confidence 0.1 &

sudo pluginctl-nodeinfo run --name sage-birdnet2-consumer --selector zone=core \
  --resource limit.memory=2Gi,request.memory=1Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-birdnet2 \
  registry.sagecontinuum.org/beckman/sage-birdnet2:2.0.0 -- \
  --source cache --input /local-cache/camera-audio/mic \
  --every 10m --all-unseen --max-frames 0 --min-confidence 0.6 &
```

Use the **same** `WAGGLE_JOB_NAME`/`WAGGLE_TASK_NAME` as before. Those values
name the seen-store under `/local-cache/.state/`, so the consumers resume where
they left off instead of reprocessing the whole backlog. If you only want the
newest frames processed, add `--select-every 0 --max-frames K` with a new
`--consumer-id`.

## 9. Verify, in the cloud and not just on the node

```bash
sudo k3s kubectl get pods | grep -iE 'camera-|yolo2-consumer|bioclip2-consumer|birdnet2-consumer'
# all Running, 0 restarts

curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H 'Content-Type: application/json' \
  -d "{\"start\":\"-15m\",\"filter\":{\"vsn\":\"$VSN\",\"name\":\"env.count.total\"}}" | tail -2
```

Recent `env.count.total` rows mean frames are flowing from the camera through
media-sampler3 and yolo2 to Beehive; their `meta` should include this node's
`lat`/`lon`. `env.species.species` rows appear once a bird is detected and
classified. For audio, `env.detection.audio.summary` appears once per clip after
birdnet2's next wake (up to 10 minutes); bird detections appear as
`env.detection.biophony.<species>`.

---

## Tip: record what is actually running

Before you change anything, and after recovery, save the live specs so you never
have to rebuild them from memory:

```bash
for p in camera-producer camera-audio-producer sage-yolo2-consumer \
         sage-bioclip2-consumer sage-birdnet2-consumer; do
  echo "== $p"; sudo k3s kubectl get pod $p \
    -o jsonpath='{.spec.containers[0].image}{"\n"}{.spec.containers[0].args}{"\n"}' 2>/dev/null
done
```

The args contain no secrets (credentials come from the env file). The full pod
spec (`-o yaml`) does show them, as plain `env` values.
