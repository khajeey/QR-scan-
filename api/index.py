import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
IMB_BASE = os.environ.get("IMB_BASE", "https://imb.imbtruck.uz").rstrip("/")
TASHKENT = timezone(timedelta(hours=5))

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


def _body_qr_scan(scan):
    return {
        "event": "qr_scan",
        "data": scan.get("code"),
        "device": scan.get("device"),
        "source": scan.get("source"),
        "scanned_at": scan.get("scanned_at") or scan.get("created_at"),
    }


def _body_hikvision(scan):
    status = scan.get("status") or "received"
    attendance = status if status in ("checkIn", "checkOut") else "checkIn"

    event = {"attendanceStatus": attendance}
    if code := str(scan.get("code") or ""):
        event["employeeNoString"] = code
    if name := str(scan.get("name") or ""):
        event["name"] = name
    if device := str(scan.get("device") or ""):
        event["deviceName"] = device

    body = {
        "AccessControllerEvent": event,
        "dateTime": scan.get("scanned_at") or scan.get("created_at") or "",
    }
    if ip := str(scan.get("ip_address") or ""):
        body["ipAddress"] = ip
    if mac := str(scan.get("mac_address") or ""):
        body["macAddress"] = mac
    if source := str(scan.get("source") or scan.get("device") or ""):
        body["deviceID"] = source
    return body


def _to_local_iso(iso):
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TASHKENT).isoformat()
    except Exception:
        return iso


def _imb_event_to_scan(ev):
    return {
        "code": ev.get("uid"),
        "name": ev.get("name"),
        "status": "checkIn" if ev.get("type") == "in" else "checkOut",
        "scanned_at": _to_local_iso(ev.get("t")),
        "source": "imb",
        "device": "imb",
    }


def _is_hikvision(url):
    return "/attendance/hikvision" in url


def _body_for_url(url, scan):
    if _is_hikvision(url):
        return _body_hikvision(scan)
    return _body_qr_scan(scan)


def _post_scan(client, url, scan, timeout=8):
    payload = _body_for_url(url, scan)
    if _is_hikvision(url):
        return client.post(
            url,
            data={"event_log": json.dumps(payload, ensure_ascii=False)},
            timeout=timeout,
        )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )


