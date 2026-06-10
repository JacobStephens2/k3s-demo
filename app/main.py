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
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:16px/1.5 system-ui,-apple-system,sans-serif; background:#0f1216; color:#e7ecf2; }
  .wrap { max-width:820px; margin:0 auto; padding:28px 20px 60px; }
  .clock { float:right; font:13px/1 ui-monospace,SFMono-Regular,Menlo,monospace; color:#9aa6b4; background:#161b22; border:1px solid #232c38; border-radius:8px; padding:8px 11px; }
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
  <div id="clock" class="clock" title="live clock - so screenshots are timestamped"></div>
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

  <div style="margin-top:16px"><button id="go" onclick="runLoad()">Run 75s load test</button>
    <button id="export" onclick="exportCsv()" style="background:#2a3340;margin-left:10px">Download CSV (<span id="samples">0</span> samples)</button></div>
  <div class="bar"><i id="prog"></i></div>

  <p class="note" id="status">Pressing the button sends many concurrent <code>/burn</code> requests, which makes the app pods work hard.
  Watch CPU spike past the 70% line on the chart, and the pod count rise behind it as the autoscaler reacts.
  After load stops, the pods scale back to 2 in about 30-60s (this HPA's scale-down is tuned for the demo; Kubernetes defaults to a cautious 5 minutes to avoid flapping). It's one node, so it caps at what the node can hold.</p>

  <details class="gloss" open>
    <summary>What am I looking at?</summary>
    <ul>
      <li><b>Pod</b> - the smallest thing Kubernetes runs: one container, i.e. one running copy of this app. Normally <b>2 copies</b> share the traffic; under load Kubernetes starts more so the work is spread out.</li>
      <li><b>HorizontalPodAutoscaler (HPA)</b> - a Kubernetes controller that <b>automatically adds or removes pods</b> based on how busy they are. This one watches CPU, targets 70%, and ranges from <b>2 to 6</b> pods. It's what makes the count change on its own.</li>
      <li><b>CPU %</b> - how hard the app pods are working, measured against the CPU each pod <b>reserves</b> (its <i>request</i>, 50m = 0.05 of a core here), not against a whole CPU - so it can go over 100%. A pod is allowed to burst up to its <i>limit</i> (250m), which reads as ~500% of its 50m request. When the average crosses <b>70%</b> (the green dashed line) the HPA adds pods; when it stays low, it removes them.</li>
      <li><b>Redis</b> - a fast in-memory datastore running next to the app (as a second tier). It holds the shared counter and the list of currently-active pods, so every pod sees the same state instead of each keeping its own.</li>
      <li><b>Container runtime</b> - the image is built with Docker, but this node runs it with <b>containerd</b> (k3s's built-in runtime, via Kubernetes' CRI), not the Docker daemon - Kubernetes dropped Docker as a runtime in v1.24. Docker-built (OCI) images run unchanged.</li>
      <li><b>Policy as code</b> - an admission gate (<b>OPA/Gatekeeper</b>) that <b>rejects a non-compliant workload before it runs</b>: every container here must be non-root, drop all Linux capabilities, have a read-only root filesystem, declare CPU/memory limits, carry health probes, and pin an explicit image tag. The same default-deny rule applies whether a human or an automated change tries to weaken it. (One rule is also written as a built-in <i>ValidatingAdmissionPolicy</i> to compare the two engines - see the repo's <code>policy/</code>.)</li>
    </ul>
    <p class="tail">Seeing more than 2 pods before pressing the button? A recent load test is still scaling back down - here that takes about 30-60s (Kubernetes' default is a cautious 5 minutes; this HPA is tuned faster for the demo). Briefly seeing more than 6? During a code deploy Kubernetes runs the old and new pods at once (a zero-downtime rolling update with maxSurge), so the count can momentarily exceed the 6-pod max before settling.</p>
  </details>
</div>
<script>
function tick(){ document.getElementById('clock').textContent = new Date().toLocaleString(undefined,{dateStyle:'medium',timeStyle:'medium'}); }
setInterval(tick, 1000); tick();

const log = [];             // full history: {t, cpu, pods, current, desired, count}
const MAXPTS = 60;
let running = false;

function draw() {
  const c = document.getElementById('chart'), ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 34;
  ctx.clearRect(0,0,W,H);
  const view = log.slice(-MAXPTS);
  const cpuMax = Math.max(100, ...view.map(h => h.cpu||0)) * 1.1;
  const x = i => pad + (W-2*pad) * (i/(MAXPTS-1));
  const yCpu = v => H-pad - (H-2*pad) * Math.min(v,cpuMax)/cpuMax;
  const yPod = v => H-pad - (H-2*pad) * (v/6);
  // grid + 70% target line
  ctx.strokeStyle='#3fb950'; ctx.setLineDash([6,6]); ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(pad,yCpu(70)); ctx.lineTo(W-pad,yCpu(70)); ctx.stroke(); ctx.setLineDash([]);
  // pods (step, blue, behind)
  ctx.strokeStyle='#326ce5'; ctx.lineWidth=3; ctx.beginPath();
  view.forEach((h,i)=>{ const xx=x(i+MAXPTS-view.length), yy=yPod(h.pods); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }); ctx.stroke();
  // cpu (orange)
  ctx.strokeStyle='#f0883e'; ctx.lineWidth=3; ctx.beginPath();
  view.forEach((h,i)=>{ const xx=x(i+MAXPTS-view.length), yy=yCpu(h.cpu||0); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy); }); ctx.stroke();
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
    log.push({t:new Date().toISOString(), cpu:s.cpu_pct||0, pods:s.active_pods, current:s.current_replicas, desired:s.desired_replicas, count:s.count});
    document.getElementById('samples').textContent=log.length;
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
  document.getElementById('status').textContent='Load stopped. CPU drops first, then the HPA scales the pods back down to 2 within about a minute.';
}

function exportCsv(){
  if(!log.length) return;
  const head=['time','cpu_pct','active_pods','current_replicas','desired_replicas','count'];
  const rows=[head.join(',')].concat(log.map(r=>[r.t,r.cpu,r.pods,r.current,r.desired,r.count].join(',')));
  const blob=new Blob([rows.join('\\n')],{type:'text/csv'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='k3s-demo-'+new Date().toISOString().replace(/[:.]/g,'-')+'.csv';
  a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}
</script>
</body></html>"""
