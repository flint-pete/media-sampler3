# Reboot recovery: restarting the media stack after a node reboot

**Why this exists.** Nothing in the media stack is part of the WES base image:

- The images were built on the node and side-loaded into k3s.
- The plugins run as `pluginctl run` pods.
- The nodeinfo change edits a live WES object.

So a reboot or power cut stops the stack, and the plugin pods do **not** come
back on their own. This page is the one ordered checklist for bringing everything
back. Every step checks first and only re-applies what is actually missing, so
running it again does no harm.

Run it on the node, in order. The commands are the same as
[INSTALLING-MEDIA-SAMPLER3.md](INSTALLING-MEDIA-SAMPLER3.md) Step 6. If you
changed a launch there, change it here too. Repo clones are assumed to be in
`~/AI-projects/` (install Step 0).

This page is retired once the CI team publishes the images to the registry and
folds the nodeinfo change into the WES base stack. Until then it is the recovery
path.

---

## What survives a reboot, and what doesn't

| Piece | After a plain reboot | Action |
|-------|---------------------|--------|
| `/media/plugin-data/local-cache` (frames, crops, `.state/` seen-stores) | survives (it's a host directory) | check it exists with the sticky bit |
| Standard WES services (rabbitmq, upload-agent, scheduler, …) | restart on their own | check that they're Running |
| Side-loaded images in containerd | **usually** survive (they did through H041's power cut), but not guaranteed | check; rebuild if missing |
| wes-local-cache-manager DaemonSet | restarts on its own **if its image survived** | check 1/1; if `ImagePullBackOff`, re-run its add script |
| `wes-identity` 5-var ConfigMap (nodeinfo Tier 1) | survived on H041, but earlier H00F notes saw it revert. Any WES reinstall or `update-stack.sh` run **does** reset it | check; re-apply if the GPS vars are missing |
| Patched edge-scheduler (nodeinfo Tier 2, only if you installed it) | the Deployment patch survives; the pod works **only if its image survived** | check; if `ImagePullBackOff`, restore stock or re-add |
| Tier 1b `~/bin/pluginctl-nodeinfo` | survives (a file in your home directory) | nothing; it reads the ConfigMap fresh at every launch, so step 4 matters |
| Producer and consumer pods (`pluginctl run`) | **gone** (the listing shows stale `Unknown`/`Failed` entries) | delete stale entries, relaunch |

**How to recognize a reboot.** Every plugin pod shows `Unknown` or `Failed`, and
the newest files in the cache stopped at the same moment. Confirm with `uptime`.
A low uptime plus stale pods means a reboot. The control plane isn't broken; k3s
just still lists the dead pods.

---

## 0. Set variables, confirm the reboot, and clear stale pods

```bash
ssh <user>@node-<VSN>.sage
VSN=$(cat /etc/waggle/vsn); echo $VSN
CAM1_IP=<CAM1_IP>        # top camera   (non-secret; keep these in your notes)
CAM2_IP=<CAM2_IP>        # side camera  (omit if you have one camera)
PCTL=~/bin/pluginctl-nodeinfo   # Tier 1b; use PCTL=pluginctl if you didn't install it

uptime                                    # low uptime => it was a reboot
sudo k3s kubectl get nodes                # expect Ready
sudo k3s kubectl get pods | grep -iE 'camera-|yolo2|bioclip2'   # expect Unknown/Failed

sudo k3s kubectl delete pod camera-producer camera-producer-side camera-audio-producer \
    sage-yolo2-consumer sage-bioclip2-consumer \
    --ignore-not-found --grace-period=0 --force
```

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
sudo k3s ctr images ls -q | grep -E 'media-sampler3|sage-yolo2|sage-bioclip2|wes-local-cache-manager|edge-scheduler'
```

Expect `localhost/media-sampler3:0.1.0`, `.../sage-yolo2:2.1.0`, and
`.../sage-bioclip2:2.0.0`, plus the cache manager and, if you used Tier 2, the
`edge-scheduler:nodeinfo-test` image. Rebuild anything missing:

```bash
cd ~/AI-projects/media-sampler3 && make sideload                     # minutes
cd ~/AI-projects/sage-yolo2     && scripts/deploy-sideload.sh --skip-register   # long
cd ~/AI-projects/sage-bioclip2  && scripts/deploy-sideload.sh --skip-register   # long
```

## 4. Check the node identity ConfigMap (nodeinfo Tier 1)

```bash
sudo k3s kubectl get configmap wes-identity -o jsonpath='{.data}'; echo
```

If `WAGGLE_NODE_GPS_LAT`, `WAGGLE_NODE_GPS_LON`, and `WAGGLE_NODE_MOBILITY` are
missing (only the 2 stock vars remain), re-apply:

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test
export KUBECTL="sudo k3s kubectl"
./test-add-configmap.sh          # expect the NodeInfo JSON with this node's vsn/lat/lon
```

The cascade below keeps working even if you skip this step, because the producer
gets `--vsn`. But do it anyway. With Tier 1b, a missing GPS here means plugins
silently lose their node GPS fallback.

## 5. Check the patched scheduler (nodeinfo Tier 2), only if you installed it

```bash
sudo k3s kubectl get deployment wes-plugin-scheduler \
  -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
sudo k3s kubectl get pods | grep wes-plugin-scheduler
```

If the image is `docker.io/library/edge-scheduler:nodeinfo-test` and the pod is
Running, you're done. If the pod is `ImagePullBackOff`, **plugin scheduling is
down for the whole node**. Fix it right away with one of these:

```bash
cd ~/AI-projects/wes-nodeinfo-injection/node-test && export KUBECTL="sudo k3s kubectl"
./test-remove-scheduler.sh       # fastest: restore the stock scheduler, or
./test-add-scheduler.sh          # rebuild + re-apply the patched one (~3-6 min;
                                 # needs ../.upstream/edge-scheduler prepared; see install Step 3)
```

## 6. Recreate the camera credentials file (if you deleted it)

```bash
ls -l ~/ms3-cam-creds.env 2>/dev/null || {
  umask 077
  printf 'CAMERA_USER=<USER>\nCAMERA_PASSWORD=<PASS>\n' > ~/ms3-cam-creds.env
  chmod 600 ~/ms3-cam-creds.env; }
```

Get the real values from the camera owner. Never commit them or paste them into
docs or chat.

## 7. Check that the cameras are reachable

```bash
for ip in $CAM1_IP $CAM2_IP; do
  echo "== $ip =="; curl -s -o /dev/null -w 'http: %{http_code}\n' --max-time 5 "http://$ip/"
done
```

After a power cut, the cameras rebooted too. A Reolink can serve its web page
(200) while its snapshot API still returns 404 for several minutes. The producers
retry and recover on their own. If it stays 404 for more than 15 minutes,
power-cycle the camera.

## 8. Relaunch the producers (media-sampler3)

Start the producers before the consumers. `pluginctl run` stays attached, so each
launch ends with `&`.

```bash
sudo $PCTL run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host "$CAM1_IP" --camera-port 80 &

sudo $PCTL run --name camera-producer-side --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream side_camera --name side \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host "$CAM2_IP" --camera-port 80 &

# Optional: audio producer (only if you ran it before)
# sudo $PCTL run --name camera-audio-producer --selector zone=core \
#   --env-from ~/ms3-cam-creds.env -v /media/plugin-data/local-cache:/local-cache \
#   localhost/media-sampler3:0.1.0 -- \
#   --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
#   --camera-host "$CAM1_IP" --camera-port 80 --audio-format flac --bandpass-fmax 8000 \
#   --stream mic --cache-name camera-audio \
#   --cache-max-count 500 --cache-max-mb 2000 --heartbeat-secs 60 --vsn "$VSN" &
```

Keep `--cache-name`/`--name` identical to before. The consumers' `--input` paths
depend on them.

Check that fresh frames are arriving every 10 s:

```bash
sleep 30; sudo ls -lt /media/plugin-data/local-cache/camera/top/ | head -3
```

## 9. Relaunch the consumers (sage-yolo2, then sage-bioclip2)

```bash
sudo $PCTL run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops &

sudo $PCTL run --name sage-bioclip2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-bioclip2 \
  registry.sagecontinuum.org/beckman/sage-bioclip2:2.0.0 -- \
  --source cache --input /local-cache/camera-crops/top-crop-0 \
  --every 10m --all-unseen --max-frames 0 --rank Species --min-confidence 0.1 &
```

Use the **same** `WAGGLE_JOB_NAME`/`WAGGLE_TASK_NAME` as before. Those values
name the seen-store under `/local-cache/.state/`, so the consumers resume where
they left off instead of reprocessing the whole backlog. If you only want the
newest frames processed, add `--select-every 0 --max-frames K` with a new
`--consumer-id`.

## 10. Verify, in the cloud and not just on the node

```bash
sudo k3s kubectl get pods | grep -iE 'camera-producer|yolo2-consumer|bioclip2-consumer'
# all Running, 0 restarts

curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H 'Content-Type: application/json' \
  -d "{\"start\":\"-15m\",\"filter\":{\"vsn\":\"$VSN\",\"name\":\"env.count.total\"}}" | tail -2
```

Recent `env.count.total` rows mean frames are flowing from the camera through
media-sampler3 and yolo2 to Beehive. `env.species.species` rows appear once a
bird is detected and classified.

---

## Tip: record what is actually running

Before you change anything, and after recovery, save the live specs so you never
have to rebuild them from memory:

```bash
for p in camera-producer camera-producer-side sage-yolo2-consumer sage-bioclip2-consumer; do
  echo "== $p"; sudo k3s kubectl get pod $p \
    -o jsonpath='{.spec.containers[0].image}{"\n"}{.spec.containers[0].args}{"\n"}' 2>/dev/null
done
```

That output contains no secrets (credentials come from the env file, not args).
