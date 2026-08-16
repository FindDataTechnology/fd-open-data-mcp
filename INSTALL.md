# finddata open-data stack — install spec (agent-executable)

> You are an AI agent. A user handed you this file's raw URL. Follow it top to
> bottom. Run every command. Stop and report on the first real failure (a
> missing key is expected — see step 4 — and is **not** a failure). This spec is
> idempotent: re-running is safe. Do not commit secrets. Do not edit the user's
> existing files unless a step tells you to.

## Goal

Install the entire finddata open-data stack from PyPI and bring it to a running
MCP server with a fully wired ontology DB:

- `fd-open-data-mcp` — the hub (CLI + FastMCP server + ontology DB)
- `fd-open-data-protocol` — pulled in transitively
- `fd-polygon` — Polygon.io US equities datasource
- `fd-cn-report` — Chinese financial-report datasource

`fd-polygon` and `fd-cn-report` auto-register via their setuptools
`[project.entry-points."fd_open_data_mcp.datasources"]` declarations once
installed — no manual wiring.

## Steps

### 1. Install the stack

```bash
# Drop "[data]" for a lighter install (MCP server + CLI only,
# without the akshare/yfinance/playwright SDKs).
pip install "fd-open-data-mcp[data]" fd-polygon fd-cn-report
```

### 2. Wire every layer (idempotent; stops on first error)

```bash
fd-open-data-mcp migrate \
  && fd-open-data-mcp import-catalog \
  && fd-open-data-mcp consume-concepts \
  && fd-open-data-mcp propose-bindings \
  && fd-open-data-mcp seed-entities \
  && fd-open-data-mcp generate-schedules \
  && fd-open-data-mcp register-discovered
```

Layer-by-layer what this does:
- `migrate` — create the ontology tables.
- `import-catalog` — scan every `fd-*` CATALOG → `sources` / `functions` / `columns`.
- `consume-concepts` — ~926 `indicator_defs` → `concepts` table.
- `propose-bindings` — `column → concept` bindings (rules + LLM hints).
- `seed-entities` — per-source entity identifiers (akshare/yfinance → stock codes, worldbank → ISO).
- `generate-schedules` — per-concept refresh schedules from `indicator_defs.frequency`.
- `register-discovered` — entry-point scan → registers `fd-polygon` + `fd-cn-report`.

### 3. Smoke-check the wiring

```bash
fd-open-data-mcp list-sources
fd-open-data-mcp rank-sources --concept-id 234    # should return ≥1 ranked source
```

### 4. Environment keys (set before live fetches; never committed)

These are **not** required to install or start the server — only for actual
data fetches against the keyed sources. Ask the user for the ones they want.

| Var | Who needs it | Required for |
|-----|--------------|--------------|
| `POLYGON_API_KEY` | `fd-polygon` | US equity fetches |
| `EDGAR_IDENTITY` | `fd-open-data-mcp` edgar adapter | SEC EDGAR fetches |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | `fd-cn-report` | AI extraction from report PDFs |
| `ES_URL` (+ `ES_API_KEY` or `ES_USERNAME`/`ES_PASSWORD`) | `fd-cn-report` | ES index/search |
| `DC_API_KEY` | `fd-datacommons` (if installed) | Google Data Commons |

CNINFO and akshare are keyless. Default LLM provider is DeepSeek on Ark; any
OpenAI-compatible `LLM_BASE_URL` works. Write keys to `.env` (gitignored) —
**never** to source or a commit.

### 5. Start the MCP server

```bash
fd-open-data-mcp serve          # FastMCP, stdio transport — for any MCP client
```

## Self-check (optional, no network)

```bash
uv run --with pytest pytest -q
```

## What you should report back

- pip install exit code
- the `&&` chain's exit code + which subcommand (if any) failed
- `list-sources` count
- whether the server started (`serve` blocks on stdio — report it's up, do not background it unless asked)

## Failure modes you may hit

- **`import-catalog` / `register-discovered` find nothing**: a `fd-*` package
  isn't installed, or its entry-point is misdeclared in `pyproject.toml`.
- **`propose-bindings` empty**: `consume-concepts` produced no concepts — check
  the `fd-entities-indicators` DB path (`FINDDATA_ROOT`).
- **keyed fetch 401/403**: the relevant key from step 4 is unset/wrong.
  Install + server start do **not** require any key.
