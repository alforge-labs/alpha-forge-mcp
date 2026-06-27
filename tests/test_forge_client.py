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
    forge_status,
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

    def test_構造化エラーJSONのcodeをそのまま返す(self) -> None:
        """#12: forge が stdout に返す構造化エラーの code を握りつぶさない。

        旧実装は exit 2 を一律 authentication_required に写像し、
        strategy_not_found 等を誤分類して AI クライアントを無意味な
        auth login へ誘導していた。
        """
        body = {
            "error": "戦略 'does_not_exist' が見つかりません",
            "code": "strategy_not_found",
            "id": "does_not_exist",
        }
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(
                stdout=json.dumps(body, ensure_ascii=False),
                stderr="⚠ Trial プランで実行中…",  # stderr バナーより stdout JSON を優先
                returncode=2,
            )
            with pytest.raises(ForgeError) as exc:
                self._client().get_strategy("does_not_exist")
        assert exc.value.code == "strategy_not_found"
        assert "見つかりません" in exc.value.message
        assert "Trial プラン" not in exc.value.message

    def test_構造化JSONのauthentication系codeもpassthroughされる(self) -> None:
        """forge 自身が authentication_required を返す場合はそのまま伝播する。"""
        body = {"error": "not authenticated", "code": "authentication_required"}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(body), returncode=2)
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "authentication_required"

    def test_exit2でも構造化JSONが無ければexecution_failed(self) -> None:
        """#12: exit 2 は多義 (Click usage error 等) なので auth とは断定しない。"""
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(
                stderr="Usage: forge strategy show [OPTIONS]\nError: No such option",
                returncode=2,
            )
            with pytest.raises(ForgeError) as exc:
                self._client().list_strategies()
        assert exc.value.code == "execution_failed"

    def test_有料プラン限定パネルはfreemium_blockedに分類する(self) -> None:
        """#12: Trial の Pine ブロックは専用 code + Rich 罫線除去で返す。"""
        panel = (
            "╭─ 🔒 有料プラン限定機能 ─╮\n"
            "│ Pine Script エクスポートは有料プランのみ │\n"
            "│ アップグレード: https://example.com      │\n"
            "╰──────────────────────╯"
        )
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=panel, returncode=1)
            with pytest.raises(ForgeError) as exc:
                self._client().generate_pinescript("sma_cross_qs")
        assert exc.value.code == "freemium_blocked"
        assert "有料プラン限定" in exc.value.message
        # Rich パネルの罫線は人間可読メッセージから除去される
        for box_char in ("╭", "│", "╰", "─"):
            assert box_char not in exc.value.message

    def test_英語版Premium_onlyパネルもfreemium_blocked(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(
                stdout="╭─ 🔒 Premium-only feature ─╮\n│ Upgrade your license │\n╰─╯",
                returncode=1,
            )
            with pytest.raises(ForgeError) as exc:
                self._client().generate_pinescript("sma_cross_qs")
        assert exc.value.code == "freemium_blocked"

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
        # summary 既定 True で --summary を付与する（issue #36）
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
            "--summary",
            "--json",
        ]

    def test_run_backtest_不正な日付を拒否(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_backtest("AAPL", "sma_v1", start="bad")
        assert exc.value.code == "invalid_argument"

    def test_run_optimize(self) -> None:
        # #27: save=True が既定なので --save が付く（結果を揮発させず apply に渡せる）。
        cmd = self._capture(
            lambda c: c.run_optimize("AAPL", "sma_v1", metric="sharpe_ratio", trials=50)
        )
        assert cmd == [
            "/fake/forge",
            "optimize",
            "run",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--metric",
            "sharpe_ratio",
            "--trials",
            "50",
            "--save",
            "--json",
        ]

    def test_run_optimize_最小引数(self) -> None:
        # #27: 最小引数でも save 既定 True のため --save が付く。
        cmd = self._capture(lambda c: c.run_optimize("AAPL", "sma_v1"))
        assert cmd == [
            "/fake/forge",
            "optimize",
            "run",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--save",
            "--json",
        ]

    def test_run_optimize_save_既定でtrue_savedpathを残す(self) -> None:
        # #27: --save を付けないと CLI は結果 JSON を残さず optimize apply に渡せない。
        cmd = self._capture(lambda c: c.run_optimize("AAPL", "sma_v1"))
        assert "--save" in cmd

    def test_run_optimize_save_falseで_saveを付けない(self) -> None:
        # #27: 明示的に save=False のときは結果を保存しない（--save 不付与）。
        cmd = self._capture(lambda c: c.run_optimize("AAPL", "sma_v1", save=False))
        assert "--save" not in cmd
        assert cmd == [
            "/fake/forge",
            "optimize",
            "run",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--json",
        ]

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5, "50"])
    def test_run_optimize_不正なtrialsを拒否する(self, bad) -> None:
        # 0/負/非int を拒否。bool は int サブクラスだが除外する（True==1 を弾く）。
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_optimize("AAPL", "sma_v1", trials=bad)
        assert exc.value.code == "invalid_argument"

    def test_run_optimize_空metricを拒否する(self) -> None:
        # metric="" はサイレント省略でなく invalid_argument として弾く（is not None 判定）。
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_optimize("AAPL", "sma_v1", metric="")
        assert exc.value.code == "invalid_argument"

    def test_generate_pinescript_preview経由でjson無しで本文を返す(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="//@version=6\nindicator('x')\n")
            out = ForgeClient(forge_bin="/fake/forge").generate_pinescript("sma_v1")
        cmd = run.call_args.args[0]
        # pine preview は本文（非 JSON）を stdout に出すため --json は付けない
        assert cmd == ["/fake/forge", "pine", "preview", "--strategy", "sma_v1"]
        assert "--json" not in cmd
        assert out == {
            "strategy_id": "sma_v1",
            "pinescript": "//@version=6\nindicator('x')\n",
        }

    def test_generate_pinescript_with_webhook(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="//@version=6\n")
            ForgeClient(forge_bin="/fake/forge").generate_pinescript("sma_v1", with_webhook=True)
        assert run.call_args.args[0] == [
            "/fake/forge",
            "pine",
            "preview",
            "--strategy",
            "sma_v1",
            "--with-webhook",
        ]


