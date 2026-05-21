# Mean Well NPB charger web UI — small Python image.
#
# Built on python:3.12-slim. SocketCAN is a kernel feature, so the
# container just needs --network=host (or the can0 interface mapped
# in) and the python-can package; no extra apt packages required.

# Pinned to a specific patch release so rebuilds are reproducible
# without the 'latest' tag drift.  Bump deliberately when upgrading.
FROM python:3.12.7-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so we get a cached layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + Flask templates/static (small enough to copy everything;
# .dockerignore trims fat).  Both `templates/` and `static/` are
# required: Flask's render_template() and url_for('static', ...) look
# for them relative to the app's root_path (i.e. /app).
COPY charger_app.py charger_web.py ./
COPY templates/ ./templates/
COPY static/    ./static/

# Non-root user — CAN access on Linux works fine for any uid as long
# as the interface is up; only `ip link set` needs root, and that's
# the host's job (or done in the compose file's init).
RUN useradd --create-home --uid 1000 charger && chown -R charger:charger /app
USER charger

EXPOSE 8080

# Default to demo so `docker run` Just Works without any CAN setup;
# override the CMD to talk to real hardware (see docker-compose.yml).
CMD ["python", "charger_web.py", "--demo", "--host", "0.0.0.0", "--port", "8080"]
