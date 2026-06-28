"""DX polish のテスト（issue #29）。

長時間ジョブの progress 契約・tool description の前提/後続明記・metric の enum 化・
サーバ instructions（ワークフロー全体像）をクライアントへ正しく公開することを検証する。
forge バイナリには触れず、必要箇所は ``_get_client`` をモックして委譲のみ確認する。
"""

from __future__ import annotations

import asyncio
import inspect
from importlib.metadata import version
from unittest.mock import AsyncMock, MagicMock

import pytest

from alpha_forge_mcp import server as server_mod
from alpha_forge_mcp.server import mcp

# 長時間ジョブ（外部 subprocess を起動する run 系）。progress を送るため async + ctx。
_PROGRESS_TOOLS = {
    "run_backtest",
    "run_optimize",
    "run_walk_forward",
    "run_monte_carlo",
    "fetch_data",
    "save_strategy",
    "apply_optimization",
}
# metric を enum（Literal）で受ける tool。
_METRIC_TOOLS = {"run_optimize", "run_walk_forward"}


def _tools_by_name() -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


class TestServerInstructions:
    """#29: FastMCP の instructions でワークフロー全体像をクライアントへ提示する。"""

    def test_instructionsが設定される(self) -> None:
        opts = mcp._mcp_server.create_initialization_options()
        assert opts.instructions, "instructions が空（未設定）"

    def test_instructionsにワークフローの順序が含まれる(self) -> None:
        # status→fetch_data→run_backtest→run_optimize→run_walk_forward→generate_pinescript。
        opts = mcp._mcp_server.create_initialization_options()
        text = opts.instructions or ""
        for token in (
            "forge_status",
            "fetch_data",
            "run_backtest",
            "run_optimize",
            "run_walk_forward",
            "generate_pinescript",
        ):
            assert token in text, token

    def test_instructionsにsave_strategyとrun_monte_carloが含まれる(self) -> None:
        # #39: 標準ワークフローに戦略登録（save_strategy）とリスク評価（run_monte_carlo）を
        # 1 句ずつ補い、エージェントが登録〜リスク評価まで一気通貫で辿れるようにする。
        opts = mcp._mcp_server.create_initialization_options()
        text = opts.instructions or ""
        assert "save_strategy" in text
        assert "run_monte_carlo" in text


class TestMetricEnum:
    """#29-1: metric を Literal enum 化し inputSchema に enum 制約を出す。"""

    @pytest.mark.parametrize("name", sorted(_METRIC_TOOLS))
    def test_metricがenum制約を持つ(self, name: str) -> None:
        tools = _tools_by_name()
        schema = tools[name].inputSchema
        metric_schema = schema["properties"]["metric"]
        # Optional[Literal[...]] は anyOf/oneOf でラップされうるため再帰的に enum を探す。
        enum = _find_enum(metric_schema)
        assert enum is not None, (name, metric_schema)
        # 実際に CLI が受ける主要メトリクスを含む（既存テストが渡す値を含むこと）。
        assert "sharpe_ratio" in enum, enum
        assert "sortino_ratio" in enum
        assert "calmar_ratio" in enum
        assert "total_return_pct" in enum
        assert "profit_factor" in enum

    def test_既存テストが渡すmetric値がenumに含まれる(self) -> None:
        # test_error_envelope / test_forge_client が渡す sharpe_ratio が弾かれないこと。
        tools = _tools_by_name()
        enum = _find_enum(tools["run_optimize"].inputSchema["properties"]["metric"])
        assert "sharpe_ratio" in (enum or [])

    @pytest.mark.parametrize("name", sorted(_METRIC_TOOLS))
    def test_metric説明にCLIより狭いenumである旨がある(self, name: str) -> None:
        # #39: enum は CLI の --metric より狭い 9 値限定（意図的）。エージェントが
        # 「enum 外の値も CLI なら通る」ことを description から読めるよう明記する。
        tools = _tools_by_name()
        metric_schema = tools[name].inputSchema["properties"]["metric"]
        desc = (_find_description(metric_schema) or "").lower()
        assert "cli" in desc, (name, desc)
        assert "narrow" in desc, (name, desc)


def _find_enum(schema: dict) -> list | None:
    """JSON Schema 片の中から最初に見つかる enum を返す（anyOf/oneOf を再帰探索）。"""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("enum"), list):
        return schema["enum"]
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            found = _find_enum(sub)
            if found is not None:
                return found
    return None


