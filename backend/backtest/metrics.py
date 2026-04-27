# ============================================================
# 文件: backend/backtest/metrics.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   ← backend/backtest/engine.py  (回測完成後調用)
#   ← backend/api/routes.py       (API 查詢績效)
#   → backend/db/models.py        (Trade, Metrics)
# 函數結構:
#   - MetricsCalculator.calculate_all(trades, capital) -> Metrics
#   - MetricsCalculator.win_rate(trades) -> float
#   - MetricsCalculator.expectancy(trades) -> float
#   - MetricsCalculator.max_drawdown(trades, capital) -> (float, float)
#   - MetricsCalculator.sharpe_ratio(trades) -> float
#   - MetricsCalculator.profit_factor(trades) -> float
#   - MetricsCalculator.max_consecutive_losses(trades) -> int
#   - MetricsCalculator.daily_pnl_summary(trades) -> dict
# ============================================================
"""
績效指標計算模組
"""

from __future__ import annotations
import math
from typing import Dict, List, Tuple

from backend.db.models import ExitReason, Metrics, Trade, StrategyType


class MetricsCalculator:

    def calculate_all(
        self, trades: List[Trade], initial_capital: float
    ) -> Metrics:
        """一次計算所有指標"""
        if not trades:
            return Metrics()

        completed = [t for t in trades if t.pnl is not None]
        if not completed:
            return Metrics()

        # Win = positive PnL (TP hits AND profitable trail-SL stops both count as wins).
        # Trail SL set at entry + N ticks locks in a small profit, so its PnL is usually
        # positive — counting it as a "loss" because exit_reason != TP was a mis-classification.
        wins = [t for t in completed if t.pnl > 0]
        losses = [t for t in completed if t.pnl <= 0]

        win_pnls = [t.pnl for t in wins]
        loss_pnls = [t.pnl for t in losses]

        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

        wr = len(wins) / len(completed) if completed else 0

        dd, dd_pct = self.max_drawdown(completed, initial_capital)

        total_pnl = sum(t.pnl for t in completed)
        metrics = Metrics(
            total_trades=len(completed),
            wins=len(tp_hits),
            losses=len(non_tp),
            win_rate=wr,
            avg_win=avg_win,
            avg_loss=avg_loss,
            avg_rr_ratio=abs(avg_win / avg_loss) if avg_loss != 0 else 0,
            expectancy=self.expectancy(completed),
            max_drawdown=dd,
            max_drawdown_pct=dd_pct,
            calmar_ratio=self.calmar_ratio(total_pnl, dd),
            profit_factor=self.profit_factor(completed),
            max_consecutive_losses=self.max_consecutive_losses(completed),
            total_pnl=total_pnl,
            daily_pnl=self.daily_pnl_summary(completed),
        )

        # ── Per-strategy breakdown ──
        # Reversion / Trend Follow
        rev_trades = [t for t in completed if t.strategy == StrategyType.REVERSION]
        tf_trades = [t for t in completed if t.strategy == StrategyType.TREND_FOLLOW]

        if rev_trades:
            metrics.reversion_metrics = self._sub_metrics(rev_trades, initial_capital)
        if tf_trades:
            metrics.trend_follow_metrics = self._sub_metrics(tf_trades, initial_capital)

        return metrics

    def _sub_metrics(self, trades: List[Trade], initial_capital: float) -> Metrics:
        """Calculate metrics for a subset of trades."""
        if not trades:
            return Metrics()
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        win_pnls = [t.pnl for t in wins]
        loss_pnls = [t.pnl for t in losses]
        avg_w = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_l = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
        wr = len(wins) / len(trades) if trades else 0
        dd, dd_pct = self.max_drawdown(trades, initial_capital)
        return Metrics(
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=wr,
            avg_win=avg_w,
            avg_loss=avg_l,
            avg_rr_ratio=abs(avg_w / avg_l) if avg_l != 0 else 0,
            expectancy=self.expectancy(trades),
            max_drawdown=dd,
            max_drawdown_pct=dd_pct,
            profit_factor=self.profit_factor(trades),
            total_pnl=sum(t.pnl for t in trades),
        )

    @staticmethod
    def win_rate(trades: List[Trade]) -> float:
        """Win rate based on PnL > 0 (TP + profitable trail-SL both count)."""
        completed = [t for t in trades if t.pnl is not None]
        if not completed:
            return 0.0
        wins = sum(1 for t in completed if t.pnl > 0)
        return wins / len(completed)

    @staticmethod
    def expectancy(trades: List[Trade]) -> float:
        """期望值 = (wr × avg_win) - (lr × avg_loss)"""
        completed = [t for t in trades if t.pnl is not None]
        if not completed:
            return 0.0

        wins = [t.pnl for t in completed if t.pnl > 0]
        losses = [abs(t.pnl) for t in completed if t.pnl <= 0]

        wr = len(wins) / len(completed)
        lr = 1 - wr
        avg_w = sum(wins) / len(wins) if wins else 0
        avg_l = sum(losses) / len(losses) if losses else 0

        return (wr * avg_w) - (lr * avg_l)

    @staticmethod
    def max_drawdown(
        trades: List[Trade], initial_capital: float
    ) -> Tuple[float, float]:
        """最大回撤 (金額, 百分比)"""
        equity = initial_capital
        peak = equity
        max_dd = 0.0
        max_dd_pct = 0.0

        for t in trades:
            if t.pnl is not None:
                equity += t.pnl
                peak = max(peak, equity)
                dd = peak - equity
                if dd > max_dd:
                    max_dd = dd
                    max_dd_pct = dd / peak if peak > 0 else 0

        return max_dd, max_dd_pct

    @staticmethod
    def calmar_ratio(total_pnl: float, max_drawdown: float) -> float:
        """Calmar Ratio = Total PnL / Max Drawdown.
        High ratio = good return relative to worst drawdown.
        Returns 0 if no profit, 999 if no drawdown but profitable.
        """
        if total_pnl <= 0:
            return 0.0
        if max_drawdown <= 0:
            return 999.0   # no drawdown = perfect
        return total_pnl / max_drawdown

    @staticmethod
    def profit_factor(trades: List[Trade]) -> float:
        """Profit Factor = gross_profit / gross_loss"""
        gross_profit = sum(t.pnl for t in trades if t.pnl and t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl and t.pnl < 0))

        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0
        return gross_profit / gross_loss

    @staticmethod
    def max_consecutive_losses(trades: List[Trade]) -> int:
        """最大連續虧損次數"""
        max_streak = 0
        current = 0
        for t in trades:
            if t.pnl is not None and t.pnl <= 0:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    @staticmethod
    def daily_pnl_summary(trades: List[Trade]) -> Dict[str, float]:
        """每日盈虧匯總"""
        daily: Dict[str, float] = {}
        for t in trades:
            if t.pnl is not None and t.exit_time:
                date_str = t.exit_time.strftime("%Y-%m-%d")
                daily[date_str] = daily.get(date_str, 0) + t.pnl
        return daily