def forward_scan(scan, urls):
    for url in urls:
        try:
            with httpx.Client(timeout=8) as c:
                _post_scan(c, url, scan)
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
    test_scan = {"code": "TEST-123", "device": "cloud-test",
                 "source": "cloud-test", "status": "received",
                 "scanned_at": datetime.now(timezone.utc).isoformat()}
    try:
        with httpx.Client(timeout=10) as c:
            r = _post_scan(c, url, test_scan, timeout=10)
        return {"ok": True, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/imb/attendance")
def imb_attendance(page: int = 1, page_size: int = 50):
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    url = IMB_BASE + "/api/v1/attendance/daily-attendance/?page=" + str(page) + "&page_size=" + str(page_size)
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        return JSONResponse(r.json())
    except httpx.HTTPError as exc:
        return _err(502, "IMB o'qish xatosi: " + str(exc))


def _imb_at_work_users():
    url = IMB_BASE + "/api/v1/attendance/at-work-users/"
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


@app.get("/api/v1/imb/sync")
def imb_sync(src: str = "unknown"):
    try:
        users = _imb_at_work_users()
    except httpx.HTTPError as exc:
        return _err(502, "IMB at-work o'qish xatosi: " + str(exc))
    cur, names = {}, {}
    for u in users:
        uid = str(u.get("id"))
        cur[uid] = bool(u.get("at_work"))
        names[uid] = u.get("full_name") or ("ID " + uid)
    if not _configured():
        return {"stored": False, "users": users, "events": [], "new": 0}
    snap = db_get_setting("imb_aw_snap", None)
    events = db_get_setting("imb_events", [])
    if not isinstance(events, list):
        events = []
    now = datetime.now(timezone.utc).isoformat()
    new_events = []
    if isinstance(snap, dict):
        for uid, val in cur.items():
            prev = snap.get(uid)
            if prev is None or val == prev:
                continue
            new_events.append({"t": now, "name": names.get(uid), "type": "in" if val else "out", "uid": uid})
    if new_events:
        events = (events + new_events)[-2000:]
        fwd = db_get_setting("forward_urls", [])
        urls = [u for u in fwd if isinstance(u, str) and u.strip()] if isinstance(fwd, list) else []
        if urls:
            for ev in new_events:
                forward_scan(_imb_event_to_scan(ev), urls)
    if new_events or not isinstance(snap, dict):
        try:
            db_set_setting("imb_aw_snap", cur)
            if new_events:
                db_set_setting("imb_events", events)
        except httpx.HTTPError:
            pass
    try:
        db_set_setting("imb_last_sync", {"t": now, "src": src})
    except httpx.HTTPError:
        pass
    return {"stored": True, "users": users, "events": events, "new": len(new_events), "src": src}


def _imb_daily_sessions(max_pages=10):
    sessions = []
    page, total = 1, 1
    while page <= total and page <= max_pages:
        url = IMB_BASE + "/api/v1/attendance/daily-attendance/?page=" + str(page) + "&page_size=200"
        with httpx.Client(timeout=12) as c:
            r = c.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
        for row in d.get("results", []):
            sessions.append(row)
        total = d.get("total_pages") or 1
        page += 1
    return sessions


@app.get("/api/v1/imb/state")
def imb_state():
    try:
        users = _imb_at_work_users()
    except httpx.HTTPError as exc:
        return _err(502, "IMB at-work o'qish xatosi: " + str(exc))
    by_id = {}
    try:
        for s in _imb_daily_sessions():
            u = s.get("user") or {}
            by_id[str(u.get("id"))] = s
    except httpx.HTTPError:
        by_id = {}
    rows = []
    for u in users:
        uid = str(u.get("id"))
        s = by_id.get(uid, {})
        rows.append({
            "id": u.get("id"),
            "name": u.get("full_name") or ("ID " + uid),
            "at_work": bool(u.get("at_work")),
            "entry_time": s.get("entry_time") or "",
            "duration": s.get("duration") or "",
        })
    events = db_get_setting("imb_events", []) if _configured() else []
    if not isinstance(events, list):
        events = []
    last_sync = db_get_setting("imb_last_sync", None) if _configured() else None
    return {"rows": rows, "events": events, "inside": sum(1 for r in rows if r["at_work"]), "last_sync": last_sync}


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
  .pill.ok{background:rgba(63,185,80,.15);color:var(--ok)}.pill.received{background:rgba(62,166,255,.15);color:var(--accent)}.pill.error{background:rgba(248,81,73,.15);color:var(--err)}.pill.warn{background:rgba(210,153,34,.15);color:var(--warn)}.pill.in{background:rgba(63,185,80,.15);color:var(--ok)}
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
  .subtabs{display:flex;gap:8px;margin-bottom:16px}
  .btn.subtab{padding:8px 18px}
  .btn.subtab.active{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:12px 20px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;max-width:92vw;text-align:center}
  .toast.show{opacity:1}
  .tscroll{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px}
  .tscroll table{min-width:560px}
  @media(max-width:640px){
    header{padding:12px 16px;gap:10px}
    header h1{font-size:15px}
    header .sub{font-size:11px}
    nav{padding:0 8px;overflow-x:auto}
    nav button{padding:12px 12px;white-space:nowrap;font-size:13px}
    main{padding:14px}
    th,td{padding:9px 10px}
    .card{padding:16px}
    .toolbar{gap:8px}
    .subtabs{gap:6px}
    .btn.subtab{flex:1;padding:8px 10px}
    .urlrow{flex-wrap:wrap}
    .urlrow input{order:-1;flex-basis:100%;min-width:0}
  }
</style>
</head>
<body>
<header>
  <div class="logo">QR</div>
  <div><h1>IMB Davomat Panel</h1><div class="sub">Jonli kirish-chiqish (face-id / barmoq izi)</div></div>
  <div class="spacer"></div>
</header>
<nav>
  <button data-tab="imb" class="active">Davomat (IMB)</button>
  <button data-tab="settings">Sozlamalar</button>
</nav>
<main>

  <section id="tab-imb">
    <div class="toolbar">
      <input class="grow" id="imb-q" placeholder="Ism yoki familya bo'yicha qidirish...">
      <span class="muted" id="imb-updated"></span>
      <span class="muted" id="imb-sync"></span>
      <label class="chk"><input type="checkbox" id="imb-auto" checked> Avto (1 daq)</label>
      <button class="btn" id="imb-refresh">Yangilash</button>
    </div>

    <div class="subtabs">
      <button class="btn subtab active" data-sub="dav">Davomat</button>
      <button class="btn subtab" data-sub="tarix">Tarix</button>
    </div>

    <div id="imb-view-dav">
      <div class="tscroll"><table>
        <thead><tr><th style="width:55px">#</th><th>Xodim</th><th style="width:185px">Bugun birinchi kirgan</th><th style="width:130px">Davomiylik</th><th style="width:170px">Holat (hozir)</th></tr></thead>
        <tbody id="imb-rows"></tbody>
      </table></div>
      <div id="imb-empty" class="empty" style="display:none">Ma'lumot yo'q.</div>
    </div>

    <div id="imb-view-tarix" style="display:none">
      <p class="desc" style="color:var(--muted);font-size:13px;margin:0 0 12px">Har bir kirish/chiqish o'zgarishi yoziladi (panel ochiq turganda har daqiqa tekshiriladi). Bir odam necha marta kirib-chiqsa, hammasi alohida qator bo'lib qoladi — oxirgisi bilan almashtirilmaydi. Vaqt — o'zgarish aniqlangan moment (±1 daqiqa).</p>
      <div class="tscroll"><table>
        <thead><tr><th style="width:55px">#</th><th style="width:200px">Vaqt</th><th>Xodim</th><th style="width:170px">Hodisa</th></tr></thead>
        <tbody id="imb-ev-rows"></tbody>
      </table></div>
      <div id="imb-ev-empty" class="empty" style="display:none">Ma'lumot yo'q.</div>
      <div class="pager"><span id="imb-ev-pginfo"></span></div>
    </div>
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

    <div class="card">
      <h2>API (GET + POST)</h2>
      <p class="desc"><b>GET</b> <code>/api/v1/scans</code> — ma'lumotni olish. Filtrlar: <code>?from=2026-06-01&amp;to=2026-06-10&amp;q=...&amp;date=...&amp;source=...</code><br>
      <b>POST</b> <code>/api/v1/scans</code> — yangi yozuv qo'shish. Tana (JSON): <code>{"code":"ABC123","device":"...","source":"..."}</code> yoki ro'yxat.</p>
      <div id="endpoints"></div>
      <div class="hint">Bu manzillarni istalgan qurilma/serverdan ishlatish mumkin (internet orqali ochiq).</div>
    </div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
const api=(p)=>fetch(p).then(r=>r.json());
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  $('#tab-imb').style.display=b.dataset.tab==='imb'?'':'none';
  $('#tab-settings').style.display=b.dataset.tab==='settings'?'':'none';
  if(b.dataset.tab==='settings')loadConfig();
  if(b.dataset.tab==='imb')loadImb();
});

