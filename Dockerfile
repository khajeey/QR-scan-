# QR Cloud — FastAPI scan server
FROM python:3.12-slim

# Logarni real vaqtda ko'rsatish, .pyc fayllarni yozmaslik
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Dependency'larni alohida layerga (cache uchun)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ilova kodi
COPY api ./api

EXPOSE 8000

# Healthcheck — /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health'); sys.exit(0)" || exit 1

# $PORT env'dan o'qiladi (Render/Railway/Fly mos)
CMD ["sh", "-c", "uvicorn api.index:app --host 0.0.0.0 --port ${PORT:-8000}"]
