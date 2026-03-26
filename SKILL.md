# ancserTPX — 技術技能樹 & 文件開發追蹤

## 文件狀態標記規範

每個代碼文件開頭必須包含以下註解區塊：

```python
# ============================================================
# 文件: <文件路徑>
# 狀態: 空白 | 正在修改 | 未完成 | 已完成 | 已檢查 x 次
# 問題: 無 | 有（描述）
# 關聯文件: <列出所有依賴和被依賴的文件>
# 函數結構:
#   - function_name(params) -> return_type : 簡述
#   - ...
# ============================================================
```

```tsx
// ============================================================
// 文件: <文件路徑>
// 狀態: 空白 | 正在修改 | 未完成 | 已完成 | 已檢查 x 次
// 問題: 無 | 有（描述）
// 關聯文件: <列出所有依賴和被依賴的文件>
// 函數結構:
//   - ComponentName(props) : 簡述
//   - ...
// ============================================================
```

**狀態流轉規則：**
```
空白 -> 正在修改 -> 未完成 / 已完成 -> 已檢查 1 次 -> 已檢查 2 次 -> ...
                ^                          |
                +---- 發現問題需修改 --------+
```

**操作規範：**
1. 開始寫任何代碼文件前 -> 先將狀態更新為 `正在修改`
2. 寫完後 -> 更新為 `未完成`（有 TODO）或 `已完成`
3. 審查後 -> 更新為 `已檢查 x 次`
4. 發現問題 -> `問題: 有（描述）`，狀態回到 `正在修改`

---

## 文件依賴總覽

```
                        main.py
                           │
              ┌────────────┼────────────┐
              v            v            v
          routes.py   websocket.py   database.py
              |            |            |
              v            v            v
    +---------+-----+   models.py <----+
    v               v       ^
engine.py      manager.py  |
    |               |       |
    v               v       |
+---+---+     topstepx.py  |
v       v                   |
reversion.py  trend_follow.py
    |               |
    v               v
consolidation.py <--+
    |
    v
volume_profile.py
    |
    v
metrics.py
```

---

## 一、後端 Python 文件

### 1.1 `backend/main.py` — FastAPI 入口

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | -> `api/routes.py`, `api/websocket.py`, `db/database.py` |

**函數結構：**
```python
create_app() -> FastAPI
    # 初始化 FastAPI 應用，掛載路由、WebSocket、CORS、啟動/關閉事件

lifespan(app: FastAPI) -> AsyncGenerator
    # 應用生命週期：啟動時初始化 DB + 連接 TopstepX，關閉時清理

startup_init_db() -> None
    # 初始化 SQLite 資料庫表結構

startup_connect_broker() -> None
    # 連接 TopstepX API（Practice Account）
```

**職責：**
- FastAPI 應用建立與配置
- CORS 中介軟體（允許前端 localhost 連接）
- 路由掛載
- 生命週期管理（DB 初始化、API 連接）

---

### 1.2 `backend/api/routes.py` — REST API 路由

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `main.py` / -> `backtest/engine.py`, `strategy/*`, `db/models.py`, `risk/manager.py` |

**函數結構：**
```python
# === 回測相關 ===
POST /api/backtest/run
    run_backtest(config: BacktestConfig) -> BacktestResult
    # 執行回測，返回績效結果

GET /api/backtest/results
    list_backtest_results() -> List[BacktestSummary]
    # 列出所有回測結果

GET /api/backtest/results/{id}
    get_backtest_detail(id: str) -> BacktestDetail
    # 獲取單次回測詳細數據（含每筆交易）

# === 盤整區間相關 ===
GET /api/zones
    list_zones(date: Optional[str]) -> List[ConsolidationZone]
    # 列出指定日期的所有盤整區間

GET /api/zones/{zone_id}
    get_zone_detail(zone_id: str) -> ZoneDetail
    # 獲取單個盤整區間詳情 + 關聯交易

# === 交易相關 ===
GET /api/trades
    list_trades(date: Optional[str], strategy: Optional[str]) -> List[Trade]
    # 列出交易紀錄

GET /api/trades/stats
    get_trade_stats(period: str) -> TradeStats
    # 獲取績效統計（日/週/月/全部）

# === 即時交易控制 ===
POST /api/live/start
    start_live_trading(config: LiveConfig) -> StatusResponse
    # 啟動即時交易策略

POST /api/live/stop
    stop_live_trading() -> StatusResponse
    # 停止即時交易

GET /api/live/status
    get_live_status() -> LiveStatus
    # 獲取即時交易狀態

# === 數據相關 ===
GET /api/data/candles
    get_candles(symbol: str, start: str, end: str, interval: str) -> List[Candle]
    # 獲取 K 線數據

GET /api/data/volume-profile
    get_volume_profile(symbol: str, start: str, end: str) -> VolumeProfile
    # 計算並返回 Volume Profile
```

---

### 1.3 `backend/api/websocket.py` — WebSocket 即時推送

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `main.py` / -> `broker/topstepx.py`, `strategy/consolidation.py` |

**函數結構：**
```python
class ConnectionManager:
    connect(websocket: WebSocket) -> None
    disconnect(websocket: WebSocket) -> None
    broadcast(message: dict) -> None
        # 管理 WebSocket 連接池，廣播訊息給所有前端客戶端

ws_endpoint(websocket: WebSocket) -> None
    # WebSocket 主端點 /ws

broadcast_candle(candle: dict) -> None
    # 推送新 K 線數據

broadcast_zone_update(zone: dict) -> None
    # 推送盤整區間狀態變更（forming/active/left）

broadcast_trade_signal(signal: dict) -> None
    # 推送交易信號（入場/出場）

broadcast_position_update(position: dict) -> None
    # 推送持倉變更
```

