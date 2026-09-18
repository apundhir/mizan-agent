# syntax=docker/dockerfile:1

# Pinned by digest as well as by patch tag. "Runnable on a machine that is not this one" is a claim
# about a specific set of bytes, and the floating `3.12-slim` tag moves under you between builds.
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Two things are settled by this one install, and they pull in the same direction.
#
# 1. It is editable, and from /app, because every path default in this codebase is derived from
#    `__file__`: `src/tda/cli.py` walks up two levels for `policy.yaml`, `corpus/demo/submission`
#    and `artifacts/`, `provider/replay.py` walks up four for `tests/cassettes/`, and
#    `agents/reviewer_assist.py` walks up three for `docs/01-definitions.md`. A wheel installed
#    into site-packages resolves all of those under /usr/local/lib, where none of them exist, so
#    `mizan run` with no arguments would fail on a path rather than on anything real.
# 2. It caches. Hatchling needs `src/tda` to exist to build the project, but not to contain the
#    shipped code, so a placeholder package keeps this layer keyed on `pyproject.toml` alone. The
#    editable install points at /app/src, so the real source copied in below replaces the
#    placeholder with no reinstall and no cache miss on the dependency graph.
#
# The extras: `extract` (pdfplumber, openpyxl) and `outputs` (python-docx, openpyxl) are the
# pipeline, `review` (streamlit, pillow) is the screen, and `agents` is required rather than
# optional because `tda.graph.build` imports langgraph at module scope. `agents` also drags in the
# `anthropic` client, which is imported lazily and never reached: the default provider is `replay`
# (policy.yaml `model.provider`) and this image carries no key. `dev` and `datagen` are build and
# test tooling and are deliberately absent here; see the `toolchain` stage.
COPY pyproject.toml ./
RUN mkdir -p src/tda \
 && touch src/tda/__init__.py \
 && pip install -e ".[extract,outputs,agents,review]"

FROM deps AS app

# Bind mounts are the reason this is a build argument rather than a fixed uid. A container writing
# `artifacts/` as uid 1000 into a host directory owned by uid 501 fails on the first run, so the
# image is built for the uid that will own the mount:
#   APP_UID=$(id -u) APP_GID=$(id -g) docker compose -f docker/compose.yaml build
# Docker Desktop on macOS virtualises bind-mount ownership and the default works there unchanged.
ARG APP_UID=1000
ARG APP_GID=1000
# The guards are not defensive padding: a macOS host user is uid 501 gid 20, and gid 20 is already
# `dialout` in Debian, so an unconditional `groupadd` fails the build on the most likely host.
RUN if ! getent group "${APP_GID}" >/dev/null; then groupadd --gid "${APP_GID}" mizan; fi \
 && if ! getent passwd "${APP_UID}" >/dev/null; then \
      useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin mizan; \
    fi \
 && install -d -o "${APP_UID}" -g "${APP_GID}" /home/mizan

# Source last, and as files rather than as an installed package: Streamlit is handed the app by
# path (`streamlit run src/tda/review/app.py`), so the tree has to be on disk under /app.
COPY --chown=${APP_UID}:${APP_GID} src/ src/
COPY --chown=${APP_UID}:${APP_GID} policy.yaml policy.schema.json ./
# The reviewer-assist agent reads clause text out of `docs/01-definitions.md` at question time, so
# the docs are a runtime dependency of the screen and not just reading material.
COPY --chown=${APP_UID}:${APP_GID} docs/ docs/
# Committed and frozen. Compose remounts it read-only from the host so a reviewer can point the
# screen at a different submission without rebuilding; baking it in keeps the image self-contained.
COPY --chown=${APP_UID}:${APP_GID} corpus/ corpus/
# Replay is the only provider this image can use, and replay without cassettes is a hard error at
# the first model call. `tests/` is otherwise excluded from the build context.
COPY --chown=${APP_UID}:${APP_GID} tests/cassettes/ tests/cassettes/

# Pre-created and owned, so a plain `docker run` with no mount can still complete a verification.
RUN install -d -o "${APP_UID}" -g "${APP_GID}" /app/artifacts

ENV HOME=/home/mizan \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

USER ${APP_UID}:${APP_GID}
EXPOSE 8501

# Localhost only, so the check itself makes no egress.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"]

# No ENTRYPOINT: the officer's workflow is two commands against one image, so
# `docker run mizan mizan run` and `docker run mizan mizan trace` have to override this cleanly.
CMD ["python", "-m", "streamlit", "run", "/app/src/tda/review/app.py"]

# `docker build --target toolchain` for the reproducibility check: the generator rebuilds
# `corpus/demo` into a temporary directory and diffs digests against the committed copy, which is
# how somebody who did not author the corpus confirms it was generated rather than hand-edited.
# Separate stage because reportlab and `tools/` have no business in the image that serves the app.
FROM app AS toolchain
# Redeclared: a build argument does not cross a stage boundary, and an empty `USER :` below would
# silently hand this stage back to root.
ARG APP_UID=1000
ARG APP_GID=1000
USER root
RUN pip install -e ".[extract,outputs,agents,review,datagen]"
# PYTHONPATH rather than a console script, matching the Makefile: the generator is build tooling
# and must not land on the PATH of anything that installs mizan.
ENV PYTHONPATH=/app/tools
COPY --chown=${APP_UID}:${APP_GID} tools/ tools/
USER ${APP_UID}:${APP_GID}
CMD ["python", "-m", "datagen", "--out", "corpus/demo", "--verify"]

FROM app AS runtime
LABEL org.opencontainers.image.title="mizan" \
      org.opencontainers.image.description="Agentic reconciliation and verification system (POC), offline replay only"
