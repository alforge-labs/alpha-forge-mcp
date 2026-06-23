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

# forge の Trial/freemium ブロックは Rich パネル（exit 1・stdout）で返る
# （コア側 _helpers.py の「🔒 有料プラン限定機能 / Premium-only feature」パネル）。
_FREEMIUM_MARKERS = ("有料プラン限定", "Premium-only feature")
# Rich パネルの罫線（Unicode Box Drawing ブロック U+2500-257F）。
# AI クライアント向けの人間可読メッセージからは除去する。
_RICH_BOX_RE = re.compile(r"[─-╿]+")


def _strip_rich_decoration(text: str) -> str:
    """Rich パネルの罫線を除き、行内の余分な空白を畳んだテキストを返す。"""
    lines = []
    for line in _RICH_BOX_RE.sub(" ", text).splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def _classify_failure(args: list[str], proc: subprocess.CompletedProcess) -> ForgeError:
    """非ゼロ終了の subprocess 結果を ForgeError に分類する (#12)。

    優先順:
    1. stdout の構造化エラー JSON（``--json`` 時に forge が返す
       ``{"error": ..., "code": "strategy_not_found", "id": ...}``）の
       ``code`` をそのまま passthrough する。detail も stdout の
       ``error`` フィールドを優先する（stderr の Trial バナーで
       上書きしない）。
    2. Trial/freemium ブロック（Rich パネル）→ ``freemium_blocked``。
       罫線を除いた本文を detail にする。
    3. それ以外 → ``execution_failed``。

    旧実装は exit code 2 を一律 ``authentication_required`` に写像していたが、
    exit 2 は多義（Click の usage error / not-found 系も 2）で、forge が
    stdout に返す正しい code を握りつぶし AI クライアントを無意味な
    ``auth login`` へ誘導していた。forge 自身が ``authentication_required``
    を構造化 JSON で返す場合は 1. の passthrough で正しく伝播する。
    """
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    prefix = f"`forge {' '.join(args)}` failed (exit {proc.returncode})"

    body: Any = None
    if stdout:
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError:
            body = None
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        error_msg = body.get("error")
        detail = error_msg if isinstance(error_msg, str) and error_msg else stdout
        return ForgeError(body["code"], f"{prefix}: {detail}")

    if any(marker in stdout or marker in stderr for marker in _FREEMIUM_MARKERS):
        detail = _strip_rich_decoration(stdout or stderr)
        return ForgeError("freemium_blocked", f"{prefix}: {detail}")

    return ForgeError("execution_failed", f"{prefix}: {stderr or stdout}")

# 識別子（symbol / strategy_id / result_id）の許容文字。
# 先頭は英数字または ``^``（指数: ^VIX 等）。先頭ハイフンを禁止して forge への
# 引数注入（値が ``--flag`` と解釈される）を防ぐ。shell=False と併せて安全側に倒す。
_IDENT_RE = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9._\-=^:]*$")
_MAX_IDENT_LEN = 256
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# data fetch の --period（例: 1y / 5y / 6m / 30d）または "max"。
# 引数注入を防ぐため形式を厳密に制限する（先頭ハイフン等を弾く）。
_PERIOD_RE = re.compile(r"^(?:max|\d+[ymwd])$", re.IGNORECASE)


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


def _validate_period(value: str) -> str:
    """data fetch の --period 形式（例: 1y, 5y, 6m, 30d, max）を検証。不正なら ForgeError。"""
    if not isinstance(value, str) or _PERIOD_RE.match(value) is None:
        raise ForgeError(
            "invalid_argument", f"invalid period (expected e.g. 1y, 6m, 30d, max): {value!r}"
        )
    return value