class TestRunWalkForward:
    """#24: ``optimize walk-forward`` への 1:1 マッピング（--json あり）。"""

    def _capture(self, fn) -> tuple[list[str], MagicMock]:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="{}")
            fn(ForgeClient(forge_bin="/fake/forge"))
            return list(run.call_args.args[0]), run

    def test_最小引数_symbolはpositional_strategyはフラグ(self) -> None:
        cmd, _ = self._capture(lambda c: c.run_walk_forward("AAPL", "sma_v1"))
        assert cmd == [
            "/fake/forge",
            "optimize",
            "walk-forward",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--json",
        ]

    def test_windowsとmetricを渡す(self) -> None:
        cmd, _ = self._capture(
            lambda c: c.run_walk_forward("AAPL", "sma_v1", windows=8, metric="sharpe_ratio")
        )
        assert cmd == [
            "/fake/forge",
            "optimize",
            "walk-forward",
            "AAPL",
            "--strategy",
            "sma_v1",
            "--windows",
            "8",
            "--metric",
            "sharpe_ratio",
            "--json",
        ]

    def test_optimize同等のtimeoutを適用する(self) -> None:
        from alpha_forge_mcp.forge_client import _OPTIMIZE_TIMEOUT

        _, run = self._capture(lambda c: c.run_walk_forward("AAPL", "sma_v1"))
        assert run.call_args.kwargs.get("timeout") == _OPTIMIZE_TIMEOUT

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
    def test_不正なwindowsを拒否する(self, bad) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_walk_forward("AAPL", "sma_v1", windows=bad)
        assert exc.value.code == "invalid_argument"

    def test_空metricを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_walk_forward("AAPL", "sma_v1", metric="")
        assert exc.value.code == "invalid_argument"

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"symbol": "AAPL", "windows": [], "valid_oos_windows": 0}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").run_walk_forward("AAPL", "sma_v1")
        assert out == payload


class TestRunMonteCarlo:
    """#24: ``backtest monte-carlo`` への 1:1 マッピング（--json あり）。"""

    def _capture(self, fn) -> tuple[list[str], MagicMock]:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="{}")
            fn(ForgeClient(forge_bin="/fake/forge"))
            return list(run.call_args.args[0]), run

    def test_最小引数_result_idはpositional(self) -> None:
        cmd, _ = self._capture(lambda c: c.run_monte_carlo("run_abc"))
        assert cmd == [
            "/fake/forge",
            "backtest",
            "monte-carlo",
            "run_abc",
            "--json",
        ]

    def test_simulationsを渡す(self) -> None:
        cmd, _ = self._capture(lambda c: c.run_monte_carlo("run_abc", simulations=5000))
        assert cmd == [
            "/fake/forge",
            "backtest",
            "monte-carlo",
            "run_abc",
            "--simulations",
            "5000",
            "--json",
        ]

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1000"])
    def test_不正なsimulationsを拒否する(self, bad) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").run_monte_carlo("run_abc", simulations=bad)
        assert exc.value.code == "invalid_argument"

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"initial_capital": 100000, "ruin_probability_pct": 0.0}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").run_monte_carlo("run_abc")
        assert out == payload


