"""MCP tool の error envelope 契約（issue #23）。

各 tool は例外を素通しせず、必ず以下の構造化 envelope を **正常 return** する。

- 成功: ``{"ok": True, "data": <forge の JSON>, "error": None}``
- 失敗: ``{"ok": False, "data": None, "error": {"code", "message", "detail"}}``

これにより、エージェントは ``ForgeError.code``（``forge_client._classify_failure``
の分類名: ``forge_not_found`` / ``authentication_required`` / ``freemium_blocked`` /
``strategy_not_found`` / ``timeout`` / ``bad_output`` / ``execution_failed`` 等）を
自由文ではなく構造化フィールドとして読み、失敗種別で機械的に分岐できる。

例外を tool 関数から漏らすと FastMCP が ``ToolError`` の自由文テキストへ再ラップし、
``code`` が「``Error executing tool ...: [strategy_not_found] ...``」という文字列の
一部としてしか届かない（＝この issue が解決しようとしている問題そのもの）。
``@envelope`` デコレータで全 tool を統一的に包み、契約を 1 箇所に集約する。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypedDict, TypeVar

from alpha_forge_mcp.errors import ForgeError


class ErrorInfo(TypedDict):
    """失敗 envelope の ``error`` フィールド。

    - ``code``: 機械可読な失敗分類（``ForgeError.code``）。
    - ``message``: 人間可読なメッセージ（``ForgeError.message``）。
    - ``detail``: 追加文脈（現状は予約。常に存在し、無ければ None）。
    """

    code: str
    message: str
    detail: str | None


class Envelope(TypedDict):
    """全 tool 共通の戻り値型。``ok`` を判別子に success/error を区別する。

    TypedDict にすることで FastMCP が ``ok`` / ``data`` / ``error`` の枝を持つ
    ``outputSchema`` を生成する（type=object 維持・issue #23 の bonus）。
    """

    ok: bool
    data: dict[str, Any] | None
    error: ErrorInfo | None


def ok_envelope(data: dict[str, Any]) -> Envelope:
    """成功 envelope を作る。"""
    return {"ok": True, "data": data, "error": None}


def error_envelope(code: str, message: str, detail: str | None = None) -> Envelope:
    """失敗 envelope を作る。"""
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message, "detail": detail},
    }


_F = TypeVar("_F", bound=Callable[..., dict[str, Any]])


def _to_envelope(exc: Exception) -> Envelope:
    """例外を失敗 envelope に正規化する（ForgeError は code/message を温存）。"""
    if isinstance(exc, ForgeError):
        return error_envelope(exc.code, exc.message)
    # ForgeError 以外（バグ等）も自由文 ToolError にせず execution_failed に正規化。
    return error_envelope("execution_failed", str(exc))


def envelope(fn: _F) -> Callable[..., Any]:
    """tool 関数を error envelope 契約で包むデコレータ（同期・非同期の両対応）。

    - 正常終了: 戻り値（dict）を ``ok_envelope`` でラップ。
    - ``ForgeError``: ``code`` / ``message`` を構造化フィールドとして載せる。
    - その他の例外（バグ等）: ``execution_failed`` に正規化して契約を破らない。
      例外を素通しさせると FastMCP が自由文 ToolError に再ラップしてしまう。

    progress 通知を ``await`` する run 系 tool（#29）は async 関数になるため、
    ラップ対象がコルーチン関数なら ``await`` する async wrapper を返す。これにより
    FastMCP の ``inspect.iscoroutinefunction`` 判定（await 要否の分岐）が正しく働く。
    同期 tool は従来どおり同期 wrapper のままで契約は不変。
    """

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Envelope:
            try:
                return ok_envelope(await fn(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - 契約維持のため全例外を envelope 化する
                return _to_envelope(exc)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Envelope:
        try:
            return ok_envelope(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - 契約維持のため全例外を envelope 化する
            return _to_envelope(exc)

    return wrapper
