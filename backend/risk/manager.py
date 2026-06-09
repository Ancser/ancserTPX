"""Close-window risk helper / 收盤前風控輔助.

v0.17.0 keeps this module intentionally small. Live and backtest engines own
their execution state; this helper only answers whether the clock is inside
the no-new-trade or flatten window.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List

from backend.db.models import RiskCheckResult, RiskStatus, TradeSignal


class RiskManager:
    """RiskManager for no-new-trade-before-close only.

    中文:
      - no_new_trade_minutes 內不允許開新倉。
      - flatten_time 後應強制平倉。
      - 其他舊版 daily loss / position cap / cooldown 規則已移除。
    """

    def __init__(
        self,
        flatten_time: time = time(15, 5),
        no_new_trade_minutes: int = 30,
    ):
        self.flatten_time = flatten_time
        self.no_new_trade_minutes = max(0, int(no_new_trade_minutes or 0))

    def _no_new_trade_time(self, current_time: datetime) -> time:
        flatten_dt = datetime.combine(current_time.date(), self.flatten_time)
        return (flatten_dt - timedelta(minutes=self.no_new_trade_minutes)).time()

    def pre_trade_check(
        self,
        signal: TradeSignal,
        daily_pnl: float,
        current_positions: int,
        current_time: datetime,
        recent_trades=None,
    ) -> RiskCheckResult:
        reasons: List[str] = []
        if current_time.time() >= self._no_new_trade_time(current_time):
            reasons.append(f"接近收盤 {self.flatten_time}，不開新倉")
        return RiskCheckResult(allowed=not reasons, reasons=reasons)

    def check_flatten_time(self, current_time: datetime) -> bool:
        return current_time.time() >= self.flatten_time

    def get_status(
        self,
        daily_pnl: float,
        current_positions: int,
        current_time: datetime,
    ) -> RiskStatus:
        flatten_dt = datetime.combine(current_time.date(), self.flatten_time)
        mins_to_flatten = max(0, int((flatten_dt - current_time).total_seconds() / 60))
        allowed = current_time.time() < self._no_new_trade_time(current_time)
        return RiskStatus(
            daily_pnl=daily_pnl,
            daily_loss_remaining=0.0,
            current_positions=current_positions,
            minutes_to_flatten=mins_to_flatten,
            is_trading_allowed=allowed,
            reason_blocked=None if allowed else "接近收盤，不開新倉",
        )
