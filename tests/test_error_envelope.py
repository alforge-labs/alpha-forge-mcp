"""MCP tool の error envelope 契約のテスト（issue #23）。

各 tool は例外を素通しせず、成功時 ``{"ok": True, "data": <dict>, "error": None}`` /
失敗時 ``{"ok": False, "data": None, "error": {"code", "message", "detail"}}`` を
**正常 return** として返す。これにより AI クライアントは ``ForgeError`` の
``code``（``_classify_failure`` の分類名）を構造化フィールドとして読み、
失敗種別で機械的に分岐できる（forge_not_found→案内 / authentication_required→
auth login / freemium_blocked→中止 / timeout→再試行）。

forge バイナリには触れず ``_get_client`` をモックして委譲・包み込みのみ検証する。
client 層（ForgeClient）は従来どおり ForgeError を raise する（test_forge_client.py）。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from alpha_forge_mcp import server as server_mod
from alpha_forge_mcp.errors import ForgeError, ForgeNotFoundError

# (tool 関数, client メソッド名, 呼び出し引数) の対応表。
# 公開 tool すべてを同一契約で網羅する（#24/#25/#26 の拡張 tool 含む）。
# forge_status は client メソッドでなくモジュール関数のため別途検証する。
# #29 で run 系は progress 送出のため async になった（_invoke が await を吸収する）。
_TOOL_CASES = [
    (server_mod.list_strategies, "list_strategies", ()),
    (server_mod.get_strategy, "get_strategy", ("sma_v1",)),
    (server_mod.list_results, "list_results", ()),
    (server_mod.get_result, "get_result", ("run_abc",)),
    (server_mod.run_backtest, "run_backtest", ("AAPL", "sma_v1")),
    (server_mod.run_optimize, "run_optimize", ("AAPL", "sma_v1")),
    (server_mod.generate_pinescript, "generate_pinescript", ("sma_v1",)),
    (server_mod.run_walk_forward, "run_walk_forward", ("AAPL", "sma_v1")),
    (server_mod.run_monte_carlo, "run_monte_carlo", ("run_abc",)),
    (server_mod.fetch_data, "fetch_data", ("AAPL",)),
    (server_mod.save_strategy, "save_strategy", ('{"strategy_id": "x"}',)),
    # #27/#28: optimize apply + journal/explore/indicator の read 公開
    (server_mod.apply_optimization, "apply_optimization", ("/r.json", "sma_v1")),
    (server_mod.list_journals, "list_journals", ()),
    (server_mod.get_journal, "get_journal", ("sma_v1",)),
    (server_mod.exploration_status, "exploration_status", ()),
    (server_mod.get_indicator, "get_indicator", ("RSI",)),
]


def _invoke(tool_fn: Any, *args: Any) -> Any:
    """同期/非同期どちらの tool でも envelope（dict）を取り出す（#29）。"""
    result = tool_fn(*args)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(server_mod, "_get_client", lambda: client)
    return client


class TestSuccessEnvelope:
    """成功時は ``{"ok": True, "data": <client の戻り値>, "error": None}`` を返す。"""

    @pytest.mark.parametrize("tool_fn, method, args", _TOOL_CASES)
    def test_成功時にokTrueとdataを返す(
        self, monkeypatch: pytest.MonkeyPatch, tool_fn, method: str, args
    ) -> None:
        client = _mock_client(monkeypatch)
        payload = {"strategies": [], "count": 0}
        getattr(client, method).return_value = payload

        result = _invoke(tool_fn, *args)

        assert result["ok"] is True
        assert result["data"] == payload
        assert result["error"] is None
        getattr(client, method).assert_called_once()


