import csv
import io
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

REST = SUPABASE_URL + "/rest/v1"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json",
}

app = FastAPI(title="QR Cloud", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _err(code, msg):
    return JSONResponse({"error": msg}, status_code=code)


def _check_token(request):
    if not INGEST_TOKEN:
        return True
    tok = request.query_params.get("token") or request.headers.get("x-api-key")
    return tok == INGEST_TOKEN


def _s(v):
    return None if v is None else str(v)


def _as_items(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, list):
        return payload
    return [payload]


def _content_range_total(resp, fallback):
    cr = resp.headers.get("content-range", "")
    if "/" in cr:
        tail = cr.split("/")[-1]
        if tail.isdigit():
            return int(tail)
    return fallback


def db_insert(rows):
    with httpx.Client(timeout=15) as c:
        r = c.post(REST + "/scans", headers={**HEADERS, "Prefer": "return=representation"},
                   content=json.dumps(rows))
    r.raise_for_status()
    return r.json()


def db_select(q="", date_from="", date_to="", source="", limit=50, offset=0):
    parts = ["select=*", "order=id.desc", "limit=" + str(limit), "offset=" + str(offset)]
    if date_from:
        parts.append("created_at=gte." + date_from + "T00:00:00")
    if date_to:
        parts.append("created_at=lte." + date_to + "T23:59:59")
    if source:
        parts.append("source=eq." + quote(source, safe=""))
    if q:
        qq = quote(q, safe="")
        parts.append("or=(code.ilike.*" + qq + "*,device.ilike.*" + qq + "*,source.ilike.*" + qq + "*)")
    url = REST + "/scans?" + "&".join(parts)
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers={**HEADERS, "Prefer": "count=exact"})
    r.raise_for_status()
    rows = r.json()
    return rows, _content_range_total(r, len(rows))


def db_count(extra=""):
    url = REST + "/scans?select=id&limit=1"
    if extra:
        url += "&" + extra
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers={**HEADERS, "Prefer": "count=exact"})
    r.raise_for_status()
    return _content_range_total(r, len(r.json()))


def db_get_setting(key, default):
    url = REST + "/app_settings?select=value&key=eq." + key
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(url, headers=HEADERS)
        if r.status_code >= 300:
            return default
        rows = r.json()
        if rows and rows[0].get("value") is not None:
            return rows[0]["value"]
    except httpx.HTTPError:
        pass
    return default


def db_set_setting(key, value):
    body = [{"key": key, "value": value}]
    with httpx.Client(timeout=15) as c:
        r = c.post(REST + "/app_settings",
                   headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
                   content=json.dumps(body))
    r.raise_for_status()
    return r.json()


def forward_scan(scan, urls):
    payload = json.dumps({
        "event": "qr_scan",
        "data": scan.get("code"),
        "device": scan.get("device"),
        "source": scan.get("source"),
        "scanned_at": scan.get("scanned_at") or scan.get("created_at"),
    }, ensure_ascii=False).encode("utf-8")
    for url in urls:
        try:
            with httpx.Client(timeout=8) as c:
                c.post(url, content=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        except Exception:
            pass


@app.get("/health")
def health():
    return {"ok": True, "configured": _configured()}


@app.post("/api/v1/scans")
@app.post("/api/v1/fdb/backup/")
async def ingest(request: Request):
    if not _configured():
        return _err(503, "server not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY)")
    if not _check_token(request):
        return _err(401, "invalid token")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    src_default = request.query_params.get("source")
    rows = []
    for it in _as_items(payload):
        if not isinstance(it, dict):
            continue
        raw = it.get("code", it.get("data"))
        code = "" if raw is None else str(raw).strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "device": _s(it.get("device")),
            "status": str(it.get("status") or "received"),
            "source": _s(it.get("source") or src_default),
            "scanned_at": _s(it.get("scanned_at")),
        })
    if not rows:
        return _err(400, "no valid 'code' / 'data' field")
    try:
        created = db_insert(rows)
    except httpx.HTTPError as exc:
        return _err(502, "supabase insert failed: " + str(exc))
    fwd = db_get_setting("forward_urls", [])
    if isinstance(fwd, list) and fwd:
        for sc in created:
            forward_scan(sc, fwd)
    return JSONResponse({"created": len(created), "items": created, "forwarded_to": len(fwd) if isinstance(fwd, list) else 0}, status_code=201)


