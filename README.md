# alpha-forge-mcp

> **The MCP server for [AlphaForge](https://alforgelabs.com)** — the agent-native quant CLI: write strategies in JSON, optimize with Optuna TPE, validate with walk-forward, export to TradingView Pine v6. This server lets your AI agent drive the whole pipeline over MCP. → **[Try AlphaForge free](https://alforgelabs.com)**

---

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
| `get_result` | Metrics of one result (heavy arrays folded into counts by default; `summary=false` for full) | `alpha-forge backtest report <result_id> --json` |
| `run_backtest` | Run a backtest (`summary=true` by default omits heavy arrays) | `alpha-forge backtest run <symbol> --strategy <id> [--start] [--end] [--summary] --json` |
| `run_optimize` | Optimize parameters (Optuna) | `alpha-forge optimize run <symbol> --strategy <id> [--metric] [--trials] [--save] --json` |
| `apply_optimization` | Apply an optimization result file to a strategy | `alpha-forge optimize apply <result_file> --to-strategy <id> --yes` |
| `run_walk_forward` | Walk-forward (out-of-sample) optimization | `alpha-forge optimize walk-forward <symbol> --strategy <id> [--windows] [--metric] --json` |
| `run_monte_carlo` | Monte Carlo from a saved result | `alpha-forge backtest monte-carlo <result_id> [--simulations] --json` |
| `fetch_data` | Fetch & cache historical OHLCV (prereq for `run_backtest`) | `alpha-forge data fetch <symbol> [--period]` |
| `save_strategy` | Register a strategy from its JSON **body** | `alpha-forge strategy save <tmpfile>` |
| `generate_pinescript` | Generate Pine Script v6 source | `alpha-forge pine preview --strategy <id> [--with-webhook]` |
| `forge_status` | Report capabilities/prerequisites (doctor + version) | `alpha-forge system doctor --json` |
| `list_journals` | List strategies that have a journal | `alpha-forge journal list --json` |
| `get_journal` | Full journal (snapshots, runs, tags, notes) of one strategy | `alpha-forge journal show <strategy_id> --json` |
| `exploration_status` | Strategy-exploration coverage map (explored vs. untried) | `alpha-forge explore status [--goal] --json` |
| `get_indicator` | Metadata for one technical indicator | `alpha-forge analyze indicator show <name> --json` |

`save_strategy` takes the strategy-definition **JSON body** as a string (not a file path,
which is more agent-friendly); it is written to a temp file before `strategy save`.
`fetch_data` exposes only `period` because the CLI has no `--start`/`--end`. `forge_status`
is read-only and never fails when the binary is missing — it returns `binary_found: false`
so a client can triage prerequisites before doing anything else.

`run_optimize` saves the result by default (`save=true`) so its `saved_path` can be passed
to `apply_optimization`, which applies the optimized parameters and saves
`<strategy_id>_optimized` (it runs non-interactively with `--yes`). `get_indicator` returns
indicator **metadata** only (description, parameters, output) — the CLI has no
compute-over-symbol command, so it does not calculate the indicator on price data.
journal/explore reads are exposed read-first; write-oriented and ml/pairs commands are not
exposed yet.

The `metric` argument of `run_optimize` / `run_walk_forward` is a constrained **enum**
(`sharpe_ratio` (default), `sortino_ratio`, `calmar_ratio`, `total_return_pct`, `cagr_pct`,
`profit_factor`, `win_rate_pct`, `expectancy_pct`, `omega_ratio`) so clients can pick a
valid optimization target without guessing. Each tool's description states its prerequisite
(e.g. `run_backtest` needs `fetch_data` first; `apply_optimization` needs a
`run_optimize(save=true)` result) and its follow-up.

### Server instructions & long-running jobs

The server advertises `instructions` (surfaced in the MCP `initialize` response) describing
the end-to-end workflow — `forge_status` → `fetch_data` → `run_backtest` → `run_optimize`
→ `run_walk_forward` → `apply_optimization` → `generate_pinescript` — so an agent knows
which tools to call and in what order.

The run/fetch/save/apply tools are long-running (`run_backtest` up to 300 s, `run_optimize`
/ `run_walk_forward` up to 600 s, others bounded by the default timeout — stated in each
tool's description). They report **progress** to capable clients via MCP progress
notifications (a `start` → `complete` bracket; the underlying `alpha-forge` subprocess does
not expose intermediate progress) and run the blocking call off the event loop so the
server stays responsive. The timeout is enforced by `alpha-forge`; on expiry the tool
returns the `timeout` error code, which is safe to retry.

All tools carry MCP **tool annotations** (`readOnlyHint` for the read tools — the `list`/
`get` lookups, `generate_pinescript`, `forge_status`, `list_journals`, `get_journal`,
`exploration_status`, and `get_indicator`; `openWorldHint` for the run/write tools —
`run_backtest` / `run_optimize` / `run_walk_forward` / `run_monte_carlo`, plus
`fetch_data` (fetches external market data), `save_strategy` and `apply_optimization`
(write to the DB)) and return **structured output** — `structuredContent` with an object
`outputSchema` — alongside the text result.

### Error envelope

Every tool returns a uniform **error envelope** as its (always-successful) result rather
than raising, so an agent can branch on the failure category mechanically instead of
parsing free text:

- Success: `{"ok": true, "data": { ...alpha-forge JSON... }, "error": null}`
- Failure: `{"ok": false, "data": null, "error": {"code": "<category>", "message": "<human readable>", "detail": null}}`

`error.code` is the machine-readable failure category — e.g. `forge_not_found` (binary
missing → guide setup), `authentication_required` (run `alpha-forge system auth login`),
`freemium_blocked` (premium-only feature → stop), `strategy_not_found`, `timeout` (safe to
retry), `bad_output`, `execution_failed`. The `outputSchema` reflects this `ok` / `data` /
`error` shape.

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
