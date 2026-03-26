# ancserTPX — TopstepX NQ 期貨自動交易系統 計劃書

## 專案概述

基於 **Market Profile / Volume Profile** 概念，開發一套本地網頁應用，用於：
1. **回測審查** — 歷史數據視覺化，驗證策略可行性
2. **訂單進出場審查** — 每筆交易的盈虧追蹤
3. **自動化交易** — 透過 TopstepX API 執行策略（1 口 NQ，5 分鐘 K 線）

---

## 一、TopstepX 自動交易可行性研究

### 1.1 TopstepX 是否允許 Bot？

**允許。** TopstepX 明確允許自動化策略（Trading Combine 和 Funded Account 皆可）。

| 項目 | 說明 |
|------|------|
| **API 費用** | $29/月（折扣碼 "topstep" -> $14.50/月） |
| **協議** | REST + WebSocket |
| **支援語言** | Python, Java, .NET, JavaScript |
| **數據源** | TopstepX 自有（非 Rithmic / CQG） |
| **即時數據** | Level 1 & Level 2（WebSocket） |
| **歷史數據** | REST API |
| **測試環境** | 無沙盒，使用 Practice Account 測試 |

### 1.2 關鍵限制

| 限制 | 影響 |
|------|------|
| **禁止 VPS/VPN** | Bot 必須在個人電腦上運行 |
| **日內交易** | 每日 3:10 PM CT 前必須平倉 |
| **禁止 HFT** | 不可利用模擬撮合的高頻策略 |
| **無技術支援** | TopstepX 不提供 Bot 相關技術協助 |
| **訂單不可撤銷** | API 下單視為最終指令 |

### 1.3 可行性結論

[OK] **完全可行。** 本策略為中頻（5 分鐘 K 線）、單口 NQ、日內交易，完全符合 TopstepX 規則。

---

## 二、交易策略定義

### 2.1 核心概念：盤整區間 -> 離開 -> 新盤整區間

策略基於 **Volume Profile** 的正態分布概念：

```
                    ┌─ 100% High ─┐
                    │              │
                    ├─  80% VAH  ─┤  <- Value Area High
                    │              │
                    │    POC *     │  <- Point of Control（最大成交量價位）
                    │              │
                    ├─  80% VAL  ─┤  <- Value Area Low
                    │              │
                    └─ 100% Low  ─┘
```

### 2.2 策略一：均值回歸（Reversion to POC）

**觸發條件：**
1. 價格離開舊的正態分布區間
2. 等待新的盤整區間形成（新的 Volume Profile 分布）
3. 在新區間的 **80% 邊界**（VAH / VAL）入場
4. 目標回到新區間的 **POC**

**風控參數：**
| 參數 | 值 |
|------|-----|
| 合約數 | 1 NQ |
| K 線週期 | 5 分鐘 |
| 止損 (SL) | $300（15 點 NQ） |
| 止盈 (TP) | $600（30 點 NQ） |
| 風報比 | 1:2 |

**入場邏輯（偽碼）：**
```
IF 價格到達新盤整區間 VAH:
    SELL 1 NQ @ VAH
    SL = VAH + 15 pts ($300)
    TP = POC ($600)

IF 價格到達新盤整區間 VAL:
    BUY 1 NQ @ VAL
    SL = VAL - 15 pts ($300)
    TP = POC ($600)
```

### 2.3 策略二：趨勢跟隨（Trend Follow）

**觸發條件：**
1. 價格在 80% 邊界外（Outside VAH / VAL）
2. 監控成交量：**外部成交量 > 離開 80% 前最後 2 根 K 線的成交量**
3. 確認突破有效 -> 趨勢跟隨

**風控參數：**
| 參數 | 值 |
|------|-----|
| 止損 (SL) | POC（約 $300） |
| 止盈 (TP) | 2x 當前新 80%-100% range |
| 基本風報比 | 1:2（$300:$600） |
| 大趨勢風報比 | 1:3（$600:$1800） |

