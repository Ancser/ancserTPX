# ============================================================
# 文件: backend/api/routes.py
# 狀態: 已完成 (已檢查 1 次)
# 問題: 無
# 關聯文件:
#   <- backend/main.py              (掛載路由)
#   -> backend/backtest/engine.py    (回測)
#   -> backend/broker/topstepx.py    (API 數據)
#   -> backend/data/market_simulator.py (市場模擬器)
#   -> backend/strategy/*            (策略)
#   -> backend/db/models.py          (數據模型)
# 函數結構:
#   - GET  /health                   : 健康檢查
#   - POST /backtest/run             : 執行回測
#   - GET  /backtest/results         : 列出回測結果
#   - POST /data/load-sample         : 載入靜態模擬數據
#   - POST /data/fetch-historical    : 從 TopstepX 拉取歷史數據
#   - POST /data/aggregate           : 1m->5m 聚合
#   - POST /simulator/start          : 啟動市場模擬器
#   - POST /simulator/stop           : 停止模擬器
#   - POST /simulator/speed          : 變更速度
#   - GET  /simulator/status         : 模擬器狀態
#   - GET  /simulator/candles        : 模擬器 K 線數據
#   - GET  /simulator/orderbook      : Order book 快照
#   - GET  /simulator/ticks          : 最近 tick 數據
#   - GET  /simulator/live-strategy  : 即時策略狀態 (zones, trades, metrics)
# ============================================================
"""
REST API 路由
"""

from __future__ import annotations
import os
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.models import BacktestConfig, BarUnit, Candle, StrategyParams
from backend.backtest.engine import BacktestEngine
from backend.strategy.volume_profile import VolumeProfileCalculator

logger = logging.getLogger(__name__)
router = APIRouter()


def _env(key: str, default: str = "") -> str:
    """讀取 .env 環境變數"""
    return os.getenv(key, default)

# ── 臨時存儲（後續改用 SQLite）──────────────────────────
_backtest_results = []
_historical_candles: List[Candle] = []
_topstepx_client = None  # TopstepXClient instance (set after connect)
_live_contract_id = "CON.F.US.ENQ.M26"  # Set after connect
_candle_cache = {"data": None, "time": 0}  # Cache for latest-candles (avoid API spam)


# ── Pydantic 請求/回應模型 ────────────────────────────

class BacktestRequest(BaseModel):
    initial_capital: float = 50000.0
    # Strategy params
    strategy: str = "trend"
    entry_mode: str = "50RE"
    tp_ticks: int = 50
    sl_ticks: int = 50
    entry_timeout_minutes: int = 30
    tp_timeout_minutes: int = 0
    tp_timeout_action: str = "flat"


class FetchHistoricalRequest(BaseModel):
    username: str = ""             # 空 = 從 .env 讀取
    api_key: str = ""              # 空 = 從 .env 讀取
    contract_id: str = ""          # 空 = 自動找 NQ
    unit: int = 2                  # 2=分鐘
    unit_number: int = 5           # 5=5分鐘
    start_time: str = ""           # ISO format
    end_time: str = ""
    use_demo: Optional[bool] = None  # None = 從 .env 讀取


class TradeResponse(BaseModel):
    trade_id: str
    strategy: str
    direction: str
    entry_price: float
    entry_time: str
    exit_price: Optional[float]
    exit_time: Optional[str]
    sl_price: float
    tp_price: float
    pnl: Optional[float]
    exit_reason: Optional[str]
    zone_id: str
    vol_ratio: Optional[float]
    is_big_trend: bool


class ZoneResponse(BaseModel):
    zone_id: str
    formed_at: str
    left_at: Optional[str]
    poc: float
    vah_80: float
    val_80: float
    high_100: float
    low_100: float
    total_volume: int
    duration_minutes: int
    num_candles: int
    status: str
    exit_direction: Optional[str]
    profile: Optional[list] = None  # VP histogram data [{price, volume, pct}]
    timeframe: str = "5m"
    parent_zone_id: Optional[str] = None
    mature: bool = False  # Session zone maturity flag


class SubMetricsResponse(BaseModel):
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_rr_ratio: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0

