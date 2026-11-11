#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- acquisition module (Stage 1).
#
# Stage 1 scope (design 2.3 primary path): fetch a camera's NATIVE still endpoint
# and save the RAW JPEG BYTES UNTOUCHED (no decode, no re-encode), via an atomic
# temp -> fsync -> os.replace so no torn file is ever visible. Bounded by a hard
# timeout; on timeout/error the caller decides (fail-fast in one-shot, fail-soft
# in continuous -- later stages).
#
# NOT in Stage 1: capture-ts v2 naming, EXIF embed (Stage 2), upload (Stage 3),
# the ring cache (Stage 4), the OpenCV/RTSP fallback (stubbed, raises).
#
# Credentials are NEVER hardcoded. The Reolink native-still URL is built from
# parameters the caller supplies (host, port, user, password) -- typically read
# from environment variables on the node. See build_reolink_snap_url().
#
# Vendors (build_still_source): "reolink" -> a URL string (query-param auth, cmd=Snap);
# "hanwha" -> a HanwhaStill object (SUNAPI snapshot, HTTP Digest auth; the URL itself
# carries no credentials). fetch_raw_still() accepts either.

import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("media-sampler3.acquire")

# JPEG magic: starts with SOI (FF D8), ends with EOI (FF D9).
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class CaptureError(Exception):
    """Raised when a capture fails (unreachable, timeout, non-JPEG, HTTP error)."""


class CaptureTimeout(CaptureError):
    """Raised specifically when the capture exceeds the bounded timeout."""


class CaptureAuthError(CaptureError):
    """Raised when the camera rejects the credentials (or we are backing off after
    a rejection). A CaptureError subclass, so fail-soft callers need no change."""


VENDORS = ("reolink", "hanwha")

# Hanwha cameras lock an account after repeated failed logins. After an auth
# failure a HanwhaStill refuses to contact the camera until its backoff expires:
# 60 s, doubling per consecutive failure, capped at 15 min. Success resets it.
HANWHA_AUTH_BACKOFF_START_S = 60.0
HANWHA_AUTH_BACKOFF_MAX_S = 900.0


class _OneShotDigestAuthHandler(urllib.request.HTTPDigestAuthHandler):
    """Digest handler that sends the credentials ONCE per request. The stdlib
    handler silently retries a rejected login up to 5 times per request, which on
    its own can trip a Hanwha account lockout. If the request already carried an
    Authorization header, return None so the 401 surfaces as an HTTPError."""

    def http_error_401(self, req, fp, code, msg, headers):
        if req.get_header("Authorization"):
            return None
        return super().http_error_401(req, fp, code, msg, headers)


class HanwhaStill:
    """Hanwha (Wisenet, SUNAPI) native-still source: GET
    /stw-cgi/video.cgi?msubmenu=snapshot&action=view[&Profile=N]&Channel=C with HTTP
    Digest auth (verified on an XNP-6400RW: Digest, MD5, qop=auth).

    `url` holds NO credentials; user/password stay in this object and are handed to
    urllib's digest handler only. repr()/str() are redacted.
    """

    def __init__(self, host, port, user, password, channel=0, profile=None):
        if not host:
            raise ValueError("camera host is required")
        if not user or password is None:
            raise ValueError("camera user and password are required")
        q = "msubmenu=snapshot&action=view"
        if profile is not None:
            q += f"&Profile={int(profile)}"
        q += f"&Channel={int(channel)}"
        self.url = f"http://{host}:{int(port)}/stw-cgi/video.cgi?{q}"
        self._user = str(user)
        self._password = str(password)
        self._backoff_s = 0.0
        self._blocked_until = 0.0
        self._now = time.monotonic

    def redacted(self):
        return f"{self.url} (hanwha digest, user={self._user})"

    def __repr__(self):
        return f"HanwhaStill({self.redacted()})"

    __str__ = __repr__

    def _note_auth_failure(self):
        self._backoff_s = (HANWHA_AUTH_BACKOFF_START_S if self._backoff_s == 0
                           else min(self._backoff_s * 2, HANWHA_AUTH_BACKOFF_MAX_S))
        self._blocked_until = self._now() + self._backoff_s

    def fetch_bytes(self, timeout_s):
        """Fetch the raw snapshot body. Raises CaptureAuthError on rejection (and
        while backing off), CaptureTimeout / CaptureError otherwise."""
        now = self._now()
        if now < self._blocked_until:
            raise CaptureAuthError(
                "camera rejected credentials earlier; backing off %.0fs more (Hanwha "
                "locks accounts after repeated failed logins)" % (self._blocked_until - now))
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.url, self._user, self._password)
        opener = urllib.request.build_opener(_OneShotDigestAuthHandler(pm))
        req = urllib.request.Request(self.url, headers={"User-Agent": "media-sampler3"})
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                status = getattr(resp, "status", resp.getcode())
                if status != 200:
                    raise CaptureError(f"HTTP {status} from camera")
                data = resp.read()
        except CaptureError:
            raise
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._note_auth_failure()
                raise CaptureAuthError(
                    "camera rejected credentials (HTTP 401); next attempt in %.0fs"
                    % self._backoff_s) from None
            raise CaptureError(f"HTTP {e.code} from camera") from None
        except TimeoutError as e:
            raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
        except OSError as e:
            reason = getattr(e, "reason", e)
            if "timed out" in str(reason).lower():
                raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
            raise CaptureError(f"capture failed: {reason}") from e
        self._backoff_s = 0.0
        self._blocked_until = 0.0
        return data