@app.get("/api/v1/scans")
def list_scans(
    q: str = "", date: str = "", source: str = "",
    date_from: str = Query("", alias="from"), date_to: str = Query("", alias="to"),
    limit: int = 50, offset: int = 0,
):
    if not _configured():
        return _err(503, "server not configured")
    if date:
        date_from = date_to = date
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    try:
        rows, total = db_select(q, date_from, date_to, source, limit, offset)
    except httpx.HTTPError as exc:
        return _err(502, "supabase query failed: " + str(exc))
    return {"total": total, "count": len(rows), "limit": limit, "offset": offset, "data": rows}


@app.get("/api/v1/scans.csv")
def scans_csv(
    q: str = "", date: str = "", source: str = "",
    date_from: str = Query("", alias="from"), date_to: str = Query("", alias="to"),
):
    if not _configured():
        return _err(503, "server not configured")
    if date:
        date_from = date_to = date
    rows, _ = db_select(q, date_from, date_to, source, 100000, 0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "scanned_at", "code", "device", "source", "status"])
    for r in rows:
        w.writerow([r.get("id"), r.get("created_at"), r.get("scanned_at"),
                    r.get("code"), r.get("device"), r.get("source"), r.get("status")])
    return Response(
        buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=scans.csv"},
    )


@app.get("/api/v1/stats")
def stats():
    if not _configured():
        return _err(503, "server not configured")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        total = db_count()
        today_n = db_count("created_at=gte." + today + "T00:00:00")
    except httpx.HTTPError as exc:
        return _err(502, "supabase query failed: " + str(exc))
    return {"total": total, "today": today_n}


@app.get("/api/v1/config")
def get_config():
    if not _configured():
        return _err(503, "server not configured")
    return {"forward_urls": db_get_setting("forward_urls", [])}


@app.post("/api/v1/config")
async def set_config(request: Request):
    if not _configured():
        return _err(503, "server not configured")
    if not _check_token(request):
        return _err(401, "invalid token")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    clean = []
    for u in payload.get("forward_urls") or []:
        if isinstance(u, str) and u.strip() and u.strip() not in clean:
            clean.append(u.strip())
    try:
        db_set_setting("forward_urls", clean)
    except httpx.HTTPError as exc:
        return _err(502, "save failed: " + str(exc))
    return {"forward_urls": clean}


@app.post("/api/v1/test")
async def test_url(request: Request):
    if not _check_token(request):
        return _err(401, "invalid token")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    url = (payload.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "URL bo'sh"}
    body = json.dumps({"event": "qr_scan_test", "data": "TEST-123",
                       "device": "cloud-test", "scanned_at": "test"}).encode("utf-8")
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(url, content=body, headers={"Content-Type": "application/json"})
        return {"ok": True, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return INDEX_HTML


@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)


