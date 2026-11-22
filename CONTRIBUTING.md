# Contributing to jeicob

Thank you so much for taking the time to read this. It genuinely means a lot.
**jeicob** is a small project born as a prototype for autonomous scientific
literature research, and the fact that you are here considering contributing to
it is something we do not take for granted.

---

## Response times

This project is maintained by a single developer. Response times for issues and
pull requests are typically **under one week**, but may be longer during
academic deadlines. Please be patient — every contribution is reviewed with care.

---

## Getting started

### Prerequisites

- [Python 3.13+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) (package manager)
- An **Anthropic API key** (for the Claude-based orchestrator)

### Setup

```bash
# Clone your fork
git clone git@github.com:<your-username>/jeicob.git
cd jeicob

# Install dependencies (creates .venv automatically)
uv sync

# Set up environment variables
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### Verify your environment

```bash
# Run the smoke tests for each module
uv run python -m src.tools
uv run python -m src.rag
uv run --extra mcp python -m src.mcp_smoke
```

---

## How to contribute

### Reporting bugs

Use the [bug report template](https://github.com/lgarbayo/jeicob/issues/new?template=bug_report.md).
Include:

- A clear description of the problem
- Steps to reproduce
- Expected vs. actual behavior
- Your OS, Python version, and commit hash

### Suggesting features

Use the [feature request template](https://github.com/lgarbayo/jeicob/issues/new?template=feature_request.md).
Explain the problem you are trying to solve and your proposed solution.

### Submitting code

1. **Fork** the repository and create a new branch from `main`:
   ```bash
   git checkout -b feat/my-new-feature
   ```
2. Make your changes following the coding standards below.
3. Write or update tests if applicable.
4. Update `CHANGELOG.md` if your change is user-facing.
5. Commit using [Conventional Commits](#commit-conventions) (see below).
6. **Open a pull request** against `main`.

A maintainer will review your PR within one week. You may be asked to make
changes — this is normal and part of the process.

---

## Coding standards

This project follows **[PEP 8](https://peps.python.org/pep-0008/)** for Python
code style. If a linter or formatter is configured (e.g., `ruff`), use it.

General rules:

- Write clear, readable code. Favor explicit over clever.
- Add docstrings to public functions and classes.
- Keep functions focused: one responsibility each.
- Use type hints where practical.

---

## Commit conventions

This project uses **[Conventional Commits](https://www.conventionalcommits.org/)**.
Every commit message must follow this format:

```
<type>(<optional scope>): <description>

<optional body>

<optional footer>
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | A new feature or capability |
| `fix` | A bug fix |
| `docs` | Documentation-only changes |
| `style` | Code style (formatting, whitespace) — no logic change |
| `refactor` | Code restructuring — no feature or fix |
| `perf` | Performance improvement |
| `test` | Adding or correcting tests |
| `build` | Dependencies, build tools, project version |
| `ops` | CI/CD, infrastructure, deployment |
| `chore` | Miscellaneous tasks (`.gitignore`, initial commit, …) |

### Examples

```
feat: add PDF page range selection for ingestion

fix(rag): handle empty corpus gracefully

docs: clarify local Ollama setup instructions

build: bump anthropic SDK to 0.120.0
```

### Breaking changes

If your change breaks backwards compatibility, add `!` before the colon and
explain the breaking change in the footer:

```
feat(api)!: rename search_corpus to search_documents

BREAKING CHANGE: search_corpus is now search_documents. All callers must update.
```

---

## Pull request checklist

Before submitting your PR, confirm:

- [ ] Code follows PEP 8 and existing project style
- [ ] Tests pass locally (`uv run python -m src.tools` / `uv run python -m src.rag`)
- [ ] `CHANGELOG.md` updated (if user-facing change)
- [ ] Commit messages follow Conventional Commits

---

## Security

If you discover a security vulnerability, **do not open a public issue**. Please
follow the process described in [SECURITY.md](SECURITY.md).

---

## Code of conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to uphold its standards.

---

## License

By contributing to jeicob, you agree that your contributions will be licensed
under the [MIT License](LICENSE).
