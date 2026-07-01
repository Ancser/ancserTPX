# ============================================================
# 文件: backend/db/models.py
# 狀態: v1.0.6
# 功能 / Features:
#   - Shared dataclasses and enums for candles, zones, signals, trades, metrics,
#     backtest/live strategy params, risk DTOs, broker orders, and account data.
#   - StrategyParams exposes the trend strategy, fixed 80% VA,
#     contract sizing, trail controls, and full_tp_lock.
#   - Contract helpers resolve NQ/MNQ point value, tick size, commission, and fees.
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
    TREND_FOLLOW = "trend"       # was "trend_follow" — old JSON may still show "trend_follow"


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
    # Multi-band value areas for the confluence level universe:
    # {pct: (vah, val)} with pct in {20,40,60,80,100}. 100% = full range.
    va_bands: Dict[int, Tuple[float, float]] = field(default_factory=dict)

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
    timeframe: str = "5m"                  # area timeframe bucket: 5m/15m/30m/1h/4h
    parent_zone_id: Optional[str] = None   # 1m zone → parent 5m zone_id
    va_curve: List[dict] = field(default_factory=list)  # [{ts, vah, val}] per VP recalc
    profile: Dict[float, int] = field(default_factory=dict)  # VP bin histogram {price: volume}
    # Multi-band value areas {pct: (vah, val)} for the confluence level universe.
    va_bands: Dict[int, Tuple[float, float]] = field(default_factory=dict)

    def lowest_volume_price_between(self, a: float, b: float) -> Optional[float]:
        """Return the price bin with the lowest volume in the inclusive [min(a,b), max(a,b)]
        range. Used for the volume-node SL (lowest-volume node between POC and VAH/VAL)."""
        if not self.profile:
            return None
        lo, hi = (a, b) if a <= b else (b, a)
        candidates = [(p, v) for p, v in self.profile.items() if lo <= p <= hi]
        if not candidates:
            return None
        # lowest volume; tie-break toward the price closest to the outer bound (b)
        min_vol = min(v for _, v in candidates)
        nodes = [p for p, v in candidates if v == min_vol]
        return min(nodes, key=lambda p: abs(p - b))

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
    zone_source: Optional[str] = None      # v1.0.6 uses current mature zone only
    timestamp: Optional[datetime] = None
    vol_ratio: Optional[float] = None  # 趨勢跟隨時的成交量比率
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # |H100-VAH| or |VAL-L100|, for TP recalc
    order_type: str = "limit"         # "limit" | "market"
    meta: Dict[str, Any] = field(default_factory=dict)

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
    original_sl_price: Optional[float] = None
    original_tp_price: Optional[float] = None
    pnl: Optional[float] = None        # NET PnL (after commission + fees)
    commission: float = 0.0            # round-turn commission deducted
    fees: float = 0.0                  # round-turn fees deducted
    exit_reason: Optional[ExitReason] = None
    zone_id: str = ""
    zone_source: Optional[str] = None      # v1.0.6 uses current mature zone only
    contracts: int = 1
    point_value: float = 20.0          # NQ=$20, MNQ=$2 (per single contract)
    contract_id: str = ""              # which TopstepX contract was used
    vol_ratio: Optional[float] = None
    is_big_trend: bool = False
    breakout_range: Optional[float] = None  # for TP timeout recalc
    # Post-breakout 60-minute path tracking (filled by backtest engine after exit)
    post_breakout_max_favorable_ticks: Optional[float] = None
    post_breakout_max_adverse_ticks: Optional[float] = None
    post_breakout_broke_trail_first: Optional[bool] = None
    post_breakout_broke_sl_first: Optional[bool] = None
    post_breakout_reached_tp: Optional[bool] = None
    # Confluence research metadata: {mode, side, weight, tfs, labels, band_pct, wait_min}
    meta: Dict[str, Any] = field(default_factory=dict)

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
    """Strategy parameters / 策略參數.

    Supports the legacy trend strategy and the explainable confluence scorer.
    Value Area is locked to 80% so live and backtest use the same zone width.
    """
    strategy: str = "confluence"
    tp_ticks: int = 200                  # 50-200 tick
    sl_ticks: int = 50                   # 50-200 tick
    trail_sl_ticks: int = 10            # 0..TP ticks from entry after trail triggers
    trail_trigger_pct: float = 0.30     # trigger trail when price reaches this fraction of TP
    trail_enabled: bool = True          # v1.0.6: master switch for trailing-SL mechanism
    tr_tp_ticks: int = 200              # trend TP ticks
    tr_sl_ticks: int = 50               # trend SL ticks
    tr_trail_sl_ticks: int = 10         # trend trail-SL offset from entry
    tr_trail_trigger_pct: float = 0.30  # trend trail trigger as fraction of TP
    tr_trail_enabled: bool = True       # trend trail switch
    tr_full_tp_lock: int = 0            # trend full-TP lock count
    # Candle interval (seconds)
    candle_seconds: int = 60             # v1.0.6 uses completed 1m bars in live and backtest
    # Contract & sizing (v1.0.6) — preferred default 3 × Micro NQ
    contract_id: str = "CON.F.US.MNQ.M26"  # full contractId (NQ=ENQ, MNQ=MNQ)
    contract_size: int = 3                 # number of contracts per order (1..N)
    # Full TP lock: 0=OFF, 1/2/3 = stop new entries after N full TP exits. Resets next Topstep session.
    full_tp_lock: int = 0
    # One session, one direction, one order attempt. Keeps live behavior aligned with backtest.
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True   # trend session limit
    tr_allowed_sessions: Optional[List[str]] = field(default_factory=lambda: ["ASIA"])
    # Session-zone maturity controls
    # Zone stability is enabled by default; set True only for no-stability-wait experiments.
    skip_zone_stability: bool = False
    breakout_confirm_bars: int = 7         # consecutive candles fully outside VA required
    # Area (zone) configuration — fixed clock-bucket timeframe + value-area width.
    area_timeframe: str = "5m"             # "5m" | "15m" | "30m" | "1h" | "4h"
    value_area_pct: float = 0.80           # value-area width fraction (0.50..0.95)
    # Zone method (v1.0.6): "single" = one timeframe; "overlap" = require 2..5
    # timeframes' value areas to overlap (identical to backtest/ML overlap sweep).
    method: str = "single"                 # "single" | "overlap"
    tf_combo: List[str] = field(default_factory=list)  # overlap timeframes, e.g. ["5m","15m"]
    tr_overlap_trade_tf: str = "merged"    # "merged" original overlap zone | "smallest" trade smallest TF zone
    # SL/TP model (v1.0.6): SL = lowest-volume node between POC and VAH/VAL;
    # TP = entry ± rr_ratio × SL-distance. rr_ratio selectable 1..6. No fixed ticks.
    rr_ratio: int = 2                      # reward:risk multiple (1..6)
    # --- v1.0.6: explainable multi-timeframe confluence (ML scorer) ---
    # Activated when strategy == "confluence". The live engine then runs the
    # SAME ConfluenceBacktester logic (per-TF detectors + trained scorer) so
    # live == backtest. conf_shadow=True logs signals WITHOUT placing orders.
    conf_band_ticks: float = 4.0           # level-cluster band width (ticks)
    conf_min_distinct_tf: int = 2          # cluster needs >= this many timeframes
    conf_rr: float = 1.0                   # optimized live/backtest runtime RR
    conf_wait_minutes: int = 1             # live parity: one-shot limit-order fill timeout
    conf_base_minutes: int = 1             # input candle resolution (standardized: 1m)
    conf_min_prob: float = 0.65            # optimized gate: skip signals below this win-prob
    conf_ev_floor: Optional[float] = None  # EV-priority gate: keep signals with EV>=floor (None=use win-prob gate; 0=every +EV)
    conf_rr_grid: Optional[List[float]] = None
    conf_use_scorer: bool = True           # True=trained JSON, False=heuristic prior
    conf_enable_breakout: bool = False     # include breakout-retrace candidate (False=momentum+reversion only)
    conf_max_risk_ticks: Optional[int] = None  # optional risk-width cap; None/0 = off
    conf_sl_reference_tf: str = "largest"  # "largest" original behavior, "smallest" tightens SL/TP basis
    conf_allowed_sessions: Optional[List[str]] = field(default_factory=lambda: ["ASIA", "PRE"])
    # --- STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original behaviour ---
    conf_trail_trigger_pct: float = 0.50   # optimized: fire after 50% of TP distance
    conf_trail_lock_pct: float = 0.05      # optimized: lock +5% of TP distance on trigger
    conf_full_tp_lock: int = 0             # 0 = OFF; stop new entries after N full-TP exits/session
    conf_session_limit: bool = True        # one trade per zone+direction per Topstep session
    conf_shadow: bool = False              # default LIVE — practice account places orders
    # 1.0.8: 移除 ML Consolidation V2 (mlc2_*) StrategyParams 欄位 — 該策略已刪除


