"""A small stateless HTTP service, sized to show Kubernetes operational concerns
(probes, config/secret injection, autoscaling) rather than app complexity."""
import os
import socket
import time

from fastapi import FastAPI, Response

app = FastAPI(title="k3s-demo")
START = time.monotonic()

VERSION = os.environ.get("APP_VERSION", "dev")
GREETING = os.environ.get("GREETING", "hello from k3s")
# Injected from a Kubernetes Secret as an env var; presence only, never echoed.
HAS_TOKEN = bool(os.environ.get("API_TOKEN"))


@app.get("/")
def index():
    return {
        "service": "k3s-demo",
        "version": VERSION,
        "greeting": GREETING,
        "pod": socket.gethostname(),
        "uptime_seconds": round(time.monotonic() - START, 1),
        "secret_loaded": HAS_TOKEN,
    }


@app.get("/healthz")
def healthz():
    """Liveness: process is up."""
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