def build_still_source(vendor, host, port, user, password, channel=0, profile=None):
    """Return the native-still source for a camera vendor: a URL string for
    "reolink" (unchanged behavior), a HanwhaStill for "hanwha". Raises ValueError
    on missing host/credentials or an unknown vendor."""
    if vendor == "reolink":
        return build_reolink_snap_url(host, port, user, password, channel)
    if vendor == "hanwha":
        return HanwhaStill(host, port, user, password, channel=channel, profile=profile)
    raise ValueError(f"unknown camera vendor {vendor!r} (expected one of {VENDORS})")


def build_reolink_snap_url(host, port, user, password, channel=0):
    """Build a Reolink native-still (cmd=Snap) URL with query-param auth.

    Reolink compares the password literally as it arrives, so we must NOT
    percent-encode password-legal punctuation (see sage-waggle reolink ref). We
    only quote characters that would actually break the URL structure. `rs` is a
    random cache-buster the camera expects.

    Returns the full URL. The password appears in the URL by the camera's API
    design; callers should source it from an env var and avoid logging the URL.
    """
    if not host:
        raise ValueError("camera host is required")
    if not user or password is None:
        raise ValueError("camera user and password are required")
    # Quote only URL-breaking chars; leave Reolink-legal punctuation intact.
    safe = "!$'()*,;=:@/~-._"  # not quoted: these are safe in a query value
    u = urllib.parse.quote(str(user), safe=safe)
    p = urllib.parse.quote(str(password), safe=safe)
    rs = str(int(time.time() * 1000))  # cache-buster
    return (f"http://{host}:{port}/cgi-bin/api.cgi?cmd=Snap&channel={channel}"
            f"&rs={rs}&user={u}&password={p}")


def _redact(url):
    """Strip the password value from a URL for safe logging."""
    try:
        parts = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        # Rebuild the query manually so the '***' placeholder is not re-encoded.
        rebuilt = "&".join(
            f"{k}=***" if k.lower() == "password" else f"{k}={v}"
            for k, v in pairs)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, rebuilt, parts.fragment))
    except Exception:
        return "<url>"


def looks_like_jpeg(data):
    """True if bytes look like a complete JPEG (SOI prefix, EOI suffix)."""
    return (isinstance(data, (bytes, bytearray))
            and len(data) > 4
            and data[:2] == JPEG_SOI
            and data[-2:] == JPEG_EOI)


def fetch_raw_still(url, timeout_s):
    """Fetch raw bytes from a native-still HTTP endpoint, untouched.

    Bounded by timeout_s (hard socket timeout). Returns the raw response body
    (bytes). Raises CaptureTimeout on timeout, CaptureError on HTTP/other error
    or if the body is not a JPEG. Does NOT decode or re-encode.
    """
    if timeout_s is None or timeout_s <= 0:
        raise ValueError("timeout_s must be a positive number")
    if isinstance(url, HanwhaStill):
        logger.info("fetching still: %s (timeout %.1fs)", url.redacted(), timeout_s)
        return _validate_jpeg(url.fetch_bytes(timeout_s))
    logger.info("fetching still: %s (timeout %.1fs)", _redact(url), timeout_s)
    req = urllib.request.Request(url, headers={"User-Agent": "media-sampler3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", resp.getcode())
            if status != 200:
                raise CaptureError(f"HTTP {status} from camera")
            data = resp.read()
    except CaptureError:
        raise
    except TimeoutError as e:
        raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
    except OSError as e:
        # socket.timeout is an OSError subclass on modern Python; catch both.
        if "timed out" in str(e).lower():
            raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
        raise CaptureError(f"capture failed: {e}") from e
    except Exception as e:  # urllib.error.URLError etc.
        reason = getattr(e, "reason", e)
        if "timed out" in str(reason).lower():
            raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
        raise CaptureError(f"capture failed: {reason}") from e

    return _validate_jpeg(data)


def _validate_jpeg(data):
    if not data:
        raise CaptureError("camera returned an empty body")
    if not looks_like_jpeg(data):
        # Reolink returns a small JSON error blob (not a JPEG) on auth failure.
        head = bytes(data[:80])
        raise CaptureError(
            f"camera did not return a JPEG (got {len(data)} bytes starting {head!r})")
    logger.info("captured %d bytes (valid JPEG)", len(data))
    return data


def save_bytes_atomic(data, final_path):
    """Write raw bytes to final_path atomically: temp -> fsync -> os.replace.

    The final path never exists in a torn/partial state; a reader either sees the
    complete file or nothing. Returns final_path. Groundwork for the ring cache
    (2.6) which relies on this exact atomic-write property.
    """
    if not looks_like_jpeg(data):
        raise CaptureError("refusing to save: bytes are not a complete JPEG")
    final_dir = os.path.dirname(os.path.abspath(final_path))
    os.makedirs(final_dir, exist_ok=True)
    tmp_path = final_path + ".tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)  # durable before rename
        finally:
            os.close(fd)
        os.replace(tmp_path, final_path)  # atomic on POSIX (same filesystem)
    except Exception:
        # Clean up a stray temp on failure; never leave .tmp litter.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return final_path


def capture_still_to_path(url, final_path, timeout_s):
    """Stage 1 end-to-end: fetch raw still -> save atomically. Returns path.

    Raises CaptureTimeout / CaptureError on failure (caller decides fail-fast vs
    fail-soft). No naming/EXIF/upload here -- those are Stage 2/3.
    """
    data = fetch_raw_still(url, timeout_s)
    return save_bytes_atomic(data, final_path)


def capture_opencv_fallback(*args, **kwargs):
    """OpenCV/RTSP fallback (design 2.3 fallback path) -- NOT in Stage 1."""
    raise NotImplementedError(
        "OpenCV/RTSP fallback is not implemented yet (Stage 1 = native-still only)")
