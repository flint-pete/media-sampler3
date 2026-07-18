#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  Please see the file
#  LICENSE.waggle.txt for the legal details of the copyright and software
#  license.  For more details on the Waggle project, visit:
#           http://www.wa8.gl
# ANL:waggle-license
#
# image-sampler2 -- enhanced fork of the Sage/Waggle imagesampler.
# See docs/imagesampler.flint.analysis.txt for the full design (section refs
# below, e.g. "2.2", point at that document).
#
# STAGE 0 (this file): CLI contract + fail-fast validation only. It parses and
# validates every flag combination and then prints the validated configuration
# and exits 0. Acquisition, naming/EXIF, upload, the continuous ring cache, and
# the heartbeat are added in later stages onto this validated spine.

import argparse
import logging
import os
import sys
import time

import acquire
import cache
import capture as capture_mod
import heartbeat as heartbeat_mod
import metadata
import nodemeta
import upload

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%Y/%m/%d %H:%M:%S')

logger = logging.getLogger("image-sampler2")


# Exit codes: fail-FAST on bad config/CLI is a clean, distinct nonzero code so a
# scheduler/operator can tell "you invoked me wrong" from a runtime failure.
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_CAPTURE_ERROR = 3

# Stage 5 heartbeat topics (design §3.2, resolved: keep env.imagesampler.cache.*).
HB_TOPIC_COUNT = "env.imagesampler.cache.count"
HB_TOPIC_BYTES = "env.imagesampler.cache.bytes"
HB_TOPIC_WRITTEN = "env.imagesampler.cache.written"
HB_TOPIC_EVICTED = "env.imagesampler.cache.evicted"
HB_TOPIC_STATUS = "env.imagesampler.cache.last_status"


class ConfigError(Exception):
    """Raised for an invalid flag combination (fail-fast config error).

    Kept separate from argparse's own errors so validate_args() is a pure,
    unit-testable function: it raises ConfigError instead of calling exit(),
    and main() is the only place that turns it into a process exit code.
    """


# Filesystem-safe cache-name: letters, digits, dot, dash, underscore. No path
# separators (would let a --cache-name escape SHARED_ROOT/<cache-name>/, 2.12),
# no whitespace, non-empty.
_CACHE_NAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _is_valid_cache_name(name):
    return bool(name) and all(c in _CACHE_NAME_ALLOWED for c in name)