**入場邏輯（偽碼）：**
```
IF 價格 > VAH:
    vol_outside = 最近 K 線成交量
    vol_before  = 離開 VAH 前 2 根 K 線的平均成交量

    IF vol_outside > vol_before:
        BUY 1 NQ（趨勢跟隨）
        SL = POC
        TP = VAH + 2 x (100%_High - VAH)

        IF 判定為大趨勢模式:
            TP 可延伸至 $1800（風報比 1:3）
```

### 2.4 盤整區間偵測演算法

**定義「新盤整區間形成」：**
1. 收集最近 N 根 K 線的價格 + 成交量
2. 計算 Volume Profile -> 找出 POC
3. 以 POC 為中心，累計 70%（或 80%）成交量 -> 確定 VAH / VAL
4. 判定盤整：**價格在 VAH-VAL 之間來回至少 M 次**
5. 穩定性檢查：POC 偏移量 < 閾值

**數據化框架：**
```python
class ConsolidationZone:
    poc: float          # Point of Control 價位
    vah: float          # Value Area High (80%)
    val: float          # Value Area Low (80%)
    high_100: float     # 100% 區間高點
    low_100: float      # 100% 區間低點
    volume_profile: dict  # {price_level: volume}
    formed_at: datetime
    status: str         # 'forming' | 'active' | 'left'
```

---

## 三、系統架構

### 3.1 技術棧

```
┌──────────────────────────────────────────┐
│           本地網頁前端 (React)             │
│  ┌──────────┬──────────┬───────────────┐  │
│  │ 回測審查  │ 交易審查  │  即時監控      │  │
│  │ Backtest │ Trade    │  Live         │  │
│  │ Review   │ Review   │  Monitor      │  │
│  └──────────┴──────────┴───────────────┘  │
└─────────────────┬────────────────────────┘
                  │ HTTP / WebSocket
┌─────────────────┴────────────────────────┐
│           後端 API (Python FastAPI)        │
│  ┌──────────┬──────────┬───────────────┐  │
│  │ 策略引擎  │ 回測引擎  │  風控模組      │  │
│  │ Strategy │ Backtest │  Risk Mgmt    │  │
│  └──────────┴──────────┴───────────────┘  │
└─────────────────┬────────────────────────┘
                  │
┌─────────────────┴────────────────────────┐
│         TopstepX API (REST/WS)            │
│  即時數據 │ 歷史數據 │ 訂單執行             │
└──────────────────────────────────────────┘
                  │
┌─────────────────┴────────────────────────┐
│          本地資料庫 (SQLite)               │
│  K線數據 │ 交易紀錄 │ 盤整區間 │ 回測結果    │
└──────────────────────────────────────────┘
```

### 3.2 目錄結構（規劃）

```
ancserTPX/
├── PLAN.md                  # 本文件
├── backend/
│   ├── main.py              # FastAPI 入口
│   ├── api/
│   │   ├── routes.py        # API 路由
│   │   └── websocket.py     # WebSocket 即時推送
│   ├── strategy/
│   │   ├── volume_profile.py    # Volume Profile 計算
│   │   ├── consolidation.py     # 盤整區間偵測
│   │   ├── reversion.py         # 策略一：均值回歸
│   │   └── trend_follow.py      # 策略二：趨勢跟隨
│   ├── backtest/
│   │   ├── engine.py            # 回測引擎
│   │   └── metrics.py          # 績效指標計算
│   ├── broker/
│   │   └── topstepx.py         # TopstepX API 封裝
│   ├── risk/
│   │   └── manager.py          # 風控管理
│   └── db/
│       ├── models.py           # 資料模型
│       └── database.py         # SQLite 連接
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── BacktestReview.tsx   # 回測審查頁面
│   │   │   ├── TradeReview.tsx      # 交易審查頁面
│   │   │   └── LiveMonitor.tsx      # 即時監控頁面
│   │   ├── components/
│   │   │   ├── VolumeProfileChart.tsx   # Volume Profile 圖表
│   │   │   ├── CandlestickChart.tsx     # K 線圖
│   │   │   ├── ConsolidationZone.tsx    # 盤整區間標示
│   │   │   └── TradeTable.tsx           # 交易表格
│   │   └── App.tsx
│   └── package.json
└── data/
    └── historical/              # 歷史數據存放
```

