"""alpha-forge-mcp の MCP サーバ（stdio）。

FastMCP に以下を登録する。いずれも ``ForgeClient`` を介して forge バイナリを
subprocess で呼ぶ（コアロジックは含まない／露出しない）。

- **Tools**（17）: read 系（list/get/generate_pinescript/forge_status・#28 の
  list_journals/get_journal/exploration_status/get_indicator）と run 系
  （run_backtest / run_optimize / run_walk_forward / run_monte_carlo / fetch_data /
  save_strategy・#27 の apply_optimization）。read 系 / run 系を ``ToolAnnotations``
  で区別し（#16）、戻り値型注釈から ``outputSchema`` を生成して structured output を
  返す（#17）。#24/#25/#26 で WFT・MC・data fetch・strategy save・forge_status、
  #27/#28 で optimize apply・journal/explore/indicator の read を追加。
- **Resources**（#18）: read データを ``forge://...`` で公開し、Claude Code 等の
  @メンション参照を可能にする（Tool は能動実行用に併存）。
- **Prompts**（#19）: 定型ワークフローを公開し、Claude Code のスラッシュコマンド化。

``ForgeClient`` は遅延生成し、forge 未検出/未認証時は ``ForgeError`` として FastMCP
経由でクライアントへ返す（import や起動自体は妨げない＝IDE 側で扱いやすい）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from alpha_forge_mcp.envelope import Envelope, envelope
from alpha_forge_mcp.forge_client import (
    _DATE_RE,
    _IDENT_RE,
    _PATH_RE,
    ForgeClient,
)
from alpha_forge_mcp.forge_client import forge_status as _forge_status

# issue #29: サーバ instructions でワークフロー全体像をクライアント（Claude 等）へ提示する。
# initialize 応答に載り、エージェントが「どの tool をどの順で呼ぶか」を最初に把握できる。
_INSTRUCTIONS = (
    "AlphaForge への薄い MCP ラッパーです（forge CLI を subprocess 実行）。"
    "標準ワークフロー: "
    "1) forge_status で前提（バイナリ/認証/プラン）を確認 → "
    "2) fetch_data で対象 symbol のヒストリカルデータを取得 → "
    "3) run_backtest で戦略を検証 → "
    "4) run_optimize（save=True 既定）でパラメータ最適化 → "
    "5) run_walk_forward で out-of-sample のロバスト性を確認 → "
    "6) apply_optimization で最適化結果を戦略へ適用 → "
    "7) generate_pinescript で TradingView 用 Pine Script v6 を出力。"
    " 全 tool は {ok, data, error} の envelope を返す（例外を投げない）。"
    " run/optimize/walk-forward は長時間（最大数百秒）かかり、対応クライアントでは"
    " 進捗（progress）通知を送る。read 系（list_*/get_*/exploration_status/get_indicator/"
    "forge_status）は副作用なしで安全に呼べる。"
)

# name は PyPI パッケージ名（alpha-forge-mcp）と一致させる（issue #3）。
mcp = FastMCP("alpha-forge-mcp", instructions=_INSTRUCTIONS)

# issue #29: 最適化対象メトリクスを Literal で enum 化し inputSchema に enum 制約を出す。
# forge の backtest メトリクス dict のキー（src/alpha_forge/backtest/metrics）のうち、
# 「大きいほど良い」最適化目的として妥当な代表値に絞る。既存テスト/呼び出しが渡す
# sharpe_ratio を含む。値は forge CLI の --metric にそのまま渡される。
OptimizeMetric = Literal[
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "total_return_pct",
    "cagr_pct",
    "profit_factor",
    "win_rate_pct",
    "expectancy_pct",
    "omega_ratio",
]

# issue #37: 各引数に description / examples / pattern / minimum を付与して inputSchema へ
# 反映させる。FastMCP(mcp>=1.27) は pydantic.Field のこれらを inputSchema.properties に
# 載せる（本リポの mcp 1.27.2 で実機検証済み）。pattern は実行時検証（forge_client の
# ``_validate_*`` / 同名の正規表現）と単一ソースを共有して乖離を防ぐため、forge_client の
# 正規表現を再利用する。なお schema 制約違反は MCP 境界で（pydantic 検証として）弾かれ、
# ``_validate_*`` は resource など直接呼び出し経路向けの多層防御として残す。
# 型エイリアスで同種引数の重複定義を避ける（symbol / strategy_id は複数 tool で再利用）。
SymbolArg = Annotated[
    str,
    Field(
        description=(
            "Ticker / instrument symbol. Supports exchange-specific notation "
            "(indices, futures, FX, crypto)."
        ),
        examples=["AAPL", "^VIX", "CL=F", "USDJPY=X", "BTC-USD"],
        pattern=_IDENT_RE.pattern,
    ),
]
StrategyIdArg = Annotated[
    str,
    Field(
        description="Registered strategy id (see list_strategies).",
        examples=["sma_cross_v1", "cl_hmm_bb_rsi_v1"],
        pattern=_IDENT_RE.pattern,
    ),
]
OptionalStrategyIdArg = Annotated[
    str | None,
    Field(
        description="Optional filter by registered strategy id (see list_strategies).",
        examples=["sma_cross_v1"],
        pattern=_IDENT_RE.pattern,
    ),
]
ResultIdArg = Annotated[
    str,
    Field(
        description="Saved result id = a strategy_id or a run_id (see list_results).",
        examples=["sma_cross_v1", "run_a1b2c3"],
        pattern=_IDENT_RE.pattern,
    ),
]
StartDateArg = Annotated[
    str | None,
    Field(
        description="Inclusive start date in YYYY-MM-DD. Defaults to the cached range start.",
        examples=["2020-01-01"],
        pattern=_DATE_RE.pattern,
        json_schema_extra={"format": "date"},
    ),
]
EndDateArg = Annotated[
    str | None,
    Field(
        description="Inclusive end date in YYYY-MM-DD. Defaults to the cached range end.",
        examples=["2023-12-31"],
        pattern=_DATE_RE.pattern,
        json_schema_extra={"format": "date"},
    ),
]
TrialsArg = Annotated[
    int | None,
    Field(
        description="Number of Optuna trials (>= 1). Defaults to the alpha-forge default.",
        ge=1,
        examples=[50],
    ),
]
WindowsArg = Annotated[
    int | None,
    Field(
        description="Number of walk-forward windows (>= 1). Defaults to 5.",
        ge=1,
        examples=[5],
    ),
]
SimulationsArg = Annotated[
    int | None,
    Field(
        description="Number of Monte Carlo simulations (>= 1). Defaults to 1000.",
        ge=1,
        examples=[1000],
    ),
]
MetricArg = Annotated[
    OptimizeMetric | None,
    Field(
        description=(
            "Optimization target metric (enum; bigger is better). Defaults to sharpe_ratio."
        ),
    ),
]
PeriodArg = Annotated[
    str | None,
    Field(
        description="Lookback window, e.g. 1y / 6m / 30d / max. Defaults to 1y.",
        examples=["1y", "5y", "6m", "30d", "max"],
    ),
]
JsonBodyArg = Annotated[
    str,
    Field(
        description=(
            "The full strategy-definition JSON as a string (the JSON body itself, "
            "NOT a file path)."
        ),
        examples=['{"strategy_id": "my_strat", "version": "1.0.0", "timeframe": "1d"}'],
    ),
]
ResultFileArg = Annotated[
    str,
    Field(
        description=(
            "Filesystem path to the optimization result JSON (the saved_path from "
            "run_optimize(save=true)) — a path, not inline JSON."
        ),
        examples=["/path/to/results/optimize_sma_cross_v1.json"],
        pattern=_PATH_RE.pattern,
    ),
]
GoalArg = Annotated[
    str | None,
    Field(
        description='Exploration goal name to filter by. Defaults to the "default" goal.',
        examples=["default"],
        pattern=_IDENT_RE.pattern,
    ),
]
IndicatorArg = Annotated[
    str,
    Field(
        description="Technical indicator name to look up metadata for (no price computation).",
        examples=["RSI", "MACD"],
        pattern=_IDENT_RE.pattern,
    ),
]
SummaryArg = Annotated[
    bool,
    Field(
        description=(
            "Fold heavy arrays (trades / per-bar series) into counts to save context; "
            "set false for the full arrays."
        ),
    ),
]
SaveArg = Annotated[
    bool,
    Field(
        description=(
            "Persist the optimization result JSON (returns saved_path for "
            "apply_optimization); set false to skip saving."
        ),
    ),
]
WithWebhookArg = Annotated[
    bool,
    Field(description="Include AlphaStrike webhook alert wiring in the generated Pine Script."),
]

# issue #3: FastMCP はコンストラクタで version を受け取れず、未設定だと低レベル
# Server が mcp ライブラリ自身の版を serverInfo.version として返す。自パッケージの
# 版を低レベル Server に直接設定して initialize 応答へ反映させる。
try:
    mcp._mcp_server.version = version("alpha-forge-mcp")
except PackageNotFoundError:  # pragma: no cover - 未インストールのソース直接実行時のみ
    pass

# issue #16: read 系は副作用なし・冪等・ローカル参照のみ（外部世界に触れない）。
# run 系は外部市場データを取得し結果を永続化する非冪等な実行（openWorld）。
# クライアント（Claude 等）はこれを自動実行可否や表示の判断に使う。
_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
_RUN = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

_client: ForgeClient | None = None


def _get_client() -> ForgeClient:
    """ForgeClient を遅延生成（forge 未検出なら ForgeNotFoundError を送出）。"""
    global _client
    if _client is None:
        _client = ForgeClient()
    return _client


async def _run_with_progress(
    ctx: Context | None,
    label: str,
    work: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """長時間ジョブを実行し、対応クライアントへ progress を送る（#29）。

    ``ForgeClient`` の呼び出しは ``subprocess.run`` でブロックするため、
    ``anyio.to_thread.run_sync`` でワーカースレッドへ退避し、その間イベント
    ループ（progress 通知や他リクエスト）を塞がない。進捗は subprocess の
    途中経過を取得できないため「開始(0/1)→完了(1/1)」の確定ブラケットで送る
    （真の途中経過の捏造はしない）。``ctx`` 未指定（progress 非対応クライアント
    やテスト）でもジョブ自体は通常どおり実行する。

    例外はここでは握らず ``@envelope`` まで伝播させ、統一 envelope 契約を保つ。
    """
    if ctx is not None:
        await ctx.report_progress(progress=0.0, total=1.0, message=f"{label} 開始")
    result = await anyio.to_thread.run_sync(work)
    if ctx is not None:
        await ctx.report_progress(progress=1.0, total=1.0, message=f"{label} 完了")
    return result


# ---------------------------------------------------------------------------
# Tools（#16 annotations / #17 structured output / #23 error envelope）
# 戻り値は ``@envelope`` で統一した error envelope（Envelope TypedDict）。
# 成功時 {"ok": True, "data": <forge の JSON>, "error": None} /
# 失敗時 {"ok": False, "data": None, "error": {"code", "message", "detail"}}。
# forge は全コマンドで JSON オブジェクトを返すため data は object であり、
# FastMCP は Envelope から ok/data/error 枝を持つ object の outputSchema を生成する。
# 例外を素通しさせると FastMCP が自由文 ToolError に再ラップし code が構造化
# フィールドとして届かないため、各 tool は @envelope で必ず envelope を返す（#23）。
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
@envelope
def list_strategies() -> Envelope:
    """List all registered AlphaForge strategies (strategy_id, name, version, timeframe)."""
    return _get_client().list_strategies()


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_strategy(strategy_id: StrategyIdArg) -> Envelope:
    """Get the full JSON definition of a registered strategy by its strategy_id."""
    return _get_client().get_strategy(strategy_id)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def list_results(strategy_id: OptionalStrategyIdArg = None) -> Envelope:
    """List saved backtest results, optionally filtered by strategy_id."""
    return _get_client().list_results(strategy_id)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_result(result_id: ResultIdArg, summary: SummaryArg = True) -> Envelope:
    """Get metrics for a saved backtest result (result_id = strategy_id or run_id).

    summary=True (default) folds heavy arrays (trades / equity_curve / buy_hold_curve)
    into counts to save context; pass summary=false to get the full arrays.
    """
    return _get_client().get_result(result_id, summary=summary)


@mcp.tool(annotations=_RUN)
@envelope
async def run_backtest(
    symbol: SymbolArg,
    strategy_id: StrategyIdArg,
    start: StartDateArg = None,
    end: EndDateArg = None,
    summary: SummaryArg = True,
    ctx: Context | None = None,
) -> Envelope:
    """Run a backtest for `symbol` with a registered strategy. Optional dates are YYYY-MM-DD.

    Prerequisite: call `fetch_data` for the symbol first so the OHLCV cache exists.
    summary=True (default) omits heavy arrays (trades / per-bar series) to save context;
    pass summary=false for the full result.
    Long-running: up to a 300-second timeout; reports progress to capable clients.
    """
    return await _run_with_progress(
        ctx,
        "backtest",
        lambda: _get_client().run_backtest(
            symbol, strategy_id, start=start, end=end, summary=summary
        ),
    )


@mcp.tool(annotations=_RUN)
@envelope
async def run_optimize(
    symbol: SymbolArg,
    strategy_id: StrategyIdArg,
    metric: MetricArg = None,
    trials: TrialsArg = None,
    save: SaveArg = True,
    ctx: Context | None = None,
) -> Envelope:
    """Optimize strategy parameters with Optuna for `symbol`. metric defaults to sharpe_ratio.

    save defaults to true so the result JSON is persisted (with `saved_path` in the
    response) and can be fed to `apply_optimization`; pass save=false to skip saving.
    Long-running: up to a 600-second timeout; reports progress to capable clients.
    """
    return await _run_with_progress(
        ctx,
        "optimize",
        lambda: _get_client().run_optimize(
            symbol, strategy_id, metric=metric, trials=trials, save=save
        ),
    )


@mcp.tool(annotations=_READ_ONLY)
@envelope
def generate_pinescript(
    strategy_id: StrategyIdArg, with_webhook: WithWebhookArg = False
) -> Envelope:
    """Generate TradingView Pine Script v6 for a strategy. Returns {strategy_id, pinescript}."""
    return _get_client().generate_pinescript(strategy_id, with_webhook=with_webhook)


# ---------------------------------------------------------------------------
# Tool 網羅拡張（#24 WFT/MC・#25 fetch/save・#26 status）。
# 既存 7 tool と同形（@mcp.tool annotations + @envelope）で契約を揃える。
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_RUN)
@envelope
async def run_walk_forward(
    symbol: SymbolArg,
    strategy_id: StrategyIdArg,
    windows: WindowsArg = None,
    metric: MetricArg = None,
    ctx: Context | None = None,
) -> Envelope:
    """Run walk-forward optimization for `symbol` (out-of-sample robustness check).

    windows defaults to 5, metric to sharpe_ratio. Run it after run_optimize to compare
    in-sample vs out-of-sample behaviour (the optimize_and_verify workflow).
    Long-running: up to a 600-second timeout; reports progress to capable clients.
    """
    return await _run_with_progress(
        ctx,
        "walk-forward",
        lambda: _get_client().run_walk_forward(
            symbol, strategy_id, windows=windows, metric=metric
        ),
    )


@mcp.tool(annotations=_RUN)
@envelope
async def run_monte_carlo(
    result_id: ResultIdArg,
    simulations: SimulationsArg = None,
    ctx: Context | None = None,
) -> Envelope:
    """Run a Monte Carlo simulation from a saved backtest result (resamples its trades).

    Prerequisite: a saved result (run_backtest/run_optimize with save) — result_id =
    strategy_id or run_id. simulations defaults to 1000. Returns ruin probability, equity
    percentiles, and drawdown distribution for risk assessment.
    Long-running: reports progress to capable clients; has an execution timeout.
    """
    return await _run_with_progress(
        ctx,
        "monte-carlo",
        lambda: _get_client().run_monte_carlo(result_id, simulations=simulations),
    )


@mcp.tool(annotations=_RUN)
@envelope
async def fetch_data(
    symbol: SymbolArg,
    period: PeriodArg = None,
    ctx: Context | None = None,
) -> Envelope:
    """Fetch & cache historical OHLCV for `symbol` (prerequisite for run_backtest).

    period is e.g. 1y / 5y / 6m / 30d / max (defaults to 1y). Returns {symbol, period,
    output}. The CLI has no --start/--end, so only period is exposed. Run this before
    run_backtest. Reports progress to capable clients; has an execution timeout.
    """
    return await _run_with_progress(
        ctx,
        "fetch-data",
        lambda: _get_client().fetch_data(symbol, period=period),
    )


@mcp.tool(annotations=_RUN)
@envelope
async def save_strategy(json_body: JsonBodyArg, ctx: Context | None = None) -> Envelope:
    """Register a strategy from its JSON body (not a file path; agent-friendly).

    Pass the full strategy-definition JSON as a string; it is validated as a JSON object
    and written to a temp file before `strategy save`. Returns {output}. A registered
    strategy is the prerequisite for run_backtest/run_optimize. Reports progress to
    capable clients; has an execution timeout.
    """
    return await _run_with_progress(
        ctx,
        "save-strategy",
        lambda: _get_client().save_strategy(json_body),
    )


@mcp.tool(annotations=_READ_ONLY)
@envelope
def forge_status() -> Envelope:
    """Report alpha-forge capabilities/prerequisites before use (doctor + version).

    Read-only triage: returns {binary_found, version, authenticated, plan, doctor, error}.
    Never fails when the binary is missing — returns binary_found=false instead.
    """
    return _forge_status()


# ---------------------------------------------------------------------------
# optimize apply + journal/explore/indicator の read 公開（#27/#28）。
# apply_optimization は戦略を上書き保存する write 系（run 注釈）、
# 残り (list_journals/get_journal/exploration_status/get_indicator) は read 系。
# 書き込み系・ml/pairs は今回スコープ外（段階追加）。
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_RUN)
@envelope
async def apply_optimization(
    result_file: ResultFileArg,
    strategy_id: StrategyIdArg,
    ctx: Context | None = None,
) -> Envelope:
    """Apply an optimization result file to a strategy, saving `<strategy_id>_optimized`.

    Prerequisite: run_optimize(save=true) — result_file is its `saved_path`. Runs
    non-interactively (--yes). Returns {result_file, strategy_id, output}. Follow up by
    generating Pine Script for `<strategy_id>_optimized`. Reports progress to capable
    clients; has an execution timeout.
    """
    return await _run_with_progress(
        ctx,
        "apply-optimization",
        lambda: _get_client().apply_optimization(result_file, strategy_id),
    )


@mcp.tool(annotations=_READ_ONLY)
@envelope
def list_journals() -> Envelope:
    """List strategies that have a journal (history of snapshots and runs)."""
    return _get_client().list_journals()


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_journal(strategy_id: StrategyIdArg) -> Envelope:
    """Get the full journal (snapshots, runs, tags, notes) for a strategy_id."""
    return _get_client().get_journal(strategy_id)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def exploration_status(goal: GoalArg = None) -> Envelope:
    """Show the strategy-exploration coverage map (explored vs. untried combos).

    Optional `goal` filters by exploration goal; defaults to the "default" goal.
    """
    return _get_client().exploration_status(goal)


@mcp.tool(annotations=_READ_ONLY)
@envelope
def get_indicator(indicator: IndicatorArg) -> Envelope:
    """Get metadata for a technical indicator (description, parameters, output, example).

    `indicator` is the indicator name (e.g. RSI, MACD). This is metadata only — the CLI
    has no compute-over-symbol command — so it does not run a calculation on price data.
    """
    return _get_client().get_indicator(indicator)


# ---------------------------------------------------------------------------
# Resources（#18）
# read 系データを forge://... で公開する。Tool と同じ ForgeClient へ委譲し、
# application/json で返す。Claude Code 等では @メンションで context に取り込める。
# ---------------------------------------------------------------------------


def _as_json(data: Any) -> str:
    """resource ペイロードを JSON 文字列にする（非 ASCII 保持・非シリアライズ値は文字列化）。"""
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.resource("forge://strategies", mime_type="application/json")
def resource_strategies() -> str:
    """All registered strategies (same payload as the list_strategies tool)."""
    return _as_json(_get_client().list_strategies())


@mcp.resource("forge://strategy/{strategy_id}", mime_type="application/json")
def resource_strategy(strategy_id: str) -> str:
    """Full JSON definition of one strategy by strategy_id."""
    return _as_json(_get_client().get_strategy(strategy_id))


@mcp.resource("forge://results", mime_type="application/json")
def resource_results() -> str:
    """All saved backtest results (same payload as the list_results tool)."""
    return _as_json(_get_client().list_results())


@mcp.resource("forge://result/{result_id}", mime_type="application/json")
def resource_result(result_id: str) -> str:
    """Metrics and trades for one saved backtest result by result_id."""
    return _as_json(_get_client().get_result(result_id))


# ---------------------------------------------------------------------------
# Prompts（#19）
# forge を呼ばない純粋なテンプレート。Claude Code では
# /mcp__alpha-forge-mcp__<name> のスラッシュコマンドとして公開される。
# ---------------------------------------------------------------------------


@mcp.prompt(title="Backtest and review a strategy")
def backtest_and_review(strategy_id: str, symbol: str) -> str:
    """Guide: run a backtest for a strategy/symbol, then review the metrics."""
    return (
        f"Run a backtest for strategy `{strategy_id}` on symbol `{symbol}` using the "
        f"`run_backtest` tool. Then review the result: summarize the key metrics "
        f"(Sharpe ratio, max drawdown, win rate, profit factor, total return / CAGR), "
        f"judge whether the strategy looks robust, and call out red flags such as too "
        f"few trades, look-ahead bias, or an equity curve driven by a single outlier. "
        f"Be concise and quantitative."
    )


@mcp.prompt(title="Optimize and verify a strategy")
def optimize_and_verify(strategy_id: str, symbol: str) -> str:
    """Guide: optimize a strategy with Optuna, then check it is not overfit."""
    return (
        f"Optimize the parameters of strategy `{strategy_id}` for symbol `{symbol}` "
        f"using the `run_optimize` tool (Optuna TPE). Then verify the optimized result "
        f"is not overfit: compare in-sample vs walk-forward / out-of-sample behaviour, "
        f"check the number of trials and resulting trades, and warn if the improvement "
        f"looks like curve-fitting rather than a genuine edge. Recommend whether to keep "
        f"or discard the optimization, and why."
    )


def main() -> None:
    """stdio トランスポートで MCP サーバを起動する（``uvx alpha-forge-mcp`` のエントリ）。"""
    mcp.run(transport="stdio")
