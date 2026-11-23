# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| latest (`main`) | Yes |
| Anything else | No |

Only the latest version receives security updates. Please make sure you are
running the most recent commit before reporting a vulnerability.

---

## Reporting a vulnerability

If you discover a security vulnerability in jeicob, **please do not open a
public issue**. Instead, report it privately through one of these channels:

1. **GitHub Private Vulnerability Reporting** (recommended):
   Go to the [Security](https://github.com/lgarbayo/jeicob/security) tab and
   click "Report a vulnerability".

2. **Email**: Send a description to
   [lugarbayo@gmail.com](mailto:lugarbayo@gmail.com).

### What to include

- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Any suggested fix (if you have one)

---

## Response timelines

| Action | Target |
|--------|--------|
| Acknowledgement | Within 48–72 hours |
| Initial assessment | Within 1 week |
| Fix or mitigation | Best effort, depending on complexity |

> **Note:** This project is maintained by a single developer. Response times
> may vary during periods of high academic workload.

---

## Disclosure policy

We follow **coordinated disclosure**:

- Please do not publicly disclose the vulnerability until a fix has been
  released or a reasonable timeframe has been agreed upon.
- We will credit reporters in the changelog unless anonymity is requested.

---

## Known limitations

This project has the following known security considerations:

- **No external security audit** has been performed.
- **API keys** (Anthropic) are required for the Claude-based orchestrator.
  Keys are stored in a local `.env` file and must never be committed.
- The agent **downloads PDFs from URLs chosen by the LLM** (normally found via
  Semantic Scholar/arXiv, but any public http(s) host is accepted unless
  `JEICOB_DOWNLOAD_ALLOWED_HOSTS` is set). Outbound requests go through
  `jeicob/netguard.py` (SSRF guard: public IPs only, including IPv4 embedded in
  IPv6; re-checked on the connected socket and on every redirect).
  `JEICOB_ALLOW_PRIVATE_NETWORK=1` disables the public-IP restriction.
  Downloaded files are sandboxed to `knowledge_base/`, never overwrite existing
  files, must contain a `%PDF-` marker in the first KB, are capped at 50 MB and
  120 s, and PDFs over 3000 pages are rejected; their content is otherwise
  untrusted.
- **Prompt injection and data exfiltration.** A downloaded paper (or any
  document) can carry instructions aimed at the LLM. Combined with private data
  in `knowledge_base/` and tools that reach the internet, this could leak data
  through `search_references` queries or download URLs. If `knowledge_base/`
  holds clinical data, set `JEICOB_ENABLE_NETWORK=0`: the CLI orchestrators
  and the MCP server then drop the network tools, and `jeicob/netguard.py` refuses
  any outbound request as a second barrier. The embedding model is still
  downloaded by fastembed (from Hugging Face, no document data) if it is not
  cached; on air-gapped machines pre-fetch it with
  `uv run python -m jeicob.rag --prefetch`.
- **Where data goes.** With `jeicob.main` document text is sent to the Anthropic
  API; with the MCP server, everything the tools read is sent to the MCP
  client's LLM. Only `jeicob.main_local` (Ollama) keeps document contents on the
  machine, and its `search_references` queries still go to Semantic
  Scholar/arXiv.
- **Qdrant server mode** (`QDRANT_URL`, `docker-compose.yml`): the bundled
  compose file binds Qdrant to `127.0.0.1` only, requires `QDRANT_API_KEY` and
  disables Qdrant telemetry. The server stores chunk text (document content)
  in the `qdrant_storage` Docker volume. If you point `QDRANT_URL` at another
  host, use https: with plain http the API key and the content travel
  unencrypted (qdrant-client warns about it).
- The MCP server writes an audit log to stderr (method, tool/prompt name or
  resource type, outcome, duration) with no arguments or content, so it does
  not record which document was read.
- Dependencies have not been independently audited. Dependabot is configured
  to alert on known CVEs.
- This project was originally developed in a hackathon setting and has not
  been tested under adversarial conditions.
