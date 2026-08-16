# fd-open-data-mcp — container image (all 45 tools, incl. ai_search).
# Includes CPU-only torch + sentence-transformers for semantic_search*/ai_search
# (the public /demo playground depends on ai_search, so slim is NOT viable here).
# No Chromium (Playwright is a lazy, optional import used only by the
# cisa-industry scraper; set BROWSER_CDP_URL if that path is ever needed).
#
# Build context MUST be the finddata workspace root (parent of this repo) so
# the local-path sibling fd-open-data-protocol is COPY-able. From /opt/fd/finddata:
#   docker build -f fd-open-data-mcp/Dockerfile -t finddata/fd-open-data-mcp:latest .
# Then load into k3s (no registry needed):
#   docker save finddata/fd-open-data-mcp:latest | sudo k3s ctr images import -
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# uv: resolve from the repo's uv.lock.
RUN pip install --no-cache-dir uv
# CPU-only torch (~200 MB) via the official PyTorch index, so the image does NOT
# pull the multi-GB default CUDA wheel. uv reads UV_* env at each RUN below.
ENV UV_TORCH_BACKEND=cpu

WORKDIR /app

# Sibling local-path dep (pyproject.toml [tool.uv.sources] path=../fd-open-data-protocol).
COPY fd-open-data-protocol/ /app/fd-open-data-protocol/
COPY fd-open-data-mcp/ /app/fd-open-data-mcp/

WORKDIR /app/fd-open-data-mcp

# Core + [data] extra + semantic search. sentence-transformers/torch are NOT in
# pyproject.toml (uv.lock has no torch pins); install them unpinned via uv pip
# AFTER uv sync. UV_TORCH_BACKEND=cpu forces the CPU wheel. Then bake the
# embedding model so no HF download happens at container start (the box can't
# reach huggingface.co — hf-mirror.com works). Must run AFTER venv exists.
RUN uv sync --extra data \
 && uv pip install sentence-transformers \
 && HF_ENDPOINT=https://hf-mirror.com python -c \
      "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Runtime DB lives on a mounted volume (k3s hostPath/PVC), never baked in.
RUN mkdir -p /data

# Defaults; overridden by k8s envFrom the on-box .env secret.
ENV FD_OPEN_DATA_MCP_DATABASE_URL=sqlite:////data/daas.db \
    FINDDATA_ROOT=/app \
    LOG_LEVEL=INFO

EXPOSE 8899

# migrate is idempotent (Base.metadata.create_all); safe to run every boot.
# ponytail: `fastmcp run` not `fd-open-data-mcp serve` — the repo's cli.py
# `serve` is stdio-only (no --transport flag); the systemd unit uses
# `fastmcp run ... --transport streamable-http`, so we mirror the proven path.
CMD ["sh", "-c", "fd-open-data-mcp migrate && exec fastmcp run fd_open_data_mcp/server.py:mcp --transport streamable-http --host 0.0.0.0 --port 8899"]
