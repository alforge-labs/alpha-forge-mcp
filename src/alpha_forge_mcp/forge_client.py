"""forge バイナリへの薄い subprocess ラッパー。

コア alpha-forge は商用クローズドの Nuitka スタンドアロンバイナリ（Python import
不可）で配布される。本クライアントは forge CLI を **subprocess** で呼び出し、
``--json`` 出力をパースして返すだけの薄い層であり、コアロジックは一切含まない／
露出しない（open-core: MCP は OSS, コアは商用クローズド）。

設計（スペック docs/superpowers/specs/2026-06-01-alpha-forge-mcp-mvp-design.md 準拠）:
- ``shell=False`` + 引数 list でシェルを介さない（インジェクション防止）。
- symbol / strategy_id / result_id は先頭ハイフンを禁止する等の入力検証（引数注入防止）。
- タイムアウト・stdout/stderr 分離・終了コードと JSON パース失敗のエラーマッピング。
- 認証は forge へ委譲（``forge system auth login`` 済みの環境を前提）。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from alpha_forge_mcp.errors import ForgeError, ForgeNotFoundError

_DEFAULT_TIMEOUT = 30.0
_BACKTEST_TIMEOUT = 300.0
_OPTIMIZE_TIMEOUT = 600.0

# 識別子（symbol / strategy_id / result_id）の許容文字。
# 先頭は英数字または ``^``（指数: ^VIX 等）。先頭ハイフンを禁止して forge への
# 引数注入（値が ``--flag`` と解釈される）を防ぐ。shell=False と併せて安全側に倒す。
_IDENT_RE = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9._\-=^:]*$")
_MAX_IDENT_LEN = 256
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _find_forge_binary() -> str | None:
    """forge バイナリを優先度順に探索する。見つからなければ ``None``。

    1. 環境変数 ``ALPHA_FORGE_BIN``
    2. ``PATH`` 上の ``forge`` / ``alpha-forge``
    3. OS 別の既定インストールパス
    """
    env_path = os.environ.get("ALPHA_FORGE_BIN")
    if env_path and Path(env_path).exists():
        return env_path

    for name in ("forge", "alpha-forge"):
        found = shutil.which(name)
        if found:
            return found

    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/AlphaForge.app/Contents/MacOS/forge",
            str(Path.home() / "Applications/AlphaForge.app/Contents/MacOS/forge"),
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\AlphaForge\forge.exe",
            str(Path.home() / r"AppData\Local\AlphaForge\forge.exe"),
        ]
    else:
        candidates = [
            "/opt/alpha-forge/forge",
            "/usr/local/bin/forge",
            str(Path.home() / ".local/bin/forge"),
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _validate_identifier(value: str) -> str:
    """symbol / strategy_id / result_id の基本検証。不正なら ForgeError。"""
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= _MAX_IDENT_LEN)
        or _IDENT_RE.match(value) is None
    ):
        raise ForgeError("invalid_argument", f"invalid identifier: {value!r}")
    return value


def _validate_date(value: str) -> str:
    """YYYY-MM-DD 形式の日付を検証。不正なら ForgeError。"""
    if not isinstance(value, str) or _DATE_RE.match(value) is None:
        raise ForgeError("invalid_argument", f"invalid date (expected YYYY-MM-DD): {value!r}")
    return value


def _validate_positive_int(value: object, name: str = "value") -> str:
    """正の整数を検証して文字列で返す。不正なら ForgeError（bool は除外）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ForgeError("invalid_argument", f"{name} must be a positive integer: {value!r}")
    return str(value)


