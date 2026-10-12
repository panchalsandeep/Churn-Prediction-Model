# One image serves the API and the built frontend. Heavy ML work belongs in a container, not a serverless function.
FROM node:22-slim AS web
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
COPY static ./static
COPY public ./public
# Optional overrides at image build time (Railway: set as Docker build args / variables available at build).
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
ARG VITE_FIREBASE_STORAGE_BUCKET
ARG VITE_FIREBASE_MESSAGING_SENDER_ID
ARG VITE_FIREBASE_APP_ID
ARG VITE_FIREBASE_MEASUREMENT_ID
ENV VITE_FIREBASE_API_KEY=$VITE_FIREBASE_API_KEY \
    VITE_FIREBASE_AUTH_DOMAIN=$VITE_FIREBASE_AUTH_DOMAIN \
    VITE_FIREBASE_PROJECT_ID=$VITE_FIREBASE_PROJECT_ID \
    VITE_FIREBASE_STORAGE_BUCKET=$VITE_FIREBASE_STORAGE_BUCKET \
    VITE_FIREBASE_MESSAGING_SENDER_ID=$VITE_FIREBASE_MESSAGING_SENDER_ID \
    VITE_FIREBASE_APP_ID=$VITE_FIREBASE_APP_ID \
    VITE_FIREBASE_MEASUREMENT_ID=$VITE_FIREBASE_MEASUREMENT_ID
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data PORT=8080 PYTHONPATH=/app/api \
    WEB_CONCURRENCY=1 GUNICORN_THREADS=8 GUNICORN_TIMEOUT=600 EMBEDDED_WORKER=1
WORKDIR /app
COPY requirements.txt requirements-extras.txt ./
RUN pip install -r requirements.txt -r requirements-extras.txt gunicorn
COPY api ./api
COPY --from=web /app/dist ./dist
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080
# Web: threads keep one copy of each loaded model per process. Jobs run in-process by default (EMBEDDED_WORKER=1).
# Raise WEB_CONCURRENCY only when the instance has enough RAM (each worker loads sklearn models).
# To scale out: EMBEDDED_WORKER=0 on web and run the same image with `python -m churn.worker`.
CMD ["sh", "-c", "exec gunicorn --chdir api --workers ${WEB_CONCURRENCY:-1} --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-600} --bind 0.0.0.0:${PORT:-8080} index:app"]