---

## 四、可視化設計

### 4.1 回測審查頁面

| 區塊 | 內容 |
|------|------|
| **主圖表** | 5 分鐘 K 線 + Volume Profile 側邊疊加 |
| **盤整區間** | 半透明色塊框出 VAH/VAL/POC，標記 80% 和 100% 邊界 |
| **交易標記** | 入場（^/v）、出場（x）、SL 線、TP 線 |
| **績效面板** | 勝率、盈虧比、最大回撤、Sharpe Ratio |
| **交易列表** | 每筆交易細節（時間、方向、入場價、出場價、盈虧） |

### 4.2 盤整區間可視化

```
價格軸
  │
  │  ┄┄┄┄┄ 100% High ┄┄┄┄┄          <- 虛線
  │  ━━━━━  80% VAH  ━━━━━          <- 粗線（入場位）
  │  ░░░░░░░░░░░░░░░░░░░░░          <- 淺色填充
  │  *****   POC    *****          <- 紅色粗線（目標位）
  │  ░░░░░░░░░░░░░░░░░░░░░
  │  ━━━━━  80% VAL  ━━━━━          <- 粗線（入場位）
  │  ┄┄┄┄┄ 100% Low  ┄┄┄┄┄          <- 虛線
  │
  └──────────────────────── 時間軸
```

### 4.3 離開區域 + 趨勢判定可視化

```
價格軸
  │         成交量放大 ^
  │              █ █
  │         █ █  █ █  <- vol_outside > vol_before -> 趨勢確認
  │    ┃ ┃  █ █  █ █
  │ ━━━VAH━━━━━━━━━━━
  │    ┃ ┃
  │    POC
  │
  └──────────────────── 時間軸
      ^ 離開前2根K線    ^ 外部K線
      (vol_before)     (vol_outside)
```

---

## 五、數據化分析框架

### 5.1 盤整區間數據化

每個盤整區間記錄以下數據：

```python
{
    "zone_id": "Z001",
    "formed_at": "2026-03-21 09:35:00",
    "left_at": "2026-03-21 10:15:00",
    "poc": 21450.50,
    "vah_80": 21465.00,     # 80% 上邊界
    "val_80": 21436.00,     # 80% 下邊界
    "high_100": 21478.00,   # 100% 上邊界
    "low_100": 21423.00,    # 100% 下邊界
    "total_volume": 15420,
    "duration_minutes": 40,
    "num_candles": 8,
    "status": "left",       # forming / active / left
    "exit_direction": "up"  # up / down
}
```

### 5.2 離開區域分析

```python
{
    "breakout_id": "B001",
    "from_zone": "Z001",
    "direction": "up",
    "breakout_candle_time": "2026-03-21 10:15:00",
    "vol_before_2candles_avg": 1200,   # 離開前 2 根 K 線平均量
    "vol_outside_current": 1850,       # 外部當前 K 線成交量
    "vol_ratio": 1.54,                 # 1850/1200
    "is_trend_signal": True,           # vol_ratio > 1.0
    "trade_type": "trend_follow",      # reversion / trend_follow
    "entry_price": 21466.00,
    "sl_price": 21450.50,              # POC
    "tp_price": 21492.00,              # VAH + 2x(100%_High - VAH)
    "result": "win",
    "pnl": 520.00
}
```

### 5.3 績效指標

| 指標 | 計算方式 |
|------|---------|
| 勝率 | wins / total_trades |
| 平均盈虧比 | avg_win / avg_loss |
| 期望值 | (勝率 x 平均獲利) - (敗率 x 平均虧損) |
| 最大回撤 | 帳戶淨值峰值到谷底的最大跌幅 |
| Sharpe Ratio | (平均報酬 - 無風險利率) / 報酬標準差 |
| Profit Factor | 總獲利 / 總虧損 |
| 每日最大虧損 | TopstepX 帳戶規則限制 |

