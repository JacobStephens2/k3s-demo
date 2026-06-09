"""A small two-tier app that demonstrates Kubernetes operations. The root page lets
a visitor generate CPU load and watch the HorizontalPodAutoscaler add app pods
live; pod liveness is tracked in Redis (a sorted set of recently-active pods), so
no in-cluster RBAC is needed to show the scaling."""
import os
import socket
import time

import redis
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

app = FastAPI(title="k3s-demo")
START = time.monotonic()
HOSTNAME = socket.gethostname()

VERSION = os.environ.get("APP_VERSION", "dev")
GREETING = os.environ.get("GREETING", "hello from k3s")
HAS_TOKEN = bool(os.environ.get("API_TOKEN"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
ACTIVE_KEY = "k3s-demo:active_pods"
ACTIVE_WINDOW = 15  # seconds a pod counts as "active" since its last request

_client = None


def rds():
    global _client
    if _client is None:
        _client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                              socket_connect_timeout=2, decode_responses=True)
    return _client


@app.middleware("http")
async def track_pod(request, call_next):
    # Record that this pod served a request, so /status can report how many pods
    # are currently handling traffic (rises as the HPA scales up).
    try:
        rds().zadd(ACTIVE_KEY, {HOSTNAME: time.time()})
    except Exception:
        pass
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/info")
def info():
    return {
        "service": "k3s-demo",
        "version": VERSION,
        "greeting": GREETING,
        "pod": HOSTNAME,
        "uptime_seconds": round(time.monotonic() - START, 1),
        "secret_loaded": HAS_TOKEN,
        "redis": f"{REDIS_HOST}:{REDIS_PORT}",
    }


@app.get("/status")
def status():
    pods, count = 0, 0
    try:
        now = time.time()
        rds().zremrangebyscore(ACTIVE_KEY, 0, now - ACTIVE_WINDOW)
        pods = rds().zcard(ACTIVE_KEY)
        count = int(rds().get("k3s-demo:hits") or 0)
    except redis.RedisError:
        pass
    return {"active_pods": pods, "count": count, "served_by": HOSTNAME}


@app.get("/count")
def count():
    try:
        n = rds().incr("k3s-demo:hits")
        return {"count": n, "served_by": HOSTNAME}
    except redis.RedisError:
        return Response(status_code=503, content="redis unavailable")


@app.get("/burn", response_class=PlainTextResponse)
def burn(ms: int = 300):
    """Burn CPU for up to 2s so load tests can drive the HPA."""
    end = time.monotonic() + min(ms, 2000) / 1000.0
    n = 0
    while time.monotonic() < end:
        n += 1
    return f"{HOSTNAME} {n}"


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return Response(status_code=200 if time.monotonic() - START > 2 else 503)


PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>k3s-demo - live autoscaling</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:16px/1.5 system-ui,-apple-system,sans-serif; background:#0f1216; color:#e7ecf2; }
  .wrap { max-width:760px; margin:0 auto; padding:28px 20px 60px; }
  h1 { font-size:1.5rem; margin:0 0 .2rem; }
  .sub { color:#8b97a6; margin:0 0 1.4rem; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:18px 0; }
  .card { background:#161b22; border:1px solid #232c38; border-radius:12px; padding:16px 18px; }
  .num { font-size:2.6rem; font-weight:700; line-height:1; }
  .num small { font-size:1rem; color:#8b97a6; font-weight:400; }
  .label { color:#8b97a6; font-size:.85rem; text-transform:uppercase; letter-spacing:.06em; margin-bottom:6px; }
  button { background:#326ce5; color:#fff; border:0; border-radius:10px; padding:13px 20px; font-size:1rem; font-weight:600; cursor:pointer; }
  button:disabled { background:#2a3340; color:#8b97a6; cursor:default; }
  .bar { height:10px; background:#232c38; border-radius:6px; overflow:hidden; margin-top:10px; }
  .bar > i { display:block; height:100%; width:0; background:#326ce5; transition:width .4s; }
  .note { color:#8b97a6; font-size:.9rem; margin-top:16px; }
  code { background:#1b222c; padding:1px 6px; border-radius:5px; }
  .pods { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .pod { width:18px; height:18px; border-radius:4px; background:#326ce5; opacity:.9; }
</style></head>
<body><div class="wrap">
  <h1>k3s-demo</h1>
  <p class="sub" id="sub">Kubernetes on a single k3s node - press the button and watch it scale.</p>

  <div class="grid">
    <div class="card">
      <div class="label">Active app pods</div>
      <div class="num"><span id="pods">-</span><small> / 6 max</small></div>
      <div class="pods" id="podviz"></div>
    </div>
    <div class="card">
      <div class="label">Shared Redis counter</div>
      <div class="num" id="count">-</div>
    </div>
  </div>

  <button id="go" onclick="runLoad()">Run 75s load test</button>
  <div class="bar"><i id="prog"></i></div>

  <p class="note" id="status">The HorizontalPodAutoscaler scales the app 2 &rarr; 6 pods when CPU passes 70%.
  Generating load below sends many concurrent <code>/burn</code> requests; watch <b>Active app pods</b> climb.
  Scale-down is gradual (~5 min after load stops), and this is one node, so it caps at what the node can hold.</p>
</div>
<script>
let running = false;
async function poll() {
  try {
    const s = await (await fetch('/status', {cache:'no-store'})).json();
    document.getElementById('pods').textContent = s.active_pods;
    document.getElementById('count').textContent = s.count.toLocaleString();
    const viz = document.getElementById('podviz'); viz.innerHTML = '';
    for (let i=0;i<s.active_pods;i++){ const d=document.createElement('div'); d.className='pod'; viz.appendChild(d); }
  } catch(e) {}
}
setInterval(poll, 2000); poll();

async function worker(deadline){ while(performance.now()<deadline && running){ try{ await fetch('/burn?ms=400',{cache:'no-store'}); }catch(e){} } }
async function runLoad(){
  if(running) return; running = true;
  const btn=document.getElementById('go'); btn.disabled=true;
  const secs=75, deadline=performance.now()+secs*1000, prog=document.getElementById('prog');
  const st=document.getElementById('status'); st.textContent='Load running - watch the pod count rise (HPA reacts in ~30-60s)...';
  const workers=[]; for(let i=0;i<14;i++) workers.push(worker(deadline));
  const t=setInterval(()=>{ const left=Math.max(0,deadline-performance.now()); prog.style.width=(100*(1-left/(secs*1000)))+'%'; if(left<=0){clearInterval(t);} }, 300);
  await Promise.all(workers);
  running=false; btn.disabled=false; prog.style.width='0';
  st.textContent='Load stopped. The HPA will scale the pods back down to 2 over the next few minutes.';
}
</script>
</body></html>"""