def build_parser():
    """Build the argparse parser for image-sampler2.

    Returned as a function so tests can construct the parser in isolation.

    Mode is a REQUIRED, mutually-exclusive group (2.2):
      --one-shot            capture once, upload, exit 0.
      --continuous SECONDS  run forever on a fixed period of SECONDS.
    """
    parser = argparse.ArgumentParser(
        prog="image-sampler2",
        description="Sample still images from a camera stream: upload once "
                    "(--one-shot) or maintain a local ring cache (--continuous).")

    # --- mode: required, mutually exclusive (2.2) -------------------------------
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        '--one-shot', dest='one_shot', action='store_true',
        help='Capture exactly one frame, queue it for cloud upload, then exit 0. '
             'External cadence: rely on the SES scheduler to relaunch the pod. '
             'Upload-only: never writes to the ring cache.')
    mode.add_argument(
        '--continuous', dest='continuous', metavar='SECONDS',
        action='store', type=int, default=None,
        help='Run forever, capturing on a FIXED PERIOD of SECONDS. Local-only: '
             'writes each frame into the ring cache (--cache-root) and NEVER '
             'uploads. SECONDS must be a positive integer. ONE stream per process '
             '(run a separate plugin/job per camera).')

    # --- source ----------------------------------------------------------------
    parser.add_argument(
        '--stream', dest='stream', action='append',
        help='ID or name of a camera stream (e.g. top_camera) or a raw URL '
             '(rtsp://IP:PORT/...). Repeat --stream for multiple streams; each '
             'runs in its own worker process. REQUIRED (at least one).')
    parser.add_argument(
        '--name', dest='name', default=[], action='append',
        help='(optional) Label to report for a stream. When given, the count and '
             'order MUST match the --stream options.')
    parser.add_argument(
        '--from-cache', dest='from_cache', metavar='DIR',
        action='store', default=None, type=str,
        help='ONE-SHOT ONLY. Instead of hitting the camera, upload the NEWEST '
             'image already present in cache DIR (populated by a --continuous '
             'producer). Does not touch the camera, write, or evict.')

    # --- continuous ring cache (2.6 / 2.12) ------------------------------------
    parser.add_argument(
        '--cache-root', dest='cache_root', metavar='DIR',
        action='store', default=None, type=str,
        help='CONTINUOUS ONLY. Base dir for the ring cache. The per-stream ring '
             'lives at <cache-root>/<cache-name>/<camera>/. OPTIONAL: defaults to '
             '$IS2_CACHE_ROOT -> /local-cache (the shared node cache provided by '
             'wes-local-cache-manager). The resolved dir MUST already exist and be '
             'writable, else the plugin fails fast. Pass an existing directory here '
             'for local development off-node.')
    parser.add_argument(
        '--cache-name', dest='cache_name', metavar='NAME',
        action='store', default=None, type=str,
        help='CONTINUOUS ONLY. Filesystem-safe identifier for this cache instance '
             '(the <cache-name> path segment) so consumers can find it and two '
             'configs on the same camera do not collide. OPTIONAL: defaults to the '
             'job id (WAGGLE_APP_ID/--job). Allowed: letters, digits, dot, dash, '
             'underscore (no path separators).')
    parser.add_argument(
        '--cache-max-count', dest='cache_max_count', metavar='N',
        action='store', default=None, type=int,
        help='CONTINUOUS ONLY. Max number of images kept per stream in the ring. '
             'Oldest are evicted first. At least one of --cache-max-count / '
             '--cache-max-mb is REQUIRED with --continuous.')
    parser.add_argument(
        '--cache-max-mb', dest='cache_max_mb', metavar='MB',
        action='store', default=None, type=float,
        help='CONTINUOUS ONLY. Max total size (decimal MB, 10^6 bytes) of the '
             'per-stream ring. Oldest are evicted first. At least one of '
             '--cache-max-count / --cache-max-mb is REQUIRED with --continuous.')
    parser.add_argument(
        '--heartbeat-secs', dest='heartbeat_secs', metavar='SECONDS',
        action='store', default=None, type=int,
        help='CONTINUOUS ONLY. Cache-heartbeat cadence in seconds (default 60), '
             'INDEPENDENT of --continuous SECONDS. The heartbeat is the sole '
             'liveness signal in continuous mode (local-only never uploads); it '
             'publishes env.imagesampler.cache.* stats and fires even when '
             'captures fail. Must be a positive integer.')
    parser.add_argument(
        '--max-count', dest='max_count', metavar='N',
        action='store', default=0, type=int,
        help='CONTINUOUS ONLY. Clean self-exit after N captures (default 0 = '
             'unbounded). For windowed/duty-cycled scheduling. Exit is a success '
             '(0). Counts captures only, not heartbeats.')
    parser.add_argument(
        '--max-runtime', dest='max_runtime', metavar='SECONDS',
        action='store', default=0, type=int,
        help='CONTINUOUS ONLY. Clean self-exit after S wall-clock seconds (default '
             '0 = unbounded), checked on a capture edge so >=1 frame is always '
             'delivered. Pairs with cron scheduling; exit is a success (0).')

    # --- camera connection (native-still fetch, design 2.3/4.3) ----------------
    # Address may come from flags or env (CAMERA_HOST/PORT/CHANNEL). Credentials
    # are ENV-ONLY (CAMERA_USER / CAMERA_PASSWORD) and never a flag, so they do
    # not appear in process args / shell history / logs.
    parser.add_argument(
        '--camera-host', dest='camera_host', metavar='HOST',
        action='store', default=os.environ.get('CAMERA_HOST'), type=str,
        help='Camera IP/host for the native-still fetch. Defaults to env '
             'CAMERA_HOST. Required for a from-camera capture.')
    parser.add_argument(
        '--camera-port', dest='camera_port', metavar='PORT',
        action='store', default=int(os.environ.get('CAMERA_PORT', "80")), type=int,
        help='Camera HTTP port (default env CAMERA_PORT or 80).')
    parser.add_argument(
        '--camera-channel', dest='camera_channel', metavar='N',
        action='store', default=int(os.environ.get('CAMERA_CHANNEL', "0")), type=int,
        help='Camera channel (default env CAMERA_CHANNEL or 0).')
    parser.add_argument(
        '--capture-timeout', dest='capture_timeout', metavar='SECONDS',
        action='store', default=float(os.environ.get('CAPTURE_TIMEOUT', "10")), type=float,
        help='Hard timeout (seconds) for a single capture (default 10).')

    # --- node / provenance identity (design 2.9/2.11 field set) ----------------
    # Node identity (vsn, node_id, gps) is resolved at runtime by
    # nodemeta.resolve_identity(): the WES wes-nodeinfo-injection change now projects
    # the 5 WAGGLE_NODE_* env vars into every plugin pod, and the manifest is read as
    # a host-only fallback (dev/spikes). These flags OVERRIDE both, so the plugin
    # self-identifies on any node with no per-node config. Defaults are None here.
    parser.add_argument(
        '--vsn', dest='vsn', metavar='VSN',
        action='store', default=None, type=str,
        help='Node VSN (e.g. H00F). Overrides the node manifest. Used in the v2 '
             'name and EXIF. If omitted, read from /etc/waggle/node-manifest-v2.json.')
    parser.add_argument(
        '--node-id', dest='node_id', metavar='ID',
        action='store', default=None, type=str,
        help='Node hardware id. Overrides the manifest (.name) / /etc/waggle/node-id.')
    parser.add_argument(
        '--job', dest='job', metavar='NAME',
        action='store', default=os.environ.get('WAGGLE_JOB_NAME', 'sage'), type=str,
        help='Job name for provenance. Default env WAGGLE_JOB_NAME or "sage".')
    parser.add_argument(
        '--task', dest='task', metavar='NAME',
        action='store', default=os.environ.get('WAGGLE_TASK_NAME', 'image-sampler2'),
        type=str, help='Task name for provenance. Default env WAGGLE_TASK_NAME.')
    parser.add_argument(
        '--plugin-version', dest='plugin_version', metavar='REF',
        action='store', default=os.environ.get('IS2_PLUGIN_VERSION', 'image-sampler2:dev'),
        type=str, help='Plugin image ref:version for EXIF Software/plugin field.')
    parser.add_argument(
        '--lat', dest='lat', metavar='DEG', action='store',
        default=None, type=float,
        help='Node latitude (decimal degrees). Overrides the manifest (.gps_lat). '
             'If unresolved, omitted from EXIF GPS.')
    parser.add_argument(
        '--lon', dest='lon', metavar='DEG', action='store',
        default=None, type=float,
        help='Node longitude (decimal degrees). Overrides the manifest (.gps_lon). '
             'If unresolved, omitted from EXIF GPS.')
    parser.add_argument(
        '--node-manifest', dest='node_manifest', metavar='PATH',
        action='store', default=None, type=str,
        help='Path to the node manifest JSON (default env WAGGLE_NODE_MANIFEST or '
             '/etc/waggle/node-manifest-v2.json). For testing/off-node use.')

    return parser