class TestFetchData:
    """#25: ``data fetch`` への 1:1 マッピング（CLI は --json 非対応のため text）。"""

    def _capture(self, fn) -> tuple[list[str], MagicMock]:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="Fetched and saved data for AAPL (1234 lines)\n")
            fn(ForgeClient(forge_bin="/fake/forge"))
            return list(run.call_args.args[0]), run

    def test_最小引数_symbolはpositional(self) -> None:
        cmd, _ = self._capture(lambda c: c.fetch_data("AAPL"))
        # data fetch は --json 非対応・stdout はテキスト → --json は付けない
        assert cmd == ["/fake/forge", "data", "fetch", "AAPL"]
        assert "--json" not in cmd

    def test_periodを渡す(self) -> None:
        cmd, _ = self._capture(lambda c: c.fetch_data("AAPL", period="5y"))
        assert cmd == ["/fake/forge", "data", "fetch", "AAPL", "--period", "5y"]

    def test_period形式を検証して不正なら拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").fetch_data("AAPL", period="bad period")
        assert exc.value.code == "invalid_argument"

    def test_テキスト出力を構造化dictで包む(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(
                stdout="Fetched and saved data for AAPL (1234 lines)\n"
            )
            out = ForgeClient(forge_bin="/fake/forge").fetch_data("AAPL", period="5y")
        assert out == {
            "symbol": "AAPL",
            "period": "5y",
            "output": "Fetched and saved data for AAPL (1234 lines)\n",
        }

    def test_不正なsymbolを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").fetch_data("--watchlist")
        assert exc.value.code == "invalid_argument"


class TestSaveStrategy:
    """#25: ``strategy save`` への 1:1 マッピング（JSON 本文を一時ファイル経由で渡す）。"""

    def test_JSON本文を一時ファイルに書きstrategy_saveを呼ぶ(self) -> None:
        body = json.dumps({"strategy_id": "sma_v1", "version": "1.0.0"})
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="✅ Strategy 'sma_v1' registered\n")
            ForgeClient(forge_bin="/fake/forge").save_strategy(body)
        cmd = run.call_args.args[0]
        # strategy save <tmpfile>（--json 非対応・ファイルパス引数）
        assert cmd[:3] == ["/fake/forge", "strategy", "save"]
        assert "--json" not in cmd
        # 第4引数は一時ファイルパス（位置引数）
        assert len(cmd) >= 4

    def test_一時ファイルは実行後に削除される(self) -> None:
        body = json.dumps({"strategy_id": "sma_v1", "version": "1.0.0"})
        captured: dict[str, str] = {}

        def _capture_path(*args, **kwargs):
            captured["path"] = args[0][-1]
            return _completed(stdout="ok\n")

        with patch("alpha_forge_mcp.forge_client.subprocess.run", side_effect=_capture_path):
            ForgeClient(forge_bin="/fake/forge").save_strategy(body)
        from pathlib import Path as _P

        assert not _P(captured["path"]).exists()

    def test_不正なJSON本文を拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").save_strategy("{not valid json")
        assert exc.value.code == "invalid_argument"

    def test_非dictのJSONを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").save_strategy("[1, 2, 3]")
        assert exc.value.code == "invalid_argument"

    def test_テキスト出力を構造化dictで包む(self) -> None:
        body = json.dumps({"strategy_id": "sma_v1", "version": "1.0.0"})
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="✅ Strategy 'sma_v1' registered\n")
            out = ForgeClient(forge_bin="/fake/forge").save_strategy(body)
        assert out["output"] == "✅ Strategy 'sma_v1' registered\n"


