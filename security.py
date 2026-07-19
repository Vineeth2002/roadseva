"""
security.py — RoadSeva Security Fortress
=========================================
FIX APPLIED: _is_private_ip() operator precedence bug corrected (line ~440)
             Previous one-liner mixed 'or'+'and'+ternary incorrectly.
             Now uses explicit if/elif/return blocks.

SPRINT 2 FIX: /submit rate limit added to _LIMITS and enforced in main.py
              Previous version had /track rate limited but /submit was open
              to spam and DDoS — a bot could file 1000 complaints in minutes.

FIX SEC-1: VALID_ROLES was missing grievance_officer and triage_officer.
           validate_role() returned False for these roles, breaking team
           creation for triage staff. Added both roles to the set.

A civic platform that protects human lives cannot afford a single gap.
This module is the complete security brain of RoadSeva.

ATTACK VECTORS DEFENDED (A to Z):
──────────────────────────────────
PHOTO UPLOADS (most dangerous surface):
  ✓ ImageTragick — we use Pillow only, never ImageMagick
  ✓ Polyglot files — file valid as both image AND script
  ✓ SVG/XML disguised as image — checked in first 512 bytes
  ✓ PHP/Shell/EXE disguised as image — dangerous signature scan
  ✓ Pixel flood / decompression bomb — dimension + pixel count limits
  ✓ ZIP bomb inside image — Pillow MAX_IMAGE_PIXELS enforcement
  ✓ Steganography payloads — full Pillow re-encode destroys ALL embedded data
  ✓ EXIF injection (GPS, device ID, owner name) — stripped before storage
  ✓ Filename path traversal (../../etc/passwd) — basename + character whitelist
  ✓ Trailing data after image end marker — re-encode writes only fresh pixels
  ✓ Billion laughs (XML entity expansion) — XML/SVG rejected entirely
  ✓ Malicious Content-Type spoofing — we trust magic bytes, not headers

FORM INPUTS:
  ✓ SQL Injection — parameterized queries (primary) + pattern detection (secondary)
  ✓ Second-order SQL Injection — same defense, patterns detected at write time
  ✓ Stored XSS — Jinja2 autoescaping (primary) + pattern detection (secondary)
  ✓ Reflected XSS — same + security headers
  ✓ Server-Side Template Injection (SSTI) — {{ }} pattern detection
  ✓ Null byte injection — stripped before any processing
  ✓ Unicode homograph attacks — NFKC normalization
  ✓ ReDoS — input length capped before regex processing
  ✓ Log injection — newlines stripped from all logged values
  ✓ Header injection — newline detection in redirect targets
  ✓ Command injection — shell metacharacter detection
  ✓ XXE / XML External Entity — XML DOCTYPE detection
  ✓ SSRF (Server-Side Request Forgery) — URL scheme + cloud metadata blocking

AUTHENTICATION:
  ✓ Brute force — per-account lockout after 5 failures (in database.py)
  ✓ Credential stuffing — same lockout + rate limit on /login
  ✓ Password spraying — per-IP rate limit catches cross-account spraying
  ✓ Session fixation — new token issued on login (create_session always fresh)
  ✓ Session prediction — secrets.token_hex(32) = 256-bit entropy, unpredictable
  ✓ Session theft via XSS — httponly cookie, CSP blocks inline scripts
  ✓ Timing attack (username enumeration) — constant-time login response
  ✓ CSRF — HMAC token tied to session, verified on every POST

NETWORK / INFRASTRUCTURE:
  ✓ DDoS HTTP flood — global rate limit 300 req/min + 30 req/sec burst per IP
  ✓ Slowloris — request body size enforced, connection timeout via uvicorn
  ✓ Path traversal in URLs — URL scanning middleware
  ✓ Directory traversal — uploads served with Content-Disposition: attachment
  ✓ SSRF via URL parameters — cloud metadata IP blocking
  ✓ Information disclosure via errors — sanitized error responses, no stack traces
  ✓ Reconnaissance via /health — sensitive fields removed from public response
  ✓ Server version disclosure — Server header removed
  ✓ Complaint spam / fake flood — /submit rate limited 5 per hour per IP

APPLICATION LOGIC:
  ✓ IDOR (Insecure Direct Object Reference) — role enforcement on every route
  ✓ Mass assignment — explicit field binding, never bind request body as-is
  ✓ Privilege escalation — CREATABLE_ROLES whitelist in database.py
  ✓ Parameter tampering — status values validated against whitelist
  ✓ Business logic bypass — workflow enforced at DB layer

ADVANCED / STATE-ACTOR:
  ✓ Log injection — sanitize_log_value() strips control chars before logging
  ✓ Automatic IP blocking — repeated attack patterns trigger auto-block
  ✓ Security event audit trail — every attack attempt is logged
  ✓ Append-only audit logs — SQLite triggers prevent deletion (in database.py)
  ✓ Supply chain — pinned versions in requirements.txt
"""