def validate_args(args):
    """Validate a parsed args namespace; raise ConfigError on any bad combo.

    Pure function (no exit(), no I/O side effects) so it is unit-testable. Encodes
    the fail-fast rules from the design doc:
      - exactly one mode (argparse already enforces required + mutually
        exclusive, but we re-check for direct callers/tests)                (2.2)
      - --continuous SECONDS must be a positive integer                     (2.2)
      - at least one --stream                                              (2.2)
      - --continuous is ONE stream per process (a1): >1 --stream -> error   (2.6)
      - if --name given, its count must match --stream                     (1.2/2.2)
      - --from-cache is one-shot only                                       (2.8)
      - cache flags (--cache-root/-name/-max-*) are continuous only         (2.6)
      - --continuous REQUIRES at least one cap; root/name are OPTIONAL       (2.6)
        (root defaults to $IS2_CACHE_ROOT -> /local-cache; name -> job id)
      - --cache-name, if given, must be filesystem-safe                     (2.12)
      - caps, if set, must be positive                                      (2.6)
    Cache-root existence/writability IS enforced at startup by
    cache.assert_cache_root_available (fail-fast; no fallback), separately from
    this arg-shape validation.
    """
    one_shot = bool(getattr(args, 'one_shot', False))
    continuous = getattr(args, 'continuous', None)

    # exactly one mode
    if one_shot and continuous is not None:
        raise ConfigError("choose exactly one mode: --one-shot OR --continuous, not both")
    if not one_shot and continuous is None:
        raise ConfigError("a mode is required: pass --one-shot or --continuous SECONDS")

    # continuous interval must be positive
    if continuous is not None and continuous <= 0:
        raise ConfigError(f"--continuous SECONDS must be a positive integer (got {continuous})")

    # at least one stream
    streams = args.stream or []
    if len(streams) == 0:
        raise ConfigError("at least one --stream is required")

    # --continuous is ONE stream per process (a1): reject >1 --stream. One plugin
    # instance = one camera stream; run a separate plugin/job per camera (2.6).
    if continuous is not None and len(streams) > 1:
        raise ConfigError(
            f"--continuous supports exactly one --stream (got {len(streams)}); "
            "run a separate plugin/job per camera (one plugin = one camera stream)")

    # --name count must match --stream count when names are given
    names = args.name or []
    if len(names) > 0 and len(names) != len(streams):
        raise ConfigError(
            f"--name count ({len(names)}) must match --stream count ({len(streams)})")

    # cache flags: which ones were supplied?
    cache_flags_set = [
        ('--cache-root', args.cache_root is not None),
        ('--cache-name', args.cache_name is not None),
        ('--cache-max-count', args.cache_max_count is not None),
        ('--cache-max-mb', args.cache_max_mb is not None),
        ('--heartbeat-secs', args.heartbeat_secs is not None),
    ]
    any_cache_flag = any(present for _, present in cache_flags_set)

    if one_shot:
        # one-shot is upload-only: cache flags are meaningless -> fail-fast (2.6)
        offending = [flag for flag, present in cache_flags_set if present]
        if offending:
            raise ConfigError(
                "cache flags are only valid with --continuous; "
                f"remove {', '.join(offending)} in --one-shot mode")
        # self-exit bounds are continuous-only (one-shot is already one bounded run)
        bound_offending = [f for f, set_ in (("--max-count", args.max_count),
                                             ("--max-runtime", args.max_runtime))
                           if set_]
        if bound_offending:
            raise ConfigError(
                "self-exit bounds are only valid with --continuous; "
                f"remove {', '.join(bound_offending)} in --one-shot mode")
        # --from-cache: newest-from-cache is a valid one-shot source
        if args.from_cache is not None:
            if not args.from_cache:
                raise ConfigError("--from-cache requires a non-empty directory path")
            # existence/emptiness of the cache dir is checked at run time in the
            # Stage that implements --from-cache (2.8); Stage 0 only enforces the
            # flag-combination rules.
        return  # one-shot config is valid

    # --- continuous mode ------------------------------------------------------
    # --from-cache is one-shot only (2.8)
    if args.from_cache is not None:
        raise ConfigError("--from-cache is only valid with --one-shot")

    # root and name are OPTIONAL (2.6): root defaults to $IS2_CACHE_ROOT ->
    # /local-cache, name defaults to the job id. Neither is required here.

    # at least one cap, else the ring is unbounded (2.6)
    if args.cache_max_count is None and args.cache_max_mb is None:
        raise ConfigError(
            "--continuous requires at least one of --cache-max-count / --cache-max-mb "
            "(an unbounded cache is not allowed)")

    # caps, if given, must be positive
    if args.cache_max_count is not None and args.cache_max_count <= 0:
        raise ConfigError(
            f"--cache-max-count must be a positive integer (got {args.cache_max_count})")
    if args.cache_max_mb is not None and args.cache_max_mb <= 0:
        raise ConfigError(
            f"--cache-max-mb must be a positive number (got {args.cache_max_mb})")

    # --cache-name, IF given, must be filesystem-safe (2.12). When omitted it
    # defaults to the job id at run time (validated there).
    if args.cache_name is not None and not _is_valid_cache_name(args.cache_name):
        raise ConfigError(
            f"--cache-name '{args.cache_name}' is not filesystem-safe; use only "
            "letters, digits, dot, dash, underscore (no path separators/spaces)")

    # --heartbeat-secs, IF given, must be a positive int; else default to 60 (§3.2).
    if args.heartbeat_secs is not None and args.heartbeat_secs <= 0:
        raise ConfigError(
            f"--heartbeat-secs must be a positive integer (got {args.heartbeat_secs})")
    if args.heartbeat_secs is None:
        args.heartbeat_secs = 60

    # --max-count / --max-runtime self-exit bounds (§3.3): non-negative ints,
    # 0 = unbounded. Negative is a config error.
    if args.max_count < 0:
        raise ConfigError(
            f"--max-count must be >= 0 (0 = unbounded) (got {args.max_count})")
    if args.max_runtime < 0:
        raise ConfigError(
            f"--max-runtime must be >= 0 (0 = unbounded) (got {args.max_runtime})")