def _normalize_strategy_row(row: dict[str, Any]) -> dict[str, Any]:
    """``list_strategies`` の 1 行を正規化する（issue #4 の暫定対応）。

    forge CLI（repository の ``list_all``）は ``tags`` を JSON 文字列・
    ``created_at``/``updated_at`` を空文字で返すことがある（根本原因は
    upstream の alpha-forge 側）。構造化データとして妥当な形（tags=配列・
    空タイムスタンプ=None）に直して返す。元 dict は変異させない。
    upstream 修正後も無害（既に配列/非空なら何もしない）。
    """
    normalized = dict(row)
    tags = normalized.get("tags")
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
        except json.JSONDecodeError:
            parsed = None
        # list 以外（'"x"' 等の正当な JSON）は安全側でそのまま残す。
        if isinstance(parsed, list):
            normalized["tags"] = parsed
    for key in ("created_at", "updated_at"):
        if normalized.get(key) == "":
            normalized[key] = None
    return normalized


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
            # #12: 一律の exit code 写像はせず、stdout の構造化エラー JSON /
            # freemium パネル / その他 の順で分類する。
            raise _classify_failure(args, proc)

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
        """``forge strategy list --json``（strategies 行を正規化して返す）"""
        data = self._call(["strategy", "list"])
        # 想定外の応答形はそのまま返す（薄いラッパー方針を維持）。
        if isinstance(data, dict) and isinstance(data.get("strategies"), list):
            return {
                **data,
                "strategies": [
                    _normalize_strategy_row(r) if isinstance(r, dict) else r
                    for r in data["strategies"]
                ],
            }
        return data

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
        # 空文字を None と区別し、metric="" は検証で弾く（サイレント省略を避ける）。
        if metric is not None:
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
        validated_id = _validate_identifier(strategy_id)
        args = ["pine", "preview", "--strategy", validated_id]
        if with_webhook:
            args.append("--with-webhook")
        script = self._call_text(args)
        return {"strategy_id": validated_id, "pinescript": script}

    def run_walk_forward(
        self,
        symbol: str,
        strategy_id: str,
        windows: int | None = None,
        metric: str | None = None,
    ) -> Any:
        """``forge optimize walk-forward <symbol> --strategy <id> [--windows ..] [--metric ..] --json``

        各ウィンドウで Optuna 最適化を回すため、run_optimize と同等の長いタイムアウト
        （``_OPTIMIZE_TIMEOUT``）を適用する（#24）。
        """  # noqa: E501
        args = [
            "optimize",
            "walk-forward",
            _validate_identifier(symbol),
            "--strategy",
            _validate_identifier(strategy_id),
        ]
        if windows is not None:
            args += ["--windows", _validate_positive_int(windows, "windows")]
        # 空文字を None と区別し、metric="" は検証で弾く（run_optimize と同方針）。
        if metric is not None:
            args += ["--metric", _validate_identifier(metric)]
        return self._call(args, timeout=_OPTIMIZE_TIMEOUT)

    def run_monte_carlo(
        self,
        result_id: str,
        simulations: int | None = None,
    ) -> Any:
        """``forge backtest monte-carlo <result_id> [--simulations ..] --json``

        既存のバックテスト結果（trades）からの再標本化のみで重い計算実行ではない
        ため、backtest と同等のタイムアウト（``_BACKTEST_TIMEOUT``）を適用する（#24）。
        """
        args = ["backtest", "monte-carlo", _validate_identifier(result_id)]
        if simulations is not None:
            args += ["--simulations", _validate_positive_int(simulations, "simulations")]
        return self._call(args, timeout=_BACKTEST_TIMEOUT)

    def fetch_data(self, symbol: str, period: str | None = None) -> dict[str, str | None]:
        """``forge data fetch <symbol> [--period ..]``（外部市場データを取得・保存）。

        ``data fetch`` は ``--json`` 非対応で stdout がテキスト（"Fetched and saved
        data for AAPL (1234 lines)" 等）のため、``generate_pinescript`` と同様に
        ``_call_text`` で取得し構造化 dict に包んで返す（#25）。``--start`` / ``--end``
        は CLI 側に存在しないため公開しない（period のみ）。外部データ取得は時間が
        かかりうるため backtest と同等のタイムアウト（``_BACKTEST_TIMEOUT``）を使う。
        """
        validated_symbol = _validate_identifier(symbol)
        args = ["data", "fetch", validated_symbol]
        if period is not None:
            args += ["--period", _validate_period(period)]
        output = self._call_text(args, timeout=_BACKTEST_TIMEOUT)
        return {"symbol": validated_symbol, "period": period, "output": output}

    def save_strategy(self, json_body: str) -> dict[str, str]:
        """``forge strategy save <tmpfile>``（戦略 JSON 本文を登録）。

        ``strategy save`` はファイルパス引数を取り ``--json`` 非対応のため、
        エージェント親和的に **JSON 本文（文字列）** を受け取り、一時ファイルへ
        書き出してから ``strategy save <tmpfile>`` を呼ぶ（#25）。一時ファイルは
        実行後に必ず削除する。本文は事前に JSON object として妥当か検証する
        （非 JSON / 非 object はサブプロセスに渡す前に invalid_argument で弾く）。
        """
        try:
            parsed = json.loads(json_body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ForgeError("invalid_argument", f"json_body is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ForgeError(
                "invalid_argument", "json_body must be a JSON object (strategy definition)"
            )

        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        )
        try:
            tmp.write(json_body)
            tmp.flush()
            tmp.close()
            output = self._call_text(["strategy", "save", tmp.name])
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        return {"output": output}


def forge_status(forge_bin: str | None = None) -> dict[str, Any]:
    """forge の能力・前提を起動前に判定する read-only ステータス（#26）。

    ``forge system doctor --json`` と version を集約し、binary が見つからない場合でも
    例外を投げずに ``binary_found: False`` を返す（クライアントが起動前提を機械的に
    トリアージできるようにするため）。doctor が壊れて非ゼロ終了しても status 取得
    自体は落とさず、``error`` に文脈を載せて返す。

    Returns:
        ``{"binary_found", "version", "authenticated", "plan", "doctor", "error"}``。
        ``doctor`` は doctor の生 JSON レポート（失敗時 None）。
    """
    resolved = forge_bin or _find_forge_binary()
    if not resolved:
        return {
            "binary_found": False,
            "version": None,
            "authenticated": False,
            "plan": None,
            "doctor": None,
            "error": "forge binary not found (set ALPHA_FORGE_BIN or add it to PATH)",
        }

    client = ForgeClient(forge_bin=resolved)
    try:
        report = client._call(["system", "doctor"])
    except ForgeError as exc:
        # doctor が落ちても binary は存在する。read-only 診断なので status は返す。
        return {
            "binary_found": True,
            "version": None,
            "authenticated": False,
            "plan": None,
            "doctor": None,
            "error": exc.message,
        }

    license_info = report.get("license") if isinstance(report, dict) else None
    license_info = license_info if isinstance(license_info, dict) else {}
    return {
        "binary_found": True,
        "version": report.get("version") if isinstance(report, dict) else None,
        "authenticated": bool(license_info.get("authenticated", False)),
        "plan": license_info.get("plan"),
        "doctor": report,
        "error": None,
    }
