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
    # #27/#28: optimize apply + journal/explore/indicator の read 公開
    "apply_optimization",
    "list_journals",
    "get_journal",
    "exploration_status",
    "get_indicator",
}


def test_全toolが登録される() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert _EXPECTED.issubset(names), names


def test_main_prints_cta_to_stderr_not_stdout(capsys, monkeypatch) -> None:
    """起動時 CTA は stderr のみに出し、stdout（MCP の JSON-RPC チャネル）を汚さない（C3）。"""
    import alpha_forge_mcp.server as server

    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: None)
    server.main()
    captured = capsys.readouterr()
    assert "alforgelabs.com" in captured.err
    assert "AlphaForge" in captured.err
    assert "alforgelabs.com" not in captured.out  # stdout 非汚染（プロトコル安全）


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


def test_apply_optimizationのスキーマにresult_fileとstrategy_idが必須() -> None:
    # #27: optimize apply（result_file を戦略に適用する write tool）。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    required = set(by_name["apply_optimization"].inputSchema.get("required", []))
    assert {"result_file", "strategy_id"}.issubset(required)


def test_list_journalsは引数を取らない() -> None:
    # #28: journal list（read）。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["list_journals"].inputSchema
    assert schema.get("required", []) == []


def test_get_journalのスキーマにstrategy_idが必須() -> None:
    # #28: journal show（read）。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["get_journal"].inputSchema
    assert "strategy_id" in schema.get("required", [])


def test_exploration_statusはgoalが任意() -> None:
    # #28: explore status（read）。goal はオプショナル。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["exploration_status"].inputSchema
    assert schema.get("required", []) == []
    assert "goal" in schema.get("properties", {})


def test_get_indicatorのスキーマにindicatorが必須() -> None:
    # #28: analyze indicator show（read・指標メタ情報）。
    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["get_indicator"].inputSchema
    assert "indicator" in schema.get("required", [])