**推送消息格式：**
```python
{
    "type": "candle" | "zone" | "signal" | "position" | "status",
    "data": { ... },
    "timestamp": "2026-03-21T10:15:00"
}
```

---

### 1.4 `backend/strategy/volume_profile.py` — Volume Profile 計算

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `strategy/consolidation.py` / -> 無外部依賴（純計算） |

**函數結構：**
```python
class VolumeProfileCalculator:
    __init__(tick_size: float = 0.25, value_area_pct: float = 0.80)
        # tick_size: NQ 最小跳動 0.25 點
        # value_area_pct: Value Area 百分比，預設 80%

    calculate(candles: List[Candle]) -> VolumeProfileResult
        # 輸入 K 線列表，計算完整 Volume Profile
        # 返回: POC, VAH, VAL, high_100, low_100, profile_data

    _build_price_volume_map(candles: List[Candle]) -> Dict[float, int]
        # 將每根 K 線的成交量按價格分配
        # 方法：假設成交量在 high-low 間均勻分布（TPO 近似）

    _find_poc(price_volume: Dict[float, int]) -> float
        # 找出最大成交量對應的價格 = POC

    _calculate_value_area(
        price_volume: Dict[float, int],
        poc: float,
        pct: float
    ) -> Tuple[float, float]
        # 從 POC 向兩側擴展，直到累計成交量達到 pct%
        # 返回 (VAH, VAL)

    _calculate_range(price_volume: Dict[float, int]) -> Tuple[float, float]
        # 返回 (100% high, 100% low)
```

**核心算法：**
```
1. 將每根 K 線的 volume 按 OHLC 分配到各 tick 價位
2. 彙總所有 tick 的累計 volume -> price_volume_map
3. POC = argmax(price_volume_map)
4. 從 POC 向上/下逐步擴展，每步選擇 volume 較大的一側
5. 當累計 volume >= 80% x total_volume -> VAH/VAL 確定
```

---

### 1.5 `backend/strategy/consolidation.py` — 盤整區間偵測

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `strategy/reversion.py`, `strategy/trend_follow.py`, `api/websocket.py` / -> `strategy/volume_profile.py`, `db/models.py` |

**函數結構：**
```python
class ConsolidationDetector:
    __init__(
        min_candles: int = 6,           # 最少 K 線數量才判定盤整
        poc_drift_threshold: float = 3.0, # POC 偏移閾值（點數）
        min_touches: int = 2,           # 價格觸及 VAH/VAL 最少次數
        value_area_pct: float = 0.80
    )

    update(candle: Candle) -> Optional[ZoneEvent]
        # 每根新 K 線進來時調用
        # 返回: None | ZoneForming | ZoneActive | ZoneLeft
        # 這是主入口，串接以下所有內部方法

    _detect_new_zone(candles: List[Candle]) -> Optional[ConsolidationZone]
        # 嘗試從最近 N 根 K 線偵測新盤整區間
        # 條件：K 線數 >= min_candles，POC 穩定，價格在 VA 內

    _check_zone_stability(zone: ConsolidationZone, new_candle: Candle) -> bool
        # 檢查現有盤整區間是否仍然穩定
        # 條件：POC 偏移 < threshold，價格未突破 100% 邊界

    _detect_zone_exit(zone: ConsolidationZone, candle: Candle) -> Optional[str]
        # 檢測是否離開盤整區間
        # 返回: None | "up" | "down"
        # 判定：收盤價連續在 100% 邊界外

    _check_volume_breakout(
        candle: Candle,
        prev_candles: List[Candle],
        direction: str
    ) -> BreakoutAnalysis
        # 分析突破成交量
        # vol_outside vs avg(vol_before_2_candles)
        # 返回: BreakoutAnalysis(vol_ratio, is_trend_signal)

    get_active_zones() -> List[ConsolidationZone]
        # 返回當前所有 active 狀態的盤整區間

    get_zone_history() -> List[ConsolidationZone]
        # 返回所有歷史盤整區間（含 left 狀態）
```

**狀態機：**
```
[無區間] ──新 K 線累積──-> [forming]
[forming] ──穩定確認──-> [active]
[forming] ──不穩定──-> [無區間]（重置）
[active] ──價格離開──-> [left] ──等待新累積──-> [forming]
```

---

### 1.6 `backend/strategy/reversion.py` — 策略一：均值回歸

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `backtest/engine.py`, `api/routes.py` / -> `strategy/consolidation.py`, `db/models.py` |

**函數結構：**
```python
class ReversionStrategy:
    __init__(
        sl_points: float = 15.0,    # 止損 15 點 = $300
        tp_target: str = "poc",      # 止盈目標 = POC
        contract_size: int = 1,      # 1 口 NQ
        point_value: float = 20.0    # NQ 每點 $20
    )

    evaluate(
        candle: Candle,
        active_zone: ConsolidationZone
    ) -> Optional[TradeSignal]
        # 評估當前 K 線是否觸發入場條件
        # 條件：價格觸及 VAH -> 做空 / 觸及 VAL -> 做多
        # 返回: TradeSignal(direction, entry, sl, tp) | None

    _check_vah_touch(price: float, vah: float, tolerance: float) -> bool
        # 價格是否觸及 VAH（含容差）

    _check_val_touch(price: float, val: float, tolerance: float) -> bool
        # 價格是否觸及 VAL（含容差）

    _calculate_sl(entry: float, direction: str) -> float
        # 計算止損價位
        # SELL: entry + sl_points
        # BUY:  entry - sl_points

    _calculate_tp(poc: float) -> float
        # 止盈 = POC

    _validate_risk_reward(entry: float, sl: float, tp: float) -> bool
        # 驗證風報比 >= 1:2
        # |tp - entry| / |sl - entry| >= 2.0
```

---

