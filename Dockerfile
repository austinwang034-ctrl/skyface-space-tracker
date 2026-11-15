# Works on Render (free plan included) and any other Docker host.
FROM python:3.12-slim

# Run as an ordinary user, not root; everything the app writes lives in that user's home.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SPHEREX_VIEWER_CACHE=/home/user/cache
WORKDIR /home/user/app

COPY --chown=user requirements-server.txt .
RUN pip install --no-cache-dir --user -r requirements-server.txt

# Everything else in the repository: the server, the page, and any saved images in saved/.
COPY --chown=user . .
# Stop the build here, with a clear message, if an upload lost or renamed a folder.
RUN python -c "import pathlib, sys; missing = [p for p in ('server.py', 'spherex_viewer/tracker.py', 'web/index.html', 'web/app.js', 'web/vendor/plotly.min.js', 'LICENSE', 'NOTICE') if not pathlib.Path(p).exists()]; sys.exit('Missing from the repository: ' + ', '.join(missing)) if missing else print('All site files present.')" \
    && python -c "import server; print('Server starts. Saved objects:', [s['id'] for s in server.STORE.summaries()])"

# Hosts such as Render set $PORT and the server listens there; 7860 is only the fallback.
EXPOSE 7860
# One worker on purpose: jobs are kept in memory.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --proxy-headers --forwarded-allow-ips=*"]