def summarize(args):
    """Human-readable one-line summary of the validated configuration."""
    if args.one_shot:
        src = f"from-cache={args.from_cache}" if args.from_cache else "from-camera"
        return (f"mode=one-shot ({src}) streams={args.stream} "
                f"names={args.name or '(auto)'}")
    caps = []
    if args.cache_max_count is not None:
        caps.append(f"max_count={args.cache_max_count}")
    if args.cache_max_mb is not None:
        caps.append(f"max_mb={args.cache_max_mb}")
    bounds = []
    if getattr(args, "max_count", 0):
        bounds.append(f"max_count={args.max_count}")
    if getattr(args, "max_runtime", 0):
        bounds.append(f"max_runtime={args.max_runtime}s")
    bounds_str = f" bounds=[{', '.join(bounds)}]" if bounds else " bounds=none"
    return (f"mode=continuous interval={args.continuous}s streams={args.stream} "
            f"names={args.name or '(auto)'} cache_root={args.cache_root or '(auto)'} "
            f"cache_name={args.cache_name or '(job)'} caps=[{', '.join(caps)}] "
            f"heartbeat={args.heartbeat_secs or 60}s{bounds_str}")


def _one_shot_from_camera(args):
    """One-shot from camera -> Beehive upload (Stage 3).

    Resolves node identity (flags override; runtime lookup is a sage-ci
    placeholder today), builds the Reolink URL from env-only credentials, then
    captures + embeds + uploads with capture-time keying. Fail-fast on config
    (missing camera host/creds); fail-soft on runtime. Node identity is never
    fatal -- Beehive attributes the node via routing.
    """
    if not args.camera_host:
        logger.error("config error: --camera-host (or env CAMERA_HOST) is required "
                     "for a from-camera capture")
        return EXIT_CONFIG_ERROR

    user = os.environ.get("CAMERA_USER")
    password = os.environ.get("CAMERA_PASSWORD")
    if not user or password is None:
        logger.error("config error: set CAMERA_USER and CAMERA_PASSWORD in the "
                     "environment (credentials are never passed as flags)")
        return EXIT_CONFIG_ERROR

    # Node identity. Flags override; otherwise resolved via nodemeta (runtime
    # lookup now reads the WES-injected WAGGLE_NODE_* env vars in-pod; falls back to
    # a placeholder VSN + omits GPS only when truly unresolvable). Identity is NEVER
    # fatal: Beehive attributes the node via routing regardless; vsn only shapes the
    # v2 filename, lat/lon only enrich EXIF GPS.
    ident = nodemeta.resolve_identity(
        vsn=args.vsn, node_id=args.node_id, lat=args.lat, lon=args.lon,
        manifest_path=args.node_manifest)
    if ident["vsn_is_placeholder"]:
        logger.warning("node VSN not resolvable at runtime (no --vsn, no injected "
                       "WAGGLE_NODE_VSN, no manifest); using PLACEHOLDER vsn=%r. "
                       "Beehive still attributes the real node via routing.",
                       ident["vsn"])
    if ident["lat"] is None or ident["lon"] is None:
        logger.warning("node GPS not resolvable at runtime (no --lat/--lon, no "
                       "injected WAGGLE_NODE_GPS_*, no manifest); omitting EXIF GPS "
                       "coordinates).")

    camera_name = (args.name[0] if args.name else args.stream[0])

    try:
        url = acquire.build_reolink_snap_url(
            args.camera_host, args.camera_port, user, password, args.camera_channel)
    except ValueError as e:
        logger.error("config error: %s", e)
        return EXIT_CONFIG_ERROR

    ok, res = upload.one_shot_upload(
        url=url, capture_timeout=args.capture_timeout,
        vsn=ident["vsn"], node_id=ident["node_id"], job=args.job, task=args.task,
        plugin_version=args.plugin_version, camera=camera_name,
        lat=ident["lat"], lon=ident["lon"])

    if not ok:
        logger.error("STAGE 3: one-shot upload failed: %s", res.get("error"))
        return EXIT_CAPTURE_ERROR

    logger.info("STAGE 3: uploaded %s (%d bytes, uid=%s) capture_ts=%s "
                "grab=%.1fms embed=%.1fms upload=%.1fms",
                res["object_name"], res["final_bytes"], res["unique_id"][:12],
                res["capture_ts_ns"], res["grab_ns"] / 1e6,
                res["embed_ns"] / 1e6, res["upload_ns"] / 1e6)
    return EXIT_OK