### 1.7 `backend/strategy/trend_follow.py` — 策略二：趨勢跟隨

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `backtest/engine.py`, `api/routes.py` / -> `strategy/consolidation.py`, `db/models.py` |

**函數結構：**
```python
class TrendFollowStrategy:
    __init__(
        vol_ratio_threshold: float = 1.0,  # 成交量比率閾值
        tp_multiplier: float = 2.0,        # TP = 2x (100% edge - 80% edge)
        big_trend_multiplier: float = 3.0, # 大趨勢模式 TP 倍數
        big_trend_vol_ratio: float = 2.0,  # 觸發大趨勢的成交量比率
        contract_size: int = 1,
        point_value: float = 20.0
    )

    evaluate(
        candle: Candle,
        left_zone: ConsolidationZone,
        prev_candles: List[Candle]       # 離開前的 K 線（至少 2 根）
    ) -> Optional[TradeSignal]
        # 評估趨勢跟隨入場條件
        # 1. 價格在 80% 邊界外
        # 2. 外部成交量 > 前 2 根 K 線平均量
        # 返回: TradeSignal | None

    _is_outside_value_area(price: float, zone: ConsolidationZone) -> Optional[str]
        # 判斷價格是否在 VA 外
        # 返回: "above_vah" | "below_val" | None

    _check_volume_confirmation(
        current_vol: int,
        prev_2_candles: List[Candle]
    ) -> Tuple[bool, float]
        # 成交量確認
        # vol_ratio = current_vol / avg(prev_2_vols)
        # 返回: (is_confirmed, vol_ratio)

    _is_big_trend(vol_ratio: float) -> bool
        # vol_ratio >= big_trend_vol_ratio -> 大趨勢模式

    _calculate_sl(poc: float) -> float
        # SL = POC

    _calculate_tp(
        entry: float,
        zone: ConsolidationZone,
        direction: str,
        is_big_trend: bool
    ) -> float
        # 正常: TP = entry + 2 x (100% edge - 80% edge)
        # 大趨勢: TP = entry + 3 x range（目標 $1800）

    _calculate_pnl_estimate(entry: float, sl: float, tp: float) -> dict
        # 返回 {"sl_dollar": $300, "tp_dollar": $600/$1800, "rr_ratio": 1:2/1:3}
```

---

### 1.8 `backend/backtest/engine.py` — 回測引擎

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `api/routes.py` / -> `strategy/reversion.py`, `strategy/trend_follow.py`, `strategy/consolidation.py`, `backtest/metrics.py`, `db/models.py` |

**函數結構：**
```python
class BacktestEngine:
    __init__(
        strategies: List[BaseStrategy],     # 策略列表
        initial_capital: float = 50000.0,   # TopstepX 初始資金
        slippage_ticks: int = 1,            # 滑價（tick）
        commission_per_contract: float = 0.0, # 手續費（TopstepX 內含）
        max_daily_loss: float = 2000.0,     # 每日最大虧損
        flatten_time: str = "15:05"         # 強制平倉時間 CT
    )

    run(candles: List[Candle]) -> BacktestResult
        # 主回測循環
        # 逐根 K 線餵入 -> 偵測盤整 -> 評估策略 -> 模擬執行 -> 記錄

    _process_candle(candle: Candle) -> List[TradeEvent]
        # 處理單根 K 線：更新盤整區間 -> 檢查持倉 SL/TP -> 評估新入場

    _simulate_fill(signal: TradeSignal, candle: Candle) -> Trade
        # 模擬成交（加入滑價）

    _check_exit_conditions(position: Position, candle: Candle) -> Optional[ExitReason]
        # 檢查出場條件：SL 觸及 / TP 觸及 / 時間平倉

    _check_daily_loss_limit(date: str) -> bool
        # 檢查是否觸及每日最大虧損

    _flatten_all(time: str) -> List[Trade]
        # 強制平倉所有持倉

    load_data(filepath: str) -> List[Candle]
        # 從 CSV 讀取歷史 K 線數據

    load_data_from_db(symbol: str, start: str, end: str) -> List[Candle]
        # 從 SQLite 讀取歷史數據
```

---

### 1.9 `backend/backtest/metrics.py` — 績效指標計算

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `backtest/engine.py`, `api/routes.py` / -> 無外部依賴（純計算） |

**函數結構：**
```python
class MetricsCalculator:
    calculate_all(trades: List[Trade], initial_capital: float) -> Metrics
        # 一次計算所有指標

    win_rate(trades: List[Trade]) -> float
        # 勝率 = wins / total

    avg_win_loss_ratio(trades: List[Trade]) -> float
        # 平均盈虧比 = avg(winning_pnl) / abs(avg(losing_pnl))

    expectancy(trades: List[Trade]) -> float
        # 期望值 = (win_rate x avg_win) - (loss_rate x avg_loss)

    max_drawdown(trades: List[Trade], initial_capital: float) -> Tuple[float, float]
        # 最大回撤（金額, 百分比）

    sharpe_ratio(trades: List[Trade], risk_free_rate: float = 0.0) -> float
        # Sharpe = (avg_return - rf) / std(returns)

    profit_factor(trades: List[Trade]) -> float
        # Profit Factor = gross_profit / gross_loss

    max_consecutive_losses(trades: List[Trade]) -> int
        # 最大連續虧損次數

    daily_pnl_summary(trades: List[Trade]) -> Dict[str, float]
        # 每日盈虧匯總

    monthly_pnl_summary(trades: List[Trade]) -> Dict[str, float]
        # 每月盈虧匯總

    strategy_breakdown(trades: List[Trade]) -> Dict[str, Metrics]
        # 按策略分類的績效（reversion vs trend_follow）
```

---

### 1.10 `backend/broker/topstepx.py` — TopstepX API 封裝

