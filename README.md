# alpha-forge-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that exposes the
**AlphaForge `forge` CLI** to AI coding agents — Claude Code, Cursor, Codex, and any
MCP-capable client — over **stdio**.

> ⚠️ **Pre-release / Alpha (`0.1.0aN`).** Tool signatures and return formats may change
> without notice. Not recommended for production automation yet. Feedback welcome via Issues.

It is a thin **open-source wrapper**: it shells out to the (commercial, closed-source) `forge`
binary with `--json` and returns the parsed result. The MCP server itself contains no core
logic — `forge` plus a valid license are required for anything to actually run.

## Tools (MVP)

| Tool | What it does | Underlying command |
|------|--------------|--------------------|
| `list_strategies` | List registered strategies | `forge strategy list --json` |
| `get_strategy` | Full JSON of one strategy | `forge strategy show <id> --json` |
| `list_results` | List saved backtest results | `forge backtest list [--strategy <id>] --json` |
| `get_result` | Metrics & trades of one result | `forge backtest report <result_id> --json` |
| `run_backtest` | Run a backtest | `forge backtest run <symbol> --strategy <id> --json` |

`run_optimize` / `generate_pinescript` are planned for the next release.

## Prerequisites

1. The **`forge` binary** must be installed and on your `PATH` (or set `ALPHA_FORGE_BIN`).
2. You must be **authenticated**: run `forge system auth login` once.
3. Python **3.11+** (only needed if not using `uvx`).

## Install & run

The recommended way is via [`uvx`](https://docs.astral.sh/uv/) — no manual install needed;
your IDE launches it on demand.

```bash
uvx alpha-forge-mcp        # starts the stdio MCP server
```

Or install explicitly:

```bash
pip install alpha-forge-mcp
alpha-forge-mcp
```

### Claude Code — `~/.claude/mcp.json`

```json
{
  "mcpServers": {
    "alpha-forge": { "command": "uvx", "args": ["alpha-forge-mcp"] }
  }
}
```

### Cursor / Codex

Use the same `command` / `args` in the client's MCP server configuration:

```json
{
  "mcpServers": {
    "alpha-forge": { "command": "uvx", "args": ["alpha-forge-mcp"] }
  }
}
```

If `forge` is installed at a non-standard location, pass it via env:

```json
{
  "mcpServers": {
    "alpha-forge": {
      "command": "uvx",
      "args": ["alpha-forge-mcp"],
      "env": { "ALPHA_FORGE_BIN": "/path/to/forge" }
    }
  }
}
```

## Troubleshooting

- **`forge binary not found`** — ensure `forge`/`alpha-forge` is on `PATH`, or set
  `ALPHA_FORGE_BIN=/path/to/forge`.
- **`authentication_required`** — run `forge system auth login`. The MCP server does not
  store credentials; it relies on `forge`'s own auth.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Forge binary discovery order: `ALPHA_FORGE_BIN` → `PATH` (`forge`, `alpha-forge`) → OS
default install paths.

## License

[Apache License 2.0](LICENSE)