def _resolve_camera_config(args):
    """Shared camera/identity resolution for continuous mode. Returns
    (url, ident, camera_name) or raises ConfigError on missing host/creds."""
    if not args.camera_host:
        raise ConfigError("--camera-host (or env CAMERA_HOST) is required for a "
                          "from-camera capture")
    user = os.environ.get("CAMERA_USER")
    password = os.environ.get("CAMERA_PASSWORD")
    if not user or password is None:
        raise ConfigError("set CAMERA_USER and CAMERA_PASSWORD in the environment "
                          "(credentials are never passed as flags)")
    ident = nodemeta.resolve_identity(
        vsn=args.vsn, node_id=args.node_id, lat=args.lat, lon=args.lon,
        manifest_path=args.node_manifest)
    camera_name = (args.name[0] if args.name else args.stream[0])
    try:
        url = acquire.build_reolink_snap_url(
            args.camera_host, args.camera_port, user, password, args.camera_channel)
    except ValueError as e:
        raise ConfigError(str(e))
    return url, ident, camera_name


def run_capture_loop(*, interval_s, do_capture, max_ticks=None,
                     monotonic=None, sleep=None):
    """Monotonic-grid scheduler with skip-on-overrun (design 2.2).

    Fires do_capture() on a fixed grid t0, t0+N, t0+2N ...; recomputes the next
    tick from elapsed monotonic time each cycle so an overrun jumps to the next
    FUTURE slot (no backlog, no busy-loop). Pure/testable: clock + sleep + the
    capture action are injected (default to the real module clock, looked up at
    call time so monkeypatching app.time works). `max_ticks` bounds the loop for
    tests (None = forever). do_capture() must be fail-soft (never raise); its
    return value is ignored here. Returns the number of ticks executed.
    """
    if monotonic is None:
        monotonic = time.monotonic_ns
    if sleep is None:
        sleep = time.sleep
    n_ns = interval_s * 1_000_000_000
    start = monotonic()
    tick = 0
    executed = 0
    while True:
        target = start + tick * n_ns
        now = monotonic()
        if target > now:
            sleep((target - now) / 1e9)
        do_capture()
        executed += 1
        if max_ticks is not None and executed >= max_ticks:
            return executed
        now = monotonic()
        tick = (now - start) // n_ns + 1     # next future slot; drops missed ticks


