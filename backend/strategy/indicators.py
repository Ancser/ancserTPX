"""Technical indicators — MACD, VWAP."""
from datetime import timedelta
from typing import Optional


class EMACalculator:
    """Incremental EMA."""
    def __init__(self, period: int):
        self.period = period
        self._k = 2.0 / (period + 1)
        self._value: Optional[float] = None
        self._count: int = 0
        self._seed_sum: float = 0.0

    def update(self, price: float) -> Optional[float]:
        self._count += 1
        if self._count < self.period:
            self._seed_sum += price
            return None
        if self._count == self.period:
            self._seed_sum += price
            self._value = self._seed_sum / self.period
            return self._value
        self._value = price * self._k + self._value * (1 - self._k)
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value

    def reset(self):
        self._value = None
        self._count = 0
        self._seed_sum = 0.0


class MACDCalculator:
    """Incremental MACD (fast EMA - slow EMA, signal EMA of MACD)."""
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._fast = EMACalculator(fast)
        self._slow = EMACalculator(slow)
        self._signal = EMACalculator(signal)
        self._macd: Optional[float] = None
        self._signal_val: Optional[float] = None
        self._histogram: Optional[float] = None

    def update(self, price: float):
        f = self._fast.update(price)
        s = self._slow.update(price)
        if f is None or s is None:
            return
        self._macd = f - s
        sig = self._signal.update(self._macd)
        if sig is None:
            self._histogram = None
            return
        self._signal_val = sig
        self._histogram = self._macd - sig

    @property
    def histogram(self) -> Optional[float]:
        return self._histogram

    @property
    def macd_line(self) -> Optional[float]:
        return self._macd

    @property
    def signal_line(self) -> Optional[float]:
        return self._signal_val

    def reset(self):
        self._fast.reset()
        self._slow.reset()
        self._signal.reset()
        self._macd = None
        self._signal_val = None
        self._histogram = None


class VWAPCalculator:
    """
    Session VWAP — 每個 CME Globex session 重置一次。

    NQ session 邊界: 22:00 UTC (= 18:00 ET = Globex open)
    規則: hour >= 22 → session key = 當日日期
          hour < 22  → session key = 前一日日期
    典型價 = (high + low + close) / 3
    """

    SESSION_RESET_HOUR = 22  # UTC

    def __init__(self):
        self._cum_pv: float = 0.0
        self._cum_vol: int = 0
        self._vwap: Optional[float] = None
        self._session_key = None  # date object for current session

    def update(self, candle) -> Optional[float]:
        """Feed a candle (Candle dataclass). Returns current VWAP or None."""
        ts = candle.timestamp
        # Determine session key — same logic as backtest engine's SESSION_START
        if ts.hour >= self.SESSION_RESET_HOUR:
            key = ts.date()
        else:
            key = (ts - timedelta(days=1)).date()

        if key != self._session_key:
            # New session → reset accumulators
            self._cum_pv = 0.0
            self._cum_vol = 0
            self._vwap = None
            self._session_key = key

        typical = (candle.high + candle.low + candle.close) / 3.0
        vol = max(candle.volume, 1)  # guard zero-volume bars
        self._cum_pv += typical * vol
        self._cum_vol += vol
        self._vwap = self._cum_pv / self._cum_vol
        return self._vwap

    @property
    def value(self) -> Optional[float]:
        return self._vwap

    def reset(self):
        self._cum_pv = 0.0
        self._cum_vol = 0
        self._vwap = None
        self._session_key = None
