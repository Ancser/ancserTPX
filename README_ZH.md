# ancserTPX

繁體中文｜[English](README.md)

ancserTPX 是一套運行於 **TopstepX（ProjectX API）** 的 NQ（Nasdaq 100 E-mini）期貨自動交易系統。

系統以 **1 分鐘 K 線** 為核心，支援回測與即時監控 / 交易。

---

## 策略選項

Web 回測與 Web LIVE 提供以下策略選項：

- **TREND**
- **DAY ZONE**（`LIMIT`、`REJECTION`、`OR15`）
- **DISTRIBUTION**
- **PMO**
- **FACTOR**（`EMAPMO`、`KDJMA`、`MREV`）

Preset 選單包含 **Default** 與使用者儲存的 presets。Terminal-only LIVE 沒有獨立策略選單，會使用已儲存或已指定的 LIVE preset。

README 不公開策略規則、公式、門檻、SL/TP 內部邏輯與 preset 參數。

---

## 架構

### 專案結構

```
ancserTPX/
├── backend/
│   ├── api/routes.py              # FastAPI 路由 — 所有端點
│   ├── backtest/
│   │   ├── confluence_backtest.py  # 匯流回測器 + zone timeline 建構
│   │   ├── confluence_worker.py    # 獨立子進程回測運行器（自己的 GIL）
│   │   ├── engine.py              # Trend 回測引擎
│   │   └── metrics.py             # 共用績效計算器
│   ├── broker/topstepx.py         # TopstepX API 客戶端（認證、K 線、下單、websocket）
│   ├── data/
│   │   └── candle_store.py        # 持久 K 線累積器 + 缺 K 檢測
│   ├── db/models.py               # Candle、Trade、合約規格
│   ├── live/engine.py             # 即時交易引擎
│   └── strategy/
│       ├── confluence.py           # 多時間框加權匯流訊號引擎
│       ├── confluence_scorer.py    # 邏輯回歸評分器（可解釋 ML）
│       ├── consolidation.py        # ClockBucketZoneDetector、五時段架構
│       ├── exit_policy.py          # 移動停損觸發 / 鎖利 / Full-TP-Lock
│       └── trend_follow.py         # Trend 策略
├── frontend/static/
│   ├── ancserTPX.html             # 單頁應用（回測 + 即時兩個分頁）
│   ├── ancserTPX.css              # 暗色主題 + 可重用 CSS 類別
│   └── ancserTPX.js               # UI 邏輯、圖表、preset、LATEST 模型
├── data/
│   ├── store/                     # 持久 1m K 線累積器（.pkl）
│   ├── models/confluence_scorer.json # 唯一 LATEST production 模型
│   └── presets.json               # 使用者 preset
└── scripts/
    ├── accumulate_history.py      # CLI：擴充持久 K 線庫
    └── train_confluence.py        # CLI：重訓唯一 LATEST 模型
```

### 回測進程模型

Web 回測在**獨立子進程**（`ProcessPoolExecutor`）中運行，擁有自己的 GIL，因此運算期間 Web 伺服器保持完全回應（fetch 資料 / live / 圖表都不會卡住）。子進程內部快取 K 線集和 zone timeline——同一份資料用不同模型 / RR / band / trail 參數重跑時，會跳過最慢的偵測器建構步驟。

### 資料持久化

歷史 1 分鐘 K 線持久化到 `data/store/MNQ_accumulated_1m.pkl` —— 一個只增不減的累積庫。fetch 時：
1. 從本地庫載入（瞬間，無 API 呼叫）
2. 增量 API 抓取（只拉最後一根 bar 之後的尾巴）
3. 合併 + 自動缺 K 檢測 + 補回 wifi 斷線遺失的 bar
4. 存回庫

庫在伺服器重啟後依然存在，後續啟動只需幾百根增量 bar，而不是整段 60 天重新下載。

### ML production 模型

系統只保留一個可解釋邏輯回歸模型：`data/models/confluence_scorer.json`。重訓會直接取代這個 LATEST canonical 模型，避免誤選舊模型，並維持 live == 回測 == 訓練。

### 市場時段

| 事件 | 美東 ET | UTC | 加州 PT（夏令） |
|---|---|---|---|
| 新交易日（亞盤開始）| 6:00 PM | 22:00 | 3:00 PM |
| 每日維護 gap | 5:00–6:00 PM | 22:00–23:00 | 3:00–4:00 PM |
| RTH 開盤 | 9:30 AM | 13:30 | 6:30 AM |
| Bot 平倉 | 3:45 PM | 19:45 | 12:45 PM |
| AH 結束 | ~5:59 PM | ~21:59 | ~2:59 PM |

---

## 安裝前準備

### 1. ProjectX API 設定

1. 前往 https://dashboard.projectx.com/dashboard 並開通 ProjectX API Access
2. 綁定 TopstepX 帳戶
3. 前往 https://topstepx.com/settings
4. 進入 **API** 頁面
5. 複製 API Key

### 2. 帳號設定