def run_dual_grid_loop(*, capture_interval_s, do_capture, heartbeat, do_heartbeat,
                       max_iters=None, max_captures=None, max_runtime_ns=None,
                       monotonic=None, sleep=None):
    """Two monotonic grids on ONE thread (design §3.1, resolution 1B).

    - CAPTURE grid: fixed period capture_interval_s, skip-on-overrun (as 2.2).
    - HEARTBEAT grid: owned by the `heartbeat` (Heartbeat) object, its own period.

    Each iteration sleeps to the NEAREST of (next capture edge, next heartbeat
    edge), then fires whichever grid(s) are due on wake. This keeps the heartbeat
    on ~its own cadence even when the capture interval is much longer (a slow
    timelapse still reports alive every ~60s), while never emitting more than one
    heartbeat per grid slot. Both callbacks must be fail-soft (never raise).

    do_capture(): performs one capture (and should call heartbeat.record_capture).
    do_heartbeat(now_ns): publishes one heartbeat (caller reads
        heartbeat.snapshot_and_reset inside). Only called when heartbeat.due().

    Clock/sleep injected (looked up at call time so monkeypatch works). Bounds
    (None = unbounded): max_iters caps WAKE iterations (test harness); max_captures
    caps do_capture() calls (--max-count / test bound); max_runtime_ns caps
    wall-clock ns from loop start (--max-runtime), checked at the TAIL after a
    completed capture so exit lands on a window edge (§3.3). Whichever trips first
    ends the loop; it returns normally so callers' cleanup (finally) runs. Returns
    the number of iterations executed.
    """
    if monotonic is None:
        monotonic = time.monotonic_ns
    if sleep is None:
        sleep = time.sleep
    cap_ns = capture_interval_s * 1_000_000_000
    start = monotonic()
    cap_tick = 0
    iters = 0
    captures = 0
    while True:
        now = monotonic()
        next_cap = start + cap_tick * cap_ns
        next_hb = heartbeat.next_due_ns(now)
        wake_at = min(next_cap, next_hb)
        if wake_at > now:
            sleep((wake_at - now) / 1e9)
        now = monotonic()

        # fire heartbeat first if due, so an immediate startup beat lands before
        # the first capture (count=0/bytes=0 == "I came up").
        if heartbeat.due(now):
            do_heartbeat(now)

        # fire capture if its grid edge has arrived
        if now >= next_cap:
            do_capture()
            captures += 1
            cap_tick = (now - start) // cap_ns + 1   # next future capture slot

        iters += 1
        if max_iters is not None and iters >= max_iters:
            return iters
        if max_captures is not None and captures >= max_captures:
            return iters
        # Wall-clock bound (§3.3): checked at the loop TAIL so exit lands after a
        # completed capture edge, never mid-interval. Requires >=1 capture so a
        # sub-interval runtime still delivers at least one frame.
        if (max_runtime_ns is not None and captures >= 1
                and (now - start) >= max_runtime_ns):
            return iters


