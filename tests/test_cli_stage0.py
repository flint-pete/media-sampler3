# Stage 0 tests for image-sampler2 CLI contract + fail-fast validation.
#
# These are PURE tests: no camera, no network, no pywaggle. They exercise the
# real argparse parser (build_parser) and the pure validator (validate_args),
# asserting that every bad flag combination is rejected with a clear message and
# every good combination is accepted. Section refs (e.g. 2.6) point at
# docs/imagesampler.flint.analysis.txt.
#
# Run:  python3 -m pytest tests/ -q      (or: python3 -m pytest tests/test_cli_stage0.py -v)

import os
import sys
from unittest import mock

import pytest

# Make app.py importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402
import nodemeta  # noqa: E402


def parse(argv):
    """Parse argv with the real parser. Returns the args namespace.

    argparse errors (e.g. missing required mode, mutually-exclusive violation,
    bad int) raise SystemExit -- tests that expect those catch SystemExit.
    Cross-flag rules are enforced by validate_args and raise ConfigError.
    """
    return app.build_parser().parse_args(argv)


def validate(argv):
    """Parse then validate. Raises SystemExit (argparse) or ConfigError."""
    args = parse(argv)
    app.validate_args(args)
    return args


# ---------------------------------------------------------------------------
# MODE: required + mutually exclusive (2.2)
# ---------------------------------------------------------------------------

def test_no_mode_is_rejected_by_argparse():
    # argparse enforces the required mode group -> SystemExit
    with pytest.raises(SystemExit):
        parse(["--stream", "top_camera"])


def test_both_modes_rejected_by_argparse():
    with pytest.raises(SystemExit):
        parse(["--one-shot", "--continuous", "10", "--stream", "top_camera"])


def test_continuous_non_integer_rejected_by_argparse():
    with pytest.raises(SystemExit):
        parse(["--continuous", "abc", "--stream", "top_camera"])


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_continuous_non_positive_rejected(bad):
    with pytest.raises(app.ConfigError, match="positive integer"):
        validate(["--continuous", bad, "--stream", "top_camera",
                  "--cache-root", ".", "--cache-name", "c", "--cache-max-count", "5"])


# ---------------------------------------------------------------------------
# STREAM / NAME (1.2 / 2.2)
# ---------------------------------------------------------------------------

def test_missing_stream_rejected():
    with pytest.raises(app.ConfigError, match="at least one --stream"):
        validate(["--one-shot"])


def test_name_count_mismatch_rejected():
    with pytest.raises(app.ConfigError, match="must match --stream count"):
        validate(["--one-shot", "--stream", "a", "--stream", "b", "--name", "only_one"])


def test_name_count_match_ok():
    args = validate(["--one-shot", "--stream", "a", "--stream", "b",
                     "--name", "x", "--name", "y"])
    assert args.name == ["x", "y"]


def test_names_optional_ok():
    args = validate(["--one-shot", "--stream", "a"])
    assert args.name == []


# ---------------------------------------------------------------------------
# ONE-SHOT: upload-only, cache flags rejected (2.6); --from-cache ok (2.8)
# ---------------------------------------------------------------------------

def test_one_shot_from_camera_ok():
    args = validate(["--one-shot", "--stream", "top_camera"])
    assert args.one_shot is True
    assert args.from_cache is None


@pytest.mark.parametrize("cache_flag", [
    ["--cache-root", "."],
    ["--cache-name", "c"],
    ["--cache-max-count", "10"],
    ["--cache-max-mb", "50"],
])
def test_one_shot_rejects_cache_flags(cache_flag):
    with pytest.raises(app.ConfigError, match="only valid with --continuous"):
        validate(["--one-shot", "--stream", "a"] + cache_flag)


def test_one_shot_from_cache_ok(tmp_path):
    args = validate(["--one-shot", "--stream", "a", "--from-cache", str(tmp_path)])
    assert args.from_cache == str(tmp_path)


# ---------------------------------------------------------------------------
# --from-cache is one-shot only (2.8)
# ---------------------------------------------------------------------------

def test_from_cache_rejected_in_continuous(tmp_path):
    with pytest.raises(app.ConfigError, match="only valid with --one-shot"):
        validate(["--continuous", "10", "--stream", "a",
                  "--cache-root", str(tmp_path), "--cache-name", "c",
                  "--cache-max-count", "5", "--from-cache", str(tmp_path)])


# ---------------------------------------------------------------------------
# CONTINUOUS: root + name OPTIONAL (auto-detect / job-id default); >=1 cap (2.6)
# ---------------------------------------------------------------------------

def test_continuous_root_and_name_optional(tmp_path):
    # Neither --cache-root nor --cache-name is required; a cap is enough.
    args = validate(["--continuous", "10", "--stream", "a", "--cache-max-count", "5"])
    assert args.cache_root is None      # auto-detected at run time
    assert args.cache_name is None      # defaults to job id at run time


def test_continuous_single_stream_enforced():
    # a1: --continuous rejects more than one --stream.
    with pytest.raises(app.ConfigError, match="exactly one --stream"):
        validate(["--continuous", "10", "--stream", "top", "--stream", "bottom",
                  "--cache-max-count", "5"])


def test_continuous_requires_a_cap(tmp_path):
    with pytest.raises(app.ConfigError, match="at least one of --cache-max"):
        validate(["--continuous", "10", "--stream", "a",
                  "--cache-root", str(tmp_path), "--cache-name", "c"])


def test_continuous_count_cap_ok(tmp_path):
    args = validate(["--continuous", "10", "--stream", "a",
                     "--cache-root", str(tmp_path), "--cache-name", "c",
                     "--cache-max-count", "100"])
    assert args.continuous == 10
    assert args.cache_max_count == 100


def test_continuous_mb_cap_ok(tmp_path):
    args = validate(["--continuous", "10", "--stream", "a",
                     "--cache-root", str(tmp_path), "--cache-name", "c",
                     "--cache-max-mb", "50"])
    assert args.cache_max_mb == 50.0


def test_continuous_both_caps_ok(tmp_path):
    args = validate(["--continuous", "10", "--stream", "a",
                     "--cache-root", str(tmp_path), "--cache-name", "c",
                     "--cache-max-count", "100", "--cache-max-mb", "50"])
    assert args.cache_max_count == 100 and args.cache_max_mb == 50.0


@pytest.mark.parametrize("cap,val", [
    ("--cache-max-count", "0"),
    ("--cache-max-count", "-1"),
    ("--cache-max-mb", "0"),
    ("--cache-max-mb", "-2.5"),
])
def test_continuous_non_positive_caps_rejected(tmp_path, cap, val):
    with pytest.raises(app.ConfigError, match="must be a positive"):
        validate(["--continuous", "10", "--stream", "a",
                  "--cache-root", str(tmp_path), "--cache-name", "c", cap, val])


# ---------------------------------------------------------------------------
# --cache-name filesystem safety, IF given (2.12)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_name", [
    "has/slash", "has\\back", "has space", "", "tab\tname", "dot/../escape",
])
def test_continuous_bad_cache_name_rejected(tmp_path, bad_name):
    with pytest.raises(app.ConfigError, match="not filesystem-safe"):
        validate(["--continuous", "10", "--stream", "a",
                  "--cache-root", str(tmp_path), "--cache-name", bad_name,
                  "--cache-max-count", "5"])


@pytest.mark.parametrize("ok_name", [
    "hummingcam", "top_camera", "cam-1", "cam.2", "H00F_top",
])
def test_continuous_good_cache_name_ok(tmp_path, ok_name):
    args = validate(["--continuous", "10", "--stream", "a",
                     "--cache-root", str(tmp_path), "--cache-name", ok_name,
                     "--cache-max-count", "5"])
    assert args.cache_name == ok_name


# ---------------------------------------------------------------------------
# main() exit codes (fail-fast config error -> distinct nonzero code)
# ---------------------------------------------------------------------------

def test_main_ok_one_shot_requires_camera_config():
    # As of Stage 1, one-shot-from-camera needs a camera host (flag or env). With
    # neither, it fails fast as a config error rather than silently exiting OK.
    import os as _os
    saved = _os.environ.pop("CAMERA_HOST", None)
    try:
        rc = app.main(["--one-shot", "--stream", "top_camera"])
    finally:
        if saved is not None:
            _os.environ["CAMERA_HOST"] = saved
    assert rc == app.EXIT_CONFIG_ERROR


def test_main_continuous_needs_camera_config(tmp_path):
    # Continuous now runs the producer loop, which fail-fasts without a camera
    # host (flag or env), same as one-shot-from-camera.
    import os as _os
    saved = _os.environ.pop("CAMERA_HOST", None)
    try:
        rc = app.main(["--continuous", "10", "--stream", "top_camera",
                       "--cache-root", str(tmp_path), "--cache-name", "c",
                       "--cache-max-count", "5"])
    finally:
        if saved is not None:
            _os.environ["CAMERA_HOST"] = saved
    assert rc == app.EXIT_CONFIG_ERROR


def test_main_config_error_returns_nonzero():
    # one-shot with a cache flag -> ConfigError -> EXIT_CONFIG_ERROR (not 0)
    rc = app.main(["--one-shot", "--stream", "a", "--cache-max-count", "5"])
    assert rc == app.EXIT_CONFIG_ERROR
    assert rc != app.EXIT_OK


def test_main_argparse_error_exits(tmp_path):
    # missing mode -> argparse SystemExit with code 2
    with pytest.raises(SystemExit):
        app.main(["--stream", "a"])


