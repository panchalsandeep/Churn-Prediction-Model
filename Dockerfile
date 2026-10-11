# One image serves the API and the built frontend. Heavy ML work belongs in a container, not a serverless function.
FROM node:22-slim AS web
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
COPY static ./static
COPY public ./public
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data PORT=8080 PYTHONPATH=/app/api
WORKDIR /app
COPY requirements.txt requirements-extras.txt ./
RUN pip install -r requirements.txt -r requirements-extras.txt gunicorn
COPY api ./api
COPY --from=web /app/dist ./dist
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080
# Web: threads keep one copy of each loaded model per process. Jobs run in-process by default (EMBEDDED_WORKER=1). To scale out,
# set EMBEDDED_WORKER=0 on web containers and run this same image with:  python -m churn.worker
CMD ["sh", "-c", "exec gunicorn --chdir api --workers 2 --threads 8 --timeout 600 --bind 0.0.0.0:${PORT} index:app"]
