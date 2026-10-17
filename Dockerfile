# ─────────────────────────────────────────────
# BlackSwanSI: Python 3.12 slim image
# ─────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /bot

# gosu lets the entrypoint fix disk permissions as root, then run the bot as an unprivileged user
RUN apt-get update && apt-get install -y --no-install-recommends gcc gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 bot && mkdir -p data \
    && chown -R bot:bot /bot && chmod +x /bot/docker-entrypoint.sh

EXPOSE 8000

# Port comes from $WEB_PORT or $PORT (Render injects PORT)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % (os.getenv('WEB_PORT') or os.getenv('PORT') or '8000'))" || exit 1

ENTRYPOINT ["/bot/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
