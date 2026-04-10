# ============================================================
# 文件: backend/db/models.py
# 狀態: 已完成
# 問題: 無
# 關聯文件: ← 幾乎所有後端文件依賴此文件
# 函數結構:
#   - Candle(dataclass)         : 單根 K 線數據
#   - VolumeProfileResult(dc)   : VP 計算結果 (POC/VAH/VAL)
#   - ConsolidationZone(dc)     : 盤整區間完整定義
#   - TradeSignal(dc)           : 策略發出的交易信號
#   - Trade(dc)                 : 已成交交易紀錄
#   - BreakoutAnalysis(dc)      : 突破成交量分析
#   - BacktestConfig(dc)        : 回測參數配置
#   - BacktestResult(dc)        : 回測完整結果
#   - Metrics(dc)               : 績效指標集合
#   - RiskStatus(dc)            : 風控狀態快照
#   - OrderRequest/Response(dc) : TopstepX 訂單格式
# ============================================================

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from enum import Enum


# ── 枚舉 ─────────────────────────────────────────────

class ZoneStatus(str, Enum):
    FORMING = "forming"
    ACTIVE  = "active"
    LEFT    = "left"


class Direction(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class ExitReason(str, Enum):
    TP      = "tp"
    SL      = "sl"
    FLATTEN = "flatten"   # 3:10 PM CT 強制平倉
    MANUAL  = "manual"


class StrategyType(str, Enum):
    REVERSION    = "reversion"
    TREND_FOLLOW = "trend_follow"


class BarUnit(int, Enum):
    """TopstepX retrieveBars unit 參數"""
    SECOND = 1
    MINUTE = 2
    HOUR   = 3
    DAY    = 4
    WEEK   = 5
    MONTH  = 6


# ── 市場數據 ──────────────────────────────────────────

@dataclass
class Candle:
    """單根 K 線"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str = "NQ"
    interval: str = "5m"

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def body_range(self) -> float:
        return abs(self.close - self.open)

    @property
    def full_range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


# ── Volume Profile ────────────────────────────────────

@dataclass
class VolumeProfileResult:
    """Volume Profile 計算結果"""
    poc: float                      # Point of Control 最大成交量價位
    vah: float                      # Value Area High (80%)
    val: float                      # Value Area Low  (80%)
    high_100: float                 # 100% 區間高點
    low_100: float                  # 100% 區間低點
    total_volume: int               # 總成交量
    profile: Dict[float, int]       # {price_level: volume}
    value_area_pct: float = 0.80    # 使用的 VA 百分比

    @property
    def value_area_range(self) -> float:
        """80% 區間寬度（點數）"""
        return self.vah - self.val

    @property
    def full_range(self) -> float:
        """100% 區間寬度"""
        return self.high_100 - self.low_100

    @property
    def poc_relative_position(self) -> float:
        """POC 在 100% 區間中的相對位置 (0=底, 1=頂)"""
        if self.full_range == 0:
            return 0.5
        return (self.poc - self.low_100) / self.full_range


# ── 盤整區間 ──────────────────────────────────────────

@dataclass
class ConsolidationZone:
    """盤整區間（對應你手動畫的 Fixed Range VP 框選區域）"""
    zone_id: str
    formed_at: datetime
    left_at: Optional[datetime]
    poc: float
    vah_80: float                   # 80% 上邊界 = 入場做空位
    val_80: float                   # 80% 下邊界 = 入場做多位
    high_100: float                 # 100% 上邊界
    low_100: float                  # 100% 下邊界
    total_volume: int
    duration_minutes: int
    num_candles: int
    status: ZoneStatus = ZoneStatus.FORMING
    exit_direction: Optional[str] = None   # "up" | "down"
    mature: bool = False                   # 是否曾達到成熟條件
    candles: List[Candle] = field(default_factory=list)
    timeframe: str = "5m"                  # "5m" | "1m"
    parent_zone_id: Optional[str] = None   # 1m zone → parent 5m zone_id

    @property
    def range_80(self) -> float:
        return self.vah_80 - self.val_80

    @property
    def range_100(self) -> float:
        return self.high_100 - self.low_100

    @property
    def upper_tail(self) -> float:
        """80%-100% 上方尾部"""
        return self.high_100 - self.vah_80

    @property
    def lower_tail(self) -> float:
        """80%-100% 下方尾部"""
        return self.val_80 - self.low_100

    def is_price_inside_va(self, price: float) -> bool:
        """價格是否在 80% Value Area 內"""
        return self.val_80 <= price <= self.vah_80

    def is_price_inside_range(self, price: float) -> bool:
        """價格是否在 100% Range 內"""
        return self.low_100 <= price <= self.high_100

    def is_price_outside(self, price: float) -> Optional[str]:
        """價格是否在 100% 外部, 返回方向"""
        if price > self.high_100:
            return "above"
        elif price < self.low_100:
            return "below"
        return None


# ── 交易信號 & 紀錄 ──────────────────────────────────

@dataclass
class TradeSignal:
    """策略引擎發出的交易信號"""
    strategy: StrategyType
    direction: Direction
    entry_price: float
    sl_price: float
    tp_price: float
    zone_id: str
    reason: str
    timestamp: Optional[datetime] = None
    vol_ratio: Optional[float] = None  # 趨勢跟隨時的成交量比率
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # |H100-VAH| or |VAL-L100|, for TP recalc

    @property
    def sl_points(self) -> float:
        return abs(self.entry_price - self.sl_price)

    @property
    def tp_points(self) -> float:
        return abs(self.tp_price - self.entry_price)

    @property
    def rr_ratio(self) -> float:
        if self.sl_points == 0:
            return 0
        return self.tp_points / self.sl_points

    @property
    def sl_dollar(self) -> float:
        """NQ 每點 $20"""
        return self.sl_points * 20.0

    @property
    def tp_dollar(self) -> float:
        return self.tp_points * 20.0


@dataclass
class Trade:
    """已成交交易紀錄"""
    trade_id: str
    strategy: StrategyType
    direction: Direction
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    sl_price: float = 0.0
    tp_price: float = 0.0
    pnl: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    zone_id: str = ""
    contracts: int = 1
    vol_ratio: Optional[float] = None
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # for TP timeout recalc

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def is_win(self) -> bool:
        return self.pnl is not None and self.pnl > 0

    @property
    def duration_minutes(self) -> Optional[int]:
        if self.entry_time and self.exit_time:
            return int((self.exit_time - self.entry_time).total_seconds() / 60)
        return None


# ── 突破分析 ──────────────────────────────────────────

@dataclass
class BreakoutAnalysis:
    """
    離開盤整區間的成交量分析
    你的規則: outside volume > 離開前 2 根 K 線的 volume → 趨勢跟隨
    """
    breakout_id: str
    from_zone_id: str
    direction: str                  # "up" | "down"
    breakout_time: datetime
    vol_before_avg: float           # 離開 80% 前最後 2 根 K 線平均量
    vol_outside: float              # 外部 K 線成交量
    vol_ratio: float                # vol_outside / vol_before_avg
    is_trend_signal: bool           # vol_ratio > 1.0

    @property
    def is_big_trend(self) -> bool:
        """成交量比率 ≥ 2.0 → 大趨勢模式 ($600:$1800)"""
        return self.vol_ratio >= 2.0


# ── 策略參數 ──────────────────────────────────────────

@dataclass
class StrategyParams:
    """可配置的策略參數 (SessionTrendFollow)"""
    strategy: str = "trend"              # "trend" | "reversion"
    entry_mode: str = "100RE"            # "50RE" | "100RE"
    tp_ticks: int = 75                   # 25-600 tick
    sl_ticks: int = 50                   # 25-600 tick
    entry_timeout_minutes: int = 10      # 10, 20, 30
    tp_timeout_minutes: int = 0          # 0 (OFF), 30, 60
    tp_timeout_action: str = "flat"      # "flat", "3", "2", "1"


# ── 回測 ──────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """回測配置"""
    strategies: List[str] = field(default_factory=lambda: ["trend_follow"])
    symbol: str = "NQ"
    interval: str = "5m"
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 50000.0
    slippage_ticks: int = 1         # 滑價 tick 數
    commission_rt: float = 1.0      # 往返佣金 (Mini: $1.00, Micro: $0.50)
    max_daily_loss: float = 2000.0
    flatten_time: str = "15:05"     # CT
    # 盤整偵測參數
    min_candles_for_zone: int = 6
    poc_drift_threshold: float = 3.0
    value_area_pct: float = 0.80


@dataclass
class BacktestResult:
    """回測結果"""
    config: BacktestConfig
    trades: List[Trade]
    zones: List[ConsolidationZone]
    metrics: Metrics
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)


@dataclass
class Metrics:
    """績效指標"""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_rr_ratio: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_losses: int = 0
    total_pnl: float = 0.0
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    # 按策略分類
    reversion_metrics: Optional[Metrics] = None
    trend_follow_metrics: Optional[Metrics] = None


# ── 風控 ──────────────────────────────────────────────

@dataclass
class RiskStatus:
    """風控狀態快照"""
    daily_pnl: float = 0.0
    daily_loss_remaining: float = 2000.0
    current_positions: int = 0
    is_cooldown: bool = False
    cooldown_until: Optional[datetime] = None
    minutes_to_flatten: int = 999
    is_trading_allowed: bool = True
    reason_blocked: Optional[str] = None


@dataclass
class RiskCheckResult:
    """下單前風控檢查結果"""
    allowed: bool
    reasons: List[str] = field(default_factory=list)


# ── TopstepX API 相關 ─────────────────────────────────

@dataclass
class OrderRequest:
    """TopstepX 下單請求"""
    account_id: int
    contract_id: str                # e.g. "CON.F.US.ENQ.H26"
    order_type: int                 # 2=Market, 1=Limit, 3=Stop
    side: int                       # 1=Buy, 2=Sell
    size: int = 1
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None


@dataclass
class OrderResponse:
    """TopstepX 下單回應"""
    order_id: int
    success: bool
    error_code: int = 0
    error_message: Optional[str] = None


@dataclass
class AccountInfo:
    """TopstepX 帳戶資訊"""
    account_id: int
    name: str
    balance: float
    open_pnl: float
    closed_pnl: float
    daily_pnl: float
