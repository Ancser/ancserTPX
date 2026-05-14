# ============================================================
# 文件: backend/db/models.py
# 功能: 共用的資料結構 (dataclasses) 定義 — 全後端共用的型別契約
# 主要職責:
#   1. 市場資料: Candle / VolumeProfileResult
#   2. 策略狀態: ConsolidationZone / TradeSignal / Trade / BreakoutAnalysis
#   3. 配置/結果: StrategyParams / BacktestConfig / BacktestResult / Metrics
#   4. 風控: RiskStatus / RiskCheckResult
#   5. Broker DTO: OrderRequest / OrderResponse / AccountInfo
# 重要欄位:
#   - StrategyParams.contract_id  : 預設合約代碼 (如 CON.F.US.MNQ.M26)
#   - StrategyParams.contract_size: 下單口數 (1..N)
#   - StrategyParams.trail_enabled: trail SL 主開關 (v0.11+)
#   - Trade.contracts             : 此筆交易實際使用的口數
#   - Trade.point_value           : 此合約每點美元值 (NQ=$20, MNQ=$2)
#   - Metrics.post_breakout_*     : 突破確認後 60 分鐘的 MFE/MAE/路徑統計
# 合約規格 (_CONTRACT_SPECS):
#   - point_value, tick_size, commission_rt, fees_rt (per-contract round-turn)
#   - 由 get_point_value / get_tick_size / get_commission_rt / get_fees_rt 取用
# 關聯:
#   ← backend/api/routes.py / backtest/engine.py / live/engine.py / strategy/* / broker/*
# 狀態: 已完成 (v0.11 加入 contract_id / size / fees / trail_enabled / post-breakout stats)
# ============================================================

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
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
    TRAIL_SL = "trail_sl"
    FLATTEN = "flatten"   # 3:10 PM CT 強制平倉
    MANUAL  = "manual"


class StrategyType(str, Enum):
    REVERSION    = "reversion"
    TREND_FOLLOW = "trend"       # was "trend_follow" — old JSON may still show "trend_follow"
    MACD         = "macd"


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
    zone_source: Optional[str] = None      # "current" | "previous" (which zone generated the setup)
    timestamp: Optional[datetime] = None
    vol_ratio: Optional[float] = None  # 趨勢跟隨時的成交量比率
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # |H100-VAH| or |VAL-L100|, for TP recalc
    order_type: str = "limit"         # "limit" | "market"
    macd_hist: Optional[float] = None # MACD histogram value at signal

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
    pnl: Optional[float] = None        # NET PnL (after commission + fees)
    commission: float = 0.0            # round-turn commission deducted
    fees: float = 0.0                  # round-turn fees deducted
    exit_reason: Optional[ExitReason] = None
    zone_id: str = ""
    zone_source: Optional[str] = None      # "current" | "previous" (set by backtest engine)
    contracts: int = 1
    point_value: float = 20.0          # NQ=$20, MNQ=$2 (per single contract)
    contract_id: str = ""              # which TopstepX contract was used
    vol_ratio: Optional[float] = None
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # for TP timeout recalc
    macd_hist: Optional[float] = None       # MACD histogram value at entry
    # Post-breakout 60-minute path tracking (filled by backtest engine after exit)
    post_breakout_max_favorable_ticks: Optional[float] = None
    post_breakout_max_adverse_ticks: Optional[float] = None
    post_breakout_broke_trail_first: Optional[bool] = None
    post_breakout_broke_sl_first: Optional[bool] = None
    post_breakout_reached_tp: Optional[bool] = None

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
    """可配置的策略參數 (SessionTrendFollow / MACDOnlyStrategy)"""
    strategy: str = "trend"              # "trend" | "macd" | "reversion"
    tp_ticks: int = 150                  # 5-200 tick
    sl_ticks: int = 50                   # 5-200 tick
    trail_sl_ticks: int = 5             # -SL..TP ticks from entry after trail triggers
    trail_trigger_pct: float = 0.30     # trigger trail when price reaches this fraction of TP
    trail_enabled: bool = True          # v0.11+: master switch for trailing-SL mechanism
    # MACD params (hardcoded 12/26/9 — not user-configurable)
    # Candle interval (seconds)
    candle_seconds: int = 30             # 30 for live 30s bars; 60 for 1m backtest
    # Contract & sizing (v0.11+) — preferred default 3 × Micro NQ
    contract_id: str = "CON.F.US.MNQ.M26"  # full contractId (NQ=ENQ, MNQ=MNQ)
    contract_size: int = 3                 # number of contracts per order (1..N)
    # Max profit lock: 0=OFF, 150/500/1000 — block new trades when daily PnL ≥ threshold (resets PT 22:00)
    max_profit_lock: int = 0
    # Session-zone maturity controls
    # Zone stability is enabled by default; set True only for no-stability-wait experiments.
    skip_zone_stability: bool = False
    # --- Removed (hardcoded internally) ---
    # entry_mode: always "100RE" (VAH/VAL entry)
    # entry_timeout_minutes: hardcoded 10 min inside strategy
    # tp_timeout_minutes / tp_timeout_action: removed
    # use_trail_sl: always True (forced)
    # use_vwap_filter: always True (MACD forced VWAP filter)


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
    slippage_ticks: int = 0         # 滑價 tick 數 (0 = clean fill @ signal price)
    commission_rt: float = 1.0      # 往返佣金 (Mini: $1.00, Micro: $0.50)
    fees_rt: float = 2.80           # 交易所/監管費 — TopstepX Mini NQ 每輪 $2.80
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
    calmar_ratio: float = 0.0          # Total PnL / Max Drawdown
    profit_factor: float = 0.0
    max_consecutive_losses: int = 0
    total_pnl: float = 0.0
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    # Post-breakout 1-hour path statistics (averaged across all confirmed-breakout trades)
    post_breakout_sample_size: int = 0          # how many trades produced these stats
    post_breakout_avg_max_fav_ticks: float = 0.0  # avg MFE in ticks within 60m
    post_breakout_avg_max_adv_ticks: float = 0.0  # avg MAE in ticks within 60m
    post_breakout_tp_clean: int = 0             # trades that hit TP without ever touching trail level
    post_breakout_tp_after_trail: int = 0       # hit TP but first crossed trail-trigger
    post_breakout_tp_after_sl: int = 0          # hit TP but first crossed SL price
    # Zone-source performance: whether the setup used the current mature zone
    # or the most recent previous/left zone.
    previous_zone_trades: int = 0
    previous_zone_wins: int = 0
    previous_zone_win_rate: float = 0.0
    previous_zone_avg_pnl: float = 0.0
    previous_zone_total_pnl: float = 0.0
    current_zone_trades: int = 0
    current_zone_wins: int = 0
    current_zone_win_rate: float = 0.0
    current_zone_avg_pnl: float = 0.0
    current_zone_total_pnl: float = 0.0
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
    stop_loss_bracket: Optional[Dict[str, Any]] = None
    take_profit_bracket: Optional[Dict[str, Any]] = None


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


