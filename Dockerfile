# Ventoryn (dashboard + worker) — production image.
#
# Built and run by deploy/docker/ventoryn.sh; see docs/deploy-docker.md.
# Code is baked in; everything stateful (data/, reports/) and every
# secret (.env.ventoryn) stays on the host and is mounted at run time.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: the "Video render" job.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cached until pyproject.toml changes). Runtime deps
# + the llm / market / video extras; postgres is left out (the deploy
# uses the SQLite state file).
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; deps = list(p['dependencies']) + [d for e in ('llm', 'market', 'video', 'screenshot') for d in p['optional-dependencies'][e]]; open('/tmp/requirements.txt', 'w').write(chr(10).join(deps))" \
 && pip install -r /tmp/requirements.txt

# Chromium for the website-snapshot screenshot (runs WITH its sandbox;
# see ventoryn/site_screenshot.py). Browsers in a shared path so the
# non-root app user can run them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright

COPY . .

# Every Python process in the container (incl. `docker exec ... python`
# maintenance scripts) loads the mounted secrets file like the app does.
RUN cp deploy/docker/sitecustomize.py     "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/sitecustomize.py"

# Run as the host user that owns data/ and reports/ (uid 1000 on the
# server), so files written by the container keep their owner.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "${APP_GID}" app \
 && useradd -u "${APP_UID}" -g "${APP_GID}" -m -d /home/app app \
 && mkdir -p /app/data /app/reports \
 && chown -R app:app /app/data /app/reports
USER app
ENV HOME=/home/app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1

# 127.0.0.1 only: the Cloudflare tunnel reaches it on localhost (host
# networking), and nothing on the LAN can bypass the tunnel.
CMD ["python", "launch.py", "all", "--host", "127.0.0.1", "--port", "8000"]
