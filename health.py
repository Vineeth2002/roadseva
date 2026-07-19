# health.py — RoadSeva Health Check Endpoints
# Rewritten for SQLite stack (PostgreSQL/Redis are post-pilot upgrades)
#
# GET /health        → full check (DB + disk + uploads)
# GET /health/live   → liveness  (is the process alive?)
# GET /health/ready  → readiness (can it serve requests?)

import time
import shutil
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])

APP_VERSION   = "1.0.0"
ENVIRONMENT   = os.getenv("ENVIRONMENT", "production")
DB_PATH       = "roadseva.db"
UPLOADS_PATH  = "uploads"

DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90


def check_database() -> dict:
    """Ping the database — works for both SQLite and PostgreSQL."""
    start = time.perf_counter()
    try:
        if os.getenv("DATABASE_URL"):
            import psycopg
            with psycopg.connect(os.getenv("DATABASE_URL"), connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            engine = "postgresql"
        else:
            conn = sqlite3.connect(DB_PATH, timeout=3.0)
            conn.execute("SELECT COUNT(*) FROM staff")
            conn.close()
            engine = "sqlite"
        return {"status": "ok", "engine": engine,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
    except Exception as e:
        return {"status": "error", "engine": "postgresql" if os.getenv("DATABASE_URL") else "sqlite",
                "error": str(e), "latency_ms": round((time.perf_counter() - start) * 1000, 2)}

def check_disk() -> dict:
    """Check available disk space on the server root."""
    try:
        usage = shutil.disk_usage("/")
        used_pct = round((usage.used / usage.total) * 100, 1)
        free_gb  = round(usage.free / (1024 ** 3), 2)
        if used_pct >= DISK_CRIT_PCT:
            disk_status = "critical"
        elif used_pct >= DISK_WARN_PCT:
            disk_status = "warning"
        else:
            disk_status = "ok"
        return {"status": disk_status, "used_percent": used_pct, "free_gb": free_gb}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def check_uploads() -> dict:
    """Verify the uploads directory exists and is writable."""
    try:
        os.makedirs(UPLOADS_PATH, exist_ok=True)
        test_file = os.path.join(UPLOADS_PATH, ".health_check")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        usage = shutil.disk_usage(UPLOADS_PATH)
        free_gb = round(usage.free / (1024 ** 3), 2)
        return {"status": "ok", "path": UPLOADS_PATH, "free_gb": free_gb}
    except Exception as e:
        return {"status": "error", "path": UPLOADS_PATH, "error": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/health/live", include_in_schema=False)
async def liveness():
    """Liveness — is the process alive? Used by Docker / Render health checks."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/health/ready", include_in_schema=False)
async def readiness():
    """Readiness — can the app serve traffic? Used by load balancers."""
    db = check_database()
    ready = db["status"] == "ok"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": {"database": db},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@router.get("/health", include_in_schema=False)
async def full_health():
    """Full health check — all dependencies. Called by UptimeRobot / monitoring."""
    start = time.perf_counter()
    db      = check_database()
    disk    = check_disk()
    uploads = check_uploads()

    checks = {"database": db, "disk": disk, "uploads": uploads}
    statuses = [c["status"] for c in checks.values()]

    if "error" in statuses or "critical" in statuses:
        overall = "degraded"
        http_code = 503
    elif "warning" in statuses:
        overall = "warning"
        http_code = 200
    else:
        overall = "ok"
        http_code = 200

    return JSONResponse(
        status_code=http_code,
        content={
            "status": overall,
            "version": APP_VERSION,
            "environment": ENVIRONMENT,
            "checks": checks,
            "total_ms": round((time.perf_counter() - start) * 1000, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )