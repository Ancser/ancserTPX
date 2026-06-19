# ============================================================
# 文件: backend/backtest/metrics.py
# 功能: 績效指標計算 — 把成交 Trade 列表彙總成 Metrics dataclass
# 主要 API:
#   - calculate_all(trades, capital) → Metrics
#   - 子方法: win_rate / expectancy / max_drawdown / calmar_ratio /
#             profit_factor / max_consecutive_losses / daily_pnl_summary
# 版本變更 (v1.0.6):
#   - 新增 _aggregate_post_breakout: 彙總 60 分鐘 MFE/MAE/路徑統計
#   - 把這些欄位填入 Metrics.post_breakout_* (供前端顯示)
# 勝率定義: pnl > 0 即為 win — TP 與賺錢的 trail-SL 都算勝
# 關聯:
#   ← backend/backtest/engine.py
#   → backend/db/models.py (Trade, Metrics)
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
        total_gain = sum(t.pnl for t in completed if t.pnl and t.pnl > 0)
        total_loss = sum(t.pnl for t in completed if t.pnl and t.pnl < 0)
        metrics = Metrics(
            total_trades=len(completed),
            wins=len(wins),
            losses=len(losses),
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
            total_gain=total_gain,
            total_loss=total_loss,
            daily_pnl=self.daily_pnl_summary(completed),
        )

        self._aggregate_post_breakout(completed, metrics)
        self._aggregate_zone_source(completed, metrics)

        # ── Per-strategy breakdown (trend only) ──
        tf_trades = [t for t in completed if t.strategy == StrategyType.TREND_FOLLOW]
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

    @staticmethod
    def _aggregate_post_breakout(trades: List[Trade], metrics: Metrics) -> None:
        """Aggregate the per-trade 60m post-breakout fields onto Metrics.

        Buckets only count trades whose post-breakout window populated a value
        (post_breakout_max_favorable_ticks is not None). Trades that never had
        a tracker (e.g. pre-v1.0.6 cached results) are skipped silently.

        TP-clean   : reached TP within 60m, did NOT first cross trail or SL
        TP-trail   : reached TP within 60m, but first crossed the trail level
        TP-SL      : reached TP within 60m, but first crossed the SL level
        """
        sample = [t for t in trades if t.post_breakout_max_favorable_ticks is not None]
        if not sample:
            return

        fav_total = sum(t.post_breakout_max_favorable_ticks or 0 for t in sample)
        adv_total = sum(t.post_breakout_max_adverse_ticks or 0 for t in sample)
        n = len(sample)

        tp_clean = 0
        tp_after_trail = 0
        tp_after_sl = 0
        for t in sample:
            if not t.post_breakout_reached_tp:
                continue
            if t.post_breakout_broke_sl_first:
                tp_after_sl += 1
            elif t.post_breakout_broke_trail_first:
                tp_after_trail += 1
            else:
                tp_clean += 1

        metrics.post_breakout_sample_size      = n
        metrics.post_breakout_avg_max_fav_ticks = round(fav_total / n, 2) if n else 0.0
        metrics.post_breakout_avg_max_adv_ticks = round(adv_total / n, 2) if n else 0.0
        metrics.post_breakout_tp_clean         = tp_clean
        metrics.post_breakout_tp_after_trail   = tp_after_trail
        metrics.post_breakout_tp_after_sl      = tp_after_sl

    @staticmethod
    def _aggregate_zone_source(trades: List[Trade], metrics: Metrics) -> None:
        """Aggregate current-zone performance. v1.0.6 does not trade previous zones."""

        def _apply(prefix: str, bucket: List[Trade]) -> None:
            total = len(bucket)
            wins = sum(1 for t in bucket if (t.pnl or 0) > 0)
            pnl = sum(t.pnl or 0 for t in bucket)
            setattr(metrics, f"{prefix}_zone_trades", total)
            setattr(metrics, f"{prefix}_zone_wins", wins)
            setattr(metrics, f"{prefix}_zone_win_rate", (wins / total) if total else 0.0)
            setattr(metrics, f"{prefix}_zone_avg_pnl", (pnl / total) if total else 0.0)
            setattr(metrics, f"{prefix}_zone_total_pnl", pnl)

        current = [t for t in trades if getattr(t, "zone_source", None) == "current"]
        _apply("current", current)
