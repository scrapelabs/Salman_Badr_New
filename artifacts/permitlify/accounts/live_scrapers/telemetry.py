"""Per-run telemetry: records every HTTP request and every error so a run can
export ``requests`` and ``errors`` CSVs alongside its items/data CSV.

The column layouts and value formats deliberately match the user's production
scraping framework so the downloaded files are drop-in compatible:

- requests: ``request_id,duration,fingerprint,http_method,response_size,http_status,last_seen,url``
- errors:   ``timestamp,level,message,exception``
"""

import csv
import hashlib
import io
import re
import threading
import traceback
import uuid
from datetime import datetime, timezone as _tz

REQUEST_COLUMNS = [
    "request_id", "duration", "fingerprint", "http_method",
    "response_size", "http_status", "last_seen", "url",
]
ERROR_COLUMNS = ["timestamp", "level", "message", "exception"]


def sanitize_cell(value):
    """Guard a CSV cell against spreadsheet formula injection."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


# Masks ``user:pass`` credentials embedded in any URL (e.g. a proxy address).
_CRED_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")


def redact_secrets(value):
    """Scrub ``user:pass`` credentials out of any URL inside ``value``.

    A proxy address may embed credentials and libcurl/exception text can echo
    the proxy URL, so this runs before anything reaches a log line or the
    errors CSV. Never log a raw proxy address.
    """
    if not value:
        return value
    return _CRED_RE.sub(r"\1***:***@", str(value))


def _write_csv(columns, rows):
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: sanitize_cell(row.get(c, "")) for c in columns})
    return buf.getvalue()


class Telemetry:
    """Collects request + error records during a single run."""

    def __init__(self):
        self._requests = []
        self._errors = []
        self._lock = threading.Lock()

    # -- recording -------------------------------------------------------
    def record_request(self, *, url, method, status, size, duration_ms):
        size = size or 0
        status_part = status if status is not None else "timeout"
        fingerprint = hashlib.sha1(
            f"{url}{status_part}{size}".encode("utf-8")
        ).hexdigest()
        entry = {
            "request_id": str(uuid.uuid4()),
            "duration": f"{duration_ms:.0f} ms",
            "fingerprint": fingerprint,
            "http_method": method,
            "response_size": f"{size} bytes",
            "http_status": status if status is not None else "",
            "last_seen": datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "url": url,
        }
        with self._lock:
            self._requests.append(entry)

    def record_error(self, message, *, level="ERROR", exc=None):
        exception = ""
        if exc is not None:
            exception = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "level": level,
            "message": redact_secrets(message),
            "exception": redact_secrets(exception),
        }
        with self._lock:
            self._errors.append(entry)

    # -- counts / export -------------------------------------------------
    @property
    def request_count(self):
        return len(self._requests)

    @property
    def error_count(self):
        return len(self._errors)

    def requests_csv(self):
        return _write_csv(REQUEST_COLUMNS, self._requests)

    def errors_csv(self):
        return _write_csv(ERROR_COLUMNS, self._errors)
