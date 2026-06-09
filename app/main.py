"""A small stateless HTTP service with a Redis-backed counter, sized to show
Kubernetes operational concerns (probes, config/secret injection, autoscaling) and
a stateful backing service (StatefulSet + PVC) rather than app complexity."""
import os
import socket
import time

import redis
from fastapi import FastAPI, Response

app = FastAPI(title="k3s-demo")
START = time.monotonic()

VERSION = os.environ.get("APP_VERSION", "dev")
GREETING = os.environ.get("GREETING", "hello from k3s")
# Injected from a Kubernetes Secret as an env var; presence only, never echoed.
HAS_TOKEN = bool(os.environ.get("API_TOKEN"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

_client = None


def rds():
    global _client
    if _client is None:
        _client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                              socket_connect_timeout=2, decode_responses=True)
    return _client


@app.get("/")
def index():
    return {
        "service": "k3s-demo",
        "version": VERSION,
        "greeting": GREETING,
        "pod": socket.gethostname(),
        "uptime_seconds": round(time.monotonic() - START, 1),
        "secret_loaded": HAS_TOKEN,
        "redis": f"{REDIS_HOST}:{REDIS_PORT}",
    }


@app.get("/count")
def count():
    """Increment and return a counter in Redis - shared across all app pods,
    persisted by the Redis StatefulSet's PVC."""
    try:
        n = rds().incr("k3s-demo:hits")
        return {"count": n, "served_by": socket.gethostname()}
    except redis.RedisError:
        return Response(status_code=503, content="redis unavailable")


@app.get("/healthz")
def healthz():
    """Liveness: the process is up (independent of Redis)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness: gate traffic for the first 2s so rollouts are observable."""
    ready = time.monotonic() - START > 2
    return Response(status_code=200 if ready else 503)


@app.get("/burn")
def burn(ms: int = 200):
    """Burn CPU briefly so the HorizontalPodAutoscaler has a signal to react to."""
    end = time.monotonic() + min(ms, 2000) / 1000.0
    n = 0
    while time.monotonic() < end:
        n += 1
    return {"iterations": n}
