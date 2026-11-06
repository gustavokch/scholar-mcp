# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅        |
| < 1.0   | ❌        |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report them privately via [GitHub's private vulnerability reporting](https://github.com/gustavokch/scholar-mcp/security/advisories/new).

I'll acknowledge within a few days and aim to release a fix within 30 days depending on severity.

## Notes

This server makes outbound network requests to the literature and medical APIs listed in the README, plus any mirror base URLs set in `FULLTEXT_MIRRORS`. It does not store credentials or personal data; API responses are cached in a local SQLite database (`SCHOLAR_CACHE_DB`). The `download_paper` tool writes files only inside `SCHOLAR_DOWNLOAD_DIR` and rejects paths that resolve outside it — still configure your MCP client to restrict what it may write.