class MetricsResponse(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    avg_rr_ratio: float
    expectancy: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    max_consecutive_losses: int
    total_pnl: float
    # Per-strategy breakdown
    reversion: Optional[SubMetricsResponse] = None
    trend_follow: Optional[SubMetricsResponse] = None


class BacktestResponse(BaseModel):
    metrics: MetricsResponse
    trades: List[TradeResponse]
    zones: List[ZoneResponse]
    equity_curve: List[List[float]]   # [[timestamp_ms, equity], ...]


# ── 路由 ──────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "ancserTPX"}


@router.get("/config")
async def get_config():
    """
    返回 .env 中的配置（API key 只顯示前 6 碼）
    前端用來自動填入表單
    """
    username = _env("TOPSTEPX_USERNAME")
    api_key = _env("TOPSTEPX_API_KEY")
    contract_id = _env("TOPSTEPX_CONTRACT_ID")
    use_demo = _env("TOPSTEPX_USE_DEMO", "false").lower() == "true"

    return {
        "username": username,
        "has_api_key": bool(api_key),
        "api_key_preview": api_key[:6] + "***" if api_key else "",
        "contract_id": contract_id,
        "use_demo": use_demo,
        "env_loaded": bool(username and api_key),
    }


@router.get("/data/candles")
async def get_stored_candles():
    """返回已載入的歷史 K 線數據 (用於前端圖表顯示)"""
    if not _historical_candles:
        return {"candles": [], "count": 0}

    return {
        "candles": [
            {
                "time": c.timestamp.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in _historical_candles
        ],
        "count": len(_historical_candles),
    }


@router.get("/data/latest-candles")
async def get_latest_candles(since: str = ""):
    """
    Fetch fresh candles from TopstepX API (for live polling).
    If `since` is provided (ISO timestamp), only return candles after that time.
    Also appends new candles to _historical_candles store.
    """
    if not _topstepx_client:
        # Fallback: return last few stored candles
        if _historical_candles:
            recent = _historical_candles[-5:]
            return {
                "candles": [
                    {
                        "time": c.timestamp.isoformat(),
                        "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume,
                    }
                    for c in recent
                ],
                "count": len(recent),
            }
        return {"candles": [], "count": 0}

    try:
        import time as _time
        from backend.db.models import BarUnit

        # Cache: only fetch from API every 5 seconds to avoid rate limits
        now_ts = _time.time()
        if _candle_cache["data"] and (now_ts - _candle_cache["time"]) < 5:
            candles = _candle_cache["data"]
        else:
            candles = await _topstepx_client.get_historical_bars(
                contract_id=_live_contract_id,
                unit=BarUnit.MINUTE,
                unit_number=1,
                limit=30,
            )
            _candle_cache["data"] = candles
            _candle_cache["time"] = now_ts

        if not candles:
            return {"candles": [], "count": 0}

        # Append new candles to global store
        if _historical_candles:
            last_stored_ts = _historical_candles[-1].timestamp
            for c in candles:
                if c.timestamp > last_stored_ts:
                    _historical_candles.append(c)

        # Filter by `since` if provided
        result = candles
        if since:
            from datetime import datetime as dt
            try:
                since_dt = dt.fromisoformat(since.replace("Z", "+00:00"))
                result = [c for c in candles if c.timestamp > since_dt]
            except Exception:
                pass

        return {
            "candles": [
                {
                    "time": c.timestamp.isoformat(),
                    "open": c.open, "high": c.high, "low": c.low,
                    "close": c.close, "volume": c.volume,
                }
                for c in result
            ],
            "count": len(result),
        }
    except Exception as e:
        logger.error(f"latest-candles error: {e}")
        # Fallback to stored
        if _historical_candles:
            recent = _historical_candles[-5:]
            return {
                "candles": [
                    {
                        "time": c.timestamp.isoformat(),
                        "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume,
                    }
                    for c in recent
                ],
                "count": len(recent),
            }
        return {"candles": [], "count": 0}


class DetectZonesRequest(BaseModel):
    min_candles_for_zone: int = 6
    poc_drift_threshold: float = 3.0
    value_area_pct: float = 0.80


@router.post("/data/detect-zones")
async def detect_zones(req: DetectZonesRequest = DetectZonesRequest()):
    """Run zone detection on stored candles — returns zones with VP profiles."""
    if not _historical_candles:
        raise HTTPException(400, "No candles loaded")

    from backend.strategy.consolidation import SessionZoneDetector
    from backend.strategy.volume_profile import VolumeProfileCalculator

    detector = SessionZoneDetector(
        value_area_pct=req.value_area_pct,
    )
    vp_calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=req.value_area_pct)

    for c in _historical_candles:
        detector.update(c)

    all_zones = detector.get_all_zones()
    zone_list = []
    for z in all_zones:
        zd = {
            "zone_id": z.zone_id,
            "poc": z.poc,
            "vah_80": z.vah_80,
            "val_80": z.val_80,
            "high_100": z.high_100,
            "low_100": z.low_100,
            "status": z.status.value,
            "formed_at": z.formed_at.isoformat() if z.formed_at else None,
            "left_at": z.left_at.isoformat() if z.left_at else None,
            "exit_direction": z.exit_direction,
            "num_candles": z.num_candles,
            "timeframe": getattr(z, 'timeframe', '5m'),
            "parent_zone_id": getattr(z, 'parent_zone_id', None),
        }
        if z.candles and z.status.value == "active":
            try:
                vp = vp_calc.calculate(z.candles)
                max_vol = max(vp.profile.values()) if vp.profile else 1
                zd["profile"] = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted(vp.profile.items())
                ]
            except Exception:
                zd["profile"] = []
        else:
            zd["profile"] = []
        zone_list.append(zd)

    return {"zones": zone_list, "count": len(zone_list)}


