import time
from collections import defaultdict
from redis_client import r

# In-memory fallback when Redis is unavailable
_memory_store: dict = defaultdict(list)

def check_rate_limit(key: str, limit: int, window: int):
    """
    Returns (allowed: bool, message: str, retry_after: int)
    Degrades to in-memory fallback if Redis is unavailable.
    """
    now = int(time.time())

    # ── Redis path ────────────────────────────────────────────────────────────
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {str(now): now})  # add first
            pipe.zcard(key)                  # then count — fixes off-by-one
            pipe.expire(key, window)
            _, _, count, _ = pipe.execute()

            if count > limit:
                return False, "Rate limit exceeded. Try again later.", window
            return True, "", 0
        except Exception:
            pass  # Fall through to in-memory

    # ── In-memory fallback ────────────────────────────────────────────────────
    timestamps = _memory_store[key]
    timestamps = [t for t in timestamps if t > now - window]
    timestamps.append(now)
    _memory_store[key] = timestamps

    if len(timestamps) > limit:
        return False, "Rate limit exceeded. Try again later.", window
    return True, "", 0