"""alpha-forge-mcp の例外型。

すべて ``code``（機械可読な分類）と ``message``（人間可読）を持つ。MCP の tool
実行で raise されると FastMCP がエラー結果としてクライアントへ返す。
"""

from __future__ import annotations


class ForgeError(Exception):
    """forge コマンド実行に関する一般エラー。

    Args:
        code: ``"timeout"`` / ``"execution_failed"`` / ``"authentication_required"`` /
            ``"bad_output"`` / ``"invalid_argument"`` 等の機械可読な分類。
        message: 人間可読なエラーメッセージ。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class ForgeNotFoundError(ForgeError):
    """forge バイナリが見つからない場合のエラー。"""

    def __init__(self, message: str) -> None:
        super().__init__("forge_not_found", message)