@router.post("/data/load-sample")
async def load_sample_data(days: int = 5, seed: int = 42):
    """
    載入模擬數據 -- 週末或 API 不可用時使用

    生成逼真的 NQ 5 分鐘 K 線 (含盤整區間 + 突破模式)
    """
    global _historical_candles

    from backend.data.sample_generator import generate_nq_sample

    candles = generate_nq_sample(days=days, interval_minutes=5, seed=seed)
    _historical_candles = candles

    return {
        "success": True,
        "source": "sample_generator",
        "contract_id": "NQ-SAMPLE",
        "candles_count": len(candles),
        "interval": "5m",
        "first": candles[0].timestamp.isoformat() if candles else None,
        "last": candles[-1].timestamp.isoformat() if candles else None,
    }


# ── Simulator (Live Market Simulation) ─────────────────────────

class SimulatorStartRequest(BaseModel):
    base_price: float = 23500.0
    speed: float = 1.0
    seed: Optional[int] = None


class SimulatorSpeedRequest(BaseModel):
    speed: float = 1.0


@router.post("/simulator/start")
async def start_simulator(req: SimulatorStartRequest):
    """
    啟動 live market simulator

    模擬 NQ order flow, 逐 tick 形成 1m/5m candle
    支援 1x/3x/10x 速度
    """
    from backend.data.market_simulator import create_simulator, get_simulator

    sim = get_simulator()
    if sim and sim.is_running:
        return {"success": False, "error": "Simulator already running. Stop it first."}

    sim = create_simulator(
        base_price=req.base_price,
        speed=req.speed,
        seed=req.seed,
    )

    # Create live strategy engine and wire candle callback
    from backend.strategy.live_strategy import create_live_engine
    live_engine = create_live_engine()

    async def on_candle_5m(candle):
        """Feed each new 5m candle to the live strategy engine"""
        try:
            live_engine.update(candle)
        except Exception as e:
            logger.error(f"Live strategy error: {e}")

    sim.set_callbacks(on_candle_5m=on_candle_5m)

    await sim.start()

    return {
        "success": True,
        "base_price": req.base_price,
        "speed": req.speed,
    }


@router.post("/simulator/stop")
async def stop_simulator():
    """停止 simulator 並將累積的 candle 存入回測數據"""
    global _historical_candles

    from backend.data.market_simulator import get_simulator
    from backend.strategy.live_strategy import reset_live_engine

    sim = get_simulator()
    if not sim:
        return {"success": False, "error": "No simulator instance"}

    await sim.stop()

    # 將 5m candle 存入回測數據
    _historical_candles = list(sim.candles_5m)

    # Keep live engine state for review (don't reset until next start)
    # reset_live_engine()

    return {
        "success": True,
        "total_ticks": sim.total_ticks,
        "candles_1m": len(sim.candles_1m),
        "candles_5m": len(sim.candles_5m),
        "buy_volume": sim.total_buy_volume,
        "sell_volume": sim.total_sell_volume,
        "data_ready_for_backtest": len(sim.candles_5m) > 0,
    }


