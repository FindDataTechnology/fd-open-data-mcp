# fd-open-data-mcp image — MCP server only (base deps, no `data` extra).
#
# Serves Streamable HTTP on :8300. Auth is env-gated: set MCP_BEARER_TOKEN
# at deploy time and requests must carry the matching bearer token (401
# otherwise); unset token = no auth. Built and pushed by
# .github/workflows/docker-publish.yml -> <HARBOR_HOST>/finddata/fd-open-data-mcp.

# ---- builder: install the package + base deps into a clean prefix ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY . .
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime: only the installed tree, no build tooling ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8300
ENTRYPOINT ["fd-open-data-mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8300"]
