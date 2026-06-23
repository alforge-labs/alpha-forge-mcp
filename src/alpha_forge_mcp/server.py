"""alpha-forge-mcp の MCP サーバ（stdio）。

FastMCP に以下を登録する。いずれも ``ForgeClient`` を介して forge バイナリを
subprocess で呼ぶ（コアロジックは含まない／露出しない）。

- **Tools**（12）: read 系（list/get/generate_pinescript/forge_status）と run 系
  （run_backtest / run_optimize / run_walk_forward / run_monte_carlo / fetch_data /
  save_strategy）。read 系 / run 系を ``ToolAnnotations`` で区別し（#16）、戻り値型
  注釈から ``outputSchema`` を生成して structured output を返す（#17）。
  #24/#25/#26 で WFT・MC・data fetch・strategy save・forge_status を追加。
- **Resources**（#18）: read データを ``forge://...`` で公開し、Claude Code 等の
  @メンション参照を可能にする（Tool は能動実行用に併存）。
- **Prompts**（#19）: 定型ワークフローを公開し、Claude Code のスラッシュコマンド化。

``ForgeClient`` は遅延生成し、forge 未検出/未認証時は ``ForgeError`` として FastMCP
経由でクライアントへ返す（import や起動自体は妨げない＝IDE 側で扱いやすい）。
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from alpha_forge_mcp.envelope import Envelope, envelope
from alpha_forge_mcp.forge_client import ForgeClient
from alpha_forge_mcp.forge_client import forge_status as _forge_status

# name は PyPI パッケージ名（alpha-forge-mcp）と一致させる（issue #3）。
mcp = FastMCP("alpha-forge-mcp")

# issue #3: FastMCP はコンストラクタで version を受け取れず、未設定だと低レベル
# Server が mcp ライブラリ自身の版を serverInfo.version として返す。自パッケージの
# 版を低レベル Server に直接設定して initialize 応答へ反映させる。
try:
    mcp._mcp_server.version = version("alpha-forge-mcp")
except PackageNotFoundError:  # pragma: no cover - 未インストールのソース直接実行時のみ
    pass

# issue #16: read 系は副作用なし・冪等・ローカル参照のみ（外部世界に触れない）。
# run 系は外部市場データを取得し結果を永続化する非冪等な実行（openWorld）。
# クライアント（Claude 等）はこれを自動実行可否や表示の判断に使う。
_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
_RUN = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

_client: ForgeClient | None = None


def _get_client() -> ForgeClient:
    """ForgeClient を遅延生成（forge 未検出なら ForgeNotFoundError を送出）。"""
    global _client
    if _client is None:
        _client = ForgeClient()
    return _client


# ---------------------------------------------------------------------------
# Tools（#16 annotations / #17 structured output / #23 error envelope）
# 戻り値は ``@envelope`` で統一した error envelope（Envelope TypedDict）。
# 成功時 {"ok": True, "data": <forge の JSON>, "error": None} /
# 失敗時 {"ok": False, "data": None, "error": {"code", "message", "detail"}}。
# forge は全コマンドで JSON オブジェクトを返すため data は object であり、
# FastMCP は Envelope から ok/data/error 枝を持つ object の outputSchema を生成する。
# 例外を素通しさせると FastMCP が自由文 ToolError に再ラップし code が構造化
# フィールドとして届かないため、各 tool は @envelope で必ず envelope を返す（#23）。
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
@envelope
def list_strategies() -> Envelope:
    """List all registered AlphaForge strategies (strategy_id, name, version, timeframe)."""
    return _get_client().list_strategies()


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_strategy(strategy_id: str) -> Envelope:
    """Get the full JSON definition of a registered strategy by its strategy_id."""
    return _get_client().get_strategy(strategy_id)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def list_results(strategy_id: str | None = None) -> Envelope:
    """List saved backtest results, optionally filtered by strategy_id."""
    return _get_client().list_results(strategy_id)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_result(result_id: str) -> Envelope:
    """Get metrics and trades for a saved backtest result (result_id = strategy_id or run_id)."""
    return _get_client().get_result(result_id)


@mcp.tool(annotations=_RUN)
@envelope
def run_backtest(
    symbol: str,
    strategy_id: str,
    start: str | None = None,
    end: str | None = None,
) -> Envelope:
    """Run a backtest for `symbol` with a registered strategy. Optional dates are YYYY-MM-DD."""
    return _get_client().run_backtest(symbol, strategy_id, start=start, end=end)


@mcp.tool(annotations=_RUN)
@envelope
def run_optimize(
    symbol: str,
    strategy_id: str,
    metric: str | None = None,
    trials: int | None = None,
) -> Envelope:
    """Optimize strategy parameters with Optuna for `symbol`. metric defaults to sharpe_ratio."""
    return _get_client().run_optimize(symbol, strategy_id, metric=metric, trials=trials)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def generate_pinescript(strategy_id: str, with_webhook: bool = False) -> Envelope:
    """Generate TradingView Pine Script v6 for a strategy. Returns {strategy_id, pinescript}."""
    return _get_client().generate_pinescript(strategy_id, with_webhook=with_webhook)


# ---------------------------------------------------------------------------
# Tool 網羅拡張（#24 WFT/MC・#25 fetch/save・#26 status）。
# 既存 7 tool と同形（@mcp.tool annotations + @envelope）で契約を揃える。
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_RUN)
@envelope
def run_walk_forward(
    symbol: str,
    strategy_id: str,
    windows: int | None = None,
    metric: str | None = None,
) -> Envelope:
    """Run walk-forward optimization for `symbol` (out-of-sample robustness check).

    windows defaults to 5, metric to sharpe_ratio. Required by the optimize_and_verify
    workflow to compare in-sample vs out-of-sample behaviour.
    """
    return _get_client().run_walk_forward(
        symbol, strategy_id, windows=windows, metric=metric
    )


@mcp.tool(annotations=_RUN)
@envelope
def run_monte_carlo(result_id: str, simulations: int | None = None) -> Envelope:
    """Run a Monte Carlo simulation from a saved backtest result (resamples its trades).

    result_id = strategy_id or run_id. simulations defaults to 1000. Returns ruin
    probability, equity percentiles, and drawdown distribution for risk assessment.
    """
    return _get_client().run_monte_carlo(result_id, simulations=simulations)


@mcp.tool(annotations=_RUN)
@envelope
def fetch_data(symbol: str, period: str | None = None) -> Envelope:
    """Fetch & cache historical OHLCV for `symbol` (prerequisite for run_backtest).

    period is e.g. 1y / 5y / 6m / 30d / max (defaults to 1y). Returns {symbol, period,
    output}. The CLI has no --start/--end, so only period is exposed.
    """
    return _get_client().fetch_data(symbol, period=period)


@mcp.tool(annotations=_RUN)
@envelope
def save_strategy(json_body: str) -> Envelope:
    """Register a strategy from its JSON body (not a file path; agent-friendly).

    Pass the full strategy-definition JSON as a string; it is validated as a JSON object
    and written to a temp file before `strategy save`. Returns {output}.
    """
    return _get_client().save_strategy(json_body)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def forge_status() -> Envelope:
    """Report alpha-forge capabilities/prerequisites before use (doctor + version).

    Read-only triage: returns {binary_found, version, authenticated, plan, doctor, error}.
    Never fails when the binary is missing — returns binary_found=false instead.
    """
    return _forge_status()


# ---------------------------------------------------------------------------
# Resources（#18）
# read 系データを forge://... で公開する。Tool と同じ ForgeClient へ委譲し、
# application/json で返す。Claude Code 等では @メンションで context に取り込める。
# ---------------------------------------------------------------------------


def _as_json(data: Any) -> str:
    """resource ペイロードを JSON 文字列にする（非 ASCII 保持・非シリアライズ値は文字列化）。"""
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.resource("forge://strategies", mime_type="application/json")
def resource_strategies() -> str:
    """All registered strategies (same payload as the list_strategies tool)."""
    return _as_json(_get_client().list_strategies())


@mcp.resource("forge://strategy/{strategy_id}", mime_type="application/json")
def resource_strategy(strategy_id: str) -> str:
    """Full JSON definition of one strategy by strategy_id."""
    return _as_json(_get_client().get_strategy(strategy_id))


@mcp.resource("forge://results", mime_type="application/json")
def resource_results() -> str:
    """All saved backtest results (same payload as the list_results tool)."""
    return _as_json(_get_client().list_results())


@mcp.resource("forge://result/{result_id}", mime_type="application/json")
def resource_result(result_id: str) -> str:
    """Metrics and trades for one saved backtest result by result_id."""
    return _as_json(_get_client().get_result(result_id))


# ---------------------------------------------------------------------------
# Prompts（#19）
# forge を呼ばない純粋なテンプレート。Claude Code では
# /mcp__alpha-forge-mcp__<name> のスラッシュコマンドとして公開される。
# ---------------------------------------------------------------------------


@mcp.prompt(title="Backtest and review a strategy")
def backtest_and_review(strategy_id: str, symbol: str) -> str:
    """Guide: run a backtest for a strategy/symbol, then review the metrics."""
    return (
        f"Run a backtest for strategy `{strategy_id}` on symbol `{symbol}` using the "
        f"`run_backtest` tool. Then review the result: summarize the key metrics "
        f"(Sharpe ratio, max drawdown, win rate, profit factor, total return / CAGR), "
        f"judge whether the strategy looks robust, and call out red flags such as too "
        f"few trades, look-ahead bias, or an equity curve driven by a single outlier. "
        f"Be concise and quantitative."
    )


@mcp.prompt(title="Optimize and verify a strategy")
def optimize_and_verify(strategy_id: str, symbol: str) -> str:
    """Guide: optimize a strategy with Optuna, then check it is not overfit."""
    return (
        f"Optimize the parameters of strategy `{strategy_id}` for symbol `{symbol}` "
        f"using the `run_optimize` tool (Optuna TPE). Then verify the optimized result "
        f"is not overfit: compare in-sample vs walk-forward / out-of-sample behaviour, "
        f"check the number of trials and resulting trades, and warn if the improvement "
        f"looks like curve-fitting rather than a genuine edge. Recommend whether to keep "
        f"or discard the optimization, and why."
    )


def main() -> None:
    """stdio トランスポートで MCP サーバを起動する（``uvx alpha-forge-mcp`` のエントリ）。"""
    mcp.run(transport="stdio")
