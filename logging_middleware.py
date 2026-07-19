# logging_middleware.py — RoadSeva Request Context Middleware
# Lives at project root: C:\RoadSeva\logging_middleware.py
# Runs on every request — sets request_id, staff_id, role, ip in ContextVars
# so every log line emitted during that request is automatically tagged.

import database
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from logger import get_logger, ip_var, request_id_var, role_var, staff_id_var

log = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Per-request context injection + structured access logging.

    What it does:
    1. Generates a unique request_id for every request
    2. Captures real client IP (handles X-Forwarded-For from Render/Nginx)
    3. Tries to read staff context from session cookie (best-effort)
    4. Logs request_start and request_end with duration_ms
    5. Injects X-Request-ID into response header (useful for support tickets)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 1. Request ID
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request_id_var.set(request_id)

        # 2. Real client IP (Render puts real IP in X-Forwarded-For)
        forwarded_for = request.headers.get("X-Forwarded-For")
        client_ip = (
            forwarded_for.split(",")[0].strip()
            if forwarded_for
            else (request.client.host if request.client else "unknown")
        )
        ip_var.set(client_ip)

        # 3. Staff context from session cookie (best-effort — never breaks request)
        staff_id = "anonymous"
        role = ""
        try:
            token = request.cookies.get("session_token")
            if token:
                staff = database.get_staff_by_token(token)
                if staff:
                    staff_id = f"{staff['username']}({staff['id']})"
                    role = staff["role"]
        except Exception:
            pass

        staff_id_var.set(staff_id)
        role_var.set(role)

        # 4. Log request start (skip static/health to reduce noise)
        path = request.url.path
        is_noisy = path.startswith("/uploads") or path in ("/health/live", "/health/ready")

        start = time.perf_counter()
        if not is_noisy:
            log.info("request_start", method=request.method, path=path)

        # 5. Process request
        response: Response = await call_next(request)

        # 6. Log request end
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if not is_noisy:
            level = "warning" if response.status_code >= 400 else "info"
            getattr(log, level)(
                "request_end",
                method=request.method,
                path=path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )

        # 7. Inject request ID into response header
        response.headers["X-Request-ID"] = request_id
        return response