def test_main_one_shot_from_camera_requires_host(tmp_path, monkeypatch):
    # one-shot-from-camera needs a camera host (flag or env); without it -> config
    # error. (Credential/capture/embed logic is tested in the acquire/metadata
    # suites; the CLI upload path itself lands in Stage 3.)
    monkeypatch.delenv("CAMERA_HOST", raising=False)
    rc = app.main(["--one-shot", "--stream", "top_camera"])
    assert rc == app.EXIT_CONFIG_ERROR


def test_main_one_shot_requires_creds(monkeypatch):
    # camera host present but no creds -> config error (creds are env-only).
    monkeypatch.delenv("CAMERA_USER", raising=False)
    monkeypatch.delenv("CAMERA_PASSWORD", raising=False)
    rc = app.main(["--one-shot", "--stream", "top_camera", "--camera-host", "10.0.0.9"])
    assert rc == app.EXIT_CONFIG_ERROR


def test_main_one_shot_placeholder_vsn_still_uploads(tmp_path, monkeypatch):
    # creds present, vsn unresolvable (empty manifest, no --vsn) -> NOT a config
    # error anymore: identity falls back to a placeholder vsn and the upload still
    # runs (Beehive attributes the node via routing). sage-ci runtime calls pending.
    import upload as _upl
    monkeypatch.setenv("CAMERA_USER", "admin")
    monkeypatch.setenv("CAMERA_PASSWORD", "pw")
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(tmp_path / "no-vsn"))
    monkeypatch.setattr(nodemeta, "DEFAULT_NODE_ID_FILE", str(tmp_path / "no-id"))
    monkeypatch.setattr(nodemeta, "PLACEHOLDER_VSN", "NODE")
    seen = {}

    def fake_upload(**kw):
        seen.update(kw)
        return True, {"object_name": "1-v2-NODE-top_camera.jpg", "final_bytes": 10,
                      "unique_id": "abc123", "capture_ts_ns": 1,
                      "grab_ns": 1e6, "embed_ns": 1e6, "upload_ns": 1e6}

    monkeypatch.setattr(_upl, "one_shot_upload", fake_upload)
    empty = tmp_path / "no-manifest.json"
    rc = app.main(["--one-shot", "--stream", "top_camera", "--camera-host", "10.0.0.9",
                   "--node-manifest", str(empty)])
    assert rc == app.EXIT_OK
    assert seen["vsn"] == "NODE"          # placeholder passed through to upload
    assert seen["lat"] is None            # gps omitted, not faked
    assert seen["lon"] is None


def test_main_one_shot_dispatch_ok(monkeypatch):
    # full one-shot dispatch: creds + resolved vsn -> calls upload.one_shot_upload.
    import upload as _upl
    monkeypatch.setenv("CAMERA_USER", "admin")
    monkeypatch.setenv("CAMERA_PASSWORD", "pw")
    calls = {}

    def fake_upload(**kw):
        calls.update(kw)
        return True, {"object_name": "1-v2-H00F-top_camera.jpg", "final_bytes": 100,
                      "unique_id": "abc123def456", "capture_ts_ns": 1,
                      "grab_ns": 1e6, "embed_ns": 2e6, "upload_ns": 3e6}

    monkeypatch.setattr(_upl, "one_shot_upload", fake_upload)
    rc = app.main(["--one-shot", "--stream", "top_camera", "--camera-host", "10.0.0.9",
                   "--vsn", "H00F"])
    assert rc == app.EXIT_OK
    assert calls["vsn"] == "H00F"
    assert calls["camera"] == "top_camera"
    assert "cmd=Snap" in calls["url"]


def test_main_one_shot_upload_failure_returns_capture_code(monkeypatch):
    import upload as _upl
    monkeypatch.setenv("CAMERA_USER", "admin")
    monkeypatch.setenv("CAMERA_PASSWORD", "pw")
    monkeypatch.setattr(_upl, "one_shot_upload",
                        lambda **kw: (False, {"error": "beehive down"}))
    rc = app.main(["--one-shot", "--stream", "top_camera", "--camera-host", "10.0.0.9",
                   "--vsn", "H00F"])
    assert rc == app.EXIT_CAPTURE_ERROR


def test_no_out_flags_exist():
    # Regression guard: --out-path and --out-dir were removed (not in the design).
    parser = app.build_parser()
    opts = {a for action in parser._actions for a in action.option_strings}
    assert "--out-path" not in opts
    assert "--out-dir" not in opts


# ---------------------------------------------------------------------------
# summarize() smoke (no crash, includes mode)
# ---------------------------------------------------------------------------

def test_summarize_one_shot():
    args = validate(["--one-shot", "--stream", "top_camera"])
    s = app.summarize(args)
    assert "one-shot" in s and "top_camera" in s


def test_summarize_continuous(tmp_path):
    args = validate(["--continuous", "30", "--stream", "top_camera",
                     "--cache-root", str(tmp_path), "--cache-name", "c",
                     "--cache-max-count", "5"])
    s = app.summarize(args)
    assert "continuous" in s and "interval=30s" in s
