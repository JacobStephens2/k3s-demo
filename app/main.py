"""A small two-tier app that demonstrates Kubernetes operations. The root page lets
a visitor generate CPU load and watch - on a live chart - the CPU climb past the
HorizontalPodAutoscaler's 70% target and the app scale 2 -> 6 pods in response.

Active pods come from Redis (a sorted set of recently-active pods); the CPU% and
replica counts come from reading the HPA object via the in-cluster Kubernetes API
(a ServiceAccount with read-only RBAC on pods + HPAs)."""
import json
import os
import socket
import ssl
import time
import urllib.request

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
ACTIVE_WINDOW = 15

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_client = None


def rds():
    global _client
    if _client is None:
        _client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                              socket_connect_timeout=2, decode_responses=True)
    return _client


def read_hpa():
    """Read CPU% and replica counts from the HPA via the in-cluster API. Returns
    Nones if RBAC/API is unavailable, so the page degrades gracefully."""
    out = {"cpu_pct": None, "current_replicas": None, "desired_replicas": None}
    try:
        token = open(f"{SA}/token").read().strip()
        ns = open(f"{SA}/namespace").read().strip()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        url = (f"https://{host}:{port}/apis/autoscaling/v2/namespaces/{ns}"
               "/horizontalpodautoscalers/k3s-demo")
        ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=2) as r:
            st = json.load(r).get("status", {})
        for m in st.get("currentMetrics") or []:
            res = m.get("resource") or {}
            if res.get("name") == "cpu":
                out["cpu_pct"] = (res.get("current") or {}).get("averageUtilization")
        out["current_replicas"] = st.get("currentReplicas")
        out["desired_replicas"] = st.get("desiredReplicas")
    except Exception:
        pass
    return out


@app.middleware("http")
async def track_pod(request, call_next):
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
        "service": "k3s-demo", "version": VERSION, "greeting": GREETING,
        "pod": HOSTNAME, "uptime_seconds": round(time.monotonic() - START, 1),
        "secret_loaded": HAS_TOKEN, "redis": f"{REDIS_HOST}:{REDIS_PORT}",
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
    hpa = read_hpa()
    return {"active_pods": pods, "count": count, "served_by": HOSTNAME,
            "cpu_pct": hpa["cpu_pct"], "hpa_target": 70,
            "current_replicas": hpa["current_replicas"],
            "desired_replicas": hpa["desired_replicas"], "max_replicas": 6}


@app.get("/count")
def count():
    try:
        return {"count": rds().incr("k3s-demo:hits"), "served_by": HOSTNAME}
    except redis.RedisError:
        return Response(status_code=503, content="redis unavailable")