---

## 六、開發階段

### Phase 1：數據基礎 + 回測引擎
- [x] 建立 Python 後端專案結構 (FastAPI)
- [x] 實作 Volume Profile 計算模組 (`backend/strategy/volume_profile.py`)
- [x] 實作盤整區間偵測演算法 (`backend/strategy/consolidation.py`)
- [x] 建立回測引擎 (`backend/backtest/engine.py`)
- [x] 計算策略一（均值回歸）回測結果 (`backend/strategy/reversion.py`)
- [x] 計算策略二（趨勢跟隨）回測結果 (`backend/strategy/trend_follow.py`)

### Phase 2：前端可視化
- [x] 建立前端 (Pure HTML + lightweight-charts v4.1.3, 少女前線風格 UI)
- [x] K 線圖 + Volume Profile canvas overlay 疊加
- [x] 盤整區間框選可視化 (VP histogram, POC/VAH/VAL lines)
- [x] 交易進出場標記 (chart markers + position tool overlay)
- [x] 績效統計面板 (含 per-strategy breakdown: 1m/5m/reversion/trend)
- [x] VP overlay 隨 scroll/zoom 即時跟隨 (rAF)
- [x] Left zone: POC 在 left_at 停止延伸, 極淡顯示, 無標籤

### Phase 3：即時交易
- [x] TopstepX API 封裝 (`backend/broker/topstepx.py`)
- [x] Practice Account 連接 + 帳戶列表
- [x] 即時 K 線每 30 秒 polling (`/data/latest-candles`)
- [x] Live engine 主循環 (`backend/live/engine.py`)
- [x] 自動下單模組 (limit order + SL/TP stop order)
- [x] 風控模組 (日內平倉 CT 15:05, 每日交易上限)
- [ ] ⚠️ **即時交易未驗證** — "僅監控" 模式原因不明
- [ ] ⚠️ **SL/TP order 互相取消邏輯未完成**

### Phase 4：~~多時間框架 (MTF) 策略~~ — 已移除 (2026-03-26)
> MTF 模式已完全移除。原因：warm-up 歷史數據導致 stale zone/state，造成錯誤下單。
> 專注於已驗證的原始策略 (Reversion + Trend Follow, 5m candles)。
> 文件 `backend/strategy/mtf_strategy.py` 保留但不再 import。

### Phase 5：優化與生產
- [ ] 策略參數優化（回測結果驅動）
- [ ] Walk-forward validation
- [ ] Practice Account 實測驗證
- [ ] Funded Account 上線

---

## 九、已解決的 UI/前端 問題

| # | 問題 | 解決方案 | 狀態 |
|---|------|---------|------|
| 1 | `var(--surface)` 未定義 → 白底 | 改為 `var(--bg)` | ✅ |
| 2 | Live candle 不更新 | 新增 `/data/latest-candles` endpoint, incremental polling | ✅ |
| 3 | GO LIVE 失敗 → 全部停止 | 改為 polling 先啟動, 引擎失敗 → 僅監控模式 | ✅ |
| 4 | VP overlay 不隨 scroll/zoom | rAF + `subscribeVisibleLogicalRangeChange` + mousemove | ✅ |
| 5 | POC 線全部重疊 | Left zone: 在 left_at 停止, 極淡, 無標籤/histogram | ✅ |
| 6 | VP overlay 只在停止拖拽後更新 | 改用 rAF (持續) 取代 debounce | ✅ |
| 7 | GFL UI 按鈕風格不一致 | GO LIVE/STOP/FLATTEN 加上 glow 效果 | ✅ |
| 8 | 1min SL default $100 | 改為 $300 (models + strategy + frontend) | ✅ |
| 9 | ~~Live 和 Backtest 共用 MTF toggle~~ | ~~分離為獨立 toggle + 參數~~ | ❌ MTF 已移除 |
| 10 | 回測無 per-strategy 勝率 | 新增 reversion/trend_follow breakdown | ✅ |
| 11 | 市場時段標籤中文混亂 | 改為 PRE/NORMAL/AFTER/CLOSED | ✅ |
| 12 | Top bar 缺少 PHASE | 新增 PHASE 到 top bar + left panel | ✅ |

