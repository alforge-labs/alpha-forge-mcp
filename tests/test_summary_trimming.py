"""run_backtest / get_result の summary トリミング（issue #36）。

trades / equity_curve / buy_hold_curve の重い配列がコンテキストを圧迫するため、
run_backtest は CLI の ``--summary`` を、get_result は MCP 側で件数置換を行う。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from alpha_forge_mcp.forge_client import ForgeClient


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _client() -> ForgeClient:
    return ForgeClient(forge_bin="/fake/forge")


class TestSummaryTrimming:
    def test_run_backtest_summary_true_appends_summary_flag(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"sharpe_ratio": 1.0}))
            _client().run_backtest("AAPL", "strat", summary=True)
        assert "--summary" in run.call_args.args[0]

    def test_run_backtest_summary_false_omits_summary_flag(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"sharpe_ratio": 1.0}))
            _client().run_backtest("AAPL", "strat", summary=False)
        assert "--summary" not in run.call_args.args[0]

    def test_get_result_summary_strips_heavy_arrays_and_adds_counts(self) -> None:
        full = {
            "sharpe_ratio": 1.0,
            "trades": [{}, {}],
            "equity_curve": [1, 2, 3],
            "buy_hold_curve": [4, 5, 6],
        }
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(full))
            out = _client().get_result("rid", summary=True)
        assert "trades" not in out and out["trades_count"] == 2
        assert "equity_curve" not in out and out["equity_curve_count"] == 3
        assert "buy_hold_curve" not in out and out["buy_hold_curve_count"] == 3
        assert out["sharpe_ratio"] == 1.0

    def test_get_result_summary_false_returns_full_payload(self) -> None:
        full = {"sharpe_ratio": 1.0, "trades": [{}, {}], "equity_curve": [1, 2, 3]}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(full))
            out = _client().get_result("rid", summary=False)
        assert out == full
