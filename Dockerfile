FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 SPHEREX_VIEWER_CACHE=/data
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY spherex_viewer ./spherex_viewer
COPY server.py LICENSE NOTICE ./
COPY web ./web
VOLUME /data
EXPOSE 8000
# One worker on purpose: jobs are kept in memory. Most hosts set $PORT; default 8000.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
