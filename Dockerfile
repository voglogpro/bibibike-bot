FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

# BotHost can mount /app again at container startup. Keep application code
# outside that mount so a stale or incomplete runtime checkout cannot hide it.
WORKDIR /usr/src/bibibike

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html launcher.py ./

# Make an incomplete Git checkout fail during the build, not silently at runtime.
RUN test -s main.py \
    && test -s index.html \
    && test -s launcher.py \
    && python -m py_compile main.py launcher.py \
    && BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk PORT=3000 \
       python -c "import main; print('BibiBike build import check: OK')"

RUN mkdir -p /app/data && chmod 0777 /app/data

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + (os.getenv('PORT') or os.getenv('WEB_PORT') or '3000') + '/health', timeout=3)"

CMD ["python", "-u", "/usr/src/bibibike/launcher.py"]