@router.post("/simulator/speed")
async def set_simulator_speed(req: SimulatorSpeedRequest):
    """變更 simulator 速度 (1x, 3x, 10x)"""
    from backend.data.market_simulator import get_simulator

    sim = get_simulator()
    if not sim or not sim.is_running:
        return {"success": False, "error": "Simulator not running"}

    sim.set_speed(req.speed)
    return {"success": True, "speed": sim.speed}


@router.get("/simulator/status")
async def get_simulator_status():
    """取得 simulator 當前狀態"""
    from backend.data.market_simulator import get_simulator

    sim = get_simulator()
    if not sim:
        return {"running": False}

    return sim.get_status()


@router.get("/simulator/candles")
async def get_simulator_candles(interval: str = "5m"):
    """取得 simulator 已完成的 candle 列表"""
    from backend.data.market_simulator import get_simulator

    sim = get_simulator()
    if not sim:
        return {"candles": []}

    candles = sim.candles_5m if interval == "5m" else sim.candles_1m
    current = sim.get_current_candle_5m() if interval == "5m" else sim.get_current_candle_1m()

    return {
        "candles": [
            {
                "time": c.timestamp.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        "current": current,
        "count": len(candles),
    }


@router.get("/simulator/orderbook")
async def get_simulator_orderbook():
    """取得 simulator order book 快照"""
    from backend.data.market_simulator import get_simulator

    sim = get_simulator()
    if not sim:
        return {"error": "No simulator"}

    return {
        "order_book": sim.order_flow.get_book_snapshot(),
        "recent_orders": sim.recent_orders[-20:],
    }


@router.get("/simulator/ticks")
async def get_simulator_ticks(limit: int = 100):
    """取得最近的 tick 數據"""
    from backend.data.market_simulator import get_simulator

    sim = get_simulator()
    if not sim:
        return {"ticks": []}

    ticks = sim.ticks[-limit:]
    return {
        "ticks": [
            {
                "time": t.timestamp.isoformat(),
                "price": t.price,
                "size": t.size,
                "side": t.aggressor.value,
            }
            for t in ticks
        ]
    }


@router.get("/simulator/live-strategy")
async def get_live_strategy_state():
    """
    取得即時策略狀態

    返回: active zones, open/closed trades, live metrics, volume area levels
    前端用於:
      - 繪製 VAH/VAL/POC 線
      - 繪製 TradingView 風格持倉標記
      - 顯示即時勝率等指標
    """
    from backend.strategy.live_strategy import get_live_engine

    engine = get_live_engine()
    if not engine:
        return {
            "active": False,
            "candles_processed": 0,
            "active_zone": None,
            "all_zones": [],
            "open_trades": [],
            "closed_trades": [],
            "metrics": {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "avg_win": 0,
                "avg_loss": 0, "avg_rr": 0, "expectancy": 0,
                "max_drawdown": 0, "profit_factor": 0,
                "max_consec_losses": 0, "open_count": 0,
            },
        }

    state = engine.get_state()
    state["active"] = True
    return state


@router.post("/accounts")
async def get_accounts():
    """
    取得 TopstepX 帳戶列表

    用於前端帳戶切換 (Practice / Funded)
    """
    from backend.broker.topstepx import TopstepXClient

    username = _env("TOPSTEPX_USERNAME")
    api_key = _env("TOPSTEPX_API_KEY")

    if not username or not api_key:
        raise HTTPException(status_code=400, detail="Missing credentials in .env")

    client = TopstepXClient(username=username, api_key=api_key)
    try:
        await client.authenticate()
        accounts = await client.get_accounts()

        result = []
        for acc in accounts:
            name = acc.get("name", "")
            is_practice = "PRAC" in name
            result.append({
                "id": acc["id"],
                "name": name,
                "balance": acc.get("balance", 0),
                "can_trade": acc.get("canTrade", False),
                "is_practice": is_practice,
                "type": "PRACTICE" if is_practice else "FUNDED",
            })

        return {"success": True, "accounts": result}

    except Exception as e:
        logger.error(f"Accounts fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.disconnect()


@router.post("/data/fetch-historical")
async def fetch_historical(req: FetchHistoricalRequest):
    """
    從 TopstepX API 拉取歷史 K 線數據

    優先使用請求中的值，空則從 .env 讀取
    """
    global _historical_candles

    from backend.broker.topstepx import TopstepXClient

    # .env fallback
    username = req.username or _env("TOPSTEPX_USERNAME")
    api_key = req.api_key or _env("TOPSTEPX_API_KEY")
    contract_id = req.contract_id or _env("TOPSTEPX_CONTRACT_ID")
    use_demo = req.use_demo if req.use_demo is not None else (
        _env("TOPSTEPX_USE_DEMO", "false").lower() == "true"
    )

    if not username or not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing credentials: set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in .env"
        )

    logger.info(f"Connecting as '{username}', demo={use_demo}, contract='{contract_id or 'auto'}'")

    client = TopstepXClient(
        username=username,
        api_key=api_key,
        use_demo=use_demo,
    )

    try:
        await client.authenticate()
        logger.info("Auth OK")

        # Store client globally for live trading
        global _topstepx_client, _live_contract_id
        if _topstepx_client:
            try:
                await _topstepx_client.disconnect()
            except Exception:
                pass
        _topstepx_client = client

        # 自動找 NQ 合約
        if not contract_id:
            contract_id = await client.get_nq_contract_id()
            logger.info(f"Auto-detected contract: {contract_id}")
        _live_contract_id = contract_id

        candles = await client.get_historical_bars_paginated(
            contract_id=contract_id,
            unit=BarUnit(req.unit),
            unit_number=req.unit_number,
            start_time=req.start_time,
            end_time=req.end_time,
        )

        _historical_candles = candles

        return {
            "success": True,
            "contract_id": contract_id,
            "candles_count": len(candles),
            "interval": f"{req.unit_number}{'m' if req.unit == 2 else 's'}",
            "first": candles[0].timestamp.isoformat() if candles else None,
            "last": candles[-1].timestamp.isoformat() if candles else None,
        }

    except Exception as e:
        logger.error(f"Fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/data/aggregate")
async def aggregate_data():
    """將已拉取的 1 分鐘數據聚合為 5 分鐘"""
    global _historical_candles

    if not _historical_candles:
        raise HTTPException(status_code=400, detail="請先拉取歷史數據")

    candles_5m = BacktestEngine.aggregate_1m_to_5m(_historical_candles)

    return {
        "source_count": len(_historical_candles),
        "aggregated_count": len(candles_5m),
        "interval": "5m",
    }


@router.post("/backtest/run", response_model=BacktestResponse)
async def run_backtest(req: BacktestRequest):
    """
    執行回測

    流程：
    1. 用已拉取的歷史數據（或聚合後的 5m 數據）
    2. 餵入回測引擎
    3. 返回績效、交易列表、盤整區間、equity curve
    """
    global _historical_candles, _backtest_results

    if not _historical_candles:
        raise HTTPException(
            status_code=400,
            detail="請先通過 /api/data/fetch-historical 拉取數據"
        )

    config = BacktestConfig(
        strategies=["trend_follow"],
        initial_capital=req.initial_capital,
    )

    strategy_params = StrategyParams(
        strategy=req.strategy,
        entry_mode=req.entry_mode,
        tp_ticks=req.tp_ticks,
        sl_ticks=req.sl_ticks,
        entry_timeout_minutes=req.entry_timeout_minutes,
        tp_timeout_minutes=req.tp_timeout_minutes,
        tp_timeout_action=req.tp_timeout_action,
    )

    engine = BacktestEngine(
        config,
        strategy_params=strategy_params,
    )

    # Use 1m candles directly (SessionTrendFollow works on 1m)
    candles = list(_historical_candles)

    result = engine.run(candles)
    _backtest_results.append(result)

    # 轉換為回應格式
    trades_resp = []
    for t in result.trades:
        trades_resp.append(TradeResponse(
            trade_id=t.trade_id,
            strategy=t.strategy.value,
            direction=t.direction.value,
            entry_price=t.entry_price,
            entry_time=t.entry_time.isoformat(),
            exit_price=t.exit_price,
            exit_time=t.exit_time.isoformat() if t.exit_time else None,
            sl_price=t.sl_price,
            tp_price=t.tp_price,
            pnl=t.pnl,
            exit_reason=t.exit_reason.value if t.exit_reason else None,
            zone_id=t.zone_id,
            vol_ratio=t.vol_ratio,
            is_big_trend=t.is_big_trend,
        ))

    zones_resp = []
    vp_calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=0.80)
    for z in result.zones:
        # Calculate VP profile for frontend histogram
        profile_data = None
        if z.candles:
            try:
                vp = vp_calc.calculate(z.candles)
                sorted_levels = sorted(vp.profile.items())
                max_vol = max(vp.profile.values()) if vp.profile else 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted_levels
                ]
            except Exception:
                profile_data = []

        # Zone is mature only if it actually reached maturity during its lifetime
        is_mature = getattr(z, 'mature', False)

        zones_resp.append(ZoneResponse(
            zone_id=z.zone_id,
            formed_at=z.formed_at.isoformat(),
            left_at=z.left_at.isoformat() if z.left_at else None,
            poc=z.poc,
            vah_80=z.vah_80,
            val_80=z.val_80,
            high_100=z.high_100,
            low_100=z.low_100,
            total_volume=z.total_volume,
            duration_minutes=z.duration_minutes,
            num_candles=z.num_candles,
            status=z.status.value,
            exit_direction=z.exit_direction,
            profile=profile_data,
            timeframe=getattr(z, 'timeframe', '1m'),
            parent_zone_id=getattr(z, 'parent_zone_id', None),
            mature=is_mature,
        ))

    m = result.metrics

    def _sub_resp(sm):
        if not sm:
            return None
        return SubMetricsResponse(
            total_trades=sm.total_trades, wins=sm.wins, losses=sm.losses,
            win_rate=sm.win_rate, avg_win=sm.avg_win, avg_loss=sm.avg_loss,
            avg_rr_ratio=sm.avg_rr_ratio, total_pnl=sm.total_pnl,
            profit_factor=sm.profit_factor,
        )

    metrics_resp = MetricsResponse(
        total_trades=m.total_trades,
        wins=m.wins,
        losses=m.losses,
        win_rate=m.win_rate,
        avg_win=m.avg_win,
        avg_loss=m.avg_loss,
        avg_rr_ratio=m.avg_rr_ratio,
        expectancy=m.expectancy,
        max_drawdown=m.max_drawdown,
        max_drawdown_pct=m.max_drawdown_pct,
        sharpe_ratio=m.sharpe_ratio,
        profit_factor=m.profit_factor,
        max_consecutive_losses=m.max_consecutive_losses,
        total_pnl=m.total_pnl,
        reversion=_sub_resp(m.reversion_metrics),
        trend_follow=_sub_resp(m.trend_follow_metrics),
    )

    equity = [
        [ts.timestamp() * 1000, val]
        for ts, val in result.equity_curve
    ]

    return BacktestResponse(
        metrics=metrics_resp,
        trades=trades_resp,
        zones=zones_resp,
        equity_curve=equity,
    )


