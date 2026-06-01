"""MCP サーバの smoke テスト（tool 登録とスキーマの確認）。"""

from __future__ import annotations

import asyncio

from alpha_forge_mcp.server import mcp

_EXPECTED = {
    "list_strategies",
    "get_strategy",
    "list_results",
    "get_result",
    "run_backtest",
}


def test_MVPの5toolが登録される() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert _EXPECTED.issubset(names), names


def test_get_strategyのスキーマにstrategy_idが必須で含まれる() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["get_strategy"].inputSchema
    assert "strategy_id" in schema.get("properties", {})
    assert "strategy_id" in schema.get("required", [])


def test_run_backtestのスキーマにsymbolとstrategy_idが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    required = set(by_name["run_backtest"].inputSchema.get("required", []))
    assert {"symbol", "strategy_id"}.issubset(required)