---

## 十、🔴 即時交易 — 嚴重問題 (2026-03-25 實測發現)

### 10.1 實測事件記錄

2026-03-25 晚間，使用者在 Practice 帳戶 PRAC-V2-556574-32302360 上點擊 GO LIVE (當時使用 MTF mode，已移除)。

**TopstepX 上的真實訂單 (從 TopstepX 平台觀察到):**
```
#2708307397  2026-03-25 22:45:23  SELL 1 /NQ  Limit  23,930.25  → Filled @ 24,272.50
#2708307400  2026-03-25 22:45:23  BUY  1 /NQ  Stop Market  @ 24,287.50  [Open] ← SL
#2708307401  2026-03-25 22:45:23  BUY  1 /NQ  Limit  @ 24,227.50  [Open] ← TP
#2708422926  2026-03-25 23:14:55  SELL 1 /NQ  Limit  24,085.50  → Filled @ 24,245.25
```

**Web app 顯示 (完全不同):**
```
▼ LIMIT SELL  ENTRY: 23930.25  SL: 23915.25  TP: 23990.25  PENDING...
```

**問題:**
- Web 上的 signal 價格 (23930) 與 TopstepX 實際成交價 (24272) 差距 300+ 點
- Engine 發出了一個 SELL limit @ 23930.25, 但因為當時市場價 ~24272, limit sell 在遠低於市場價的位置 → **被 TopstepX 以市場價立即成交** (limit sell 低於市場價 = 馬上成交)
- SL 24287.50 和 TP 24227.50 是 **TopstepX 平台的 Position Bracket (Auto Follow-up SL/TP)**, 不是 engine 下的
- Engine 認為自己還在 "掛單中", 實際上已經成交並被平台的 bracket order 管理

### 10.2 根本原因分析

| # | 問題 | 根因 | 嚴重度 |
|---|------|------|--------|
| **A** | **Engine 用舊 POC 計算入場價** | warm-up 只用 CONNECT 時拉的歷史數據 (3/18-3/19), 不包含 3/25 當天數據, 所以 zone/POC 是一週前的值 | 🔴 致命 |
| **B** | **Limit Sell 低於市場價 = 立即成交** | Engine 基於舊 zone 算出 entry=23930, 但市價=24272, limit sell 在市價下方 → 交易所視為 marketable order 立即填單 | 🔴 致命 |
| **C** | **SL/TP 不是 engine 下的** | TopstepX 平台有 "Position Bracket" 功能自動掛 SL/TP, engine 自己下的 SL/TP orders 可能重複或衝突 | 🔴 高 |
| **D** | **Engine 不知道已成交** | `_sync_position` 可能沒正確偵測到新持倉, 或偵測到了但 `_pending_signal` 已被清空 | 🔴 高 |
| **E** | **圖表只顯示 1 個舊 POC 區間** | Zone detection 基於 warm-up 的歷史數據, 3/25 當天新形成的 zone 不在 warm-up 範圍內 | 🔴 高 |
| **F** | **TPX Real State 讀不到訂單** | `GET /live/account-state` 回傳 NO ORDERS/NO POSITIONS — 可能是 Order/search API 回傳格式不同, 或訂單已平倉 | 🟡 中 |
| **G** | ~~**MTF 混合時間框架造成圖表混亂**~~ | ~~1m candle 和 5m candle 的 zone 重疊顯示~~ | ✅ MTF 已移除 |

### 10.3 問題 A 深入: 為什麼 Engine 用舊數據?