function fmtD(d){return d.toLocaleString('uz',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function pad2(n){return (n<10?'0':'')+n;}
function parseImb(s){if(!s)return null;const d=new Date(String(s).replace(' ','T'));return isNaN(d)?null:d;}
function fmtImb(s){const d=parseImb(s);return d?fmtD(d):esc(s||'');}
function durSecs(dur){const m=String(dur||'').match(/^(\d+):(\d{2}):(\d{2})$/);return m?(+m[1])*3600+(+m[2])*60+(+m[3]):0;}
function imbExit(entry,dur){const sec=durSecs(dur);const d=parseImb(entry);if(!d||sec<=0)return null;return new Date(d.getTime()+sec*1000);}
let imbBusy=false,imbRows=[],imbEvents=[],imbLastOk=0;
function fetchJSON(url,ms){
  const ctl=new AbortController();
  const tid=setTimeout(()=>ctl.abort(),ms||12000);
  return fetch(url,{signal:ctl.signal,cache:'no-store'})
    .then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .finally(()=>clearTimeout(tid));
}
function setImbStatus(msg,isErr){const el=$('#imb-updated');el.textContent=msg;el.style.color=isErr?'var(--err)':'var(--muted)';}
function renderSync(ls){const se=$('#imb-sync');if(!se)return;if(!ls||!ls.t){se.textContent='';return;}
  const age=Math.round((Date.now()-new Date(ls.t).getTime())/1000);
  const who=(ls.src&&ls.src!=='unknown')?(' ['+ls.src+']'):'';
  if(age>180){se.textContent="⚠ Avto-yozuv to'xtagan ("+(age>=3600?Math.round(age/60)+'daq':age+'s')+" oldin)"+who;se.style.color='var(--err)';}
  else{se.textContent='· avto-yozuv: '+age+'s oldin'+who;se.style.color='var(--ok)';}
}
async function loadImb(){
  if(imbBusy)return;imbBusy=true;
  if(!imbLastOk)setImbStatus('Yuklanmoqda...',false);
  try{
    const d=await fetchJSON('/api/v1/imb/state',12000);
    const rows=(d.rows||[]).map(r=>({name:r.name||('ID '+r.id),at_work:!!r.at_work,entry:r.entry_time||'',dur:r.duration||''}));
    rows.sort((a,b)=>(b.at_work-a.at_work)||String(b.entry||'').localeCompare(String(a.entry||'')));
    imbRows=rows;
    imbEvents=(d.events||[]).slice().reverse();
    renderImbDav();renderImbEv();
    imbLastOk=Date.now();
    const insideN=(d.inside!=null)?d.inside:rows.filter(r=>r.at_work).length;
    const now=new Date();
    setImbStatus('Yangilandi '+pad2(now.getHours())+':'+pad2(now.getMinutes())+':'+pad2(now.getSeconds())+' · ichkarida '+insideN,false);
    renderSync(d.last_sync);
  }catch(e){
    const secs=imbLastOk?Math.round((Date.now()-imbLastOk)/1000):0;
    const reason=(e&&e.name==='AbortError')?'javob kechikdi':(e&&e.message||e);
    setImbStatus(imbLastOk?("⚠ Yangilab bo'lmadi — "+secs+"s oldingi ma'lumot ("+reason+")"):("⚠ Yuklab bo'lmadi: "+reason),true);
  }finally{imbBusy=false;}
}
function renderImbDav(){
  const q=($('#imb-q').value||'').trim().toLowerCase();
  const rows=q?imbRows.filter(r=>String(r.name).toLowerCase().includes(q)):imbRows;
  const tb=$('#imb-rows');tb.innerHTML='';$('#imb-empty').style.display=rows.length?'none':'';
  rows.forEach((r,i)=>{
    const status=r.at_work?'<span class="pill in">🟢 Ichkarida</span>':'<span class="pill ok">Tashqarida</span>';
    const entry=r.entry?fmtImb(r.entry):'<span class="muted">—</span>';
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="muted">${i+1}</td><td>${esc(r.name)}</td><td>${entry}</td><td class="muted">${esc(r.dur||'')}</td><td>${status}</td>`;
    tb.appendChild(tr);
  });
}
function renderImbEv(){
  const q=($('#imb-q').value||'').trim().toLowerCase();
  const events=q?imbEvents.filter(e=>String(e.name).toLowerCase().includes(q)):imbEvents;
  const eb=$('#imb-ev-rows');eb.innerHTML='';$('#imb-ev-empty').style.display=events.length?'none':'';
  events.forEach((e,i)=>{
    const badge=e.type==='in'?'<span class="pill in">🟢 KIRDI</span>':'<span class="pill warn">🔴 CHIQDI</span>';
    const d=new Date(e.t);
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="muted">${i+1}</td><td>${isNaN(d)?esc(e.t):fmtD(d)}</td><td>${esc(e.name)}</td><td>${badge}</td>`;
    eb.appendChild(tr);
  });
  $('#imb-ev-pginfo').textContent=q?`${events.length} / ${imbEvents.length} ta o'tish`:`Jami ${imbEvents.length} ta yozilgan o'tish`;
  if(imbEvents.length===0)$('#imb-ev-empty').textContent="Hozircha yozilgan o'tish yo'q — birinchi o'zgarish (kirish/chiqish) ro'y berganda paydo bo'ladi.";
  else if(events.length===0)$('#imb-ev-empty').textContent="Qidiruvga mos o'tish topilmadi.";
}
$('#imb-refresh').onclick=()=>loadImb();
$('#imb-q').oninput=()=>{renderImbDav();renderImbEv();};
document.querySelectorAll('.subtab').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.subtab').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  $('#imb-view-dav').style.display=b.dataset.sub==='dav'?'':'none';
  $('#imb-view-tarix').style.display=b.dataset.sub==='tarix'?'':'none';
});
setInterval(()=>{if($('#imb-auto').checked && $('#tab-imb').style.display!=='none'){loadImb();}},60000);
function urlRow(val){
  const div=document.createElement('div');div.className='urlrow';
  div.innerHTML=`<span class="dot"></span><input value="${esc(val)}" placeholder="https://..."><button class="btn" data-act="test">Tekshirish</button><button class="btn ghost" data-act="del">✕</button>`;
  div.querySelector('[data-act=del]').onclick=()=>div.remove();
  div.querySelector('[data-act=test]').onclick=async()=>{
    const url=div.querySelector('input').value.trim(),dot=div.querySelector('.dot'),btn=div.querySelector('[data-act=test]');
    dot.className='dot';btn.textContent='...';
    const r=await fetch('/api/v1/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(r=>r.json());
    btn.textContent='Tekshirish';
    const good=r.ok && r.status>=200 && r.status<400;
    dot.className='dot '+(good?'ok':'err');
    toast(good?('OK — HTTP '+r.status):(r.ok?('Server xatosi — HTTP '+r.status):('Xato: '+r.error)));
  };
  return div;
}
async function loadConfig(){
  const c=await api('/api/v1/config');const box=$('#urls');box.innerHTML='';
  (c.forward_urls||[]).forEach(u=>box.appendChild(urlRow(u)));
  loadEndpoints();
}
function endpointRow(url){
  const div=document.createElement('div');div.className='urlrow';
  div.innerHTML=`<span class="dot ok"></span><input readonly value="${esc(url)}"><button class="btn" data-act="copy">Nusxa</button><button class="btn" data-act="open">Ochish</button>`;
  div.querySelector('[data-act=copy]').onclick=()=>{navigator.clipboard.writeText(url);toast('Nusxalandi');};
  div.querySelector('[data-act=open]').onclick=()=>window.open(url,'_blank');
  return div;
}
function loadEndpoints(){
  const box=$('#endpoints');box.innerHTML='';const base=location.origin;
  box.appendChild(endpointRow(base+'/api/v1/scans'));
  box.appendChild(endpointRow(base+'/api/v1/scans.csv'));
}
$('#addurl').onclick=()=>{$('#urls').appendChild(urlRow($('#newurl').value.trim()));$('#newurl').value='';};
$('#newurl').onkeydown=e=>{if(e.key==='Enter')$('#addurl').click();};
$('#save').onclick=async()=>{
  const urls=[...document.querySelectorAll('#urls input')].map(i=>i.value.trim());
  const pending=$('#newurl').value.trim();if(pending)urls.push(pending);
  const clean=urls.filter(Boolean);
  const r=await fetch('/api/v1/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({forward_urls:clean})}).then(r=>r.json());
  $('#newurl').value='';
  await loadConfig();toast('Saqlandi ('+(r.forward_urls||[]).length+' ta manzil)');
};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200);}
loadImb();
</script>
</body>
</html>'''