# ── Contract metadata helpers ────────────────────────────────

# Per-contract specs. Mini NQ ($20/pt) vs Micro NQ ($2/pt). Tick size 0.25 for both.
# commission_rt / fees_rt are TopstepX round-turn costs PER CONTRACT — applied as
# `rt_cost * pos.contracts` in the backtest engine. Using the wrong rate is what
# made 10×MNQ look 75% worse than 1×NQ in v0.11.
_CONTRACT_SPECS = {
    "ENQ": {"point_value": 20.0, "tick_size": 0.25, "label": "NQ (Mini)",
            "commission_rt": 1.00, "fees_rt": 2.80},   # NQ Mini RT ≈ $3.80
    "NQ":  {"point_value": 20.0, "tick_size": 0.25, "label": "NQ (Mini)",
            "commission_rt": 1.00, "fees_rt": 2.80},
    "MNQ": {"point_value": 2.0,  "tick_size": 0.25, "label": "MNQ (Micro)",
            "commission_rt": 0.74, "fees_rt": 0.46},   # MNQ Micro RT ≈ $1.20
}


def _extract_symbol(contract_id: str) -> str:
    """Pull the symbol token out of a contract id like 'CON.F.US.MNQ.M26' → 'MNQ'."""
    if not contract_id:
        return "ENQ"
    parts = contract_id.split(".")
    # Standard ProjectX form: CON.F.US.<SYMBOL>.<EXPIRY>
    if len(parts) >= 4:
        return parts[3].upper()
    return contract_id.upper()


def get_point_value(contract_id: str) -> float:
    """Return the per-contract dollar value of one point. NQ=$20, MNQ=$2."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["point_value"]) if spec else 20.0


def get_tick_size(contract_id: str) -> float:
    """Return the contract tick size (default 0.25)."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["tick_size"]) if spec else 0.25


def get_contract_label(contract_id: str) -> str:
    """Return a human-friendly label for a contract id."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return spec["label"] if spec else sym


def get_commission_rt(contract_id: str) -> float:
    """Per-contract round-turn commission. NQ ≈ $1.00, MNQ ≈ $0.74."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["commission_rt"]) if spec else 1.00


def get_fees_rt(contract_id: str) -> float:
    """Per-contract round-turn exchange/regulatory fees. NQ ≈ $2.80, MNQ ≈ $0.46."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["fees_rt"]) if spec else 2.80