def _continuous_to_cache(args, *, max_ticks=None, plugin=None,
                         monotonic=None, sleep=None):
    """--continuous producer: capture on a fixed grid into a per-stream ring
    (design 2.2 + 2.6). LOCAL-ONLY (never uploads). One stream per process (a1).

    Fail-FAST on config (camera host/creds, cache root unwritable). Fail-SOFT at
    runtime: a bad capture or FS hiccup warns and skips; the loop keeps running.
    `max_ticks`/`plugin`/`monotonic`/`sleep` are injection points for tests.
    """
    try:
        url, ident, camera_name = _resolve_camera_config(args)
    except ConfigError as e:
        logger.error("config error: %s", e)
        return EXIT_CONFIG_ERROR

    if ident["vsn_is_placeholder"]:
        logger.warning("node VSN not resolvable at runtime (sage-ci runtime VSN "
                       "call not yet available); using PLACEHOLDER vsn=%r.",
                       ident["vsn"])
    if ident["lat"] is None or ident["lon"] is None:
        logger.warning("node GPS not resolvable at runtime; omitting EXIF GPS "
                       "(not faking coordinates).")

    # Resolve cache location: root defaults to $IS2_CACHE_ROOT -> /local-cache
    # (the shared node cache). Fail-FAST if it isn't a writable directory -- there
    # is no fallback, since frames written where no consumer can read them are
    # worse than a clean error. name defaults to the job id.
    cache_root = cache.resolve_cache_root(args.cache_root)
    try:
        cache.assert_cache_root_available(cache_root)
    except cache.CacheError as e:
        logger.error("config error: %s", e)
        return EXIT_CONFIG_ERROR
    cache_name = args.cache_name or _safe_job_name(args.job)
    try:
        sdir = cache.stream_dir(cache_root, cache_name, camera_name)
    except cache.CacheError as e:
        logger.error("config error: %s", e)
        return EXIT_CONFIG_ERROR

    logger.info("STAGE 4: continuous -> ring %s (interval=%ds caps: count=%s mb=%s "
                "heartbeat=%ss)", sdir, args.continuous, args.cache_max_count,
                args.cache_max_mb, args.heartbeat_secs or 60)

    # Open a pywaggle Plugin for the heartbeat (continuous is local-only, so the
    # heartbeat is the SOLE liveness signal, §3.2). Fail-SOFT: if the Plugin can't
    # be created (e.g. bare off-node test), run WITHOUT heartbeats -- the cache
    # still works. Tests inject a fake plugin.
    own_plugin = False
    if plugin is None:
        try:
            from waggle.plugin import Plugin
            plugin = Plugin()
            plugin.__enter__()
            own_plugin = True
        except Exception as e:
            logger.warning("STAGE 5: pywaggle Plugin unavailable (%s); running "
                           "WITHOUT heartbeats (cache still active).", e)
            plugin = None

    hb_secs = args.heartbeat_secs if args.heartbeat_secs else 60
    _mono = monotonic if monotonic is not None else time.monotonic_ns
    hb = heartbeat_mod.Heartbeat(hb_secs, start_ns=_mono())

    def one_tick():
        status = heartbeat_mod.STATUS_SKIP
        written = False
        evicted = 0
        try:
            cap = capture_mod.capture_and_embed_to_tmp(
                url=url, capture_timeout=args.capture_timeout,
                vsn=ident["vsn"], node_id=ident["node_id"], job=args.job,
                task=args.task, plugin_version=args.plugin_version,
                camera=camera_name, lat=ident["lat"], lon=ident["lon"],
                dest_dir=sdir)
        except capture_mod.CaptureError as e:
            logger.warning("STAGE 4: capture skipped: %s", e)
            hb.record_capture(written=False, evicted=0, status=heartbeat_mod.STATUS_SKIP)
            return
        ring = cache.scan_ring(sdir)
        plan = cache.plan_evictions(ring, cap["final_bytes"],
                                    args.cache_max_count, args.cache_max_mb)
        res = cache.commit_capture(sdir, cap["tmp_path"], cap["final_name"], plan)
        for w in res.warnings:
            logger.warning("STAGE 4: %s", w)
        evicted = len(res.evicted)
        if res.written:
            written = True
            status = heartbeat_mod.STATUS_OK
            after = cache.scan_ring(sdir)
            logger.info("STAGE 4: wrote %s size=%d evicted=%d ring_count=%d "
                        "ring_mb=%.3f", cap["final_name"], cap["final_bytes"],
                        evicted, after.count,
                        after.total_bytes / cache.BYTES_PER_MB)
        else:
            # captured but not written (E3 drop / failed publish) -> fail status
            status = heartbeat_mod.STATUS_FAIL
        hb.record_capture(written=written, evicted=evicted, status=status)

    def do_heartbeat(now_ns):
        ring = cache.scan_ring(sdir)
        payload = hb.snapshot_and_reset(ring.count, ring.total_bytes, now_ns)
        logger.info("STAGE 5: heartbeat count=%d bytes=%d written=%d evicted=%d "
                    "status=%s", payload["count"], payload["bytes"],
                    payload["written"], payload["evicted"], payload["last_status"])
        if plugin is None:
            return
        meta = {"cache_name": str(cache_name), "camera": str(camera_name),
                "vsn": str(ident["vsn"])}
        ts = metadata.now_capture_ts_ns()   # wall-clock ns for the data record
        for topic, value in ((HB_TOPIC_COUNT, payload["count"]),
                             (HB_TOPIC_BYTES, payload["bytes"]),
                             (HB_TOPIC_WRITTEN, payload["written"]),
                             (HB_TOPIC_EVICTED, payload["evicted"]),
                             (HB_TOPIC_STATUS, payload["last_status"])):
            try:
                plugin.publish(topic, value, timestamp=ts, meta=meta)
            except Exception as e:
                # publishing must NEVER kill the loop (fail-soft, §3.3)
                logger.warning("STAGE 5: heartbeat publish failed for %s: %s",
                               topic, e)

    # Self-exit bounds (§3.3). Test harness passes max_ticks (caps captures);
    # production passes --max-count / --max-runtime. Tests never set --max-count and
    # production never sets max_ticks, so take whichever capture bound is set.
    eff_max_captures = max_ticks if max_ticks is not None else (
        args.max_count if getattr(args, "max_count", 0) else None)
    eff_max_runtime_ns = (args.max_runtime * 1_000_000_000
                          if getattr(args, "max_runtime", 0) else None)
    try:
        run_dual_grid_loop(capture_interval_s=args.continuous, do_capture=one_tick,
                           heartbeat=hb, do_heartbeat=do_heartbeat,
                           max_captures=eff_max_captures,
                           max_runtime_ns=eff_max_runtime_ns,
                           monotonic=monotonic, sleep=sleep)
    finally:
        if own_plugin and hasattr(plugin, "__exit__"):
            try:
                plugin.__exit__(None, None, None)
            except Exception:
                pass
    return EXIT_OK