```
用戶操作流程:
  1. CONNECT (拉取 3/18-3/25 的 K線) → _historical_candles = [3/18 ... 3/25]
  2. GO LIVE → live_engine.start(_historical_candles)
     → warm-up: 遍歷所有 historical candles, 跑 detector
     → 但! 如果數據跨多天, detector 偵測到的 zone 是舊的
     → 到最新數據時, zone 可能:
        a) 正在 "forming" 一個新 zone (但還沒 "active")
        b) 之前的 zone 已經 "left", 新 zone 還沒形成
  3. Engine 開始 tick → 只拉最新 1 根 candle
     → 如果 signal 基於舊 zone → entry price 完全錯誤
```

**結論:** Engine 的 entry price 23930.25 ≈ 3/18 zone 的 VAL 附近, 不是 3/25 當天的值。

### 10.4 問題 B 深入: Limit Order 行為

```
Engine 下單: SELL LIMIT @ 23930.25
市場當時價: ~24272

TopstepX 行為:
  - Limit Sell = "願意以 23930.25 或更高價格賣出"
  - 當市場價 24272 > 23930.25 → 立即以市場價 24272.50 成交
  - 這不是 bug, 是 limit order 的正確行為
  - 但 Engine 不知道自己已經成交了, 因為 entry price ≠ fill price
```

### 10.5 問題 F 深入: TPX Real State 讀不到數據

可能原因:
1. `POST /api/Order/search` 回傳格式可能是 `{"data": [...]}` 而不是 `{"orders": [...]}`
2. 訂單可能在查詢時已經被 SL/TP 平倉, 變成 "Filled" 但仍應顯示
3. Position 在查詢時可能已平 (SL triggered), 所以 NO POSITIONS 是正確的
4. 需要檢查 API raw response

---

## 十一、待修復的問題 (按優先級排序)

### 🔴 P0 — 致命 (可能造成真實虧損)

| # | 問題 | 描述 | 相關文件 |
|---|------|------|---------|
| 1 | **Entry price 基於舊 zone** | warm-up 用 CONNECT 歷史數據, 如果數據跨多天, zone 是舊的, entry price 完全錯誤 | `live/engine.py` |
| 2 | **Limit order 即時成交** | 當 limit sell price << 市場價, 交易所立即成交, engine 不知道 fill price, SL/TP 計算全部錯誤 | `live/engine.py` L525-555 |
| 3 | **Engine 不追蹤 fill price** | `_check_pending_fill()` 只看有沒有 position, 不比較 fill price vs signal entry price, 無法計算真實 PnL | `live/engine.py` L579-595 |
| 4 | **SL/TP vs Platform Bracket 衝突** | Engine 下 SL/TP orders, 但 TopstepX 平台 "Position Bracket" 也自動掛 SL/TP → 重複 orders, 互相干擾 | `live/engine.py` L597-642 |

### 🔴 P1 — 高 (功能不正確)

| # | 問題 | 描述 | 相關文件 |
|---|------|------|---------|
| 5 | **圖表只顯示舊 POC** | Zone detection 來自 warm-up 歷史數據, live mode 下新 candle 不觸發新 zone detection | `live/engine.py` |
| 6 | ~~**MTF 回測 0 筆交易**~~ | ~~signal propagation 邏輯有問題~~ | ✅ MTF 已移除 |
| 7 | ~~**1m zone detection = 0**~~ | ~~INSIDE_BIG 狀態下 1m detector 偵測不到 zone~~ | ✅ MTF 已移除 |
| 8 | **TPX Real State 讀不到訂單** | `get_orders()` API 回傳格式可能不對, 需要檢查 raw response | `broker/topstepx.py`, `api/routes.py` |
| 9 | **僅監控 vs 交易中 狀態不穩定** | 之前顯示"交易中"後變成"僅監控", 可能是 engine 啟動後 crash 但沒 catch | `live/engine.py`, `api/routes.py` |

### 🟡 P2 — 中 (需改善)

