FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 app
WORKDIR /srv/backend

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ /srv/backend/
COPY dolphin/ /srv/dolphin/
COPY scripts/ /srv/scripts/
ENV DOLPHIN_ASSETS_PATH=/srv/dolphin STORAGE_PATH=/data/storage
RUN mkdir -p /data/storage && chown -R app:app /data

USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