| 項目 | 內容 |
|------|------|
| **狀態** | 已完成 + 已加入完整 logging |
| **問題** | 已修復: get_orders() 回傳格式不明 → 加入 raw response logging |
| **關聯文件** | <- `api/websocket.py`, `api/routes.py`, `risk/manager.py`, `live/engine.py` / -> `db/models.py` |

**函數結構：**
```python
class TopstepXClient:
    __init__(api_key: str, api_secret: str, base_url: str)

    # === 連接管理 ===
    async connect() -> None
    async disconnect() -> None
    async reconnect() -> None

    # === 市場數據 ===
    async subscribe_market_data(symbol: str) -> None
    async get_historical_candles(...) -> List[Candle]
    def on_candle(callback: Callable) -> None
    def on_tick(callback: Callable) -> None

    # === 訂單管理 (所有操作都有完整 logging) ===
    async place_order(order: OrderRequest) -> OrderResponse
        # [LOG] [ORDER SEND] side, type, limit/stop price, account, contract
        # [LOG] [ORDER RESP] success, order_id, error_code, raw response keys
        # 安全檢查：自動攔截 Funded 帳戶下單

    async cancel_order(order_id: int) -> bool
        # [LOG] [ORDER CANCEL] + [ORDER CANCEL RESP]

    async get_orders(account_id: int) -> List[Dict]
        # [LOG] [ORDER SEARCH] response_type, keys, raw_preview (前500字)
        # 處理兩種回傳格式: {"orders": [...]} 或直接 list

    async get_open_orders(account_id: int) -> List[Dict]
        # 過濾 status in ("Open", "Working")

    # === 持倉管理 (加入 logging) ===
    async get_positions(account_id: int) -> List[Dict]
        # [LOG] [POSITION] count, side, avgPrice, size, contractId, unrealizedPnl
        # 處理兩種格式: {"positions": [...]} 或直接 list

    async close_position(account_id, contract_id) -> OrderResponse
    async flatten_all(account_id: int) -> List[OrderResponse]

    # === 帳戶資訊 ===
    async get_account_info() -> AccountInfo
    async get_daily_pnl() -> float
```

---

### 1.11 `backend/risk/manager.py` — 風控管理

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `api/routes.py`, `backtest/engine.py` / -> `broker/topstepx.py`, `db/models.py` |

**函數結構：**
```python
class RiskManager:
    __init__(
        max_daily_loss: float = 2000.0,      # 每日最大虧損 $2000
        max_position_size: int = 1,           # 最大持倉 1 口
        flatten_time: str = "15:05",          # CT 時間強制平倉
        sl_required: bool = True,             # 每筆交易必須有 SL
        max_sl_per_trade: float = 300.0,      # 單筆最大止損 $300
        cooldown_after_loss: int = 5          # 連續虧損後冷卻（分鐘）
    )

    pre_trade_check(signal: TradeSignal, account: AccountInfo) -> RiskCheckResult
        # 下單前風控檢查
        # 1. 是否超過每日虧損限制
        # 2. 是否超過持倉上限
        # 3. 是否設定 SL
        # 4. SL 金額是否在允許範圍
        # 5. 是否在冷卻期
        # 6. 是否接近強制平倉時間

    check_daily_loss(current_pnl: float) -> bool
        # 當日虧損是否超限

    check_position_limit(current_positions: int) -> bool
        # 持倉是否超限

    check_flatten_time(current_time: datetime) -> bool
        # 是否需要強制平倉

    should_cooldown(recent_trades: List[Trade]) -> bool
        # 是否進入冷卻期（連續虧損）

    force_flatten(broker: TopstepXClient) -> None
        # 強制平倉（3:05 PM CT 或觸及日虧損限制）

    get_risk_status() -> RiskStatus
        # 返回當前風控狀態摘要
```

---

### 1.12a `backend/live/engine.py` — 即時交易引擎

| 項目 | 內容 |
|------|------|
| **狀態** | 正在修改 — 已加入 safety checks + logging |
| **問題** | 已修復 P0 致命問題 (詳見 PLAN.md §10) |
| **關聯文件** | <- `api/routes.py` / -> `broker/topstepx.py`, `strategy/consolidation.py` |

**函數結構：**
```python
class LiveTradingEngine:
    __init__(client, account_id, contract_id, ..., skip_engine_sl_tp=True)
        # skip_engine_sl_tp: True = 由 TopstepX Position Bracket 管理 SL/TP (預設)

    async start(historical_candles: List[Candle]) -> None
        # [LOG] K 線日期範圍 (first_ts ~ last_ts)，防止用舊數據

    async _place_order(signal: TradeSignal) -> None
        # [SAFETY] entry price vs market price 驗證 (±50pts)
        #   → SELL LIMIT << 市價 = 立即成交 → 攔截!
        #   → BUY LIMIT >> 市價 = 立即成交 → 攔截!
        # [LOG] [SAFETY OK/BLOCK] 市價 vs entry 差距
        # [LOG] zone_id 追蹤

    async _check_pending_fill() -> bool
        # [LOG] fill price vs entry price 比較
        # [LOG] ⚠ [FILL MISMATCH] if slippage > 5 pts
        # [LOG] position raw data (含 avgPrice)
        # 如果 skip_engine_sl_tp=True → 不下 SL/TP

    async _place_sl_tp() -> None
        # 當 skip_engine_sl_tp=True 時不會被調用
        # TopstepX Position Bracket (300:900) 自動管理

    async _sync_position() -> None
        # [LOG] [PNL] balance changes
        # [LOG] fill price 追蹤到平倉

    _last_market_price: float  # 每次拉 candle 更新 close 價
    _fill_price: float         # 實際成交價 (from avgPrice)
    _skip_engine_sl_tp: bool   # 是否由平台 bracket 管理
```

