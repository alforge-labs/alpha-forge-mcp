"""alpha-forge-mcp の MCP サーバ（stdio）。

FastMCP に MVP の 5 tool（read 4 + run_backtest）を登録する。各 tool は
``ForgeClient`` を介して forge バイナリを subprocess で呼ぶ。``ForgeClient`` は
遅延生成し、forge 未検出/未認証時は ``ForgeError`` として FastMCP 経由でクライアント
へエラーを返す（import や起動自体は妨げない＝IDE 側で扱いやすい）。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from alpha_forge_mcp.forge_client import ForgeClient

mcp = FastMCP("alpha-forge")

_client: ForgeClient | None = None


def _get_client() -> ForgeClient:
    """ForgeClient を遅延生成（forge 未検出なら ForgeNotFoundError を送出）。"""
    global _client
    if _client is None:
        _client = ForgeClient()
    return _client


@mcp.tool()
def list_strategies() -> Any:
    """List all registered AlphaForge strategies (strategy_id, name, version, timeframe)."""
    return _get_client().list_strategies()


@mcp.tool()
def get_strategy(strategy_id: str) -> Any:
    """Get the full JSON definition of a registered strategy by its strategy_id."""
    return _get_client().get_strategy(strategy_id)


@mcp.tool()
def list_results(strategy_id: str | None = None) -> Any:
    """List saved backtest results, optionally filtered by strategy_id."""
    return _get_client().list_results(strategy_id)


@mcp.tool()
def get_result(result_id: str) -> Any:
    """Get metrics and trades for a saved backtest result (result_id = strategy_id or run_id)."""
    return _get_client().get_result(result_id)


@mcp.tool()
def run_backtest(
    symbol: str,
    strategy_id: str,
    start: str | None = None,
    end: str | None = None,
) -> Any:
    """Run a backtest for `symbol` with a registered strategy. Optional dates are YYYY-MM-DD."""
    return _get_client().run_backtest(symbol, strategy_id, start=start, end=end)


@mcp.tool()
def run_optimize(
    symbol: str,
    strategy_id: str,
    metric: str | None = None,
    trials: int | None = None,
) -> Any:
    """Optimize strategy parameters with Optuna for `symbol`. metric defaults to sharpe_ratio."""
    return _get_client().run_optimize(symbol, strategy_id, metric=metric, trials=trials)


@mcp.tool()
def generate_pinescript(strategy_id: str, with_webhook: bool = False) -> Any:
    """Generate TradingView Pine Script v6 for a strategy. Returns {strategy_id, pinescript}."""
    return _get_client().generate_pinescript(strategy_id, with_webhook=with_webhook)


def main() -> None:
    """stdio トランスポートで MCP サーバを起動する（``uvx alpha-forge-mcp`` のエントリ）。"""
    mcp.run(transport="stdio")