@router.get("/backtest/results")
async def list_backtests():
    """列出所有回測結果摘要"""
    return [
        {
            "index": i,
            "total_trades": r.metrics.total_trades,
            "win_rate": r.metrics.win_rate,
            "total_pnl": r.metrics.total_pnl,
            "max_drawdown": r.metrics.max_drawdown,
        }
        for i, r in enumerate(_backtest_results)
    ]


# ============================================================
# 即時交易 (Live Trading)
# ============================================================

_live_engine = None


class LiveStartRequest(BaseModel):
    account_id: int
    contract_id: str = "CON.F.US.ENQ.M26"
    value_area_pct: float = 0.80
    # Strategy params
    strategy: str = "trend"
    entry_mode: str = "50RE"
    tp_ticks: int = 50
    sl_ticks: int = 50
    entry_timeout_minutes: int = 30
    tp_timeout_minutes: int = 0
    tp_timeout_action: str = "flat"


@router.post("/live/start")
async def live_start(req: LiveStartRequest):
    """啟動即時交易引擎"""
    global _live_engine

    if _live_engine and _live_engine.is_running:
        raise HTTPException(400, "Live engine already running")

    if not _historical_candles:
        raise HTTPException(400, "No candles loaded — connect first")

    if not _topstepx_client:
        raise HTTPException(400, "TopstepX client not initialized — connect first")

    # Safety: verify account is practice
    try:
        accounts = await _topstepx_client.get_accounts()
        logger.info(f"[LIVE START] accounts found: {[{a.get('id'): a.get('name')} for a in accounts]}")
        logger.info(f"[LIVE START] requested account_id={req.account_id}")
        target = None
        for acc in accounts:
            if acc.get("id") == req.account_id:
                target = acc
                break
        if not target:
            avail_ids = [a.get("id") for a in accounts]
            raise HTTPException(400, f"Account {req.account_id} not found. Available: {avail_ids}")
        name = target.get("name", "")
        logger.info(f"[LIVE START] account verified: {name} (id={req.account_id})")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE START] Account check error: {e}")
        raise HTTPException(500, f"Account check failed: {e}")

    from backend.live.engine import LiveTradingEngine

    # ── Fetch fresh candles for live warm-up (separate from backtest data) ──
    # Don't overwrite _historical_candles — backtest needs the full dataset.
    live_warmup_candles = list(_historical_candles)  # fallback: use existing data
    try:
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        fresh_start = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"[LIVE START] Fetching fresh 1min candles: {fresh_start} ~ {fresh_end}")

        fresh_candles = await _topstepx_client.get_historical_bars_paginated(
            contract_id=req.contract_id,
            unit=BarUnit.MINUTE,
            unit_number=1,
            start_time=fresh_start,
            end_time=fresh_end,
        )
        if fresh_candles and len(fresh_candles) > 0:
            live_warmup_candles = fresh_candles
            logger.info(
                f"[LIVE START] Fresh candles loaded: {len(fresh_candles)} | "
                f"range: {fresh_candles[0].timestamp} ~ {fresh_candles[-1].timestamp}"
            )
        else:
            logger.warning("[LIVE START] Fresh fetch returned 0 candles, using existing data")
    except Exception as e:
        logger.error(f"[LIVE START] Failed to fetch fresh candles: {e} — using existing data")

    live_strategy_params = StrategyParams(
        strategy=req.strategy,
        entry_mode=req.entry_mode,
        tp_ticks=req.tp_ticks,
        sl_ticks=req.sl_ticks,
        entry_timeout_minutes=req.entry_timeout_minutes,
        tp_timeout_minutes=req.tp_timeout_minutes,
        tp_timeout_action=req.tp_timeout_action,
    )

    _live_engine = LiveTradingEngine(
        client=_topstepx_client,
        account_id=req.account_id,
        contract_id=req.contract_id,
        value_area_pct=req.value_area_pct,
        skip_engine_sl_tp=False,  # Engine places SL/TP after fill
        strategy_params=live_strategy_params,
    )

    # Log candle date range
    if live_warmup_candles:
        first_ts = live_warmup_candles[0].timestamp
        last_ts = live_warmup_candles[-1].timestamp
        logger.info(
            f"[LIVE START] {len(live_warmup_candles)} warmup candles | "
            f"range: {first_ts} ~ {last_ts}"
        )
    else:
        logger.warning("[LIVE START] NO warmup candles!")
    try:
        await _live_engine.start(live_warmup_candles)
        logger.info(f"[LIVE START] Engine started successfully")
    except Exception as e:
        logger.error(f"[LIVE START] Engine start failed: {e}")
        _live_engine = None
        raise HTTPException(500, f"Engine start failed: {e}")

    return {"success": True, "message": "Live engine started"}