class TestForgeStatus:
    """#26: ``system doctor --json`` + version を集約した read-only ステータス。"""

    def test_binary未検出でもraiseせずbinary_found_falseを返す(self, monkeypatch) -> None:
        monkeypatch.delenv("ALPHA_FORGE_BIN", raising=False)
        with patch("alpha_forge_mcp.forge_client.shutil.which", return_value=None), patch(
            "alpha_forge_mcp.forge_client.Path.exists", return_value=False
        ):
            out = forge_status()
        assert out["binary_found"] is False
        assert out["version"] is None
        assert out["authenticated"] is False

    def test_doctorのJSONを集約して返す(self) -> None:
        report = {
            "version": "0.14.0",
            "platform": {"system": "Darwin"},
            "license": {"plan": "pro", "authenticated": True, "offline_degraded": False},
            "config": {"config_path": "/x/forge.yaml"},
            "paths": {},
            "logs": {},
        }
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(report))
            out = forge_status(forge_bin="/fake/forge")
        assert out["binary_found"] is True
        assert out["version"] == "0.14.0"
        assert out["authenticated"] is True
        assert out["plan"] == "pro"
        # 生 doctor レポートも doctor キーで保持する
        assert out["doctor"] == report

    def test_doctorコマンドはsystem_doctor_jsonを呼ぶ(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"version": "0.1.0"}))
            forge_status(forge_bin="/fake/forge")
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "system", "doctor", "--json"]

    def test_doctor失敗時もbinary_found_trueでerror情報を返す(self) -> None:
        # doctor が壊れて非ゼロ終了しても status 取得自体は落とさない（read-only 診断）。
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stderr="boom", returncode=1)
            out = forge_status(forge_bin="/fake/forge")
        assert out["binary_found"] is True
        assert out["doctor"] is None
        assert out["error"] is not None


class TestApplyOptimization:
    """#27: ``optimize apply <result_file> --to-strategy <id> --yes`` への 1:1 マッピング。

    CLI は ``--json`` 非対応・出力はテキストで、非対話環境では ``--yes`` が無いと
    UsageError(exit 2) で停止するため必ず ``--yes`` を付与する。result_file は
    ファイルパスのため識別子検証ではなくパス検証（先頭ハイフン禁止）を行う。
    """

    def _capture(self, fn) -> tuple[list[str], MagicMock]:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="✅ 最適化パラメータを適用しました\n")
            fn(ForgeClient(forge_bin="/fake/forge"))
            return list(run.call_args.args[0]), run

    def test_result_fileはpositional_strategyは_to_strategy_yes付与(self) -> None:
        cmd, _ = self._capture(
            lambda c: c.apply_optimization("/data/results/optimize_sma_v1.json", "sma_v1")
        )
        # optimize apply <result_file> --to-strategy <id> --yes（--json 非対応）
        assert cmd == [
            "/fake/forge",
            "optimize",
            "apply",
            "/data/results/optimize_sma_v1.json",
            "--to-strategy",
            "sma_v1",
            "--yes",
        ]
        assert "--json" not in cmd

    def test_テキスト出力を構造化dictで包む(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout="✅ 適用しました: strategy_id=sma_v1_optimized\n")
            out = ForgeClient(forge_bin="/fake/forge").apply_optimization(
                "/data/results/optimize_sma_v1.json", "sma_v1"
            )
        assert out == {
            "result_file": "/data/results/optimize_sma_v1.json",
            "strategy_id": "sma_v1",
            "output": "✅ 適用しました: strategy_id=sma_v1_optimized\n",
        }

    @pytest.mark.parametrize("bad", ["--evil", "-rf", "", "a\nb", "$(x)"])
    def test_危険なresult_fileを拒否する(self, bad) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").apply_optimization(bad, "sma_v1")
        assert exc.value.code == "invalid_argument"

    def test_不正なstrategy_idを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").apply_optimization(
                "/data/results/optimize_sma_v1.json", "--strategy"
            )
        assert exc.value.code == "invalid_argument"


class TestListJournals:
    """#28: ``journal list --json`` への 1:1 マッピング（read）。"""

    def test_journal_list_jsonを呼ぶ(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"journals": [], "count": 0}))
            ForgeClient(forge_bin="/fake/forge").list_journals()
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "journal", "list", "--json"]

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"journals": [{"strategy_id": "sma_v1"}], "count": 1}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").list_journals()
        assert out == payload


class TestGetJournal:
    """#28: ``journal show <strategy_id> --json`` への 1:1 マッピング（read）。"""

    def test_strategy_idはpositional(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"strategy_id": "sma_v1"}))
            ForgeClient(forge_bin="/fake/forge").get_journal("sma_v1")
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "journal", "show", "sma_v1", "--json"]

    def test_不正なstrategy_idを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").get_journal("--evil")
        assert exc.value.code == "invalid_argument"

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"strategy_id": "sma_v1", "runs": [], "snapshots": []}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").get_journal("sma_v1")
        assert out == payload


class TestExplorationStatus:
    """#28: ``explore status [--goal <name>] --json`` への 1:1 マッピング（read）。"""

    def test_最小引数でexplore_status_jsonを呼ぶ(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"summary": {}}))
            ForgeClient(forge_bin="/fake/forge").exploration_status()
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "explore", "status", "--json"]

    def test_goalフィルタを渡す(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"summary": {}}))
            ForgeClient(forge_bin="/fake/forge").exploration_status(goal="crypto")
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "explore", "status", "--goal", "crypto", "--json"]

    def test_不正なgoalを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").exploration_status(goal="--evil")
        assert exc.value.code == "invalid_argument"

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"summary": {"total_candidates": 10}, "explored": []}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").exploration_status()
        assert out == payload


