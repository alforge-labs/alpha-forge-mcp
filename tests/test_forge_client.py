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

    def test_run_optimize(self) -> None:
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
            "--json",
        ]

    def test_run_optimize_最小引数(self) -> None:
        cmd = self._capture(lambda c: c.run_optimize("AAPL", "sma_v1"))
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
