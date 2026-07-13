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
- The agent **downloads PDFs from external sources** (Semantic Scholar, arXiv).
  While downloaded files are sandboxed to `knowledge_base/`, the content of
  external papers is not validated beyond checking the `%PDF` header.
- Dependencies have not been independently audited. Dependabot is configured
  to alert on known CVEs.
- This project was originally developed in a hackathon setting and has not
  been tested under adversarial conditions.