class TestGetIndicator:
    """#28: ``analyze indicator show <name> --json`` への 1:1 マッピング（read）。

    実 CLI には「銘柄のデータに指標を計算する」コマンドは存在せず、指標メタ情報
    （説明・パラメータ・出力）を返す ``analyze indicator show`` のみが read 系として
    存在する（issue 案の compute_indicator(symbol, ...) は実体が無いため非採用）。
    """

    def test_indicatorはpositional_analyze_indicator_show_jsonを呼ぶ(self) -> None:
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps({"name": "RSI"}))
            ForgeClient(forge_bin="/fake/forge").get_indicator("RSI")
        cmd = run.call_args.args[0]
        assert cmd == ["/fake/forge", "analyze", "indicator", "show", "RSI", "--json"]

    def test_不正なindicatorを拒否する(self) -> None:
        with pytest.raises(ForgeError) as exc:
            ForgeClient(forge_bin="/fake/forge").get_indicator("--evil")
        assert exc.value.code == "invalid_argument"

    def test_成功時にJSONをパースして返す(self) -> None:
        payload = {"name": "RSI", "category": "モメンタム", "params": []}
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            out = ForgeClient(forge_bin="/fake/forge").get_indicator("RSI")
        assert out == payload


class TestListStrategiesNormalization:
    """issue #4: list_strategies 応答の暫定正規化（tags / タイムスタンプ）。

    forge CLI は tags を JSON 文字列・created_at/updated_at を空文字で返す
    ことがある（根本原因は upstream の repository.list_all）。MCP 側で
    構造化データとして妥当な形に正規化することを検証する。
    """

    def _list_with(self, payload: dict):
        with patch("alpha_forge_mcp.forge_client.subprocess.run") as run:
            run.return_value = _completed(stdout=json.dumps(payload))
            return ForgeClient(forge_bin="/fake/forge").list_strategies()

    def test_JSON文字列のtagsを配列に復元する(self) -> None:
        out = self._list_with(
            {
                "strategies": [
                    {"strategy_id": "s1", "tags": '["e2e", "sma", "trend"]'}
                ],
                "count": 1,
            }
        )
        assert out["strategies"][0]["tags"] == ["e2e", "sma", "trend"]

    def test_既に配列のtagsはそのまま返す(self) -> None:
        out = self._list_with(
            {"strategies": [{"strategy_id": "s1", "tags": ["a", "b"]}], "count": 1}
        )
        assert out["strategies"][0]["tags"] == ["a", "b"]

    def test_パース不能なtags文字列はそのまま残す(self) -> None:
        out = self._list_with(
            {"strategies": [{"strategy_id": "s1", "tags": '["broken'}], "count": 1}
        )
        assert out["strategies"][0]["tags"] == '["broken'

    def test_list以外にパースされるtags文字列はそのまま残す(self) -> None:
        out = self._list_with(
            {"strategies": [{"strategy_id": "s1", "tags": '"x"'}], "count": 1}
        )
        assert out["strategies"][0]["tags"] == '"x"'

    def test_空文字タイムスタンプをNoneに正規化する(self) -> None:
        out = self._list_with(
            {
                "strategies": [
                    {"strategy_id": "s1", "created_at": "", "updated_at": ""}
                ],
                "count": 1,
            }
        )
        row = out["strategies"][0]
        assert row["created_at"] is None
        assert row["updated_at"] is None

    def test_非空タイムスタンプはそのまま返す(self) -> None:
        ts = "2026-06-06T00:00:00+00:00"
        out = self._list_with(
            {
                "strategies": [
                    {"strategy_id": "s1", "created_at": ts, "updated_at": ts}
                ],
                "count": 1,
            }
        )
        row = out["strategies"][0]
        assert row["created_at"] == ts
        assert row["updated_at"] == ts

    def test_countなど他のキーは保持される(self) -> None:
        out = self._list_with(
            {"strategies": [{"strategy_id": "s1", "tags": "[]"}], "count": 1}
        )
        assert out["count"] == 1
        assert out["strategies"][0]["tags"] == []

    def test_strategiesキーが無い応答はそのまま返す(self) -> None:
        out = self._list_with({"error": "unexpected"})
        assert out == {"error": "unexpected"}