class TestErrorEnvelope:
    """失敗時は例外を送出せず ``{"ok": False, "error": {"code", ...}}`` を返す。"""

    @pytest.mark.parametrize("tool_fn, method, args", _TOOL_CASES)
    def test_ForgeErrorのcodeを構造化フィールドで返す(
        self, monkeypatch: pytest.MonkeyPatch, tool_fn, method: str, args
    ) -> None:
        client = _mock_client(monkeypatch)
        getattr(client, method).side_effect = ForgeError(
            "strategy_not_found", "`forge ...` failed: 戦略が見つかりません"
        )

        result = _invoke(tool_fn, *args)

        assert result["ok"] is False
        assert result["data"] is None
        assert result["error"]["code"] == "strategy_not_found"
        # message は ForgeError.message（人間可読）をそのまま載せる。
        assert "見つかりません" in result["error"]["message"]
        # detail キーが契約として存在する（値は None も許容）。
        assert "detail" in result["error"]

    @pytest.mark.parametrize("tool_fn, method, args", _TOOL_CASES)
    def test_例外を送出しない(
        self, monkeypatch: pytest.MonkeyPatch, tool_fn, method: str, args
    ) -> None:
        # 例外が漏れると FastMCP が自由文 ToolError に再ラップし code が届かない（#23 の核心）。
        client = _mock_client(monkeypatch)
        getattr(client, method).side_effect = ForgeError("timeout", "timed out")

        result = _invoke(tool_fn, *args)  # raise しないこと自体を検証

        assert result["ok"] is False
        assert result["error"]["code"] == "timeout"

    def test_forge未検出時もenvelopeで返す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ForgeNotFoundError は ForgeError サブクラス（code=forge_not_found）。
        # _get_client 自体が送出するケースも envelope で握る。
        def _raise() -> object:
            raise ForgeNotFoundError("forge バイナリが見つかりません")

        monkeypatch.setattr(server_mod, "_get_client", _raise)

        result = server_mod.list_strategies()

        assert result["ok"] is False
        assert result["error"]["code"] == "forge_not_found"

    def test_freemium_blockedのcodeが届く(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _mock_client(monkeypatch)
        client.generate_pinescript.side_effect = ForgeError(
            "freemium_blocked", "有料プラン限定機能"
        )

        result = server_mod.generate_pinescript("sma_v1")

        assert result["ok"] is False
        assert result["error"]["code"] == "freemium_blocked"

    def test_想定外の例外もexecution_failedで握る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ForgeError 以外（バグ等）も自由文 ToolError にせず envelope に正規化し、
        # 契約（ok/error.code を必ず読める）を破らない。
        client = _mock_client(monkeypatch)
        client.list_strategies.side_effect = RuntimeError("unexpected boom")

        result = server_mod.list_strategies()

        assert result["ok"] is False
        assert result["error"]["code"] == "execution_failed"
        assert "boom" in result["error"]["message"]


class TestForgeStatusEnvelope:
    """#26: forge_status tool は client メソッドでなくモジュール関数 (_forge_status)
    を委譲先とするが、他 tool と同一の envelope 契約を守る。"""

    def test_成功時にokTrueとstatus_dataを返す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        status = {"binary_found": True, "version": "0.14.0", "authenticated": True}
        monkeypatch.setattr(server_mod, "_forge_status", lambda: status)

        result = server_mod.forge_status()

        assert result["ok"] is True
        assert result["data"] == status
        assert result["error"] is None

    def test_例外を送出せずenvelopeで握る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> object:
            raise RuntimeError("boom")

        monkeypatch.setattr(server_mod, "_forge_status", _boom)

        result = server_mod.forge_status()

        assert result["ok"] is False
        assert result["error"]["code"] == "execution_failed"


class TestEnvelopeOutputSchema:
    """outputSchema に error 枝（ok / data / error）が反映されること（#23 の bonus）。"""

    def test_全toolのoutputSchemaにok枝が含まれる(self) -> None:
        import asyncio

        from alpha_forge_mcp.server import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        expected = {
            "list_strategies",
            "get_strategy",
            "list_results",
            "get_result",
            "run_backtest",
            "run_optimize",
            "generate_pinescript",
            "run_walk_forward",
            "run_monte_carlo",
            "fetch_data",
            "save_strategy",
            "forge_status",
            "apply_optimization",
            "list_journals",
            "get_journal",
            "exploration_status",
            "get_indicator",
        }
        for name in expected:
            schema = tools[name].outputSchema
            assert schema is not None, name
            # 既存契約（type=object）は維持しつつ envelope の枝を反映する。
            assert schema.get("type") == "object", (name, schema)
            props = schema.get("properties", {})
            assert "ok" in props, (name, props)
            assert "error" in props, (name, props)
