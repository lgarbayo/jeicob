# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