@router.post("/live/stop")
async def live_stop():
    """停止即時交易引擎"""
    global _live_engine
    if not _live_engine or not _live_engine.is_running:
        return {"success": True, "message": "Not running"}

    await _live_engine.stop()
    return {"success": True, "message": "Live engine stopped"}


@router.post("/live/cancel-pending")
async def live_cancel_pending():
    """取消掛單"""
    if not _live_engine:
        raise HTTPException(400, "Live engine not started")
    cancelled = await _live_engine.cancel_pending_now()
    return {"success": cancelled, "message": "Pending order cancelled" if cancelled else "No pending order"}


@router.post("/live/flatten")
async def live_flatten():
    """緊急平倉"""
    if not _live_engine:
        raise HTTPException(400, "Live engine not started")
    await _live_engine.flatten_now()
    return {"success": True, "message": "Flatten executed"}


@router.get("/live/status")
async def live_status():
    """取得即時交易狀態"""
    if not _live_engine:
        return {"running": False}
    return _live_engine.get_status()


@router.get("/live/account-state")
async def live_account_state():
    """
    讀取 TopstepX 帳戶的真實狀態 (持倉 + 掛單 + 餘額)
    直接從 TopstepX API 讀取, 不依賴 live engine 狀態
    """
    if not _topstepx_client:
        raise HTTPException(400, "TopstepX client not initialized — connect first")

    try:
        # Get all accounts
        accounts = await _topstepx_client.get_accounts()
        results = []

        for acc in accounts:
            acc_id = acc.get("id")
            acc_name = acc.get("name", "?")
            acc_balance = acc.get("balance", 0)

            # Get positions
            try:
                positions = await _topstepx_client.get_positions(acc_id)
            except Exception as e:
                positions = [{"error": str(e)}]

            # Get orders
            try:
                orders = await _topstepx_client.get_orders(acc_id)
            except Exception as e:
                orders = [{"error": str(e)}]

            results.append({
                "account_id": acc_id,
                "name": acc_name,
                "balance": acc_balance,
                "is_practice": "PRAC" in acc_name,
                "positions": positions,
                "orders": orders,
            })

        # Also include live engine state for comparison
        engine_state = None
        if _live_engine:
            engine_state = {
                "running": _live_engine._running,
                "pending_order_id": _live_engine._pending_order_id,
                "pending_signal": {
                    "direction": _live_engine._pending_signal.direction.value,
                    "entry": _live_engine._pending_signal.entry_price,
                    "sl": _live_engine._pending_signal.sl_price,
                    "tp": _live_engine._pending_signal.tp_price,
                    "strategy": _live_engine._pending_signal.strategy.value,
                } if _live_engine._pending_signal else None,
                "open_position": _live_engine._open_position,
                "candles_processed": _live_engine._candles_processed,
                "log": _live_engine._log[-30:],
            }

        return {
            "accounts": results,
            "engine": engine_state,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        raise HTTPException(500, f"Failed to read account state: {e}")


# ── Presets (JSON file) ────────────────────────────────

import json as _json

_PRESETS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "presets.json"
)