**安全機制 (2026-03-25 新增)：**
1. `PRICE_SAFETY_MARGIN = 50 pts` — entry 偏離市價超過此值 → 攔截
2. `skip_engine_sl_tp = True` — 預設由 TopstepX Bracket 管理 SL/TP，避免重複
3. Fill price tracking — 成交後比較 signal.entry_price vs position.avgPrice
4. Candle date range logging — warm-up 時顯示數據日期範圍

---

### 1.12 `backend/db/models.py` — 資料模型

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- 幾乎所有後端文件 / -> 無 |

**資料模型：**
```python
@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str = "NQ"
    interval: str = "5m"

@dataclass
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    high_100: float
    low_100: float
    total_volume: int
    profile: Dict[float, int]    # {price: volume}

@dataclass
class ConsolidationZone:
    zone_id: str
    formed_at: datetime
    left_at: Optional[datetime]
    poc: float
    vah_80: float
    val_80: float
    high_100: float
    low_100: float
    total_volume: int
    duration_minutes: int
    num_candles: int
    status: str                  # 'forming' | 'active' | 'left'
    exit_direction: Optional[str] # 'up' | 'down' | None

@dataclass
class TradeSignal:
    strategy: str                # 'reversion' | 'trend_follow'
    direction: str               # 'buy' | 'sell'
    entry_price: float
    sl_price: float
    tp_price: float
    zone_id: str
    reason: str

@dataclass
class Trade:
    trade_id: str
    strategy: str
    direction: str
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float]
    exit_time: Optional[datetime]
    sl_price: float
    tp_price: float
    pnl: Optional[float]
    exit_reason: Optional[str]   # 'tp' | 'sl' | 'flatten' | 'manual'
    zone_id: str
    contracts: int = 1

@dataclass
class BreakoutAnalysis:
    breakout_id: str
    from_zone_id: str
    direction: str
    breakout_time: datetime
    vol_before_avg: float
    vol_outside: float
    vol_ratio: float
    is_trend_signal: bool

@dataclass
class BacktestConfig:
    strategies: List[str]
    symbol: str = "NQ"
    interval: str = "5m"
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 50000.0
    slippage_ticks: int = 1

@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: List[Trade]
    zones: List[ConsolidationZone]
    metrics: 'Metrics'
    equity_curve: List[Tuple[datetime, float]]

@dataclass
class Metrics:
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
    daily_pnl: Dict[str, float]

@dataclass
class RiskStatus:
    daily_pnl: float
    daily_loss_remaining: float
    current_positions: int
    is_cooldown: bool
    minutes_to_flatten: int
    is_trading_allowed: bool
```

---

### 1.13 `backend/db/database.py` — SQLite 連接

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `main.py`, `api/routes.py` / -> `db/models.py` |

**函數結構：**
```python
class Database:
    __init__(db_path: str = "data/ancserTPX.db")

    init_tables() -> None
        # 建立所有 SQLite 表

    # === K 線數據 ===
    save_candles(candles: List[Candle]) -> None
    get_candles(symbol: str, start: str, end: str, interval: str) -> List[Candle]

    # === 盤整區間 ===
    save_zone(zone: ConsolidationZone) -> None
    update_zone_status(zone_id: str, status: str, left_at: datetime = None) -> None
    get_zones(date: str = None, status: str = None) -> List[ConsolidationZone]

    # === 交易紀錄 ===
    save_trade(trade: Trade) -> None
    update_trade(trade_id: str, **kwargs) -> None
    get_trades(date: str = None, strategy: str = None) -> List[Trade]

    # === 回測結果 ===
    save_backtest(result: BacktestResult) -> str
    get_backtest(backtest_id: str) -> BacktestResult
    list_backtests() -> List[BacktestSummary]

    # === 突破分析 ===
    save_breakout(analysis: BreakoutAnalysis) -> None
    get_breakouts(zone_id: str = None) -> List[BreakoutAnalysis]
```

**SQLite 表結構：**
```sql
candles(timestamp, symbol, interval, open, high, low, close, volume)
zones(zone_id, formed_at, left_at, poc, vah_80, val_80, high_100, low_100, ...)
trades(trade_id, strategy, direction, entry_price, entry_time, exit_price, ...)
backtests(backtest_id, config_json, created_at)
backtest_trades(backtest_id, trade_id)
breakouts(breakout_id, from_zone_id, direction, vol_before_avg, vol_outside, ...)
```

---

### 1.14 `backend/data/market_simulator.py` -- Live Market Simulator

| 項目 | 內容 |
|------|------|
| **狀態** | 已完成 |
| **問題** | 無 |
| **關聯文件** | <- `api/routes.py` (啟動/停止/速度) / -> `db/models.py` (Candle) |

**函數結構：**
```python
class OrderBookSimulator:
    __init__(mid_price: float = 23500.0)
    execute_market_order(side: OrderSide, size: int) -> List[Tick]
    snapshot() -> dict
    # 模擬 5 檔 bid/ask order book, NQ tick_size=0.25

class RegimeController:
    update(current_price: float) -> Tuple[MarketRegime, bias, vol_mult]
    # 控制市場狀態轉換: consolidation -> breakout -> trending -> 新 consolidation
    # 自動產生盤整+突破+趨勢模式

class CandleBuilder:
    add_tick(tick: Tick) -> Optional[Candle]
    flush() -> Optional[Candle]
    # 從 tick 累積構建 1m/5m candle

class MarketSimulator:
    __init__(base_price=23500.0, speed=1.0, seed=None)
    start() -> None                    # 啟動模擬 (async)
    stop() -> None                     # 停止, flush candle
    set_speed(multiplier: float)       # 1x/3x/10x
    get_status() -> dict               # 價格/regime/tick數/candle數
    get_current_candle_1m() -> dict    # 正在形成的 1m candle
    get_current_candle_5m() -> dict    # 正在形成的 5m candle
    set_callbacks(on_tick, on_candle_1m, on_candle_5m, on_order)

    # 內部:
    _run_loop()                        # 主循環: 生成訂單 -> 撮合 -> tick -> candle
    _generate_order(regime, bias, vol_mult) -> dict
    _process_tick(tick: Tick)

# Singleton:
get_simulator() -> Optional[MarketSimulator]
create_simulator(base_price, speed, seed) -> MarketSimulator
```

