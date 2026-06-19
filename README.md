# alpha-forge-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that exposes the
**AlphaForge `alpha-forge` CLI** to AI coding agents — Claude Code, Cursor, Codex, and any
MCP-capable client — over **stdio**.

> ⚠️ **Pre-release / Alpha (`0.1.0aN`).** Tool signatures and return formats may change
> without notice. Not recommended for production automation yet. Feedback welcome via Issues.

It is a thin **open-source wrapper**: it shells out to the (commercial, closed-source)
`alpha-forge` binary with `--json` and returns the parsed result. The MCP server itself
contains no core logic — `alpha-forge` plus a valid license are required for anything to
actually run.

## Tools

| Tool | What it does | Underlying command |
|------|--------------|--------------------|
| `list_strategies` | List registered strategies | `alpha-forge strategy list --json` |
| `get_strategy` | Full JSON of one strategy | `alpha-forge strategy show <id> --json` |
| `list_results` | List saved backtest results | `alpha-forge backtest list [--strategy <id>] --json` |
| `get_result` | Metrics & trades of one result | `alpha-forge backtest report <result_id> --json` |
| `run_backtest` | Run a backtest | `alpha-forge backtest run <symbol> --strategy <id> [--start] [--end] --json` |
| `run_optimize` | Optimize parameters (Optuna) | `alpha-forge optimize run <symbol> --strategy <id> [--metric] [--trials] --json` |
| `generate_pinescript` | Generate Pine Script v6 source | `alpha-forge pine preview --strategy <id> [--with-webhook]` |

All tools carry MCP **tool annotations** (`readOnlyHint` for the read tools; `openWorldHint`
for `run_backtest` / `run_optimize`, which fetch external market data) and return
**structured output** — `structuredContent` with an object `outputSchema` — alongside the
text result.

## Resources

Read-only data is also exposed as MCP **resources**, so clients such as Claude Code can
reference them by `@`-mention without an explicit tool call. They delegate to the same
`alpha-forge` commands as the read tools and return `application/json`.

| Resource URI | Payload |
|--------------|---------|
| `forge://strategies` | All registered strategies |
| `forge://strategy/{strategy_id}` | One strategy definition |
| `forge://results` | All saved backtest results |
| `forge://result/{result_id}` | Metrics & trades of one result |

## Prompts

Reusable workflows are exposed as MCP **prompts** (surfaced as
`/mcp__alpha-forge__<name>` slash commands in Claude Code):

| Prompt | Arguments | What it does |
|--------|-----------|--------------|
| `backtest_and_review` | `strategy_id`, `symbol` | Run a backtest, then review the key metrics and red flags |
| `optimize_and_verify` | `strategy_id`, `symbol` | Optimize with Optuna, then check the result for overfitting |

Streamable HTTP transport, RBAC, rate limiting, and audit logging are planned for a later
release.

## Prerequisites

1. The **`alpha-forge` binary** must be installed and on your `PATH` (or set `ALPHA_FORGE_BIN`).
2. You must be **authenticated**: run `alpha-forge system auth login` once.
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

### Claude Code

The easiest way is the `claude mcp add` command (user scope — available in every project):

```bash
claude mcp add --scope user alpha-forge -- uvx alpha-forge-mcp
```

Alternatively, add the server to a project-scoped `.mcp.json` at the repository root
(checked in and shared with your team):

```json
{
  "mcpServers": {
    "alpha-forge": { "command": "uvx", "args": ["alpha-forge-mcp"] }
  }
}
```

> Note: Claude Code does **not** read `~/.claude/mcp.json`. User-scoped servers are stored
> in `~/.claude.json` (managed by `claude mcp add`); project-scoped servers live in
> `.mcp.json` at the project root.

### Cursor / Codex

Use the same `command` / `args` in the client's MCP server configuration:

```json
{
  "mcpServers": {
    "alpha-forge": { "command": "uvx", "args": ["alpha-forge-mcp"] }
  }
}
```

If `alpha-forge` is installed at a non-standard location, pass it via env:

```json
{
  "mcpServers": {
    "alpha-forge": {
      "command": "uvx",
      "args": ["alpha-forge-mcp"],
      "env": { "ALPHA_FORGE_BIN": "/path/to/alpha-forge" }
    }
  }
}
```

## Troubleshooting

- **`forge_not_found`** — ensure `alpha-forge` (or legacy `forge`) is on `PATH`, or set
  `ALPHA_FORGE_BIN=/path/to/alpha-forge`.
- **`authentication_required`** — run `alpha-forge system auth login`. The MCP server does
  not store credentials; it relies on `alpha-forge`'s own auth.

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
