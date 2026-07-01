"""1.0.8 研究:「30m 突破確認 + 5m 邊界」進場邏輯 vs 現行 overlap / 純 5m。

三種變體,全部沿用 preset #3 的風控參數(RR6 / C3 / SL80 / Trail50L10 /
FT2 / SesOFF),只差進場條件,確保 apples-to-apples:

  A. pure5m       — method=single, 交易 5m zone 突破,完全不看 30m。
  B. 5m+30mConfirm— method=single, 交易 5m zone 突破,**且要求 30m 同方向
                    突破確認**(30m 只當方向 gate,不當邊界)。← 使用者提案
  C. #3 overlap   — 現行:要求 5m/30m 價值區間「空間重疊」才有 merged band,
                    交易最小 TF(5m)邊界。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.confirm_tf_study
"""
from __future__ import annotations

import copy

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Direction,
    StrategyParams,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.consolidation import ClockBucketZoneDetector
from backend.strategy.trend_follow import SessionTrendFollow
from backend.terminal_live import (
    BUILTIN_PRESETS,
    CODEX_630_PRESET_3,
    _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
CONFIRM_TF = "30m"


class ConfirmTFBacktest(BacktestEngine):
    """5m-border breakout, gated by a same-direction breakout on CONFIRM_TF."""

    def __init__(self, *args, confirm_tf: str = CONFIRM_TF, **kw):
        super().__init__(*args, **kw)
        self._confirm_det = ClockBucketZoneDetector(
            area_timeframe=confirm_tf,
            value_area_pct=float(self.config.value_area_pct),
            tick_size=self.TICK_SIZE,
            max_recent=10,
        )
        # Separate breakout tracker reusing the exact 5m breakout definition.
        self._confirm_follow = SessionTrendFollow(params=self.strategy_params)

        _orig_eval = self.trend_follow.evaluate

        def gated_eval(candle, zones, is_mature):
            sig = _orig_eval(candle, zones, is_mature)
            if sig is None:
                return None
            want = "up" if sig.direction == Direction.BUY else "down"
            if not self._confirm_follow._armed:
                return None
            if self._confirm_follow._breakout_direction != want:
                return None
            return sig

        self.trend_follow.evaluate = gated_eval

    def _process_candle(self, candle):
        # Advance the CONFIRM_TF breakout state BEFORE the 5m evaluate runs.
        self._confirm_det.update(candle)
        self._confirm_follow.observe(
            candle,
            self._confirm_det.get_recent_zones(),
            self._confirm_det.is_zone_mature,
        )
        super()._process_candle(candle)


def _single_5m_params(base: StrategyParams) -> StrategyParams:
    p = copy.deepcopy(base)
    p.method = "single"
    p.area_timeframe = "5m"
    p.tf_combo = []
    return p


def _run(engine_cls, params, config, candles):
    result = engine_cls(
        config=config,
        strategy_params=params,
        zone_timeline=None,
        record_equity=False,
    ).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades),
        "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl),
        "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor),
        "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
        "avg_win": float(m.avg_win),
        "avg_loss": float(m.avg_loss),
    }


def _fmt(label, r):
    print(
        f"{label:<24} "
        f"{r['trades']:>6} {100*r['win_rate']:>6.1f}% "
        f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} "
        f"{r['pf']:>6.2f} {r['calmar']:>7.2f} {r['expectancy']:>+9.2f}",
        flush=True,
    )


def main() -> None:
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles in store.")
    candles.sort(key=lambda c: c.timestamp)
    print(
        f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        flush=True,
    )

    preset = BUILTIN_PRESETS[CODEX_630_PRESET_3]
    contract_id = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base_params = _build_strategy_params(preset, contract_id)  # overlap 5m+30m

    def _config(params):
        return BacktestConfig(
            strategies=["trend"],
            initial_capital=INITIAL_CAPITAL,
            symbol=_extract_symbol(contract_id),
            commission_rt=get_commission_rt(contract_id),
            fees_rt=get_fees_rt(contract_id),
            value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
        )

    single_params = _single_5m_params(base_params)

    header = (
        f"{'variant':<24} "
        f"{'trades':>6} {'win%':>7} {'pnl':>11} {'maxDD':>9} "
        f"{'PF':>6} {'Calmar':>7} {'expect':>9}"
    )
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    a = _run(BacktestEngine, single_params, _config(single_params), candles)
    b = _run(ConfirmTFBacktest, single_params, _config(single_params), candles)
    c = _run(BacktestEngine, base_params, _config(base_params), candles)

    _fmt("A pure5m (no 30m)", a)
    _fmt("B 5m+30m confirm", b)
    _fmt("C #3 overlap(smallest)", c)


if __name__ == "__main__":
    main()