| # | 問題 | 描述 | 相關文件 |
|---|------|------|---------|
| 10 | **5m zone formed_at 不準** | zone 起點早於實際橫盤開始 | `consolidation.py` |
| 11 | **SL/TP 互相取消邏輯缺失** | SL 觸發後 TP order 仍掛著, 反之亦然 | `live/engine.py` L644-689 |
| 12 | **daily_pnl 追蹤不準** | initial capital 只在 start 設定, 不隨天更新 | `live/engine.py` L678-685 |
| 13 | ~~**MTF 時間框架混合顯示混亂**~~ | ~~1m 和 5m zone 重疊~~ | ✅ MTF 已移除 |

---

## 十二、修復計劃 (建議順序)

### Step 1: 安全優先 — 防止錯誤下單 ✅ (2026-03-25 初修, 2026-03-26 加強)

**3/26 根本原因: warm-up 結束後策略處於 BIG_BREAKOUT 狀態, 第一個 live tick 立刻基於舊 zone 下單**

- [x] **下單前驗證 entry price vs 當前市價**: `_place_order()` PRICE_SAFETY_MARGIN=50pts 攔截
- [x] **無市價時拒絕下單**: `_last_market_price=None` → 直接 return (不只是警告)
- [x] **warm-up 後強制重置 breakout 狀態**: `reset_to_safe_state()` — BIG_BREAKOUT/IN_5M_TRADE/IN_1M_TRADE → WAITING_BIG 或 INSIDE_BIG
- [x] **zone 過期警告**: warm-up 結束後如 big_zone age > 12h → error log
- [x] **追蹤 fill price**: `_check_pending_fill()` 比較 entry vs fill, 差距>5pts 報警
- [x] **Engine 不下 SL/TP**: `skip_engine_sl_tp=True`, 由 TopstepX Position Bracket 300:900 管理

### Step 2: Zone Detection 修復
- [ ] **live mode 下持續 zone detection**: 每次拉新 candle 都要 feed detector, 不只是 warm-up
- [ ] **warm-up 只用最近 N 天數據**: 如果拉了一週, 只用最後 1-2 天做 warm-up, 避免舊 zone 主導
- [ ] **前端顯示 zone 時間**: 讓使用者看到 zone 是什麼時候形成的

### ~~Step 3: MTF 策略修復~~ — 已移除 (2026-03-26)
> MTF 模式已從整個 codebase 移除，此步驟不再需要。

### Step 4: 帳戶狀態同步 (部分完成)
- [x] **修復 get_orders() API 格式**: 加入 raw response logging (type, keys, preview), 支援兩種格式
- [x] **get_positions() logging**: 加入 side, avgPrice, size 詳細 log
- [ ] **定期同步真實 position vs engine 狀態**: 如果兩邊不一致 → 警告 + 停止交易
- [ ] **加入 trade history 查詢**: 讓使用者看到所有歷史成交

---

## 七、風險與注意事項

| 風險 | 應對 |
|------|------|
| TopstepX API 斷線 | 斷線自動平倉 + 重連機制 |
| 策略過擬合 | Walk-forward 分析、樣本外測試 |
| 滑價 | 回測加入滑價模擬（1-2 tick） |
| 每日虧損限制 | 硬編碼最大日虧損，觸發即停止交易 |
| 3:10 PM CT 平倉 | 定時器強制平倉（提前至 3:05 PM） |
| 禁止 VPS | Bot 運行在本地電腦，需確保穩定網路 |

---

## 八、核心公式摘要

```
策略一（均值回歸）:
  入場 = VAH 或 VAL（80% 邊界）
  SL = 入場 ± 15 pts ($300)
  TP = POC ($600)
  風報比 = 1:2

策略二（趨勢跟隨）:
  條件 = vol_outside > avg(vol_before_2_candles)
  入場 = 突破方向
  SL = POC (~$300)
  TP = 2 x (100% edge - 80% edge) from entry
  基本風報比 = 1:2 ($300:$600)
  大趨勢風報比 = 1:3 ($600:$1800)
```
