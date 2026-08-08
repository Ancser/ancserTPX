"""Helpers for deciding whether live signal strategies have enough history.

The broker warm-up endpoint returns 1-minute candles.  FACTOR/PMO strategies
aggregate those candles into completed signal-timeframe bars and apply the same
entry-session filter in both live and backtest engines.  These helpers mirror
that aggregation so a non-empty weekend response is not mistaken for a
complete warm-up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from backend.db.models import Candle
from backend.strategy.factor import FACTOR_EMAPMO_HISTORY_BARS
from backend.strategy.session_filter import DEFAULT_ALLOWED_SESSIONS, is_allowed_session


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def signal_warmup_spec(params) -> Optional[Tuple[int, int, int]]:
    """Return ``(required bars, timeframe minutes, source seconds)`` if needed."""
    strategy = str(getattr(params, "strategy", "") or "").lower()
    # 1.0.9: 改名相容 —— 舊 preset 存的是 intramom / sessfib
    strategy = {"intramom": "momentum", "claudefib": "momentum",
                "sessfib": "betafib"}.get(strategy, strategy)
    if strategy == "factor":
        required = max(20, int(getattr(params, "factor_warmup_bars", 320) or 320))
        # 必須跟 FactorSignalStrategy.effective_warmup 同一個算式,否則抓資料的
        # 迴圈以為湊夠了就停,策略端卻還在 WARM-UP,live 會靜默地不下單。
        _fam = str(
            getattr(params, "factor_signal_family", "emapmo") or "emapmo").lower()
        if _fam in ("emapmo", "pmo", "ema_pmo"):
            required = max(required, FACTOR_EMAPMO_HISTORY_BARS)
        timeframe = max(1, int(getattr(params, "factor_timeframe_minutes", 5) or 5))
    # 1.0.10: 獨立的 strategy=="pmo" 分支已移除(與 factor+emapmo 家族重複)。
    # 上面那個 `_fam in ("emapmo", "pmo", "ema_pmo")` 是**族別**別名,必須保留 ——
    # 舊設定的 strategy="pmo" 會被正規化成 factor,族別再對到 emapmo,暖機需求
    # 因此走 FACTOR_EMAPMO_HISTORY_BARS,與先前等價。
    elif strategy == "momentum":
        # MOMENTUM 只需要 _atr_blend(ATR14 + ATR50)暖起來 —— 50 根就夠,
        # 給到 120 是留餘裕。它不看更長的歷史。
        required = 120
        timeframe = max(1, int(getattr(params, "research_tf_minutes", 5) or 5))
    elif strategy == "betafib":
        # BETA FIB 的日 ATR 需要 **5 個已完成的 RTH 交易日**
        # (_day_ranges maxlen=14,_daily_atr 在 <5 時回 None)。
        # 全時段 1 天約 276 根 5m,5 天約 1,380 —— 抓 2 天絕對不夠,
        # 不寫在這裡的話擴窗迴圈會在 2 天就停手。
        required = 1400
        timeframe = max(1, int(getattr(params, "research_tf_minutes", 5) or 5))
    else:
        return None
    source_seconds = max(1, int(getattr(params, "candle_seconds", 60) or 60))
    return required, timeframe, source_seconds


def completed_signal_bars(candles: Iterable[Candle], params) -> int:
    """Count bars the live strategy would append while replaying ``candles``."""
    spec = signal_warmup_spec(params)
    if spec is None:
        return 0
    _, timeframe, source_seconds = spec
    allowed = getattr(params, "tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS) or None
    ordered = sorted(candles, key=lambda candle: _utc(candle.timestamp))

    completed = 0
    last_appended: Optional[datetime] = None
    working: Optional[datetime] = None
    for candle in ordered:
        if not is_allowed_session(candle.timestamp, allowed):
            continue
        ts = _utc(candle.timestamp)
        finalized: Optional[datetime] = None
        if source_seconds >= timeframe * 60:
            finalized = ts
        else:
            minute = (ts.minute // timeframe) * timeframe
            bucket = ts.replace(minute=minute, second=0, microsecond=0)
            if working is not None and bucket != working:
                finalized = working
                working = None
            if working is None:
                working = bucket
            # Match FactorSignalStrategy/EMAPMOStrategy exactly: when a gap both
            # rolls the previous bucket and completes the new bucket on this
            # same input candle, the new finalized bar overwrites the old one.
            if (ts.minute % timeframe) == (timeframe - 1):
                finalized = working
                working = None
        if finalized is not None and finalized != last_appended:
            completed += 1
            last_appended = finalized
    return completed


def signal_warmup_progress(candles: Iterable[Candle], params) -> Tuple[int, int]:
    """Return completed/required signal bars; ``required=0`` means not applicable."""
    spec = signal_warmup_spec(params)
    if spec is None:
        return 0, 0
    required, _, _ = spec
    return completed_signal_bars(candles, params), required


def has_sufficient_signal_warmup(candles: Iterable[Candle], params) -> bool:
    completed, required = signal_warmup_progress(candles, params)
    return required == 0 or completed >= required
