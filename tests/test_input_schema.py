"""入力スキーマ拡充のテスト（issue #37）。

各 tool の引数に description / examples / pattern / minimum が反映され、エージェントが
型と docstring の散文だけに頼らず、引数粒度の手掛かり（銘柄記法・日付形式・整数下限・
「JSON 本文（パスではない）」等）を inputSchema から機械的に読めることを検証する。

意図（WHY）: 引数粒度の手掛かりが無いと誤用（symbol の特殊記法・start/end の形式・
trials=0 等）を誘発する。description が消えれば本テストは落ちる＝契約として固定する。
forge バイナリには触れない（list_tools はスキーマ生成のみ）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from alpha_forge_mcp.server import mcp


def _tools_by_name() -> dict[str, Any]:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _props(tool_name: str) -> dict[str, Any]:
    return _tools_by_name()[tool_name].inputSchema.get("properties", {})


def _find_minimum(prop: dict[str, Any]) -> Any:
    """minimum を取り出す（Optional は anyOf の整数枝に入る）。"""
    if "minimum" in prop:
        return prop["minimum"]
    for sub in prop.get("anyOf", []):
        if "minimum" in sub:
            return sub["minimum"]
    return None


def _find_pattern(prop: dict[str, Any]) -> Any:
    """pattern を取り出す（Optional は anyOf の文字列枝に入る）。"""
    if "pattern" in prop:
        return prop["pattern"]
    for sub in prop.get("anyOf", []):
        if "pattern" in sub:
            return sub["pattern"]
    return None


class TestSymbolSchema:
    """symbol は特殊記法（^VIX/CL=F/USDJPY=X/BTC-USD）を受理するため例が必須。"""

    def test_run_backtestのsymbolにdescriptionとexamplesがある(self) -> None:
        prop = _props("run_backtest")["symbol"]
        assert prop.get("description"), prop
        examples = prop.get("examples") or []
        assert "AAPL" in examples and "^VIX" in examples, examples

    def test_run_backtestのsymbolにpatternがある(self) -> None:
        # 引数注入防止の識別子検証（_IDENT_RE）をスキーマにも露出する。
        assert _find_pattern(_props("run_backtest")["symbol"]) is not None

    def test_fetch_dataのsymbolにもexamplesがある(self) -> None:
        # issue 指摘: period には例があるのに symbol に無いのは非対称。
        prop = _props("fetch_data")["symbol"]
        assert prop.get("description"), prop
        assert prop.get("examples"), prop


class TestDateSchema:
    """start / end は YYYY-MM-DD 形式の手掛かりを pattern / format で示す。"""

    def test_startに形式の手掛かりがある(self) -> None:
        prop = _props("run_backtest")["start"]
        assert prop.get("description"), prop
        assert _find_pattern(prop) == r"^\d{4}-\d{2}-\d{2}$" or prop.get("format") == "date"

    def test_endに形式の手掛かりがある(self) -> None:
        prop = _props("run_backtest")["end"]
        assert _find_pattern(prop) == r"^\d{4}-\d{2}-\d{2}$" or prop.get("format") == "date"


class TestPositiveIntSchema:
    """trials / windows / simulations は 1 以上（minimum:1）を示す。"""

    def test_trialsにminimum1がある(self) -> None:
        assert _find_minimum(_props("run_optimize")["trials"]) == 1

    def test_windowsにminimum1がある(self) -> None:
        assert _find_minimum(_props("run_walk_forward")["windows"]) == 1

    def test_simulationsにminimum1がある(self) -> None:
        assert _find_minimum(_props("run_monte_carlo")["simulations"]) == 1


class TestBodyAndPathSchema:
    """json_body / result_file は「JSON 本文」「ファイルパス」の前提を引数で示す。"""

    def test_json_bodyはファイルパスでなく本文であることを示す(self) -> None:
        desc = (_props("save_strategy")["json_body"].get("description") or "").lower()
        assert "body" in desc, desc
        # 「ファイルパスではない」前提が読めること。
        assert "path" in desc, desc

    def test_result_fileはパスであることを示す(self) -> None:
        prop = _props("apply_optimization")["result_file"]
        desc = (prop.get("description") or "").lower()
        assert "path" in desc, desc


class TestSchemaCoverage:
    """主要 read 系の識別子引数にも description が付くこと（網羅性の最低限の歯止め）。"""

    def test_get_strategyのstrategy_idにdescriptionがある(self) -> None:
        assert _props("get_strategy")["strategy_id"].get("description")

    def test_get_resultのresult_idにdescriptionがある(self) -> None:
        assert _props("get_result")["result_id"].get("description")