# ── 回測 ──────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """回測配置"""
    strategies: List[str] = field(default_factory=lambda: ["trend"])
    symbol: str = "NQ"
    interval: str = "5m"
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 50000.0
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
    total_gain: float = 0.0
    total_loss: float = 0.0
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    # Post-breakout 1-hour path statistics (averaged across all confirmed-breakout trades)
    post_breakout_sample_size: int = 0          # how many trades produced these stats
    post_breakout_avg_max_fav_ticks: float = 0.0  # avg MFE in ticks within 60m
    post_breakout_avg_max_adv_ticks: float = 0.0  # avg MAE in ticks within 60m
    post_breakout_tp_clean: int = 0             # trades that hit TP without ever touching trail level
    post_breakout_tp_after_trail: int = 0       # hit TP but first crossed trail-trigger
    post_breakout_tp_after_sl: int = 0          # hit TP but first crossed SL price
    # Zone-source performance: v1.0.6 only trades the current mature zone.
    current_zone_trades: int = 0
    current_zone_wins: int = 0
    current_zone_win_rate: float = 0.0
    current_zone_avg_pnl: float = 0.0
    current_zone_total_pnl: float = 0.0
    # 按策略分類
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
    raw: Optional[Dict[str, Any]] = None


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
# made 10×MNQ look 75% worse than 1×NQ in v1.0.6.
_CONTRACT_SPECS = {
    "ENQ": {"point_value": 20.0, "tick_size": 0.25, "label": "NQ (Mini)",
            "commission_rt": 1.00, "fees_rt": 2.80},   # NQ Mini RT ≈ $3.80
    "NQ":  {"point_value": 20.0, "tick_size": 0.25, "label": "NQ (Mini)",
            "commission_rt": 1.00, "fees_rt": 2.80},
    "MNQ": {"point_value": 2.0,  "tick_size": 0.25, "label": "MNQ (Micro)",
            "commission_rt": 0.50, "fees_rt": 0.74},   # MNQ Micro RT ≈ $1.24
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
    """Per-contract round-turn commission. NQ ≈ $1.00, MNQ ≈ $0.50."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["commission_rt"]) if spec else 1.00


def get_fees_rt(contract_id: str) -> float:
    """Per-contract round-turn exchange/regulatory fees. NQ ≈ $2.80, MNQ ≈ $0.74."""
    sym = _extract_symbol(contract_id)
    spec = _CONTRACT_SPECS.get(sym)
    return float(spec["fees_rt"]) if spec else 2.80

