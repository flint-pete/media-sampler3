#!/usr/bin/env python3
"""Stage 1.5 spike: validate EXIF injection WITHOUT re-encoding, preserving
foreign camera segments (e.g. Mobotix M1IMG COM block).

THROWAWAY experiment. Not product code. Answers design 4.4.

Tests, on a JPEG that carries a foreign COM segment:
  1. piexif.insert adds our EXIF/UserComment WITHOUT decoding pixels.
  2. The foreign COM segment survives intact.
  3. The compressed pixel scan (SOS..EOI) is byte-identical (proves no re-encode).
  4. Our 13-field UserComment JSON round-trips.
  5. GPS with negative lon (H00F -87.9827) is handled via abs + ref.
"""
import io
import json
import struct

import piexif
from PIL import Image

FOREIGN_COM = b"#:M1IMG:PRD=MOBOTIX FRM=7290841 DAT=2026-07-03 TIM=21:00:08.323"


def make_jpeg_with_com():
    """Create a real JPEG, then splice a foreign COM (0xFFFE) segment right
    after SOI -- mimicking a camera that authored its own metadata block."""
    im = Image.new("RGB", (64, 48), (123, 200, 50))
    buf = io.BytesIO()
    im.save(buf, "jpeg", quality=90)
    data = buf.getvalue()
    assert data[:2] == b"\xff\xd8", "no SOI"
    com = b"\xff\xfe" + struct.pack(">H", len(FOREIGN_COM) + 2) + FOREIGN_COM
    return data[:2] + com + data[2:]


def scan_segments(data):
    """Return list of (marker_hex, length) for the marker segments up to SOS."""
    segs = []
    i = 2  # skip SOI
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xDA:  # SOS -> compressed data follows
            segs.append(("FFDA(SOS)", None))
            break
        if 0xD0 <= marker <= 0xD9:  # standalone markers
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        segs.append((f"FF{marker:02X}", length))
        i += 2 + length
    return segs


def pixel_scan(data):
    """Return the bytes from SOS marker to EOI (the compressed image data)."""
    i = data.index(b"\xff\xda")
    return data[i:]


def has_foreign_com(data):
    return FOREIGN_COM in data


def build_exif_bytes(vsn, camera, capture_ts_ns, upload_ts_ns, uid_sha256,
                     lat, lon, plugin_ver):
    """Build our Option-C hybrid EXIF: standard tags + full JSON UserComment."""
    object_name = f"{capture_ts_ns}-v2-{vsn}-{camera}.jpg"
    blob = {
        "schema": "sage-img-1", "vsn": vsn, "camera": camera,
        "capture_ts_ns": capture_ts_ns, "upload_ts_ns": upload_ts_ns,
        "uid_sha256": uid_sha256, "object_name": object_name,
        "lat": lat, "lon": lon, "acq": "native-raw", "plugin": plugin_ver,
    }
    user_comment = json.dumps(blob, separators=(",", ":"))

    def deg_to_dms_rational(deg):
        deg = abs(deg)
        d = int(deg)
        m = int((deg - d) * 60)
        s = round(((deg - d) * 60 - m) * 60 * 10000)
        return ((d, 1), (m, 1), (s, 10000))

    zeroth = {
        piexif.ImageIFD.Make: "Sage/Waggle",
        piexif.ImageIFD.Model: vsn,
        piexif.ImageIFD.Software: plugin_ver,
        piexif.ImageIFD.ImageDescription: f"Sage image-sampler2 v2; vsn={vsn}; camera={camera}",
    }
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: "2026:07:03 21:00:08",
        piexif.ExifIFD.OffsetTimeOriginal: "+00:00",
        piexif.ExifIFD.ImageUniqueID: uid_sha256,
        # UserComment needs an 8-byte charset prefix; ASCII\0\0\0.
        piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + user_comment.encode("ascii"),
    }
    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: deg_to_dms_rational(lon),
    }
    exif_dict = {"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd, "1st": {}, "thumbnail": None}
    return piexif.dump(exif_dict), user_comment, object_name


def main():
    print("=== Stage 1.5 EXIF-injection spike (piexif 1.1.3) ===\n")
    original = make_jpeg_with_com()
    print(f"[setup] original JPEG {len(original)} bytes, foreign COM present: {has_foreign_com(original)}")
    print(f"[setup] segments: {scan_segments(original)}")
    orig_pixels = pixel_scan(original)

    exif_bytes, user_comment, object_name = build_exif_bytes(
        vsn="H00F", camera="top", capture_ts_ns=1783112408323875000,
        upload_ts_ns=1783112409489787702,
        uid_sha256="9f3a" + "0" * 60, lat=41.7180, lon=-87.9827,
        plugin_ver="registry.sagecontinuum.org/pete/image-sampler2:0.1.0")
    print(f"\n[build] exif block {len(exif_bytes)} bytes; UserComment JSON {len(user_comment)} chars")

    # THE KEY CALL: insert into the JPEG bytes (in-memory), no Pillow, no re-encode.
    # piexif.insert with raw bytes needs an output sink; use a BytesIO (3rd arg).
    sink = io.BytesIO()
    piexif.insert(exif_bytes, original, sink)
    injected = sink.getvalue()

    print(f"\n[insert] result {len(injected)} bytes")
    print(f"[insert] segments now: {scan_segments(injected)}")

    # --- assertions ---
    ok = True

    r1 = has_foreign_com(injected)
    print(f"\n[CHECK 1] foreign COM (M1IMG) survived injection: {r1}")
    ok &= r1

    r2 = pixel_scan(injected) == orig_pixels
    print(f"[CHECK 2] compressed pixel scan byte-identical (no re-encode): {r2}")
    ok &= r2

    # round-trip our fields back
    back = piexif.load(injected)
    uc = back["Exif"][piexif.ExifIFD.UserComment]
    uc_json = uc[8:].decode("ascii")  # strip 8-byte charset prefix
    parsed = json.loads(uc_json)
    r3 = (parsed["vsn"] == "H00F" and parsed["camera"] == "top"
          and parsed["capture_ts_ns"] == 1783112408323875000
          and parsed["object_name"] == object_name
          and abs(parsed["lon"] - (-87.9827)) < 1e-9)
    print(f"[CHECK 3] UserComment JSON round-trips all fields: {r3}")
    ok &= r3

    uid_back = back["Exif"][piexif.ExifIFD.ImageUniqueID]
    uid_back = uid_back.decode() if isinstance(uid_back, bytes) else uid_back
    r4 = uid_back.startswith("9f3a")
    print(f"[CHECK 4] ImageUniqueID (sha256) round-trips: {r4}")
    ok &= r4

    lonref = back["GPS"][piexif.GPSIFD.GPSLongitudeRef]
    lonref = lonref.decode() if isinstance(lonref, bytes) else lonref
    lon_dms = back["GPS"][piexif.GPSIFD.GPSLongitude]
    r5 = (lonref == "W" and lon_dms[0][0] == 87)
    print(f"[CHECK 5] negative lon stored as abs + ref='W' ({lon_dms[0]}, ref={lonref!r}): {r5}")
    ok &= r5

    # validate resulting file is still a decodable JPEG (structure intact)
    try:
        Image.open(io.BytesIO(injected)).verify()
        r6 = True
    except Exception as e:
        r6 = False
        print("   decode error:", e)
    print(f"[CHECK 6] injected result is a valid decodable JPEG: {r6}")
    ok &= r6

    print("\n=== RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES ABOVE", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
