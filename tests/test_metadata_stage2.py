# Stage 2 tests for image-sampler2 metadata (metadata.py).
#
# PURE tests: no camera/network. Verify v2 filename format, capture-ts handling,
# EXIF/UserComment round-trip, ImageUniqueID = SHA256(original frame), GPS abs+ref
# for negative coords, and that pixels are NOT re-encoded. Section refs -> design.
#
# Run:  python3 -m pytest tests/ -q

import io
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import metadata  # noqa: E402

# piexif + PIL are Stage-2 deps.
piexif = pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")


FOREIGN_COM = b"#:M1IMG:PRD=MOBOTIX DAT=2026-07-03 TIM=21:00:08.323"

COMMON = dict(
    vsn="H00F", node_id="00004cbb4701d16c", job="hummingbird",
    task="image-sampler2", plugin="registry.example.org/pete/image-sampler2:0.1.0",
    camera="top", capture_ts_ns=1783112408323875000, upload_ts_ns=None,
    lat=41.7180, lon=-87.9827, acquisition_path="native-raw")


def make_jpeg(width=64, height=48, with_com=False):
    im = Image.new("RGB", (width, height), (123, 200, 50))
    buf = io.BytesIO()
    im.save(buf, "jpeg", quality=90)
    data = buf.getvalue()
    if with_com:
        com = b"\xff\xfe" + struct.pack(">H", len(FOREIGN_COM) + 2) + FOREIGN_COM
        data = data[:2] + com + data[2:]
    return data


def pixel_scan(data):
    """Compressed data from SOS to EOI (proves pixels unchanged)."""
    return data[data.index(b"\xff\xda"):]


# ---------------------------------------------------------------------------
# v2 filename (2.10)
# ---------------------------------------------------------------------------

def test_v2_name_format():
    n = metadata.build_v2_name(1783112408323875000, "H00F", "top")
    assert n == "1783112408323875000-v2-H00F-top.jpg"


def test_v2_name_rejects_bad_ts():
    with pytest.raises(ValueError):
        metadata.build_v2_name(0, "H00F", "top")
    with pytest.raises(ValueError):
        metadata.build_v2_name(-5, "H00F", "top")


def test_v2_name_rejects_separators():
    with pytest.raises(ValueError):
        metadata.build_v2_name(123, "H00F", "top/cam")
    with pytest.raises(ValueError):
        metadata.build_v2_name(123, "H0 0F", "top")


def test_object_name_matches_v2_name():
    assert (metadata.object_name_for(123456, "H00F", "top")
            == metadata.build_v2_name(123456, "H00F", "top"))


# ---------------------------------------------------------------------------
# capture-ts helpers (2.9)
# ---------------------------------------------------------------------------

def test_capture_ts_is_ns_int():
    ts = metadata.now_capture_ts_ns()
    assert isinstance(ts, int)
    assert ts > 1_500_000_000_000_000_000  # well after 2017 in ns


def test_ns_to_exif_datetime():
    date_str, subsec = metadata._ns_to_exif_datetime(1783112408323875000)
    # format is "YYYY:MM:DD HH:MM:SS", subsec is 6-digit microseconds
    assert len(date_str) == 19 and date_str[4] == ":" and date_str[10] == " "
    assert subsec.isdigit() and len(subsec) == 6


# ---------------------------------------------------------------------------
# GPS abs + ref for negative coords (4.4 spike finding)
# ---------------------------------------------------------------------------

def test_gps_negative_lon_stored_abs():
    dms = metadata._deg_to_dms_rationals(-87.9827)
    assert dms[0] == (87, 1)  # degrees stored positive
    assert all(isinstance(x, tuple) and len(x) == 2 for x in dms)


# ---------------------------------------------------------------------------
# embed_all: round-trip, unique_id, no re-encode, foreign-segment preservation
# ---------------------------------------------------------------------------

def test_embed_roundtrip_all_fields():
    jpeg = make_jpeg()
    final, uid = metadata.embed_all(jpeg, **COMMON)
    payload, uid_tag = metadata.read_back_fields(final)
    # all 13 fields present
    for key in ("schema_version", "vsn", "node_id", "job", "task", "plugin",
                "camera", "capture_timestamp_ns", "upload_timestamp_ns",
                "unique_id", "object_name", "lat", "lon", "acquisition_path"):
        assert key in payload, f"missing {key}"
    assert payload["vsn"] == "H00F"
    assert payload["camera"] == "top"
    assert payload["capture_timestamp_ns"] == 1783112408323875000
    assert payload["object_name"] == "1783112408323875000-v2-H00F-top.jpg"
    assert abs(payload["lon"] - (-87.9827)) < 1e-9


def test_embed_unique_id_is_sha_of_original():
    jpeg = make_jpeg()
    expected = metadata.sha256_hex(jpeg)  # SHA of the ORIGINAL frame
    final, uid = metadata.embed_all(jpeg, **COMMON)
    payload, uid_tag = metadata.read_back_fields(final)
    assert uid == expected
    assert uid_tag == expected          # ImageUniqueID tag agrees
    assert payload["unique_id"] == expected  # JSON blob agrees


def test_embed_does_not_reencode_pixels():
    jpeg = make_jpeg()
    orig_pixels = pixel_scan(jpeg)
    final, uid = metadata.embed_all(jpeg, **COMMON)
    assert pixel_scan(final) == orig_pixels  # byte-identical compressed data


def test_embed_preserves_foreign_com_segment():
    jpeg = make_jpeg(with_com=True)
    assert FOREIGN_COM in jpeg
    final, uid = metadata.embed_all(jpeg, **COMMON)
    assert FOREIGN_COM in final  # foreign camera segment survives


def test_embed_result_is_valid_jpeg():
    jpeg = make_jpeg()
    final, uid = metadata.embed_all(jpeg, **COMMON)
    Image.open(io.BytesIO(final)).verify()  # raises if not a valid JPEG


def test_embed_native_raw_uses_preserved_make():
    jpeg = make_jpeg()
    final, uid = metadata.embed_all(jpeg, preserved_make="MOBOTIX", **COMMON)
    exif = piexif.load(final)
    make = exif["0th"][piexif.ImageIFD.Make]
    make = make.decode() if isinstance(make, bytes) else make
    assert make == "MOBOTIX"


def test_embed_reencoded_uses_sage_make():
    jpeg = make_jpeg()
    common = dict(COMMON)
    common["acquisition_path"] = "opencv-reencoded"
    final, uid = metadata.embed_all(jpeg, preserved_make="MOBOTIX", **common)
    exif = piexif.load(final)
    make = exif["0th"][piexif.ImageIFD.Make]
    make = make.decode() if isinstance(make, bytes) else make
    assert make == "Sage/Waggle"  # re-encoded path ignores preserved make


def test_embed_without_gps_when_coords_none():
    jpeg = make_jpeg()
    common = dict(COMMON)
    common["lat"] = None
    common["lon"] = None
    final, uid = metadata.embed_all(jpeg, **common)
    exif = piexif.load(final)
    assert exif["GPS"] == {}  # no GPS IFD written
    payload, _ = metadata.read_back_fields(final)
    assert payload["lat"] is None and payload["lon"] is None


def test_embed_datetimeoriginal_present():
    jpeg = make_jpeg()
    final, uid = metadata.embed_all(jpeg, **COMMON)
    exif = piexif.load(final)
    dto = exif["Exif"][piexif.ExifIFD.DateTimeOriginal]
    dto = dto.decode() if isinstance(dto, bytes) else dto
    assert dto.startswith("2026:07:")  # capture date from ns
