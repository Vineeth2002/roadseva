import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

r = None  # None = Redis unavailable, callers must handle gracefully

if REDIS_URL:
    try:
        r = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        r.ping()  # Fail fast if URL is wrong, rather than failing mid-request
    except Exception:
        r = None  # Degrade gracefully — rate limiter will use in-memory fallback