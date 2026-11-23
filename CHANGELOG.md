# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Outbound HTTP goes through `jeicob/netguard.py`: http(s) only, public IPs only
  (checked on DNS resolution and again on the connected socket, defeating DNS
  rebinding), every redirect re-validated, no environment proxies, optional
  host allowlist (`JEICOB_DOWNLOAD_ALLOWED_HOSTS`).
  `JEICOB_ALLOW_PRIVATE_NETWORK=1` lifts the public-IP restriction (e.g.
  intranet repositories).
- `download_reference` never overwrites files in `knowledge_base/`, rejects
  hidden file names and requires a `%PDF` header; arXiv search uses https.
- `write_summary` no longer overwrites an existing report unless
  `overwrite=true`.
- PDFs over 3000 pages are rejected.
- MCP server audit log (tool/resource/prompt, outcome, duration; no content).
  Only names from a fixed set are logged (no log forging), notifications are
  skipped, and the SDK's own INFO logs (which echo paths and error text) are
  silenced.
- The SSRF guard also rejects IPv6 addresses that embed a non-public IPv4
  (NAT64, 6to4, Teredo, IPv4-compatible) and URLs with an invalid port.
- Downloads have a 120 s overall deadline (a slow-drip server can no longer
  hang the tool).
- `write_summary` creates new reports atomically (`open("x")`), closing a
  check-then-write race; string flags such as `overwrite="false"` from local
  LLMs are parsed as booleans.

### Fixed

- RAG ingestion is now idempotent: deterministic chunk ids plus a per-file
  SHA-256 mean re-running `ingest_corpus` no longer duplicates chunks; changed
  files are re-indexed and files removed from `knowledge_base/` are pruned.
- `ingest_corpus` returns an error instead of pruning the whole index when
  `knowledge_base/` does not exist (misspelled `JEICOB_KB_DIR`, unmounted
  volume).
- Paths set in the repo's `.env` (`QDRANT_PATH`, `JEICOB_KB_DIR`…) are honoured
  regardless of the working directory.

- MCP server: shutting down during an ingestion no longer hangs for 30 s or
  leaves Qdrant locked; cancelled calls stop the ingestion between documents;
  queued calls no longer add up their lock waits: each call waits at most
  `JEICOB_QDRANT_LOCK_TIMEOUT`, counted from when it was made.
- `ingest_corpus` (CLI and MCP) summarises partial failures in its first line
  ("Sincronizado con N error(es)…") and only reports an error when no document
  could be indexed, instead of depending on which file failed first.
- MCP server: a document whose text starts with "ERROR" is no longer reported
  as a failure; unexpected RAG errors carry their cause.
- MCP resources are no longer URL-decoded twice; `.MD` reports and only
  readable formats are listed; completions are URL-quoted for templates;
  unknown prompts or missing prompt arguments return `INVALID_PARAMS`.
- MCP `download_reference` keeps the download confirmation (and notifies the
  client) when only the indexing step fails.
- `read_directory` reports a missing knowledge base as an error.

### Changed

- The Python package is now `jeicob` (was `src`): run modules as
  `python -m jeicob.<module>`. The project is installable (`uv_build`
  backend) and provides the `jeicob`, `jeicob-local` and `jeicob-mcp`
  commands; `.mcp.json` uses `jeicob-mcp`. Outside a checkout, data defaults
  to `~/.jeicob`.

- Unrecognised boolean values in `JEICOB_*` flags are reported on stderr;
  for `JEICOB_ENABLE_NETWORK` they disable the network (fail closed). Invalid
  numbers in `JEICOB_QDRANT_*` are reported too.

- With `JEICOB_ENABLE_NETWORK=0` the MCP instructions no longer mention the
  network tools; `ingest_corpus` is annotated as destructive (it prunes the
  index); MCP `read_file` caps `max_chars` at 20 000.

- `read_file` reads long documents in pages (`offset`, `max_chars`) and caches
  extracted text.
- Data paths are configurable via environment (`jeicob/config.py`) and resolved
  against the repo root instead of the working directory.
- The fastembed model cache now lives in `.fastembed_cache/` instead of `/tmp`.
- Single shared system prompt (`jeicob/prompts.py`) for both orchestrators.

### Added

- MCP over streamable-http: `jeicob-mcp --transport http [--host] [--port]`
  (or `JEICOB_MCP_TRANSPORT/HOST/PORT`), so several clients can share one
  server. A bearer token (`JEICOB_MCP_TOKEN`, ≥ 32 characters) is mandatory,
  it binds to `127.0.0.1` by default with the SDK's DNS-rebinding protection,
  and the smoke test covers 401/421 and tool calls over HTTP.

- Optional Qdrant server mode: `QDRANT_URL` / `QDRANT_API_KEY` switch the RAG
  engine from the on-disk store to a Qdrant server, so the CLI and several MCP
  sessions can share the index. `docker-compose.yml` runs a pinned Qdrant on
  `127.0.0.1` with a mandatory API key and telemetry disabled;
  `python -m jeicob.rag --url URL` runs the RAG smoke test against a server,
  and CI runs it against a Qdrant service container.

- MCP server (`jeicob/mcp_server.py`, stdio, official `mcp` SDK 2.x as the
  optional `mcp` extra) exposing the seven research tools to Claude Code,
  Claude Desktop and other MCP clients, with tool annotations, `isError`
  results, ingestion progress notifications, and a single-threaded Qdrant
  owner that releases the on-disk lock when idle.
- MCP resources (`jeicob://knowledge/{+path}`, `jeicob://reports/{name}`)
  listed live from disk, prompts (`research_report`, `summarize_document`,
  `ask_corpus`), argument completion, and resource list-changed
  notifications.
- `.mcp.json` registering the server for Claude Code, and a stdio smoke test
  (`python -m jeicob.mcp_smoke`) run in CI.
- `JEICOB_ENABLE_NETWORK=0` removes the internet tools (`search_references`,
  `download_reference`) from the CLI orchestrators (Claude and Ollama) and the
  MCP server, leaves them out of the system prompt and instructions, and makes
  `jeicob/netguard.py` refuse any outbound request.
- MCP server settings `JEICOB_QDRANT_IDLE_RELEASE` (30 s),
  `JEICOB_QDRANT_LOCK_TIMEOUT` (10 s) and `JEICOB_MCP_PREFETCH` (1).
- `python -m jeicob.rag --prefetch` to download the embedding model ahead of time.

- Autonomous research agent CLI with Claude tool calling (ReAct loop).
- RAG engine using Qdrant (on-disk) and fastembed for local multilingual
  embeddings.
- System tools with sandboxed file I/O (read from `knowledge_base/`, write
  to `docs_output/`).
- External reference discovery and download from Semantic Scholar and arXiv.
- Local/Ollama variant (`main_local.py`) for free, private operation without
  API keys.
- Structured Markdown report generation (abstract + full explanation).
- MIT license and open-source documentation (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT).
- CI workflows (lint, test, security scanning) via GitHub Actions.
- Dependabot configuration for automated dependency updates.