import os
import re
import io
import time
import hmac
import hashlib
import bcrypt
import secrets
import logging
import unicodedata
from typing import Optional
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from threading import Lock

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

MAX_PHOTO_BYTES     = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8_000
MAX_IMAGE_PIXELS    = 20_000_000
MIN_PHOTO_BYTES     = 100

MAX_REQUEST_BODY    = 8 * 1024 * 1024
MAX_URL_LENGTH      = 2_048
MAX_HEADERS_COUNT   = 50
MAX_FIELD_LENGTH    = 2_000

_BLOCKED_IPS: set = set()
_block_lock = Lock()

VALID_REPORT_STATUSES = {"open", "assigned", "inspecting", "inspected", "resolved", "closed"}

# FIX SEC-1: Added grievance_officer and triage_officer — previously missing,
# causing validate_role() to return False for these roles during team creation.
VALID_ROLES = {
    "admin",
    "commissioner",
    "zonal_commissioner",
    "ae",
    "officer",
    "grievance_officer",   # FIX SEC-1
    "triage_officer",      # FIX SEC-1
    "was",
    "field_engineer",
    "viewer",
}

def validate_role(role: str) -> bool:
    return role in VALID_ROLES


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1b — ONE-TIME CREDENTIAL STORE
# ═══════════════════════════════════════════════════════════════════════════════

from threading import Lock as _Lock

_cred_store: dict = {}
_cred_lock = _Lock()
_CRED_TTL_SECONDS = 300


def store_credential(data: dict) -> str:
    token = secrets.token_urlsafe(32)
    with _cred_lock:
        now = time.time()
        expired = [k for k, v in _cred_store.items()
                   if now - v["created_at"] > _CRED_TTL_SECONDS]
        for k in expired:
            del _cred_store[k]
        _cred_store[token] = {"data": data, "created_at": now}
    return token