**模擬流程：**
```
RegimeController (盤整/突破/趨勢狀態機)
        |
        v
   生成隨機 buy/sell 訂單 (size: 1-50, bias 受 regime 控制)
        |
        v
   OrderBookSimulator 撮合 -> Tick (price, size, aggressor)
        |
        v
   CandleBuilder 累積 tick
     |           |
     v           v
   1m candle   5m candle  -> 推送前端 + 存入回測數據
```

---

### 1.15 `backend/data/sample_generator.py` -- Static Sample Data

| 項目 | 內容 |
|------|------|
| **狀態** | 已完成 |
| **問題** | 無 |
| **關聯文件** | <- `api/routes.py` / -> `db/models.py` |

**函數結構：**
```python
generate_nq_sample(days=5, interval_minutes=5, base_price=21200.0, seed=42) -> List[Candle]
    # 靜態模擬數據, 用於快速測試 (不需要等即時形成)

_generate_day(date, interval_minutes, base_price, day_idx) -> List[Candle]
_plan_zones(total_bars, base_price, day_trend, day_idx) -> list
```

---

## 二、前端 React/TypeScript 文件

### 2.1 `frontend/src/App.tsx` — 應用入口

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | -> `pages/*`, `components/*` |

**組件結構：**
```tsx
App()
    // React Router 路由配置
    // 側邊導航：回測審查 / 交易審查 / 即時監控
    // WebSocket 全局連接管理（Context Provider）

WebSocketProvider({ children })
    // 管理與後端 WebSocket 的連接
    // 提供 useWebSocket() hook
```

---

### 2.2 `frontend/src/pages/BacktestReview.tsx` — 回測審查頁面

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `App.tsx` / -> `components/CandlestickChart.tsx`, `components/VolumeProfileChart.tsx`, `components/ConsolidationZone.tsx`, `components/TradeTable.tsx` |

**組件結構：**
```tsx
BacktestReview()
    // 主頁面佈局
    // 上半部：回測配置面板 + 執行按鈕
    // 中部：K 線圖 + Volume Profile + 盤整區間 + 交易標記
    // 下半部：績效統計面板 + 交易列表

BacktestConfigPanel({ onRun })
    // 回測參數配置
    // 日期範圍、策略選擇、初始資金、滑價設定

MetricsPanel({ metrics: Metrics })
    // 績效指標卡片
    // 勝率、盈虧比、期望值、最大回撤、Sharpe、Profit Factor

EquityCurveChart({ curve: [timestamp, equity][] })
    // 帳戶淨值曲線圖
```

---

### 2.3 `frontend/src/pages/TradeReview.tsx` — 交易審查頁面

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `App.tsx` / -> `components/TradeTable.tsx`, `components/CandlestickChart.tsx` |

**組件結構：**
```tsx
TradeReview()
    // 交易審查主頁面
    // 篩選器：日期、策略類型、盈/虧
    // 交易列表 + 點擊展開個別交易的 K 線圖

TradeDetailModal({ trade: Trade })
    // 單筆交易詳情彈窗
    // 顯示：入場/出場時的 K 線圖、盤整區間、成交量分析

DailyPnLChart({ dailyPnL: Record<string, number> })
    // 每日盈虧柱狀圖

StrategyBreakdown({ breakdown: Record<string, Metrics> })
    // 策略分類績效對比
```

---

### 2.4 `frontend/src/pages/LiveMonitor.tsx` — 即時監控頁面

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `App.tsx` / -> `components/*`, `api/websocket.py`（WebSocket 連接） |

**組件結構：**
```tsx
LiveMonitor()
    // 即時監控主頁面
    // 即時 K 線圖 + 當前盤整區間 + 持倉狀態
    // 啟動/停止按鈕
    // 風控狀態面板

ControlPanel({ onStart, onStop, status })
    // 交易控制面板
    // 啟動策略 / 停止策略 / 緊急平倉

PositionPanel({ position, riskStatus })
    // 當前持倉顯示
    // 方向、入場價、浮動盈虧、SL/TP 線

RiskStatusPanel({ risk: RiskStatus })
    // 風控狀態
    // 當日盈虧、剩餘虧損額度、距平倉時間、是否冷卻中

AlertLog({ alerts: Alert[] })
    // 即時警報日誌
    // 入場信號、出場信號、風控警告
```

---

### 2.5 `frontend/src/components/CandlestickChart.tsx` — K 線圖組件

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `pages/BacktestReview.tsx`, `pages/TradeReview.tsx`, `pages/LiveMonitor.tsx` |

**組件結構：**
```tsx
CandlestickChart({
    candles: Candle[],
    zones?: ConsolidationZone[],      // 盤整區間疊加
    trades?: Trade[],                 // 交易標記疊加
    volumeProfile?: VolumeProfileResult,  // VP 側邊疊加
    isLive?: boolean                  // 是否即時更新模式
})
    // 基於 lightweight-charts 或 d3.js
    // 主圖：K 線 + 盤整區間色塊 + 交易入場/出場標記
    // 副圖：成交量柱狀圖
    // 側邊：Volume Profile 水平柱狀圖
```

**圖表圖層（由底到頂）：**
1. K 線主圖
2. 盤整區間色塊（半透明矩形）
3. VAH / VAL / POC 水平線
4. 交易入場標記（^ 做多 / v 做空）
5. 交易出場標記（x TP / x SL）
6. SL / TP 水平虛線

