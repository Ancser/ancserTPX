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
- [ ] 建立 Python 後端專案結構
- [ ] 實作 Volume Profile 計算模組
- [ ] 實作盤整區間偵測演算法
- [ ] 建立回測引擎（讀取歷史 CSV/API 數據）
- [ ] 計算策略一（均值回歸）回測結果
- [ ] 計算策略二（趨勢跟隨）回測結果

### Phase 2：前端可視化
- [ ] 建立 React 前端專案
- [ ] K 線圖 + Volume Profile 疊加
- [ ] 盤整區間框選可視化
- [ ] 交易進出場標記
- [ ] 績效統計面板

### Phase 3：即時交易
- [ ] TopstepX API 封裝（REST + WebSocket）
- [ ] Practice Account 連接測試
- [ ] 即時數據串流 -> 策略引擎
- [ ] 自動下單模組
- [ ] 風控模組（止損、日內平倉、帳戶限制）

### Phase 4：優化與生產
- [ ] 策略參數優化（回測結果驅動）
- [ ] 多時間框架確認（15 分鐘輔助判斷）
- [ ] 交易日誌 + 統計報告自動生成
- [ ] Practice Account 實測驗證
- [ ] Funded Account 上線

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