class ForgeClient:
    """forge バイナリを ``--json`` で叩く薄いクライアント。"""

    def __init__(self, forge_bin: str | None = None) -> None:
        resolved = forge_bin or _find_forge_binary()
        if not resolved:
            raise ForgeNotFoundError(
                "forge バイナリが見つかりません。次のいずれかを行ってください: "
                "(1) PATH に `forge` または `alpha-forge` を置く、"
                "(2) 環境変数 ALPHA_FORGE_BIN=/path/to/forge を設定する。"
                "また使用前に `forge system auth login` で認証を済ませてください。"
            )
        self.forge_bin: str = resolved

    def _run(
        self,
        args: list[str],
        *,
        json_output: bool,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        """``forge <args> [--json]`` を実行する共通処理。

        Args:
            json_output: True なら ``--json`` を付与し stdout を JSON パースして返す。
                False なら stdout を生テキスト（str）で返す（例: ``pine preview``）。

        Raises:
            ForgeError: 実行失敗 / タイムアウト / 非ゼロ終了 / JSON パース失敗。
        """
        # shell=False + 引数 list で固定（シェルを介さずインジェクション不可）。
        cmd = [self.forge_bin, *args]
        if json_output:
            cmd.append("--json")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ForgeError(
                "timeout", f"forge timed out after {timeout}s: {' '.join(args)}"
            ) from exc
        except OSError as exc:
            raise ForgeError("execution_failed", f"failed to execute forge: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # forge は未認証時に exit code 2 を返す（commands/auth.py 準拠）。
            code = "authentication_required" if proc.returncode == 2 else "execution_failed"
            raise ForgeError(
                code,
                f"`forge {' '.join(args)}` failed (exit {proc.returncode}): {detail}",
            )

        if not json_output:
            return proc.stdout
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ForgeError(
                "bad_output", f"failed to parse forge --json output: {exc}"
            ) from exc

    def _call(self, args: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> Any:
        """``forge <args> --json`` を実行し、パースした JSON を返す。"""
        return self._run(args, json_output=True, timeout=timeout)

    def _call_text(self, args: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> str:
        """``forge <args>`` を実行し stdout を生テキストで返す（``--json`` なし）。"""
        return self._run(args, json_output=False, timeout=timeout)

    # ------------------------------------------------------------------
    # tool 実装（forge CLI コマンドへの 1:1 マッピング・スペックで検証済み）
    # ------------------------------------------------------------------

    def list_strategies(self) -> Any:
        """``forge strategy list --json``"""
        return self._call(["strategy", "list"])

    def get_strategy(self, strategy_id: str) -> Any:
        """``forge strategy show <strategy_id> --json``"""
        return self._call(["strategy", "show", _validate_identifier(strategy_id)])

    def list_results(self, strategy_id: str | None = None) -> Any:
        """``forge backtest list [--strategy <id>] --json``"""
        args = ["backtest", "list"]
        if strategy_id:
            args += ["--strategy", _validate_identifier(strategy_id)]
        return self._call(args)

    def get_result(self, result_id: str) -> Any:
        """``forge backtest report <result_id> --json``"""
        return self._call(["backtest", "report", _validate_identifier(result_id)])

    def run_backtest(
        self,
        symbol: str,
        strategy_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        """``forge backtest run <symbol> --strategy <id> [--start ..] [--end ..] --json``"""
        args = [
            "backtest",
            "run",
            _validate_identifier(symbol),
            "--strategy",
            _validate_identifier(strategy_id),
        ]
        if start:
            args += ["--start", _validate_date(start)]
        if end:
            args += ["--end", _validate_date(end)]
        return self._call(args, timeout=_BACKTEST_TIMEOUT)

    def run_optimize(
        self,
        symbol: str,
        strategy_id: str,
        metric: str | None = None,
        trials: int | None = None,
    ) -> Any:
        """``forge optimize run <symbol> --strategy <id> [--metric ..] [--trials ..] --json``"""
        args = [
            "optimize",
            "run",
            _validate_identifier(symbol),
            "--strategy",
            _validate_identifier(strategy_id),
        ]
        if metric:
            args += ["--metric", _validate_identifier(metric)]
        if trials is not None:
            args += ["--trials", _validate_positive_int(trials, "trials")]
        return self._call(args, timeout=_OPTIMIZE_TIMEOUT)

    def generate_pinescript(
        self, strategy_id: str, with_webhook: bool = False
    ) -> dict[str, str]:
        """``forge pine preview --strategy <id> [--with-webhook]``（Pine 本文を取得）。

        ``pine generate`` はファイル出力でパス表示のみ・stdout に本文を出さないため、
        本文を stdout に出す ``pine preview`` を用いる。戻り値は
        ``{"strategy_id": ..., "pinescript": <Pine v6 ソース>}``。
        """
        args = ["pine", "preview", "--strategy", _validate_identifier(strategy_id)]
        if with_webhook:
            args.append("--with-webhook")
        script = self._call_text(args)
        return {"strategy_id": strategy_id, "pinescript": script}