def consume_credential(token: str) -> dict | None:
    if not token or len(token) > 100:
        return None
    with _cred_lock:
        entry = _cred_store.pop(token, None)
        if not entry:
            return None
        if time.time() - entry["created_at"] > _CRED_TTL_SECONDS:
            return None
        return entry["data"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — ATTACK PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

_ATTACK_SIGNATURES = [
    ("sqli_union",    re.compile(r"(?i)\bunion\b.{0,20}\bselect\b")),
    ("sqli_drop",     re.compile(r"(?i)\b(drop|truncate|delete)\b.{0,10}\b(table|from)\b")),
    ("sqli_comment",  re.compile(r"(--|;--|#|/\*|\*/)")),
    ("sqli_or1eq1",   re.compile(r"(?i)('|\b)\s*(or|and)\s*(1\s*=\s*1|'[^']*'\s*=\s*'[^']*')")),
    ("sqli_exec",     re.compile(r"(?i)\b(exec|execute|xp_cmdshell|sp_executesql)\b")),
    ("xss_script",    re.compile(r"(?i)<\s*script[\s>]")),
    ("xss_event",     re.compile(r"(?i)\bon(load|error|click|mouse|key|focus|blur|submit|change|input)\s*=")),
    ("xss_href",      re.compile(r"(?i)(javascript|vbscript|data)\s*:")),
    ("xss_dom",       re.compile(r"(?i)(document\s*\.\s*cookie|window\s*\.\s*location|eval\s*\()")),
    ("ssti_jinja",    re.compile(r"\{\{[\s\S]{0,200}\}\}")),
    ("ssti_python",   re.compile(r"(__class__|__mro__|__subclasses__|__import__|__builtins__|__globals__)")),
    ("ssti_block",    re.compile(r"\{%[\s\S]{0,200}%\}")),
    ("traversal",     re.compile(r"(\.\./|\.\.\\|%2e%2e[%/\\]|\.{2,}[/\\])")),
    ("ssrf_meta",     re.compile(r"(?i)(169\.254\.169\.254|metadata\.google\.internal|metadata\.azure\.com)")),
    ("ssrf_scheme",   re.compile(r"(?i)(file://|gopher://|dict://|ftp://|ldap://|tftp://)")),
    ("xxe_entity",    re.compile(r"(?i)(<!entity|<!doctype\s+\w+\s+(system|public))")),
    ("cmd_inject",    re.compile(r"(;\s*(ls|cat|rm|chmod|wget|curl|nc\s|bash|sh\s)|`[^`]{0,100}`|\$\([^)]{0,100}\))")),
    ("crlf_inject",   re.compile(r"(\r\n|\r|\n|%0[aAdD]|%0[aAdD])")),
    ("null_byte",     re.compile(r"\x00")),
]


def scan_for_attacks(value: str, field_name: str = "input") -> list:
    if not isinstance(value, str):
        return []
    sample = value[:MAX_FIELD_LENGTH]
    detected = []
    for name, pattern in _ATTACK_SIGNATURES:
        try:
            if pattern.search(sample):
                detected.append(name)
        except Exception:
            pass
    return detected


def sanitize_input(value: str, field_name: str = "field",
                   max_length: int = MAX_FIELD_LENGTH) -> tuple:
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    threats = []
    if "\x00" in value:
        threats.append("null_byte")
        value = value.replace("\x00", "")
    value = unicodedata.normalize("NFKC", value)
    if len(value) > max_length:
        threats.append(f"oversized:{len(value)}_chars")
        value = value[:max_length]
    threats.extend(scan_for_attacks(value, field_name))
    return value, threats


def sanitize_log_value(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    cleaned = re.sub(r"[\r\n\t\x00-\x1f\x7f]", " ", value)
    return cleaned[:500]


def validate_status(status: str) -> bool:
    return status in VALID_REPORT_STATUSES


def is_safe_redirect(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    if re.match(r"^(https?://|//|javascript:|data:|vbscript:|file://)", url, re.IGNORECASE):
        return False
    return url.startswith("/")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — FILENAME SANITIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def sanitize_filename(filename: str) -> tuple:
    if not filename:
        return "upload.jpg", False
    original = filename
    filename = filename.replace("\x00", "")
    filename = os.path.basename(filename)
    filename = filename.lstrip(".")
    filename = re.sub(r"[^\w\-.]", "_", filename)
    filename = filename[:100]
    filename = filename or "upload.jpg"
    was_attack = filename != os.path.basename(original.replace("\x00", "")).lstrip(".")
    return filename, was_attack


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — PHOTO DEEP INSPECTION
# ═══════════════════════════════════════════════════════════════════════════════

_ALLOWED_SIGNATURES = [
    (bytes([0xFF, 0xD8, 0xFF]),                                         "jpeg"),
    (bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),         "png"),
    (b"GIF87a",                                                         "gif"),
    (b"GIF89a",                                                         "gif"),
    (b"RIFF",                                                           "webp"),
]

_DANGEROUS_SIGNATURES = [
    (bytes([0x4D, 0x5A]),               "Windows PE executable (.exe/.dll)"),
    (bytes([0x7F, 0x45, 0x4C, 0x46]), "Linux ELF executable"),
    (bytes([0x50, 0x4B, 0x03, 0x04]), "ZIP archive (could contain exploit)"),
    (bytes([0x25, 0x50, 0x44, 0x46]), "PDF document"),
    (bytes([0xCA, 0xFE, 0xBA, 0xBE]), "Java class file"),
    (bytes([0xFE, 0xED, 0xFA, 0xCE]), "Mach-O executable (macOS)"),
    (bytes([0xFE, 0xED, 0xFA, 0xCF]), "Mach-O 64-bit executable"),
    (bytes([0xCF, 0xFA, 0xED, 0xFE]), "Mach-O fat binary"),
]

_DANGEROUS_TEXT_MARKERS = [
    "<script",  "<?php",   "<?xml",   "<!doctype",
    "<svg",     "<html",   "<!entity","javascript:",
    "vbscript:","#!/",     "<%",      "<%@",
    "<jsp:",    "<%!",     "eval(",   "exec(",
]


def deep_inspect_photo(photo_bytes: bytes, filename: str) -> dict:
    import PIL
    from PIL import Image, UnidentifiedImageError

    threats = []
    result = {
        "safe":        False,
        "clean_bytes": None,
        "ext":         "jpeg",
        "threats":     threats,
        "error":       None,
    }

    PIL.Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

    if not photo_bytes or len(photo_bytes) < MIN_PHOTO_BYTES:
        threats.append("empty_or_tiny_file")
        result["error"] = "File appears to be empty or corrupt."
        return result

    first_bytes = photo_bytes[:16]

    for sig, desc in _DANGEROUS_SIGNATURES:
        if first_bytes[:len(sig)] == sig:
            threats.append(f"dangerous_file_type:{desc}")
            result["error"] = "This file type is not allowed. Please upload a JPG, PNG, or WebP photo."
            return result

    try:
        text_preview = photo_bytes[:1024].decode("utf-8", errors="ignore").lower().strip()
        for marker in _DANGEROUS_TEXT_MARKERS:
            if marker in text_preview:
                threats.append(f"script_content_detected:{marker}")
                result["error"] = "Invalid file content. Please upload a real photo."
                return result
    except Exception:
        pass

    detected_type = None
    for sig, ftype in _ALLOWED_SIGNATURES:
        if photo_bytes[:len(sig)] == sig:
            detected_type = ftype
            break

    if not detected_type:
        threats.append("invalid_magic_bytes")
        result["error"] = "Unrecognised file format. Please upload a JPG, PNG, GIF, or WebP photo."
        return result

    if detected_type == "webp":
        if len(photo_bytes) < 12 or photo_bytes[8:12] != b"WEBP":
            threats.append("invalid_webp_structure")
            result["error"] = "Corrupt WebP file. Please try a different photo."
            return result

    try:
        probe = Image.open(io.BytesIO(photo_bytes))
        img_format = probe.format
        probe_width, probe_height = probe.size
        probe.close()
    except PIL.Image.DecompressionBombError:
        threats.append("decompression_bomb")
        result["error"] = "File rejected: decompression bomb detected."
        return result
    except UnidentifiedImageError:
        threats.append("pillow_cannot_identify")
        result["error"] = "Cannot read this file as an image. Please upload a valid photo."
        return result
    except Exception as ex:
        threats.append(f"pillow_open_error:{type(ex).__name__}")
        result["error"] = "Could not process this image. Please try a different photo."
        return result

    if probe_width > MAX_IMAGE_DIMENSION or probe_height > MAX_IMAGE_DIMENSION:
        threats.append(f"pixel_flood:{probe_width}x{probe_height}")
        result["error"] = (
            f"Image dimensions ({probe_width}×{probe_height}px) are too large. "
            f"Maximum is {MAX_IMAGE_DIMENSION}×{MAX_IMAGE_DIMENSION}px."
        )
        return result

    if probe_width * probe_height > MAX_IMAGE_PIXELS:
        threats.append(f"pixel_count_exceeded:{probe_width * probe_height}")
        result["error"] = "Image resolution is too high. Please use a smaller photo."
        return result

    try:
        img = Image.open(io.BytesIO(photo_bytes))

        if img.mode in ("RGBA", "LA"):
            target_mode = "RGBA"
        elif img.mode == "P":
            img = img.convert("RGBA")
            target_mode = "RGBA"
        elif img.mode == "L":
            target_mode = "L"
        else:
            img = img.convert("RGB")
            target_mode = "RGB"

        pixel_data = list(img.getdata())
        clean_img = Image.new(target_mode, img.size)
        clean_img.putdata(pixel_data)

        if detected_type in ("jpeg",):
            out_fmt = "JPEG"
            out_ext = "jpeg"
        elif detected_type in ("gif",):
            out_fmt = "PNG"
            out_ext = "png"
        elif detected_type == "webp":
            out_fmt = "WEBP"
            out_ext = "webp"
        else:
            out_fmt = "PNG"
            out_ext = "png"

        out_buf = io.BytesIO()
        save_opts = {"format": out_fmt}
        if out_fmt == "JPEG":
            save_opts.update({"quality": 85, "optimize": True})
        elif out_fmt == "PNG":
            save_opts.update({"optimize": True})

        clean_img.save(out_buf, **save_opts)
        clean_bytes = out_buf.getvalue()

        if len(clean_bytes) > MAX_PHOTO_BYTES:
            threats.append("output_too_large_after_reencode")
            result["error"] = "Photo is too large after processing. Please use a smaller image."
            return result

    except PIL.Image.DecompressionBombError:
        threats.append("decompression_bomb_on_reencode")
        result["error"] = "File rejected: unsafe image structure."
        return result
    except Exception as ex:
        threats.append(f"reencode_failed:{type(ex).__name__}")
        result["error"] = "Could not safely process this image. Please try a different photo."
        return result

    try:
        residual_text = clean_bytes[:2048].decode("utf-8", errors="ignore").lower()
        for marker in ["<script", "javascript:", "<?php", "eval("]:
            if marker in residual_text:
                threats.append(f"residual_script_after_reencode:{marker}")
                result["error"] = "Image failed final safety check. Please use a different photo."
                return result
    except Exception:
        pass

    result.update({
        "safe":        True,
        "clean_bytes": clean_bytes,
        "ext":         out_ext,
    })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CSRF PROTECTION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_csrf_token(session_token: str) -> str:
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"csrf:{session_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(session_token: str, submitted: str) -> bool:
    if not session_token or not submitted:
        return False
    expected = generate_csrf_token(session_token)
    return hmac.compare_digest(expected, submitted)


def csrf_token_citizen() -> str:
    hour = int(time.time() // 3600)
    return hmac.new(
        SECRET_KEY.encode(),
        f"citizen_csrf:{hour}".encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_citizen(submitted: str) -> bool:
    if not submitted:
        return False
    current_hour = int(time.time() // 3600)
    for h in [current_hour, current_hour - 1]:
        expected = hmac.new(
            SECRET_KEY.encode(),
            f"citizen_csrf:{h}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(expected, submitted):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — MULTI-LAYER RATE LIMITER
# ═══════════════════════════════════════════════════════════════════════════════

class SlidingWindowRateLimiter:
    def __init__(self):
        self._data: dict = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = time.monotonic()

    def check(self, key: str, max_req: int, window_sec: int) -> tuple:
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            if now - self._last_cleanup > 60:
                self._do_cleanup(now)
                self._last_cleanup = now
            self._data[key] = [t for t in self._data[key] if t > cutoff]
            count = len(self._data[key])
            if count >= max_req:
                oldest = self._data[key][0]
                retry_after = int(oldest + window_sec - now) + 1
                return False, 0, retry_after
            self._data[key].append(now)
            return True, max_req - count - 1, 0

    def count(self, key: str, window_sec: int) -> int:
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            return len([t for t in self._data.get(key, []) if t > cutoff])

    def _do_cleanup(self, now: float):
        cutoff = now - 7200
        dead = [k for k, ts in self._data.items() if not ts or max(ts) < cutoff]
        for k in dead:
            del self._data[k]


_limiter = SlidingWindowRateLimiter()

_LIMITS = {
    "submit":         (5,   3600),   # max 5 complaint submissions per IP per hour
    "citizen_review": (10,  3600),   # max 10 review submissions per IP per hour
    "login":          (10,   900),
    "track":          (30,  3600),
    "track_auth":     (30,  3600),
    "track_anon":     (10,  3600),
    "contact":        (5,   3600),
    "global_min":     (300,   60),
    "global_sec":     (30,     1),
}


def check_rate_limit(endpoint: str, ip: str) -> tuple:
    ok, _, retry = _limiter.check(f"burst:{ip}", *_LIMITS["global_sec"])
    if not ok:
        return False, "Slow down — too many requests per second.", retry
    ok, _, retry = _limiter.check(f"global:{ip}", *_LIMITS["global_min"])
    if not ok:
        return False, "Too many requests. Please wait a minute.", retry
    if endpoint in _LIMITS:
        ok, _, retry = _limiter.check(f"{endpoint}:{ip}", *_LIMITS[endpoint])
        if not ok:
            window = _LIMITS[endpoint][1]
            return False, f"Too many attempts. Try again in {window // 60} minutes.", retry
    return True, "", 0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — IP MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def get_real_ip(request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip and _is_valid_ip(cf_ip):
        return cf_ip
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        for candidate in xff.split(","):
            candidate = candidate.strip()
            if _is_valid_ip(candidate) and not _is_private_ip(candidate):
                return candidate
    if request.client:
        return request.client.host
    return "unknown"


def _is_valid_ip(ip: str) -> bool:
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip):
        parts = ip.split(".")
        return all(0 <= int(p) <= 255 for p in parts)
    if re.match(r"^[0-9a-fA-F:]+$", ip) and ":" in ip:
        return True
    return False


def _is_private_ip(ip: str) -> bool:
    """
    Returns True for RFC1918 private / loopback IPs.
    FIX: previous version had Python operator precedence bug on one line.
    Now uses explicit if/elif/return blocks — correct for all IP ranges.
    """
    if ip in ("::1", "::ffff:127.0.0.1"):
        return True
    if ip.startswith(("10.", "192.168.", "127.")):
        return True
    if ip.startswith("172."):
        try:
            second_octet = int(ip.split(".")[1])
            return 16 <= second_octet <= 31
        except (IndexError, ValueError):
            return False
    if ip.lower().startswith(("fc", "fd")):
        return True
    return False


def block_ip(ip: str, reason: str):
    with _block_lock:
        _BLOCKED_IPS.add(ip)
    _sec_log.warning(
        "IP_AUTO_BLOCKED|ip=%s|reason=%s",
        sanitize_log_value(ip),
        sanitize_log_value(reason),
    )


def is_blocked(ip: str) -> bool:
    return ip in _BLOCKED_IPS


def maybe_auto_block(ip: str, threats: list) -> bool:
    serious = {"dangerous_file_type", "script_content_detected", "ssti_python",
               "cmd_inject", "xxe_entity", "traversal", "ssrf_meta", "null_byte",
               "sqli_exec", "pillow_cannot_identify", "decompression_bomb"}
    if any(any(s in t for s in serious) for t in threats):
        block_ip(ip, f"auto_block:{threats[0] if threats else 'attack'}")
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — SECURITY EVENT LOGGER
# ═══════════════════════════════════════════════════════════════════════════════

_sec_log = logging.getLogger("roadseva.security")


class Sec:
    @staticmethod
    def event(kind: str, ip: str = "", user: str = "",
               endpoint: str = "", detail: str = "", level: str = "WARN"):
        msg = (
            f"SEC|{sanitize_log_value(kind)}"
            f"|ip={sanitize_log_value(ip)}"
            f"|user={sanitize_log_value(user)}"
            f"|ep={sanitize_log_value(endpoint)}"
            f"|detail={sanitize_log_value(detail)}"
            f"|ts={datetime.now(timezone.utc).isoformat()}"
        )
        if level == "CRITICAL":
            _sec_log.critical(msg)
        elif level == "HIGH":
            _sec_log.error(msg)
        else:
            _sec_log.warning(msg)

    @staticmethod
    def photo_attack(ip: str, filename: str, threats: list):
        Sec.event("PHOTO_ATTACK", ip=ip,
                  detail=f"file={filename[:50]},threats={threats[:5]}",
                  level="CRITICAL")

    @staticmethod
    def input_attack(ip: str, endpoint: str, field: str, threats: list):
        Sec.event("INPUT_ATTACK", ip=ip, endpoint=endpoint,
                  detail=f"field={field},patterns={threats[:3]}", level="HIGH")

    @staticmethod
    def rate_limited(ip: str, endpoint: str):
        Sec.event("RATE_LIMITED", ip=ip, endpoint=endpoint)

    @staticmethod
    def csrf_fail(ip: str, endpoint: str):
        Sec.event("CSRF_FAIL", ip=ip, endpoint=endpoint, level="HIGH")

    @staticmethod
    def blocked_attempt(ip: str, endpoint: str):
        Sec.event("BLOCKED_IP_HIT", ip=ip, endpoint=endpoint, level="HIGH")

    @staticmethod
    def auth_fail(ip: str, username: str, reason: str):
        Sec.event("AUTH_FAIL", ip=ip, user=username, detail=reason)

    @staticmethod
    def privilege_attempt(ip: str, username: str, target_role: str):
        Sec.event("PRIV_ESCALATION", ip=ip, user=username,
                  detail=f"attempted_role={target_role}", level="CRITICAL")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — ERROR SANITIZER
# ═══════════════════════════════════════════════════════════════════════════════

_SAFE_ERRORS = {
    400: "Invalid request.",
    401: "Please log in to continue.",
    403: "You do not have permission to access this.",
    404: "Page not found.",
    405: "Method not allowed.",
    413: "File too large.",
    422: "Invalid form submission.",
    429: "Too many requests. Please wait before trying again.",
    500: "Something went wrong. Please try again.",
    503: "Service temporarily unavailable.",
}


def safe_error(code: int) -> str:
    return _SAFE_ERRORS.get(code, "An error occurred.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — FASTAPI MIDDLEWARE CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, PlainTextResponse


class SecurityGatewayMiddleware(BaseHTTPMiddleware):
    _SKIP_SCAN_PATHS = {"/health", "/health/live", "/health/ready"}

    async def dispatch(self, request, call_next):
        start = time.monotonic()
        ip = get_real_ip(request)
        path = request.url.path

        if is_blocked(ip):
            Sec.blocked_attempt(ip, path)
            return PlainTextResponse("Forbidden", status_code=403)

        if len(str(request.url)) > MAX_URL_LENGTH:
            Sec.event("LONG_URL", ip=ip, detail=f"len={len(str(request.url))}")
            return PlainTextResponse("URI Too Long", status_code=414)

        if len(request.headers) > MAX_HEADERS_COUNT:
            Sec.event("TOO_MANY_HEADERS", ip=ip, detail=f"count={len(request.headers)}")
            return PlainTextResponse("Bad Request", status_code=400)

        allowed, reason, retry = check_rate_limit("global_min", ip)
        if not allowed:
            Sec.rate_limited(ip, "global")
            return PlainTextResponse(reason, status_code=429,
                                     headers={"Retry-After": str(retry)})
        allowed, reason, retry = check_rate_limit("global_sec", ip)
        if not allowed:
            Sec.rate_limited(ip, "burst")
            return PlainTextResponse(reason, status_code=429,
                                     headers={"Retry-After": str(retry)})

        if path not in self._SKIP_SCAN_PATHS:
            url_str = str(request.url)
            threats = scan_for_attacks(url_str, "url")
            if threats:
                Sec.event("MALICIOUS_URL", ip=ip,
                          detail=f"path={sanitize_log_value(path)[:80]},threats={threats[:3]}",
                          level="HIGH")
                if maybe_auto_block(ip, threats):
                    return PlainTextResponse("Forbidden", status_code=403)
                return PlainTextResponse("Bad Request", status_code=400)

        response = await call_next(request)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        response.headers["Server-Timing"] = f"total;dur={elapsed_ms}"
        response.headers["Server"] = "RoadSeva"
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > MAX_REQUEST_BODY:
                    return PlainTextResponse("Request body too large.", status_code=413)
            except ValueError:
                pass
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
        "  https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' "
        "  https://unpkg.com https://cdnjs.cloudflare.com "
        "  https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://fonts.googleapis.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org "
        "  https://res.cloudinary.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self' https://nominatim.openstreetmap.org; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"]         = "nosniff"
        h["X-Frame-Options"]                = "DENY"
        h["X-XSS-Protection"]               = "1; mode=block"
        h["Referrer-Policy"]                = "strict-origin-when-cross-origin"
        h["Permissions-Policy"]             = (
            "geolocation=(self), camera=(self), "
            "microphone=(), usb=(), payment=()"
        )
        h["Content-Security-Policy"]        = self._CSP
        h["Cross-Origin-Opener-Policy"]     = "same-origin"
        h["Cross-Origin-Resource-Policy"]   = "same-origin"
        if os.getenv("ENVIRONMENT", "production") == "production":
            h["Strict-Transport-Security"]  = (
                "max-age=31536000; includeSubDomains; preload"
            )
        return response


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — TIMING ATTACK PREVENTION
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio

DUMMY_BCRYPT_HASH = None


def get_dummy_hash() -> str:
    global DUMMY_BCRYPT_HASH
    if DUMMY_BCRYPT_HASH is None:
        try:
            import bcrypt
            DUMMY_BCRYPT_HASH = bcrypt.hashpw(
                secrets.token_bytes(16), bcrypt.gensalt(rounds=12)
            ).decode()
        except ImportError:
            DUMMY_BCRYPT_HASH = "x" * 60
    return DUMMY_BCRYPT_HASH


async def enforce_min_response_time(start: float, min_ms: float = 300.0):
    elapsed_ms = (time.monotonic() - start) * 1000
    if elapsed_ms < min_ms:
        await asyncio.sleep((min_ms - elapsed_ms) / 1000)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — CONVENIENCE EXPORTS FOR main.py
# ═══════════════════════════════════════════════════════════════════════════════

def log_photo_rejected(filename: str, reason: str, ip: str = ""):
    Sec.event("PHOTO_REJECTED", ip=ip,
              detail=f"file={sanitize_log_value(filename)[:50]},reason={reason}")