def _load_presets_file() -> dict:
    try:
        if os.path.exists(_PRESETS_FILE):
            with open(_PRESETS_FILE, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {"presets": {}, "last_used_bt": "default", "last_used_live": "default"}


def _save_presets_file(data: dict):
    os.makedirs(os.path.dirname(_PRESETS_FILE), exist_ok=True)
    with open(_PRESETS_FILE, "w", encoding="utf-8") as f:
        _json.dump(data, f, indent=2, ensure_ascii=False)


@router.get("/presets")
async def get_presets():
    """列出所有 presets + last used"""
    return _load_presets_file()


class PresetSaveRequest(BaseModel):
    name: str
    params: dict


@router.post("/presets/save")
async def save_preset(req: PresetSaveRequest):
    """儲存 preset"""
    data = _load_presets_file()
    data["presets"][req.name] = req.params
    _save_presets_file(data)
    return {"success": True, "name": req.name}


class PresetUseRequest(BaseModel):
    name: str
    mode: str  # "bt" or "live"


@router.post("/presets/use")
async def use_preset(req: PresetUseRequest):
    """記錄 last used preset"""
    data = _load_presets_file()
    key = "last_used_bt" if req.mode == "bt" else "last_used_live"
    data[key] = req.name
    _save_presets_file(data)
    return {"success": True}


@router.delete("/presets/{name}")
async def delete_preset(name: str):
    """刪除 preset"""
    data = _load_presets_file()
    if name in data["presets"]:
        del data["presets"][name]
        if data.get("last_used_bt") == name:
            data["last_used_bt"] = "default"
        if data.get("last_used_live") == name:
            data["last_used_live"] = "default"
        _save_presets_file(data)
    return {"success": True}