在網頁右上角 **CONNECT** 面板輸入 **郵箱** 和 **API Key**，首次連線成功後會自動保存到 `.env`，無需手動編輯檔案。

### 3. TopstepX Auto OCO Preset

即時交易需要啟用 TopstepX **Auto OCO Brackets**。Bot 會在每一張 API 入場單裡送 `stopLossBracket` / `takeProfitBracket`；只在 preset 畫面打勾，裸 API 單不會自動附上 SL/TP。

建議在 TopstepX 這樣設定：

- 開啟 [TopstepX Risk Settings](https://topstepx.com/settings?tab=risk-settings)
- 啟用 **Auto OCO Brackets**
- 建立一個給本 bot 使用的 preset
- **Stop Loss Order Type**：`Stop Market`
- **Take Profit Order Type**：`Limit`
- preset 的 ticks 只是備用/預設值；Bot 會在 API 入場單裡送策略計算出的 SL/TP ticks
- 不要把 preset SL 設成 `Trailing Stop Market`；trail 由 bot 透過修改既有 Auto OCO stop order 來完成

實際下單流程：

1. Bot 送出帶有 SL/TP bracket ticks 的入場單。
2. 入場成交後，TopstepX 依照 API bracket 欄位生成 Auto OCO SL/TP 子單。
3. Bot 等待子單出現，篩選正確平倉方向，然後確認/修改 SL/TP 到策略算法計算出的價格。
4. Trail SL 觸發後，Bot 會修改同一張 Auto OCO SL，不會另外新掛一張 stop order。

如果 Auto OCO 子單沒有生成，live log 會出現 `[AUTO OCO]` 警告，Bot 不會退回到手動 bracket 下單。如果成交後 5 分鐘仍沒有 SL/TP，Bot 會先平倉、暫停 engine，並在 log 裡留下上面的 Risk Settings 連結。

---

## 安裝與啟動

請依照你的作業系統執行對應檔案：

### Windows 11

- 首次安裝：雙擊 `ancserTPX install win.bat`
- Web 版：雙擊 `ancserTPX web win.bat`
- Terminal-only LIVE：雙擊 `ancserTPX terminal win.bat`

> 若 Windows 跳出 SmartScreen 警告，點 **「其他資訊」→「仍要執行」**。

### macOS

- 首次安裝：雙擊 `ancserTPX install mac.command`
- Web 版：雙擊 `ancserTPX web mac.command`
- Terminal-only LIVE：雙擊 `ancserTPX terminal mac.command`

> **第一次執行**：macOS 會擋下，跳出「無法打開，因為來自未識別的開發者」。
> 解法：**對檔案按右鍵 → 打開 → 在對話框再點「打開」**。每個檔案只需做一次。
>
> 接著會自動跳出 Terminal 視窗執行，不用輸入任何指令。
> 使用 ancserTPX 期間請保持 Terminal 視窗打開，關掉就停掉伺服器了。

---

## 使用方式

### Backtest

1. 選擇策略或 preset
2. 點擊 **EXECUTE BACKTEST**

### Live Trading

1. 選擇交易帳戶
2. 確認 TopstepX Auto OCO preset 已啟用
3. 點擊 **GO LIVE**
4. 需要停止或手動平倉時，使用 **STOP** 或 **FLATTEN**

### Terminal-Only LIVE

Terminal 啟動檔不會開網頁，會直接啟動 LIVE engine。它會使用 `.env`
裡的 TopstepX email/API key；如果有設定 `TOPSTEPX_ACCOUNT_ID` 就使用該帳戶，
否則自動選第一個 practice 帳戶；策略參數使用 `data/presets.json` 裡最後使用的 live preset。

啟動 Web 或 Terminal 任一版本前，都會先停止舊的 ancserTPX Web/Terminal process，
並清掉 app ports `8000-8010`，避免同時跑兩個 trading engine。

### EMAPMO Discord 訊號圖

Web LIVE 與 Terminal LIVE 共用同一個通知模組。只有 live engine 真正產生
EMAPMO `TradeSignal` 時才會送出一則文字＋PNG 圖表；暖機、回測與一般狀態更新不會發送。

在根目錄 `.env` 設定單一 Discord 頻道。建議使用該頻道的官方 webhook：

```env
EMAPMO_MESSENGER_ENABLED=true
EMAPMO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
EMAPMO_SIGNAL_HISTORY_DAYS=30
```

如需沿用 `ancserMessenger` 的 token，可改填 `DISCORD_TOKEN`、
`EMAPMO_DISCORD_CHANNEL_ID` 與 `EMAPMO_DISCORD_AUTH_MODE`；個人 user token
可能違反 Discord 規範，建議優先使用 webhook 或 bot token。通知歷史只存中繼資料於
`data/messenger/emapmo_signals.sqlite3`，跨 Web／Terminal／多帳戶去重，預設保留 30 天，
不保存 PNG，也不把歷史訊號常駐記憶體。

---

## License

請參閱 [LICENSE](LICENSE)。