---

### 2.6 `frontend/src/components/VolumeProfileChart.tsx` — Volume Profile 圖表

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `pages/BacktestReview.tsx`, `components/CandlestickChart.tsx` |

**組件結構：**
```tsx
VolumeProfileChart({
    profile: Record<number, number>,  // {price: volume}
    poc: number,
    vah: number,
    val: number,
    orientation: "left" | "right"     // 疊加在 K 線圖的哪側
})
    // 水平柱狀圖，Y 軸 = 價格，X 軸 = 成交量
    // POC 行高亮紅色
    // VAH-VAL 範圍內藍色，範圍外灰色
```

---

### 2.7 `frontend/src/components/ConsolidationZone.tsx` — 盤整區間標示

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `components/CandlestickChart.tsx` |

**組件結構：**
```tsx
ConsolidationZoneOverlay({
    zone: ConsolidationZone,
    chartScale: ChartScale          // K 線圖的時間/價格比例尺
})
    // 在 K 線圖上繪製盤整區間
    // 80% 區域：藍色半透明填充
    // 100% 區域：灰色虛線邊框
    // POC：紅色水平線
    // 狀態標籤：forming / active / left
```

---

### 2.8 `frontend/src/components/TradeTable.tsx` — 交易表格

| 項目 | 內容 |
|------|------|
| **狀態** | 空白 |
| **問題** | 無 |
| **關聯文件** | <- `pages/BacktestReview.tsx`, `pages/TradeReview.tsx` |

**組件結構：**
```tsx
TradeTable({
    trades: Trade[],
    onRowClick?: (trade: Trade) => void,
    sortable?: boolean,
    filterable?: boolean
})
    // 交易紀錄表格
    // 欄位：時間、策略、方向、入場價、出場價、SL、TP、盈虧、出場原因
    // 盈利行綠色背景，虧損行紅色背景
    // 支持排序和篩選
```

---

## 三、文件狀態總覽

| # | 文件 | 狀態 | 問題 |
|---|------|------|------|
| 1 | `backend/main.py` | 已完成 | 無 |
| 2 | `backend/api/routes.py` | 已完成 | 無 |
| 3 | `backend/api/websocket.py` | 已完成 | 無 |
| 4 | `backend/strategy/volume_profile.py` | 已完成 | 無 |
| 5 | `backend/strategy/consolidation.py` | 已完成 | 無 |
| 6 | `backend/strategy/reversion.py` | 已完成 | 無 |
| 7 | `backend/strategy/trend_follow.py` | 已完成 | 無 |
| 8 | `backend/backtest/engine.py` | 已完成 | 無 |
| 9 | `backend/backtest/metrics.py` | 已完成 | 無 |
| 10 | `backend/broker/topstepx.py` | 已完成 | 無 |
| 11 | `backend/risk/manager.py` | 已完成 | 無 |
| 12 | `backend/db/models.py` | 已完成 | 無 |
| 13 | `backend/db/database.py` | 空白 | 無 (暫用記憶體存儲) |
| 14 | `backend/data/market_simulator.py` | 已完成 | 無 |
| 15 | `backend/data/sample_generator.py` | 已完成 | 無 |
| 16 | `frontend/static/index.html` | 已完成 | 無 (單頁應用, 含 JS) |
| 17 | `test_api_connection.py` | 已完成 | 無 |

### 前端 React 組件 (規劃中, 未開始)

| # | 文件 | 狀態 | 問題 |
|---|------|------|------|
| 18 | `frontend/src/App.tsx` | 空白 | 無 |
| 19 | `frontend/src/pages/BacktestReview.tsx` | 空白 | 無 |
| 20 | `frontend/src/pages/TradeReview.tsx` | 空白 | 無 |
| 21 | `frontend/src/pages/LiveMonitor.tsx` | 空白 | 無 |
| 22 | `frontend/src/components/CandlestickChart.tsx` | 空白 | 無 |
| 23 | `frontend/src/components/VolumeProfileChart.tsx` | 空白 | 無 |
| 24 | `frontend/src/components/ConsolidationZone.tsx` | 空白 | 無 |
| 25 | `frontend/src/components/TradeTable.tsx` | 空白 | 無 |

---

## 四、UI 設計規範 — GFL2 追放風格

### 4.1 設計參考

