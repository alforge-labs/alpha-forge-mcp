"""forge_client の単体テスト（subprocess をモックして forge バイナリ非依存）。"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from alpha_forge_mcp.errors import ForgeError, ForgeNotFoundError
from alpha_forge_mcp.forge_client import (
    ForgeClient,
    _find_forge_binary,
    _validate_identifier,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class TestForgeDiscovery:
    def test_env_var_優先で発見する(self, tmp_path, monkeypatch) -> None:
        fake = tmp_path / "forge"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setenv("ALPHA_FORGE_BIN", str(fake))
        assert _find_forge_binary() == str(fake)

    def test_env_var_が存在しないパスなら無視してPATHへ(self, monkeypatch) -> None:
        monkeypatch.setenv("ALPHA_FORGE_BIN", "/nonexistent/forge")
        with patch("alpha_forge_mcp.forge_client.shutil.which", return_value="/usr/bin/forge"):
            assert _find_forge_binary() == "/usr/bin/forge"

    def test_未発見ならForgeNotFoundError(self, monkeypatch) -> None:
        monkeypatch.delenv("ALPHA_FORGE_BIN", raising=False)
        with patch("alpha_forge_mcp.forge_client.shutil.which", return_value=None), patch(
            "alpha_forge_mcp.forge_client.Path.exists", return_value=False
        ):
            with pytest.raises(ForgeNotFoundError):
                ForgeClient()


class TestValidateIdentifier:
    @pytest.mark.parametrize(
        "value",
        ["AAPL", "^VIX", "USDJPY=X", "CL=F", "BTC-USD", "BRK.B", "EUR_USD", "sma_v1"],
    )
    def test_正常な識別子を許可する(self, value: str) -> None:
        assert _validate_identifier(value) == value

    @pytest.mark.parametrize(
        "value",
        ["--strategy", "-rf", "", "a b", "x;rm -rf", "a/b", "$(x)", "a\nb"],
    )
    def test_危険な識別子を拒否する(self, value: str) -> None:
        with pytest.raises(ForgeError) as exc:
            _validate_identifier(value)
        assert exc.value.code == "invalid_argument"


class TestForgeCall:
    def _client(self) -> ForgeClient:
        return ForgeClient(forge_bin="/fake/forge")

    def test_成功時にJSONをパースし_jsonフラグを付与する(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"strategies": [], "count": 0}))
            out = self._client().list_strategies()
        assert out == {"strategies": [], "count": 0}
        # forge <args> --json で呼ばれる（末尾に --json）
        called = run.call_args.args[0]
        assert called[0] == "/fake/forge"
        assert called[-1] == "--json"
        assert called[1:3] == ["strategy", "list"]
        # shell を介さない
        assert run.call_args.kwargs.get("shell", False) is False

    def test_非ゼロ終了でexecution_failed(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stderr="boom", returncode=1)
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "execution_failed"

    def test_exit2でauthentication_required(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stderr="not authenticated", returncode=2)
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "authentication_required"

    def test_タイムアウトでtimeout(self) -> None:
        with patch(
            "alpha_forge_mcp.forge_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="forge", timeout=30),
        ):
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "timeout"

    def test_不正JSONでbad_output(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="not json")
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "bad_output"


class TestToolArgs:
    """各 tool が正しい forge コマンド列を組み立てるか（スペック検証済みコマンド）。"""

    def _capture(self, fn) -> list[str]:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="{}")
            fn(ForgeClient(forge_bin="/fake/forge"))
            return list(run.call_args.args[0])

    def test_get_strategy(self) -> None:
        cmd = self._capture(lambda c: c.get_strategy("sma_v1"))
        assert cmd == ["/fake/forge", "strategy", "show", "sma_v1", "--json"]

    def test_list_results_strategyフィルタ(self) -> None:
        cmd = self._capture(lambda c: c.list_results("sma_v1"))
        assert cmd == ["/fake/forge", "backtest", "list", "--strategy", "sma_v1", "--json"]

    def test_get_result(self) -> None:
        cmd = self._capture(lambda c: c.get_result("run_abc"))
        assert cmd == ["/fake/forge", "backtest", "report", "run_abc", "--json"]

    def test_run_backtest_symbolはpositional_strategyはフラグ(self) -> None:
        cmd = self._capture(
            lambda c: c.run_backtest("AAPL", "sma_v1", start="2020-01-01")
        )
        assert cmd == [
            "/fake/forge",
            "backtest",
            "run",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--start",
            "2020-01-01",
            "--json",
        ]

    def test_run_backtest_不正な日付を拒否(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_backtest("AAPL", "sma_v1", start="bad")
        assert exc.value.code == "invalid_argument"
