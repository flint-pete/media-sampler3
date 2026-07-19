#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- parse_v2_name multi-extension tests (audio Stage 3).
#
# parse_v2_name must recognize audio clips (.flac/.wav) as ring members and REJECT
# sidecars (.json) so the ring counts by CLIP, never by sidecar (design audio §4).

import metadata


def test_parses_jpg_unchanged():
    assert metadata.parse_v2_name("1783-v2-H00F-top.jpg") == (1783, "H00F", "top")


def test_parses_flac():
    assert metadata.parse_v2_name("1783-v2-H00F-usb_mic_0.flac") == (
        1783, "H00F", "usb_mic_0")


def test_parses_wav():
    assert metadata.parse_v2_name("1783-v2-H00F-mic.wav") == (1783, "H00F", "mic")


def test_rejects_json_sidecar():
    # A sidecar rides with its clip; it is NOT an independent ring member.
    assert metadata.parse_v2_name("1783-v2-H00F-usb_mic_0.flac.json") is None


def test_rejects_unknown_extension():
    assert metadata.parse_v2_name("1783-v2-H00F-mic.mp3") is None


def test_rejects_tmp():
    assert metadata.parse_v2_name("1783-v2-H00F-mic.flac.tmp") is None


def test_media_exts_constant_exists():
    assert ".jpg" in metadata.MEDIA_EXTS
    assert ".flac" in metadata.MEDIA_EXTS
    assert ".wav" in metadata.MEDIA_EXTS
