# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Outbound HTTP goes through `src/netguard.py`: http(s) only, public IPs only
  (checked on DNS resolution and again on the connected socket, defeating DNS
  rebinding), every redirect re-validated, no environment proxies, optional
  host allowlist (`JEICOB_DOWNLOAD_ALLOWED_HOSTS`).
- `download_reference` never overwrites files in `knowledge_base/`, rejects
  hidden file names and requires a `%PDF` header; arXiv search uses https.
- `write_summary` no longer overwrites an existing report unless
  `overwrite=true`.
- PDFs over 3000 pages are rejected.
- MCP server audit log (tool/resource/prompt, outcome, duration; no content).

### Fixed

- RAG ingestion is now idempotent: deterministic chunk ids plus a per-file
  SHA-256 mean re-running `ingest_corpus` no longer duplicates chunks; changed
  files are re-indexed and files removed from `knowledge_base/` are pruned.

### Changed

- `read_file` reads long documents in pages (`offset`, `max_chars`) and caches
  extracted text.
- Data paths are configurable via environment (`src/config.py`) and resolved
  against the repo root instead of the working directory.
- The fastembed model cache now lives in `.fastembed_cache/` instead of `/tmp`.
- Single shared system prompt (`src/prompts.py`) for both orchestrators.

### Added

- MCP server (`src/mcp_server.py`, stdio, official `mcp` SDK 2.x as the
  optional `mcp` extra) exposing the seven research tools to Claude Code,
  Claude Desktop and other MCP clients, with tool annotations, `isError`
  results, ingestion progress notifications, and a single-threaded Qdrant
  owner that releases the on-disk lock when idle.
- MCP resources (`jeicob://knowledge/{+path}`, `jeicob://reports/{name}`)
  listed live from disk, prompts (`research_report`, `summarize_document`,
  `ask_corpus`), argument completion, and resource list-changed
  notifications.
- `.mcp.json` registering the server for Claude Code, and a stdio smoke test
  (`python -m src.mcp_smoke`) run in CI.
- `JEICOB_ENABLE_NETWORK=0` to run the MCP server without internet tools.
- `python -m src.rag --prefetch` to download the embedding model ahead of time.

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
