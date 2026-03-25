"""Quick smoke test for all backend modules"""
import sys
sys.path.insert(0, '.')

from backend.db.models import *
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.strategy.consolidation import ConsolidationDetector
from backend.strategy.reversion import ReversionStrategy
from backend.strategy.trend_follow import TrendFollowStrategy
from backend.backtest.engine import BacktestEngine
from backend.backtest.metrics import MetricsCalculator
from backend.risk.manager import RiskManager

from datetime import datetime

candles = [
    Candle(datetime(2026,3,20,9,30), 21450, 21465, 21440, 21460, 500),
    Candle(datetime(2026,3,20,9,35), 21460, 21470, 21445, 21455, 600),
    Candle(datetime(2026,3,20,9,40), 21455, 21468, 21448, 21462, 550),
    Candle(datetime(2026,3,20,9,45), 21462, 21475, 21450, 21458, 700),
    Candle(datetime(2026,3,20,9,50), 21458, 21466, 21442, 21450, 480),
    Candle(datetime(2026,3,20,9,55), 21450, 21463, 21438, 21456, 520),
    Candle(datetime(2026,3,20,10,0), 21456, 21470, 21445, 21465, 610),
    Candle(datetime(2026,3,20,10,5), 21465, 21472, 21452, 21460, 530),
]

# Test VP
calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=0.80)
vp = calc.calculate(candles)
print(f"VP: POC={vp.poc}, VAH={vp.vah}, VAL={vp.val}")
print(f"  Range: {vp.low_100} - {vp.high_100}")
print(f"  80pct width: {vp.value_area_range:.2f} pts")

# Test consolidation
detector = ConsolidationDetector(min_candles=6)
for c in candles:
    event = detector.update(c)
    if event:
        print(f"Event: {event.event_type}")

zone = detector.get_active_zone()
print(f"Active zone: {zone is not None}")

# Test backtest
engine = BacktestEngine()
result = engine.run(candles)
print(f"Backtest: {result.metrics.total_trades} trades, PnL=${result.metrics.total_pnl}")
print("ALL OK")