INDEX_HTML = r'''<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QR Cloud — Panel</title>
<style>
  :root{--bg:#0f1419;--panel:#171e26;--panel2:#1d2630;--line:#2a3744;--txt:#e6edf3;--muted:#8b9aa8;--accent:#3ea6ff;--ok:#3fb950;--warn:#d29922;--err:#f85149}
  *{box-sizing:border-box}
  body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--txt);font-size:14px}
  header{display:flex;align-items:center;gap:16px;padding:16px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
  .logo{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--accent),#7b61ff);display:flex;align-items:center;justify-content:center;font-weight:700}
  header h1{font-size:16px;margin:0;font-weight:600}
  header .sub{color:var(--muted);font-size:12px}
  .spacer{flex:1}
  .stat{text-align:right}.stat b{font-size:18px;color:var(--accent)}.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  main{padding:24px;max-width:1150px;margin:0 auto}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
  input,button.btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 12px;font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button.btn{cursor:pointer}button.btn:hover{border-color:var(--accent)}
  .grow{flex:1;min-width:160px}
  table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden}
  th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.4px;background:var(--panel2)}
  td.code{font-family:Consolas,monospace;color:#fff}
  tr:last-child td{border-bottom:none}tr:hover td{background:var(--panel2)}
  .pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
  .pill.ok{background:rgba(63,185,80,.15);color:var(--ok)}.pill.received{background:rgba(62,166,255,.15);color:var(--accent)}.pill.error{background:rgba(248,81,73,.15);color:var(--err)}
  .src{display:inline-block;padding:2px 8px;border-radius:6px;background:var(--panel2);border:1px solid var(--line);font-size:12px}
  .muted{color:var(--muted)}.empty{text-align:center;padding:48px;color:var(--muted)}
  .pager{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:14px;color:var(--muted)}
  label.chk{display:flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer;user-select:none}
  nav{display:flex;gap:4px;padding:0 24px;background:var(--panel);border-bottom:1px solid var(--line)}
  nav button{background:none;border:none;color:var(--muted);padding:12px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
  nav button.active{color:var(--txt);border-bottom-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
  button.ghost{background:none}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
  .card h2{margin:0 0 4px;font-size:15px}.card p.desc{margin:0 0 16px;color:var(--muted);font-size:13px}
  .urlrow{display:flex;gap:10px;align-items:center;margin-bottom:10px}
  .urlrow input{flex:1;font-family:Consolas,monospace;font-size:13px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
  .dot.ok{background:var(--ok)}.dot.err{background:var(--err)}
  .hint{font-size:12px;color:var(--muted);margin-top:8px}
  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:12px 20px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1}
</style>
</head>
<body>
<header>
  <div class="logo">QR</div>
  <div><h1>QR Cloud Panel</h1><div class="sub">Barcha kompyuterlardan skan ma'lumotlari</div></div>
  <div class="spacer"></div>
  <div class="stat"><b id="st-today">0</b><span>Bugun</span></div>
  <div class="stat"><b id="st-total">0</b><span>Jami</span></div>
</header>
<nav>
  <button data-tab="scans" class="active">Skanlar</button>
  <button data-tab="settings">Sozlamalar</button>
</nav>
<main>
  <section id="tab-scans">
  <div class="toolbar">
    <input id="q" class="grow" placeholder="Kod, qurilma yoki manba bo'yicha qidirish...">
    <input id="from" type="date" title="Dan">
    <input id="to" type="date" title="Gacha">
    <button class="btn" id="clear">Tozalash</button>
    <label class="chk"><input type="checkbox" id="auto" checked> Avto</label>
    <button class="btn" id="refresh">Yangilash</button>
    <button class="btn" id="csv">CSV</button>
  </div>
  <table>
    <thead><tr><th style="width:55px">#</th><th style="width:185px">Vaqt</th><th>Kod</th><th style="width:130px">Manba</th><th style="width:200px">Qurilma</th><th style="width:100px">Holat</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none">Hozircha skan yo'q.</div>
  <div class="pager"><span id="pginfo"></span><button class="btn" id="prev">‹</button><button class="btn" id="next">›</button></div>
  </section>

  <section id="tab-settings" style="display:none">
    <div class="card">
      <h2>Uzatish manzillari (forward URLs)</h2>
      <p class="desc">Serverga kelgan har bir skan shu manzillarga ham qayta yuboriladi (masalan dahua, boshqa API, Telegram bot). Format yuboriladigan tana: <code>{"event":"qr_scan","data":"...","device":"...","source":"...","scanned_at":"..."}</code></p>
      <div id="urls"></div>
      <div class="urlrow">
        <input id="newurl" placeholder="https://...">
        <button class="btn" id="addurl">+ Qo'shish</button>
      </div>
      <div style="margin-top:16px">
        <button class="btn primary" id="save">Saqlash</button>
        <span class="hint" id="savehint"></span>
      </div>
    </div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const api=(p)=>fetch(p).then(r=>r.json());
let state={offset:0,limit:50,total:0};
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmt(iso){if(!iso)return '';const d=new Date(iso);if(isNaN(d))return esc(iso);return d.toLocaleString('uz',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function shortDev(d){if(!d)return '';const m=String(d).match(/VID_[0-9A-Fa-f]{4}|PID_[0-9A-Fa-f]{4}/g);return m?m.join(' '):(String(d).length>34?String(d).slice(0,34)+'…':d);}
function qstr(){const u=new URLSearchParams();if($('#q').value.trim())u.set('q',$('#q').value.trim());if($('#from').value)u.set('from',$('#from').value);if($('#to').value)u.set('to',$('#to').value);return u;}
async function load(){
  const u=qstr();u.set('limit',state.limit);u.set('offset',state.offset);
  const d=await api('/api/v1/scans?'+u.toString());state.total=d.total||0;
  const tb=$('#rows');tb.innerHTML='';$('#empty').style.display=(d.data&&d.data.length)?'none':'';
  (d.data||[]).forEach(r=>{
    const cls=r.status==='error'?'error':(r.status==='ok'?'ok':'received');
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="muted">${r.id}</td><td>${fmt(r.created_at)}</td><td class="code">${esc(r.code)}</td><td>${r.source?'<span class="src">'+esc(r.source)+'</span>':'<span class="muted">—</span>'}</td><td class="muted">${esc(shortDev(r.device))}</td><td><span class="pill ${cls}">${esc(r.status||'-')}</span></td>`;
    tb.appendChild(tr);
  });
  const from=state.total?state.offset+1:0,to=Math.min(state.offset+state.limit,state.total);
  $('#pginfo').textContent=`${from}–${to} / ${state.total}`;
  $('#prev').disabled=state.offset<=0;$('#next').disabled=state.offset+state.limit>=state.total;
}
async function loadStats(){const s=await api('/api/v1/stats');$('#st-today').textContent=s.today||0;$('#st-total').textContent=s.total||0;}
$('#refresh').onclick=()=>{load();loadStats();};
$('#clear').onclick=()=>{$('#q').value='';$('#from').value='';$('#to').value='';state.offset=0;load();};
$('#q').oninput=()=>{state.offset=0;load();};
$('#from').onchange=$('#to').onchange=()=>{state.offset=0;load();};
$('#prev').onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);load();};
$('#next').onclick=()=>{state.offset+=state.limit;load();};
$('#csv').onclick=()=>{location.href='/api/v1/scans.csv?'+qstr().toString();};
setInterval(()=>{if($('#auto').checked && $('#tab-scans').style.display!=='none'){load();loadStats();}},5000);

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  $('#tab-scans').style.display=b.dataset.tab==='scans'?'':'none';
  $('#tab-settings').style.display=b.dataset.tab==='settings'?'':'none';
  if(b.dataset.tab==='settings')loadConfig();
});
function urlRow(val){
  const div=document.createElement('div');div.className='urlrow';
  div.innerHTML=`<span class="dot"></span><input value="${esc(val)}" placeholder="https://..."><button class="btn" data-act="test">Tekshirish</button><button class="btn ghost" data-act="del">✕</button>`;
  div.querySelector('[data-act=del]').onclick=()=>div.remove();
  div.querySelector('[data-act=test]').onclick=async()=>{
    const url=div.querySelector('input').value.trim(),dot=div.querySelector('.dot'),btn=div.querySelector('[data-act=test]');
    dot.className='dot';btn.textContent='...';
    const r=await fetch('/api/v1/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(r=>r.json());
    btn.textContent='Tekshirish';dot.className='dot '+(r.ok?'ok':'err');toast(r.ok?('OK — HTTP '+r.status):('Xato: '+r.error));
  };
  return div;
}
async function loadConfig(){
  const c=await api('/api/v1/config');const box=$('#urls');box.innerHTML='';
  (c.forward_urls||[]).forEach(u=>box.appendChild(urlRow(u)));
}
$('#addurl').onclick=()=>{$('#urls').appendChild(urlRow($('#newurl').value.trim()));$('#newurl').value='';};
$('#newurl').onkeydown=e=>{if(e.key==='Enter')$('#addurl').click();};
$('#save').onclick=async()=>{
  const urls=[...document.querySelectorAll('#urls input')].map(i=>i.value.trim()).filter(Boolean);
  const r=await fetch('/api/v1/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({forward_urls:urls})}).then(r=>r.json());
  await loadConfig();toast('Saqlandi ('+(r.forward_urls||[]).length+' ta manzil)');
};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200);}
load();loadStats();
</script>
</body>
</html>'''