def _find_description(schema: dict) -> str | None:
    """JSON Schema 片の中から最初に見つかる description を返す（anyOf/oneOf を再帰探索）。"""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("description"), str):
        return schema["description"]
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            found = _find_description(sub)
            if found is not None:
                return found
    return None


class TestToolDescriptions:
    """#29-2: 各 tool の description に前提・後続・timeout などの文脈を補う。"""

    def test_run_backtestの説明にfetch_data前提がある(self) -> None:
        tools = _tools_by_name()
        desc = tools["run_backtest"].description or ""
        assert "fetch_data" in desc

    def test_run_optimizeの説明にsaveとapply後続がある(self) -> None:
        tools = _tools_by_name()
        desc = tools["run_optimize"].description or ""
        assert "apply_optimization" in desc

    def test_run_optimizeの説明にtrials既定値が明記される(self) -> None:
        # #39: run_walk_forward は「windows defaults to 5」、run_monte_carlo は
        # 「simulations defaults to 1000」と書くのに run_optimize は trials 既定が
        # 抜けていた（非対称）。CLI の既定（200）を description に明記して揃える。
        tools = _tools_by_name()
        desc = (tools["run_optimize"].description or "").lower()
        assert "trials" in desc
        assert "200" in desc

    @pytest.mark.parametrize("name", sorted(_PROGRESS_TOOLS))
    def test_run系の説明にtimeoutが明記される(self, name: str) -> None:
        # 長時間ジョブの上限秒数を AI が把握できるよう description に明記する。
        tools = _tools_by_name()
        desc = (tools[name].description or "").lower()
        assert "timeout" in desc or "second" in desc, (name, desc)


class TestProgressContract:
    """#29-4: 長時間ジョブの run 系 tool は Context を受けて progress を送る。"""

    @pytest.mark.parametrize("name", sorted(_PROGRESS_TOOLS))
    def test_run系tool関数はコルーチンである(self, name: str) -> None:
        # progress を await するため async でなければならない。
        fn = getattr(server_mod, name)
        assert inspect.iscoroutinefunction(fn), name

    @pytest.mark.parametrize("name", sorted(_PROGRESS_TOOLS))
    def test_run系toolはctxを受け取れる(self, name: str) -> None:
        fn = getattr(server_mod, name)
        assert "ctx" in inspect.signature(fn).parameters, name

    @pytest.mark.parametrize("name", sorted(_PROGRESS_TOOLS))
    def test_ctxはinputSchemaに露出しない(self, name: str) -> None:
        # Context は FastMCP が注入する内部引数で、クライアントの入力ではない。
        tools = _tools_by_name()
        props = tools[name].inputSchema.get("properties", {})
        assert "ctx" not in props, (name, props)

    def test_ctxが渡されたら開始と完了のprogressを送る(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.run_backtest.return_value = {"metrics": {}}
        monkeypatch.setattr(server_mod, "_get_client", lambda: client)

        ctx = MagicMock()
        # report_progress は await されるため AsyncMock にする。
        ctx.report_progress = AsyncMock(return_value=None)

        result = asyncio.run(server_mod.run_backtest("AAPL", "sma_v1", ctx=ctx))

        assert result["ok"] is True
        # 開始（progress=0）と完了（progress=total）の少なくとも 2 回送る。
        assert ctx.report_progress.await_count >= 2
        # 最初は progress=0（開始）、最後は progress=total（完了）。
        first = ctx.report_progress.await_args_list[0].kwargs
        last = ctx.report_progress.await_args_list[-1].kwargs
        assert first["progress"] == 0.0
        assert last["progress"] == last["total"]

    def test_ctx無しでも動作する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ctx は任意（None 既定）。progress を送れない環境でも tool は機能する。
        client = MagicMock()
        client.run_backtest.return_value = {"metrics": {}}
        monkeypatch.setattr(server_mod, "_get_client", lambda: client)

        result = asyncio.run(server_mod.run_backtest("AAPL", "sma_v1"))

        assert result["ok"] is True
        client.run_backtest.assert_called_once()


class TestServerInfoUnchanged:
    """既存契約（#3）が壊れないこと（instructions 追加が version/name に影響しない）。"""

    def test_serverInfoのversionとnameは維持される(self) -> None:
        opts = mcp._mcp_server.create_initialization_options()
        assert opts.server_version == version("alpha-forge-mcp")
        assert opts.server_name == "alpha-forge-mcp"
