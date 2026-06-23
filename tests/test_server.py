"""MCP サーバの smoke テスト（tool 登録とスキーマの確認）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from alpha_forge_mcp.server import mcp

_EXPECTED = {
    "list_strategies",
    "get_strategy",
    "list_results",
    "get_result",
    "run_backtest",
    "run_optimize",
    "generate_pinescript",
    # #24/#25/#26: tool 網羅拡張
    "run_walk_forward",
    "run_monte_carlo",
    "fetch_data",
    "save_strategy",
    "forge_status",
}


def test_全toolが登録される() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert _EXPECTED.issubset(names), names


def test_serverInfoのversionが自パッケージ版と一致する() -> None:
    # issue #3: 未指定だと FastMCP がライブラリ（mcp）自身の版を返してしまう。
    from importlib.metadata import version

    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_version == version("alpha-forge-mcp")


def test_serverInfoのnameがパッケージ名と一致する() -> None:
    # issue #3: PyPI パッケージ名 alpha-forge-mcp と serverInfo.name を揃える。
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_name == "alpha-forge-mcp"


def test_stdioのinitialize応答が自パッケージのserverInfoを返す() -> None:
    """stdio JSON-RPC でサーバを実起動し initialize 応答を E2E 検証する（issue #3）。

    initialize のみで完結し forge バイナリには触れないため CI でも動く。
    """
    from importlib.metadata import version

    req = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        + "\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "alpha_forge_mcp"],
        input=req,
        capture_output=True,
        text=True,
        timeout=30,
    )
    info = json.loads(proc.stdout.splitlines()[0])["result"]["serverInfo"]
    assert info["name"] == "alpha-forge-mcp", info
    assert info["version"] == version("alpha-forge-mcp"), info


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


def test_run_optimizeのスキーマにsymbolとstrategy_idが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    required = set(by_name["run_optimize"].inputSchema.get("required", []))
    assert {"symbol", "strategy_id"}.issubset(required)


def test_generate_pinescriptのスキーマにstrategy_idが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["generate_pinescript"].inputSchema
    assert "strategy_id" in schema.get("required", [])


def test_run_walk_forwardのスキーマにsymbolとstrategy_idが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    required = set(by_name["run_walk_forward"].inputSchema.get("required", []))
    assert {"symbol", "strategy_id"}.issubset(required)


def test_run_monte_carloのスキーマにresult_idが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["run_monte_carlo"].inputSchema
    assert "result_id" in schema.get("required", [])


def test_fetch_dataのスキーマにsymbolが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["fetch_data"].inputSchema
    assert "symbol" in schema.get("required", [])


def test_save_strategyのスキーマにjson_bodyが必須() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["save_strategy"].inputSchema
    assert "json_body" in schema.get("required", [])


def test_forge_statusは引数を取らない() -> None:
    # read-only な能力判定 tool（起動前提のトリアージ用）。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["forge_status"].inputSchema
    assert schema.get("required", []) == []
