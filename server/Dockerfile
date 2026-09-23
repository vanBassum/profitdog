# The server: the API, the built UI, and the agent download.
#
# Two stages, because the UI is built with a toolchain the runtime has no use
# for. Node compiles the bundle; the final image is Python with a directory of
# static files in it and no node_modules anywhere.

# --- the UI ----------------------------------------------------------------
FROM node:22-alpine AS ui

WORKDIR /ui
RUN corepack enable

# Lockfile first, so a change to a component does not re-resolve every
# dependency. `--frozen-lockfile` makes a lockfile that disagrees with
# package.json a build failure rather than a silent upgrade.
COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY ui/ ./
RUN pnpm build


# --- the server ------------------------------------------------------------
FROM python:3.13-slim AS server

# Which agent build this image was released alongside, so "Get the agent" can
# say "Update to 1.4.0" instead of printing a number and leaving the reader to
# compare it. The workflow builds this image and the EXE from one tag, so the
# answer is known at build time and needs no call to GitHub at runtime. Unset
# on a local build, which makes the page say nothing about currency rather
# than something wrong.
ARG PROFITDOG_AGENT_VERSION=""
ENV PROFITDOG_AGENT_VERSION=$PROFITDOG_AGENT_VERSION

# PYTHONUNBUFFERED so container logs appear as they happen rather than when a
# buffer fills, which for a service that logs one line a minute is never.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PROFITDOG_UI=/app/ui \
    PROFITDOG_AGENT_EXE=/app/agent/profitdog.exe \
    PYTHONPATH=/app/server:/app/protocol

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# The two Python source roots, and nothing else. `agent/` is deliberately
# absent: the server has no business importing it, and leaving it out means a
# stray import fails in CI rather than in production.
COPY server/profitdog_server/ server/profitdog_server/
COPY protocol/ protocol/

COPY --from=ui /ui/dist/ ui/

# Where a built agent is mounted or copied to be served at /download. Created
# empty so the server starts and says "no agent build on this server" rather
# than failing, which is the right behaviour on a host that only serves the UI.
RUN mkdir -p /app/agent

# Unprivileged: nothing here writes to its own filesystem, and the one thing
# that does persist is in PostgreSQL.
RUN useradd --create-home --uid 10001 profitdog \
    && chown -R profitdog:profitdog /app
USER profitdog

EXPOSE 5174

# No healthcheck on /api/health: it needs a session, and a probe that has to
# sign in is a probe that will one day fail for a reason unrelated to health.
# `/login` is served to anyone and touches the same process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5174/login', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "profitdog_server", "--host", "0.0.0.0", "--port", "5174"]
