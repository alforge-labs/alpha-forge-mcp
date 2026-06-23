"""新 MCP capability のテスト。

annotations（#16）/ structured output（#17）/ resources（#18）/ prompts（#19）が
クライアント（Claude 等）へ正しく公開されることを検証する。forge バイナリには
触れず、必要な箇所は ``_get_client`` をモックして委譲のみ確認する。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from alpha_forge_mcp import server as server_mod
from alpha_forge_mcp.server import mcp

# read 系（副作用なし・冪等・ローカル参照のみ）と run 系（外部データ取得・永続化）。
# forge_status は doctor をローカル参照するだけで外部に触れない read 系（#26）。
_READ_TOOLS = {
    "list_strategies",
    "get_strategy",
    "list_results",
    "get_result",
    "generate_pinescript",
    "forge_status",
}
# run 系: 外部市場データ取得・最適化実行・DB 永続化を伴う非冪等な実行。
# fetch_data=外部データ取得 / save_strategy=DB 書込 / WFT・MC=重い計算実行。
_RUN_TOOLS = {
    "run_backtest",
    "run_optimize",
    "run_walk_forward",
    "run_monte_carlo",
    "fetch_data",
    "save_strategy",
}
_ALL_TOOLS = _READ_TOOLS | _RUN_TOOLS


def _tools_by_name() -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


class TestToolAnnotations:
    """#16: read 系と run 系を annotations で区別し、クライアントの自動実行判断を助ける。"""

    def test_read系toolはreadOnlyかつopenWorldでない(self) -> None:
        tools = _tools_by_name()
        for name in _READ_TOOLS:
            ann = tools[name].annotations
            assert ann is not None, name
            assert ann.readOnlyHint is True, name
            assert ann.openWorldHint is False, name

    def test_run系toolはreadOnlyでなく外部世界へアクセスする(self) -> None:
        # この区別が壊れると Claude が重い run 系を read 系と誤認し自動実行しうる。
        tools = _tools_by_name()
        for name in _RUN_TOOLS:
            ann = tools[name].annotations
            assert ann is not None, name
            assert ann.readOnlyHint is False, name
            assert ann.openWorldHint is True, name


class TestStructuredOutput:
    """#17: 戻り値型注釈から outputSchema が生成され structuredContent が返ること。"""

    def test_全toolがobject型のoutputSchemaを持つ(self) -> None:
        tools = _tools_by_name()
        for name in _ALL_TOOLS:
            schema = tools[name].outputSchema
            assert schema is not None, name
            # forge は全コマンドで JSON オブジェクトを返す。list 注釈だと実行時の
            # outputSchema 検証で破綻するため object であることを固定する。
            assert schema.get("type") == "object", (name, schema)


def _resource_uris() -> set[str]:
    return {str(r.uri) for r in asyncio.run(mcp.list_resources())}


def _template_uris() -> set[str]:
    return {t.uriTemplate for t in asyncio.run(mcp.list_resource_templates())}


class TestResources:
    """#18: read データを resource として公開（Claude Code の @メンション参照）。"""

    def test_静的リソースが登録される(self) -> None:
        uris = _resource_uris()
        assert "forge://strategies" in uris
        assert "forge://results" in uris

    def test_テンプレートリソースが登録される(self) -> None:
        tmpls = _template_uris()
        assert "forge://strategy/{strategy_id}" in tmpls
        assert "forge://result/{result_id}" in tmpls

    def test_strategiesリソースはclientへ委譲しJSONを返す(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.list_strategies.return_value = {"strategies": [{"strategy_id": "x"}], "count": 1}
        monkeypatch.setattr(server_mod, "_get_client", lambda: client)
        contents = list(asyncio.run(mcp.read_resource("forge://strategies")))
        assert json.loads(contents[0].content)["count"] == 1
        assert contents[0].mime_type == "application/json"
        client.list_strategies.assert_called_once_with()

    def test_strategyテンプレートはstrategy_idで委譲する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.get_strategy.return_value = {"strategy_id": "abc"}
        monkeypatch.setattr(server_mod, "_get_client", lambda: client)
        contents = list(asyncio.run(mcp.read_resource("forge://strategy/abc")))
        assert json.loads(contents[0].content)["strategy_id"] == "abc"
        client.get_strategy.assert_called_once_with("abc")

    def test_resultテンプレートはresult_idで委譲する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.get_result.return_value = {"run_id": "r1"}
        monkeypatch.setattr(server_mod, "_get_client", lambda: client)
        contents = list(asyncio.run(mcp.read_resource("forge://result/r1")))
        assert json.loads(contents[0].content)["run_id"] == "r1"
        client.get_result.assert_called_once_with("r1")


def _prompts_by_name() -> dict:
    return {p.name: p for p in asyncio.run(mcp.list_prompts())}


class TestPrompts:
    """#19: 定型ワークフローを prompt として公開（Claude Code のスラッシュコマンド化）。"""

    def test_promptが登録される(self) -> None:
        names = set(_prompts_by_name())
        assert {"backtest_and_review", "optimize_and_verify"}.issubset(names)

    def test_backtest_and_reviewはstrategy_idとsymbolを引数に取る(self) -> None:
        p = _prompts_by_name()["backtest_and_review"]
        argnames = {a.name for a in (p.arguments or [])}
        assert {"strategy_id", "symbol"}.issubset(argnames)

    def test_optimize_and_verifyはstrategy_idとsymbolを引数に取る(self) -> None:
        p = _prompts_by_name()["optimize_and_verify"]
        argnames = {a.name for a in (p.arguments or [])}
        assert {"strategy_id", "symbol"}.issubset(argnames)

    def test_get_promptが引数を埋め込んだメッセージを返す(self) -> None:
        res = asyncio.run(
            mcp.get_prompt("backtest_and_review", {"strategy_id": "s1", "symbol": "AAPL"})
        )
        text = " ".join(
            m.content.text if hasattr(m.content, "text") else str(m.content)
            for m in res.messages
        )
        assert "s1" in text
        assert "AAPL" in text
