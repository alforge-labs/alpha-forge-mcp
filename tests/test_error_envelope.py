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

from unittest.mock import MagicMock

import pytest

from alpha_forge_mcp import server as server_mod
from alpha_forge_mcp.errors import ForgeError, ForgeNotFoundError

# (tool 関数, client メソッド名, 呼び出し引数) の対応表。
# 公開 7 tool すべてを同一契約で網羅する。
_TOOL_CASES = [
    (server_mod.list_strategies, "list_strategies", ()),
    (server_mod.get_strategy, "get_strategy", ("sma_v1",)),
    (server_mod.list_results, "list_results", ()),
    (server_mod.get_result, "get_result", ("run_abc",)),
    (server_mod.run_backtest, "run_backtest", ("AAPL", "sma_v1")),
    (server_mod.run_optimize, "run_optimize", ("AAPL", "sma_v1")),
    (server_mod.generate_pinescript, "generate_pinescript", ("sma_v1",)),
]


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

        result = tool_fn(*args)

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

        result = tool_fn(*args)

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

        result = tool_fn(*args)  # raise しないこと自体を検証

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
        }
        for name in expected:
            schema = tools[name].outputSchema
            assert schema is not None, name
            # 既存契約（type=object）は維持しつつ envelope の枝を反映する。
            assert schema.get("type") == "object", (name, schema)
            props = schema.get("properties", {})
            assert "ok" in props, (name, props)
            assert "error" in props, (name, props)
