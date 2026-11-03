# ---- Frontend build ----
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Backend ----
# The official Playwright image ships Chromium plus every system library the
# meeting bot needs; its version must match playwright in requirements.deploy.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
WORKDIR /app

COPY backend/requirements.deploy.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# The built SPA; app/main.py serves it when this directory exists.
COPY --from=frontend /fe/dist ./static

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