視覺風格取自「少女前線2：追放」(Girls' Frontline 2: Exilium) 遊戲 UI。
軍事科技 HUD 感，冷色調底 + 暖色按鈕對比，零圓角，大量大寫等寬字體。

### 4.2 色彩系統

```
-- 背景層級 --
--bg:     #08090d       主背景（最深）
--bg2:    #0d1017       面板/側邊欄背景
--bg3:    #131620       懸停/二級面板
--bg4:    #1a1e2a       三級面板/表頭

-- 邊框 --
--border:   rgba(100, 220, 255, 0.08)    默認邊框（幾乎不可見）
--border-h: rgba(100, 220, 255, 0.20)    懸停/強調邊框

-- 文字 --
--text:   #c8d0e0       主文字
--text2:  #556178       二級文字/標籤
--text3:  #3a4560       三級文字/禁用態
--white:  #eaf0ff       高亮文字

-- 功能色 --
--cyan:   #64dcff       系統主色（HUD 色, 狀態指示, 活躍 tab）
--green:  #00e5a0       盈利/成功/做多
--red:    #ff4060       虧損/錯誤/做空
--amber:  #ffa726       警告/POC 標記/按鈕主色

-- 按鈕專用（橙色系）--
--btn-primary-bg:     rgba(255, 167, 38, 0.12)    按鈕默認背景
--btn-primary-border: #ffa726                       按鈕邊框
--btn-primary-text:   #ffa726                       按鈕文字
--btn-primary-hover:  rgba(255, 167, 38, 0.25)     按鈕懸停
--btn-primary-glow:   rgba(255, 167, 38, 0.15)     按鈕光暈

-- 執行按鈕（綠色系, 僅 EXECUTE BACKTEST 等關鍵操作）--
--btn-exec-bg:     rgba(0, 229, 160, 0.12)
--btn-exec-border: #00e5a0
--btn-exec-text:   #00e5a0
```

### 4.3 字體

```
標題/標籤:  'Rajdhani', sans-serif
  - font-weight: 600-700
  - letter-spacing: 2-3px
  - text-transform: uppercase
  - 用於: panel-title, tab, button, th, metric-label

數據/代碼:  'Share Tech Mono', monospace
  - font-weight: 400
  - letter-spacing: 0-2px
  - 用於: input, td, clock, log, metric-value, zone-data

中文回退:   'Noto Sans TC', sans-serif
  - 用於: 所有中文內容的 fallback

Google Fonts 載入:
  Rajdhani:wght@300;400;500;600;700
  Share+Tech+Mono
  Noto+Sans+TC:wght@300;400;500;700
```

### 4.4 GFL2 風格元素

```
1. 掃描線 (Scanline)
   - body::before pseudo-element
   - repeating-linear-gradient, 2px 間距
   - rgba(100, 220, 255, 0.008) 極淡
   - pointer-events: none, z-index: 9999

2. 零圓角
   - 全域 border-radius: 0
   - 包括 input, select, button, card
   - 唯一例外: 無

3. 面板左側光條
   - panel::before pseudo-element
   - width: 2px, height: 100%
   - linear-gradient(180deg, var(--cyan), transparent)
   - opacity: 0.15

4. Header 底部光線
   - header::after pseudo-element
   - linear-gradient(90deg, transparent, cyan 20%, cyan 80%, transparent)
   - opacity: 0.3

5. 菱形狀態燈
   - 6x6px 方形, transform: rotate(45deg)
   - 帶 box-shadow 光暈
   - ok=green, err=red, loading=amber+pulse

6. Tab 前綴
   - 活躍 tab 用 '//'' 前綴 (tab.active::before)
   - 底部 2px 色條

7. Panel Title 前綴
   - '>' 字符 (panel-title::before)
   - Share Tech Mono 字體
   - opacity: 0.4

8. 按鈕光掃效果
   - btn::after pseudo-element
   - linear-gradient 從左到右白色半透明
   - hover 時 left: -100% -> 100% 過渡

9. 方形 Checkbox
   - appearance: none
   - 14x14px, 方形
   - checked 時 cyan 邊框 + 8x8px cyan 實心填充

10. Metric 卡片左側邊條
    - 3px 寬彩色邊條
    - 根據 .value.pos/.neg 自動變色 (CSS :has 選擇器)
    - 正值=green, 負值=red, 中性=text3

11. 極細滾動條
    - 寬度: 2px
    - thumb: var(--border-h)
    - 適用: sidebar, table-wrap, log

12. 全大寫標籤
    - text-transform: uppercase
    - 適用: panel-title, tab, th, label, button
    - 搭配 letter-spacing: 1-3px
```

### 4.5 按鈕層級

```
Level 1 -- 主要操作按鈕（橙色）
  CONNECT, 一般操作
  背景: rgba(255, 167, 38, 0.12)
  邊框/文字: #ffa726
  hover: rgba(255, 167, 38, 0.25) + box-shadow

Level 2 -- 執行按鈕（綠色, 僅關鍵操作）
  EXECUTE BACKTEST, START LIVE
  背景: rgba(0, 229, 160, 0.12)
  邊框/文字: #00e5a0
  hover: rgba(0, 229, 160, 0.25) + box-shadow

Level 3 -- 危險操作（紅色）
  STOP, FLATTEN ALL
  背景: rgba(255, 64, 96, 0.12)
  邊框/文字: #ff4060

Level 4 -- 次要/輪廓按鈕
  背景: transparent
  邊框: var(--border)
  文字: var(--text2)
```

### 4.6 K 線圖配色

```
chart background:  #08090d
grid lines:        rgba(100, 220, 255, 0.03)
crosshair:         rgba(100, 220, 255, 0.20)
scale border:      rgba(100, 220, 255, 0.08)
font:              Share Tech Mono, 11px

candle up:     #00e5a0 (green)
candle down:   #ff4060 (red)
volume:        同 candle 方向色
trade marker:  win=#00e5a0, loss=#ff4060

zone overlay:
  80% area:  半透明 cyan 填充
  POC line:  amber #ffa726
  VAH/VAL:   cyan #64dcff
  100%:      text2 #556178 虛線
```

### 4.7 響應式規則

```
> 1200px:  sidebar 320px
< 1200px:  sidebar 280px
未來考慮:  < 900px 隱藏 sidebar, 改為抽屜式
```

---

## 五、開發優先級（建議順序）

```
Phase 1 — 核心計算（無 UI 依賴）
  1. models.py        -> 所有資料模型定義
  2. volume_profile.py -> VP 計算（其他模組的基礎）
  3. consolidation.py  -> 盤整區間偵測
  4. reversion.py      -> 策略一
  5. trend_follow.py   -> 策略二
  6. metrics.py        -> 績效計算

Phase 2 — 回測驗證
  7. database.py       -> 數據存儲
  8. engine.py         -> 回測引擎
  -> 此時可用 CLI 跑回測驗證策略

Phase 3 — Web 可視化
  9. main.py + routes.py -> API 層
  10. App.tsx + 前端組件  -> 可視化

Phase 4 — 即時交易
  11. topstepx.py       -> API 封裝
  12. manager.py        -> 風控
  13. websocket.py      -> 即時推送
  14. LiveMonitor.tsx   -> 即時監控頁面
```
