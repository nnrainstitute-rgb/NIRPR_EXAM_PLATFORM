"""Optional Redis/Celery infrastructure with safe local fallbacks.

Single-worker deployments can use the bounded in-memory rate limiter without
Redis. Multiple web workers/replicas require REDIS_URL for shared rate limits.
"""
import json
import os
import time
from collections import OrderedDict, deque
from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "").strip()
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL or "memory://")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL or "cache+memory://")
celery_app = Celery("nirpr", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)
celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json",
                       timezone=os.getenv("TZ", "Africa/Lagos"), enable_utc=True)

try:
    import redis.asyncio as redis
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=0.25, socket_timeout=0.25) if REDIS_URL else None
except Exception:
    redis_client = None

_memory_windows = OrderedDict()
_redis_retry_after = 0.0
_MAX_RATE_KEYS = 4096


async def rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Return True when allowed. Uses atomic Redis counters when available."""
    global _redis_retry_after
    now = time.monotonic()
    if redis_client and now >= _redis_retry_after:
        try:
            count = await redis_client.eval(
                "local n = redis.call('INCR', KEYS[1]); "
                "if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end; return n",
                1, f"rate:{key}", window_seconds)
            return count <= limit
        except Exception:
            _redis_retry_after = now + 5
    if key not in _memory_windows:
        for expired_key, (timestamps, duration) in list(_memory_windows.items()):
            if not timestamps or timestamps[-1] <= now - duration:
                del _memory_windows[expired_key]
        if len(_memory_windows) >= _MAX_RATE_KEYS:
            return False
        _memory_windows[key] = (deque(), window_seconds)
    window, _ = _memory_windows[key]
    while window and window[0] <= now - window_seconds:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now); return True


async def cache_set(key: str, value, ttl: int = 300):
    if redis_client:
        try: await redis_client.setex(key, ttl, json.dumps(value))
        except Exception: pass


async def heartbeat(attempt_id: int, payload: dict, ttl: int = 90):
    await cache_set(f"exam:heartbeat:{attempt_id}", payload, ttl)


@celery_app.task(name="nirpr.send_email")
def send_email_task(kind: str, kwargs: dict):
    import email_utils
    sender = getattr(email_utils, kind)
    return sender(**kwargs)