@app.get("/burn", response_class=PlainTextResponse)
def burn(ms: int = 300):
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
  .wrap { max-width:820px; margin:0 auto; padding:28px 20px 60px; }
  h1 { font-size:1.5rem; margin:0 0 .2rem; }
  .sub { color:#8b97a6; margin:0 0 1.4rem; }
  .grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; margin:18px 0; }
  .card { background:#161b22; border:1px solid #232c38; border-radius:12px; padding:14px 16px; }
  .num { font-size:2.2rem; font-weight:700; line-height:1; }
  .num small { font-size:.9rem; color:#8b97a6; font-weight:400; }
  .label { color:#8b97a6; font-size:.78rem; text-transform:uppercase; letter-spacing:.06em; margin-bottom:6px; }
  button { background:#326ce5; color:#fff; border:0; border-radius:10px; padding:13px 20px; font-size:1rem; font-weight:600; cursor:pointer; }
  button:disabled { background:#2a3340; color:#8b97a6; cursor:default; }
  .chart { background:#161b22; border:1px solid #232c38; border-radius:12px; padding:14px 16px; margin-top:16px; }
  .chart .label { display:flex; justify-content:space-between; }
  .legend span { display:inline-block; margin-left:14px; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:middle; margin-right:5px; }
  canvas { width:100%; height:200px; display:block; margin-top:8px; }
  .bar { height:8px; background:#232c38; border-radius:6px; overflow:hidden; margin-top:14px; }
  .bar > i { display:block; height:100%; width:0; background:#326ce5; transition:width .4s; }
  .note { color:#8b97a6; font-size:.9rem; margin-top:14px; }
  code { background:#1b222c; padding:1px 6px; border-radius:5px; }
  .pods { display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }
  .pod { width:16px; height:16px; border-radius:4px; background:#326ce5; opacity:.9; }
  .gloss { margin-top:18px; background:#161b22; border:1px solid #232c38; border-radius:12px; padding:4px 18px; }
  .gloss summary { cursor:pointer; font-weight:600; padding:10px 0; }
  .gloss ul { margin:.3rem 0 .6rem; padding-left:1.1rem; }
  .gloss li { margin:.5rem 0; color:#c4ccd6; }
  .gloss b { color:#e7ecf2; }
  .gloss .tail { color:#8b97a6; font-size:.9rem; }
</style></head>
<body><div class="wrap">
  <h1>k3s-demo</h1>
  <p class="sub">Kubernetes on a single k3s node - press the button and watch the load drive autoscaling, live.</p>

  <div class="grid">
    <div class="card">
      <div class="label">Active app pods</div>
      <div class="num"><span id="pods">-</span><small> / 6</small></div>
      <div class="pods" id="podviz"></div>
    </div>
    <div class="card">
      <div class="label">CPU vs 70% target</div>
      <div class="num"><span id="cpu">-</span><small>%</small></div>
    </div>
    <div class="card">
      <div class="label">Redis counter</div>
      <div class="num" id="count">-</div>
    </div>
  </div>

  <div class="chart">
    <div class="label"><span>Load &amp; autoscaling (last ~90s)</span>
      <span class="legend"><span><i class="dot" style="background:#f0883e"></i>CPU %</span><span><i class="dot" style="background:#326ce5"></i>pods</span><span><i class="dot" style="background:#3fb950"></i>70% target</span></span>
    </div>
    <canvas id="chart" width="1560" height="400"></canvas>
  </div>

  <div style="margin-top:16px"><button id="go" onclick="runLoad()">Run 75s load test</button></div>
  <div class="bar"><i id="prog"></i></div>

  <p class="note" id="status">Pressing the button sends many concurrent <code>/burn</code> requests, which makes the app pods work hard.
  Watch CPU spike past the 70% line on the chart, and the pod count rise behind it as the autoscaler reacts.
  Scale-down is gradual (~5 min after load stops); it's one node, so it caps at what the node can hold.</p>

  <details class="gloss" open>
    <summary>What am I looking at?</summary>
    <ul>
      <li><b>Pod</b> - the smallest thing Kubernetes runs: one container, i.e. one running copy of this app. Normally <b>2 copies</b> share the traffic; under load Kubernetes starts more so the work is spread out.</li>
      <li><b>HorizontalPodAutoscaler (HPA)</b> - a Kubernetes controller that <b>automatically adds or removes pods</b> based on how busy they are. This one watches CPU, targets 70%, and ranges from <b>2 to 6</b> pods. It's what makes the count change on its own.</li>
      <li><b>CPU %</b> - how hard the app pods are working, relative to the CPU they reserved. When it crosses <b>70%</b> (the green dashed line) the HPA adds pods; when it stays low, it removes them.</li>
      <li><b>Redis</b> - a fast in-memory datastore running next to the app (as a second tier). It holds the shared counter and the list of currently-active pods, so every pod sees the same state instead of each keeping its own.</li>
    </ul>
    <p class="tail">Seeing 6/6 pods before pressing the button? A recent load test hasn't scaled back down yet - the HPA waits ~5 minutes of low CPU before removing pods, so it settles to 2 on its own.</p>
  </details>
</div>
<script>
const hist = [];            // {cpu, pods}
const MAXPTS = 60;
let running = false;

function draw() {
  const c = document.getElementById('chart'), ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 34;
  ctx.clearRect(0,0,W,H);
  const cpuMax = Math.max(100, ...hist.map(h => h.cpu||0)) * 1.1;
  const x = i => pad + (W-2*pad) * (i/(MAXPTS-1));
  const yCpu = v => H-pad - (H-2*pad) * Math.min(v,cpuMax)/cpuMax;
  const yPod = v => H-pad - (H-2*pad) * (v/6);
  // grid + 70% target line
  ctx.strokeStyle='#3fb950'; ctx.setLineDash([6,6]); ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(pad,yCpu(70)); ctx.lineTo(W-pad,yCpu(70)); ctx.stroke(); ctx.setLineDash([]);
  // pods (step, blue, behind)
  ctx.strokeStyle='#326ce5'; ctx.lineWidth=3; ctx.beginPath();
  hist.forEach((h,i)=>{ const xx=x(i+MAXPTS-hist.length), yy=yPod(h.pods); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }); ctx.stroke();
  // cpu (orange)
  ctx.strokeStyle='#f0883e'; ctx.lineWidth=3; ctx.beginPath();
  hist.forEach((h,i)=>{ const xx=x(i+MAXPTS-hist.length), yy=yCpu(h.cpu||0); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }); ctx.stroke();
  ctx.fillStyle='#8b97a6'; ctx.font='22px system-ui'; ctx.fillText(Math.round(cpuMax)+'%', 4, yCpu(cpuMax)+18); ctx.fillText('0', 14, H-pad+6);
}

async function poll() {
  try {
    const s = await (await fetch('/status',{cache:'no-store'})).json();
    document.getElementById('pods').textContent = s.active_pods;
    document.getElementById('count').textContent = s.count.toLocaleString();
    document.getElementById('cpu').textContent = (s.cpu_pct==null?'-':s.cpu_pct);
    const viz=document.getElementById('podviz'); viz.innerHTML='';
    for(let i=0;i<s.active_pods;i++){const d=document.createElement('div');d.className='pod';viz.appendChild(d);}
    hist.push({cpu: s.cpu_pct||0, pods: s.active_pods}); if(hist.length>MAXPTS) hist.shift();
    draw();
  } catch(e) {}
}
setInterval(poll, 1500); poll();

async function worker(deadline){ while(performance.now()<deadline && running){ try{ await fetch('/burn?ms=400',{cache:'no-store'}); }catch(e){} } }
async function runLoad(){
  if(running) return; running=true;
  const btn=document.getElementById('go'); btn.disabled=true;
  const secs=75, deadline=performance.now()+secs*1000, prog=document.getElementById('prog');
  document.getElementById('status').textContent='Load running - watch CPU spike on the chart; the HPA adds pods ~30-60s in...';
  const ws=[]; for(let i=0;i<14;i++) ws.push(worker(deadline));
  const t=setInterval(()=>{ const left=Math.max(0,deadline-performance.now()); prog.style.width=(100*(1-left/(secs*1000)))+'%'; if(left<=0)clearInterval(t); },300);
  await Promise.all(ws);
  running=false; btn.disabled=false; prog.style.width='0';
  document.getElementById('status').textContent='Load stopped. CPU drops first, then the HPA scales the pods back down to 2 over the next few minutes.';
}
</script>
</body></html>"""
