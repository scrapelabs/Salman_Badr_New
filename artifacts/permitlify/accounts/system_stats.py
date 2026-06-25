"""Real-time host/container resource stats for the Overview monitor cards.

CPU is sampled with psutil (system-wide, non-blocking). Memory prefers cgroup v2
limits (container-accurate on Replit) and falls back to psutil. Disk reports the
filesystem the app lives on. Everything degrades gracefully when psutil or the
cgroup files are unavailable so the dashboard never 500s.
"""
from __future__ import annotations

from django.conf import settings

try:  # psutil is a declared dependency, but never let its absence 500 the page.
    import psutil
except Exception:  # pragma: no cover
    psutil = None

_CGROUP_MEM_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_MEM_CUR = "/sys/fs/cgroup/memory.current"

# Geometry of the SVG gauge ring (r=34 → circumference 2*pi*34).
GAUGE_CIRCUMFERENCE = round(2 * 3.141592653589793 * 34, 3)


def _prime_cpu():
    if psutil is not None:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass


# Prime psutil's CPU sampler at import so the first real reading isn't 0.0.
_prime_cpu()


def _gb(n):
    return round((n or 0) / (1024 ** 3), 2)


def _pct(used, total):
    if not total:
        return 0.0
    return round(min(100.0, max(0.0, used / total * 100)), 1)


def severity(percent):
    """Map a load percentage to a colour bucket: ok / warn / crit."""
    if percent >= 85:
        return "crit"
    if percent >= 60:
        return "warn"
    return "ok"


def gauge_offset(percent):
    """stroke-dashoffset for a ring filled to ``percent`` (0 = empty ring)."""
    p = min(100.0, max(0.0, percent or 0))
    return round(GAUGE_CIRCUMFERENCE * (1 - p / 100), 2)


def _cpu():
    if psutil is None:
        return {"percent": 0.0, "cores": 0}
    try:
        pct = psutil.cpu_percent(interval=None)
        # A fresh process's first sample can read 0.0; take a quick blocking
        # sample in that case so the gauge isn't stuck at zero on first load.
        if pct == 0.0:
            pct = psutil.cpu_percent(interval=0.1)
        return {"percent": round(pct, 1), "cores": psutil.cpu_count() or 0}
    except Exception:
        return {"percent": 0.0, "cores": 0}


def _read_int(path):
    with open(path) as fh:
        return int(fh.read().strip())


def _mem_cgroup_v2():
    """Container-accurate memory from cgroup v2; None when unavailable/unlimited."""
    try:
        with open(_CGROUP_MEM_MAX) as fh:
            raw = fh.read().strip()
        if raw == "max":
            return None
        total = int(raw)
        if total <= 0:
            return None
        return _read_int(_CGROUP_MEM_CUR), total
    except Exception:
        return None


def _mem():
    cg = _mem_cgroup_v2()
    if cg is not None:
        used, total = cg
    elif psutil is not None:
        try:
            vm = psutil.virtual_memory()
            used, total = vm.used, vm.total
        except Exception:
            used = total = 0
    else:
        used = total = 0
    return {"percent": _pct(used, total), "used_gb": _gb(used), "total_gb": _gb(total)}


def _disk():
    if psutil is None:
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
    try:
        du = psutil.disk_usage(str(settings.BASE_DIR))
        return {
            "percent": round(du.percent, 1),
            "used_gb": _gb(du.used),
            "total_gb": _gb(du.total),
        }
    except Exception:
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}


def collect_system_stats():
    """Return {cpu, mem, disk} dicts, each with percent + human-readable extras."""
    return {"cpu": _cpu(), "mem": _mem(), "disk": _disk()}


def gauge_card(title, stat, foot):
    """Bundle a stat dict with the geometry the gauge template needs."""
    percent = stat.get("percent", 0.0)
    return {
        "title": title,
        "percent": percent,
        "offset": gauge_offset(percent),
        "sev": severity(percent),
        "circumference": GAUGE_CIRCUMFERENCE,
        "foot": foot,
        **stat,
    }
