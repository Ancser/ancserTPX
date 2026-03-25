# ============================================================
# 文件: backend/risk/manager.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   ← backend/api/routes.py      (API 查詢風控狀態)
#   ← backend/backtest/engine.py  (回測時風控)
#   → backend/broker/topstepx.py  (強制平倉)
#   → backend/db/models.py        (RiskStatus, RiskCheckResult, TradeSignal)
# 函數結構:
#   - RiskManager.__init__(config)
#   - RiskManager.pre_trade_check(signal, daily_pnl, positions) -> RiskCheckResult
#   - RiskManager.check_flatten_time(current_time) -> bool
#   - RiskManager.get_status(daily_pnl, positions, current_time) -> RiskStatus
# ============================================================
"""
風控管理模組

TopstepX 規則:
  - 每日最大虧損限制（帳戶規則）
  - 3:10 PM CT 前必須平倉
  - 單口 NQ 限制
"""

from __future__ import annotations
from datetime import datetime, time, timedelta
from typing import List, Optional

from backend.db.models import (
    TradeSignal, Trade, RiskStatus, RiskCheckResult
)


class RiskManager:

    def __init__(
        self,
        max_daily_loss: float = 2000.0,
        max_position_size: int = 1,
        flatten_time: time = time(15, 5),    # 3:05 PM CT
        max_sl_per_trade: float = 350.0,     # 單筆最大 SL $350
        cooldown_minutes: int = 10,          # 連續虧損冷卻
        cooldown_after_n_losses: int = 3,    # 連虧 N 次後冷卻
        point_value: float = 20.0,
    ):
        self.max_daily_loss = max_daily_loss
        self.max_position_size = max_position_size
        self.flatten_time = flatten_time
        self.max_sl_per_trade = max_sl_per_trade
        self.cooldown_minutes = cooldown_minutes
        self.cooldown_after_n_losses = cooldown_after_n_losses
        self.point_value = point_value
        self._cooldown_until: Optional[datetime] = None

    def pre_trade_check(
        self,
        signal: TradeSignal,
        daily_pnl: float,
        current_positions: int,
        current_time: datetime,
        recent_trades: Optional[List[Trade]] = None,
    ) -> RiskCheckResult:
        """
        下單前風控檢查

        Returns:
            RiskCheckResult(allowed=True/False, reasons=[...])
        """
        reasons = []

        # 1. 每日虧損限制
        if daily_pnl <= -self.max_daily_loss:
            reasons.append(
                f"超日虧損限制: ${daily_pnl:.0f} ≤ -${self.max_daily_loss:.0f}"
            )

        # 2. 餘額不足承受 SL
        remaining = self.max_daily_loss + daily_pnl  # 還能虧多少
        sl_dollar = signal.sl_dollar
        if sl_dollar > remaining:
            reasons.append(
                f"SL ${sl_dollar:.0f} > 剩餘額度 ${remaining:.0f}"
            )

        # 3. 持倉上限
        if current_positions >= self.max_position_size:
            reasons.append(
                f"持倉已滿: {current_positions}/{self.max_position_size}"
            )

        # 4. 單筆 SL 上限
        if sl_dollar > self.max_sl_per_trade:
            reasons.append(
                f"SL ${sl_dollar:.0f} > 單筆上限 ${self.max_sl_per_trade:.0f}"
            )

        # 5. 接近強制平倉時間
        t = current_time.time()
        # 最後 30 分鐘不開新倉
        no_new_trade_time = (
            datetime.combine(datetime.today(), self.flatten_time)
            - timedelta(minutes=30)
        ).time()
        if t >= no_new_trade_time:
            reasons.append(
                f"接近平倉時間 {self.flatten_time}, 不開新倉"
            )

        # 6. 冷卻期
        if self._cooldown_until and current_time < self._cooldown_until:
            remaining_cd = (self._cooldown_until - current_time).seconds // 60
            reasons.append(f"冷卻中，剩餘 {remaining_cd} 分鐘")

        # 7. 連續虧損檢查
        if recent_trades:
            self._check_consecutive_losses(recent_trades, current_time)
            if self._cooldown_until and current_time < self._cooldown_until:
                remaining_cd = (self._cooldown_until - current_time).seconds // 60
                if f"冷卻中" not in str(reasons):
                    reasons.append(
                        f"連虧 {self.cooldown_after_n_losses} 次，冷卻 {remaining_cd} 分鐘"
                    )

        return RiskCheckResult(
            allowed=len(reasons) == 0,
            reasons=reasons,
        )

    def check_flatten_time(self, current_time: datetime) -> bool:
        """是否需要強制平倉"""
        return current_time.time() >= self.flatten_time

    def _check_consecutive_losses(
        self, recent_trades: List[Trade], current_time: datetime
    ):
        """連續虧損 → 冷卻"""
        consecutive = 0
        for t in reversed(recent_trades):
            if t.pnl is not None and t.pnl <= 0:
                consecutive += 1
            else:
                break

        if consecutive >= self.cooldown_after_n_losses:
            self._cooldown_until = current_time + timedelta(
                minutes=self.cooldown_minutes
            )

    def get_status(
        self,
        daily_pnl: float,
        current_positions: int,
        current_time: datetime,
    ) -> RiskStatus:
        """風控狀態快照"""
        flatten_dt = datetime.combine(
            current_time.date(), self.flatten_time
        )
        mins_to_flatten = max(
            0, int((flatten_dt - current_time).total_seconds() / 60)
        )

        is_cooldown = (
            self._cooldown_until is not None
            and current_time < self._cooldown_until
        )

        is_allowed = (
            daily_pnl > -self.max_daily_loss
            and current_time.time() < self.flatten_time
            and not is_cooldown
        )

        reason = None
        if not is_allowed:
            if daily_pnl <= -self.max_daily_loss:
                reason = "超日虧損限制"
            elif current_time.time() >= self.flatten_time:
                reason = "超過交易時間"
            elif is_cooldown:
                reason = "冷卻中"

        return RiskStatus(
            daily_pnl=daily_pnl,
            daily_loss_remaining=max(0, self.max_daily_loss + daily_pnl),
            current_positions=current_positions,
            is_cooldown=is_cooldown,
            cooldown_until=self._cooldown_until,
            minutes_to_flatten=mins_to_flatten,
            is_trading_allowed=is_allowed,
            reason_blocked=reason,
        )
