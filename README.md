# QR Cloud — central scan server (Vercel + Supabase)

Receives QR/barcode scans from every Windows scanner PC and shows them in one dashboard.
The local Windows app (`qrcode-catcher`) just points its **webhook URL** at this server.

```
[Windows PC + scanner]  --POST scan-->  [QR Cloud on Vercel]  <--browser/API--  you
   scaner.py (local)                      dashboard + GET/POST API + Supabase DB
```

## Endpoints
- `GET  /`                    — dashboard (all PCs)
- `POST /api/v1/scans`        — receive scan(s). Body: `{"code":"ABC","device":"...","scanned_at":"..."}` or a list. Alias: `POST /api/v1/fdb/backup/`
- `GET  /api/v1/scans`        — list. Filters: `?q=&from=YYYY-MM-DD&to=YYYY-MM-DD&date=&source=&limit=&offset=`
- `GET  /api/v1/scans.csv`    — CSV export
- `GET  /api/v1/stats`        — `{total, today}`
- `GET  /health`              — health + whether env is configured
- `GET  /api/docs`            — interactive API docs

Tag which PC a scan came from by adding `?source=NAME` to that PC's webhook URL, e.g.
`https://YOURAPP.vercel.app/api/v1/scans?source=kassa-1`

---

## 1) Supabase (database)
1. Create a project at https://supabase.com (free).
2. Open **SQL Editor** → paste the contents of `schema.sql` → **Run**.
3. **Project Settings → API**, copy:
   - **Project URL**            → `SUPABASE_URL`
   - **service_role** secret key → `SUPABASE_SERVICE_KEY`  (secret — server only, never in a browser)

## 2) Hosting

### Option A — Docker (har qanday serverda: VPS, Render, Railway, Fly.io)
Ushbu papkada `Dockerfile`, `docker-compose.yml` va `.env.example` bor.

```bash
# 1) Env faylni tayyorlang
cp .env.example .env          # SUPABASE_URL va SUPABASE_SERVICE_KEY ni to'ldiring

# 2) Build + ishga tushirish (docker compose)
docker compose up -d --build

# Tekshirish:
curl http://localhost:8000/health
# Dashboard:  http://localhost:8000/
```

Yoki sof Docker bilan:
```bash
docker build -t qr-cloud .
docker run -d --name qr-cloud -p 8000:8000 --env-file .env qr-cloud
```

Render / Railway / Fly.io kabi platformalarda: repoga push qiling, ular `Dockerfile` ni avtomatik aniqlaydi. Env o'zgaruvchilarni (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, ixtiyoriy `INGEST_TOKEN`) platforma panelidan kiriting. Port `$PORT` env'dan o'qiladi.

### Option B — Vercel (hosting)
**Vercel CLI (from this folder):**
```bash
npm i -g vercel
vercel            # first run: link/create project
vercel env add SUPABASE_URL          # paste Project URL
vercel env add SUPABASE_SERVICE_KEY  # paste service_role key
# optional shared secret to protect POSTs:
vercel env add INGEST_TOKEN          # e.g. a long random string
vercel --prod
```

**GitHub orqali:** push this folder to a repo → vercel.com → **Add New → Project → Import** →
add the same Environment Variables → **Deploy**.

## 3) Point the Windows app at it
In the local panel (`http://localhost:8765`) → **Sozlamalar** → add the webhook URL:
```
https://YOURAPP.vercel.app/api/v1/scans?source=THIS-PC-NAME
```
(If you set `INGEST_TOKEN`, append `&token=YOURTOKEN`.) Save. Done — scans now flow to the cloud.

## Notes
- Env vars: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (required), `INGEST_TOKEN` (optional, protects POST).
- The service_role key bypasses row-level security and is used only server-side; the browser never sees it.
- Local quick test: `pip install -r requirements.txt`, set the env vars, `uvicorn api.index:app --reload`.
