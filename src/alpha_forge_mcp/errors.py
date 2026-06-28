"""alpha-forge-mcp の例外型。

すべて ``code``（機械可読な分類）と ``message``（人間可読）を持つ。MCP の tool
実行で raise されると FastMCP がエラー結果としてクライアントへ返す。
"""

from __future__ import annotations


class ForgeError(Exception):
    """forge コマンド実行に関する一般エラー。

    Args:
        code: 機械可読な分類。MCP 側で生成するのは ``"timeout"`` /
            ``"execution_failed"`` / ``"freemium_blocked"`` / ``"bad_output"`` /
            ``"invalid_argument"`` / ``"forge_not_found"``。これに加え、forge が
            ``--json`` の構造化エラーで返す code（``"strategy_not_found"`` /
            ``"authentication_required"`` 等）はそのまま passthrough される (#12)。
        message: 人間可読なエラーメッセージ（要約・1 行）。
        detail: 追加文脈となる生本文（forge stderr / freemium パネル本文など）。
            ``message`` を要約に絞り、長い詳細はこちらへ分離する (#38)。無ければ None。
    """

    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(f"[{code}] {message}")


class ForgeNotFoundError(ForgeError):
    """forge バイナリが見つからない場合のエラー。"""

    def __init__(self, message: str) -> None:
        super().__init__("forge_not_found", message)