def _safe_job_name(job):
    """Coerce a job label into a filesystem-safe cache-name; fallback if empty."""
    import re
    candidate = re.sub(r"[^A-Za-z0-9._-]", "-", str(job or "")).strip("-")
    return candidate or "imagesampler2"


def _one_shot_from_cache(args):
    """--one-shot --from-cache <dir>: upload the NEWEST cached v2 image (§2.8).

    Reads the newest managed v2 file in the STREAM dir <from_cache>, uploads it via
    the from-cache path (original capture-ts preserved, no camera hit, no evict).
    Fail-FAST (exit 2) if the dir is missing/not a dir or holds ZERO valid v2
    images. Runtime read/upload failure -> exit 3.
    """
    d = args.from_cache
    if not d or not os.path.isdir(d):
        logger.error("config error: --from-cache dir does not exist: %r", d)
        return EXIT_CONFIG_ERROR

    # Reuse the ring scan so "valid managed v2 file" is defined in ONE place
    # (ignores .tmp / non-v2 names); newest = max capture_ts_ns.
    ring = cache.scan_ring(d)
    if ring.count == 0:
        logger.error("config error: --from-cache dir has no v2 images: %s", d)
        return EXIT_CONFIG_ERROR
    newest = max(ring.members, key=lambda m: m.capture_ts_ns)
    path = os.path.join(d, newest.name)
    logger.info("STAGE 6: from-cache newest=%s (of %d cached)", newest.name,
                ring.count)

    ok, info = upload.cache_upload(path=path)
    if not ok:
        logger.error("STAGE 6: from-cache upload failed: %s", info.get("error"))
        return EXIT_CAPTURE_ERROR
    logger.info("STAGE 6: uploaded %s (capture_ts=%s upload_ns=%s)",
                info.get("object_name"), info.get("capture_ts_ns"),
                info.get("upload_ns"))
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
    except ConfigError as e:
        logger.error("config error: %s", e)
        return EXIT_CONFIG_ERROR

    logger.info("image-sampler2 config OK: %s", summarize(args))

    # Dispatch. One-shot-from-camera (Stage 3) and continuous-to-cache (Stage 4)
    # are wired; --from-cache is Stage 6.
    if args.one_shot and args.from_cache is None:
        if len(args.stream) > 1:
            logger.warning("STAGE 3: multi-stream not wired in one-shot; capturing "
                           "the first stream only (%s)", args.stream[0])
        return _one_shot_from_camera(args)

    if args.one_shot and args.from_cache is not None:
        return _one_shot_from_cache(args)

    # continuous (single stream, enforced by validate_args)
    return _continuous_to_cache(args)


if __name__ == '__main__':
    sys.exit(main())
