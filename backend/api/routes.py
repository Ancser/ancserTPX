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

from backend.db.models import BacktestConfig, BarUnit, Candle
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


# ── Pydantic 請求/回應模型 ────────────────────────────

class BacktestRequest(BaseModel):
    strategies: List[str] = ["reversion", "trend_follow"]
    initial_capital: float = 50000.0
    slippage_ticks: int = 1
    max_daily_loss: float = 2000.0
    min_candles_for_zone: int = 6
    poc_drift_threshold: float = 3.0
    value_area_pct: float = 0.80


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

        # 自動找 NQ 合約
        if not contract_id:
            contract_id = await client.get_nq_contract_id()
            logger.info(f"Auto-detected contract: {contract_id}")

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
    finally:
        await client.disconnect()


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
        strategies=req.strategies,
        initial_capital=req.initial_capital,
        slippage_ticks=req.slippage_ticks,
        max_daily_loss=req.max_daily_loss,
        min_candles_for_zone=req.min_candles_for_zone,
        poc_drift_threshold=req.poc_drift_threshold,
        value_area_pct=req.value_area_pct,
    )

    engine = BacktestEngine(config)

    # 如果是 1m 數據，先聚合
    if _historical_candles and _historical_candles[0].interval in ("1m", "1s"):
        candles = engine.aggregate_1m_to_5m(_historical_candles)
    else:
        candles = _historical_candles

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
        ))

    m = result.metrics
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


@router.post("/backtest/run-replay")
async def run_backtest_replay(req: BacktestRequest):
    """
    Run backtest with per-candle snapshots for replay mode.
    Returns candles + snapshots for animated playback.
    """
    global _historical_candles

    if not _historical_candles:
        raise HTTPException(status_code=400, detail="請先拉取數據")

    config = BacktestConfig(
        strategies=req.strategies,
        initial_capital=req.initial_capital,
        slippage_ticks=req.slippage_ticks,
        max_daily_loss=req.max_daily_loss,
        min_candles_for_zone=req.min_candles_for_zone,
        poc_drift_threshold=req.poc_drift_threshold,
        value_area_pct=req.value_area_pct,
    )

    engine = BacktestEngine(config)

    if _historical_candles and _historical_candles[0].interval in ("1m", "1s"):
        candles = engine.aggregate_1m_to_5m(_historical_candles)
    else:
        candles = _historical_candles

    data = engine.run_with_replay(candles)

    m = data["metrics"]
    return {
        "metrics": {
            "total_trades": m.total_trades,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": m.win_rate,
            "total_pnl": m.total_pnl,
            "profit_factor": m.profit_factor,
            "max_drawdown": m.max_drawdown,
            "expectancy": m.expectancy,
        },
        "trades": data["trades"],
        "snapshots": data["snapshots"],
        "candles": data["candles"],
    }


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
