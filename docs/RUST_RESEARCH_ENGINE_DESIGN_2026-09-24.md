# Rust 量化研究引擎與五年資料可用性

日期：2026-09-24。範圍：唯讀資料盤點、既有研究證據整理、Rust 研究架構規格。此文件不調整 production、preset、live 設定或 canonical 市場資料。

## 決策摘要

- 目前 MNQ/MES 1 分鐘資料足以支援近五年中低頻期貨研究。2022–2025 RTH 逐日 bar 數只呈現 390 分鐘完整日與 210/225 分鐘短盤日；兩個商品的 2026 年覆蓋都尚未到年底。
- QQQ/SPY Theta EOD 與 OI 的 0–60 DTE 月檔，2021-01 至 2026-09 每個商品、每種資料各 69 個月，月份連續。這能支援選項持倉結構與盤勢 regime 的五年以上回顧研究；檔案定義為部分到期鏈時，研究須沿用該標籤。
- CFTC TFF equity-index futures-only 檔案有 2,696 列，ES/NQ 歷史始於 2006，兩個 micro 對應市場始於 2020；週報採 Tuesday as-of、Friday 發布時序，研究使用保守延遲。
- MBO 只有 2026-08 至 09 的數週樣本。它適合近期前瞻資料檢查，無法單獨支持完整五年 MBO 歷史回測。
- Rust 適合成為重複回放、特徵運算、風險狀態機與共用 live/backtest 決策核心。資料品質、provider publication time 與網路延遲仍由資料來源和 adapters 決定。速度收益以相同輸入的 Python/Rust parity benchmark 實測後再宣稱。
- 第一個 Rust 樣本採已凍結的 MNQ/MES Volume Profile + Theta OI 研究候選。先重現既有 Python BacktestEngine，再測試少量預先聲明的 option-regime 特徵。現有 OI gate 的成本壓力結果偏弱，研究目標先確認基準可重現與規則穩健性。

## 現有資料盤點

### 期貨分鐘行情

來源：`F:/ancserQuant/ancserMarketData/source/futures/continuous_1m`。本次使用專案既有 `backend.data.candle_store.load_snapshot()` 唯讀載入兩份 canonical pickle，按 `America/New_York` 計算 RTH bar 數。

| 商品 | 全部 bars | 起訖 UTC | 最新 metadata | 2022–2025 RTH 分布 |
|---|---:|---|---|---|
| MNQ | 2,376,404 | 2020-01-01 23:00 至 2026-09-23 20:59 | `frozen_through=2026-09-22 20:59`；最後資料日較 freeze 多一天 | 2022：250×390、7×210、1×225；2023：248×390、7×210、2×225；2024：249×390、7×210、3×225；2025：247×390、7×210、3×225 |
| MES | 2,365,689 | 2020-01-01 23:00 至 2026-09-14 17:45 | 最近 metadata 更新 2026-09-14；最後 RTH session 有 256 根，屬盤中截尾 | 2022：250×390、7×210、1×225；2023：248×390、7×210、2×225；2024：249×390、7×210、3×225；2025：247×390、7×210、3×225 |

2022–2025 的逐日數量與完整日、短盤日模式吻合。2026 MNQ 有 182 個 390-bar RTH 日與 6 個 210-bar 短盤日，另有一個尚未 freeze 的最新資料日；MES 有 174 個 390-bar 日、6 個短盤日，以及 2026-09-14 的 256-bar 盤中尾端。MES 後續需要完成補齊和 freeze 才能納入完整的 2026 holdout。

rolling continuous series 的 source、instrument roll 與回溯調整會影響跨季價格指標。`backend/data/futures_data.py` 定義 UTC exact-minute、缺分鐘不補造、roll 只依 `instrument_id` 變化。`docs/HANDOFF.md` 另記有 2026-08-31 MNQ/NQ 來源拼接疑點，當前研究切片需再與最新 canonical snapshot 核對；受影響日期先加資料品質標記。RTH 統計已找出完整 bar 日與 210/225-bar 短盤日分布；下一輪將把每個短盤日期逐一對照 CME 歷史交易時段資料，完成官方日曆核驗。

### Theta 期權資料

`source/options/thetadata/raw/{QQQ,SPY}` 已有 4 類月檔：`monthly_open_interest_dte_0_60` 和 `monthly_eod_dte_0_60`。逐目錄盤點結果為每商品、每種類型 69 檔，2021-01 至 2026-09 無缺月，共 276 個月檔。既有 QC 對 276/276 月檔完成完整性檢查；月檔保留供應商 publication timestamp 和 effective-session 語義。

QQQ 2016–2026、SPY 2020–2026 另有 0DTE trade/quote 與 Greeks 日檔，已完成的研究樣本合計 1,257,290,327 rows。Theta 0DTE flow 的歷史 signed-side 研究發現 quote/trade 同時間戳會進入現有分類器；嚴格時間先後敏感度尚待完整量化。該訊號在既有 14-tick 成本壓力下沒有通過驗證，Theta live entitlement 與端到端延遲仍要獨立核實。

月度 OI 研究已按逐列供應商發布時間做 as-of join，沒有把整月資料指派給同一可用時間。選項 OI 代表公開未平倉合約數；dealer 方向與實際持有人身分保持未知。可優先測試 unsigned OI、put/call 結構、近到期占比與 moneyness 集中度，dealer gamma sign 維持明確假設欄位。

### CFTC 與 MBO

CFTC 檔案：`source/positioning/cftc_tff_futures_only/tff_futures_only_equity_index_codes_20260923.csv.gz`，manifest 記錄 2,696 列、gzip 與解壓 CSV SHA-256。CME/CFTC 文件指出持倉以 Tuesday 為基準，週報通常 Friday 15:30 ET 發布；研究以發布後時間連接。已完成的 weekly leveraged-money follow replay，MNQ n=349、14-tick PnL −$11,154.26；MES n=347、14-tick PnL −$14,402.78。它可作 lagged macro context，當前規則無法充當獲利引擎。

MNQ MBO 的已完成 engine replay 只有 31 個 RTH 日期；更新版 VP/MBO 快照截至 2026-09-22 有 33 日、27 筆交易、23 筆 MBO join。MBO raw feed 成本與延遲不符合五年主研究的覆蓋要求，先列為近期輔助資料。

### 儲存與來源邊界

F: 本次剩餘約 373.63 GiB。既有 Theta 0DTE archive 已約 25 GiB。續抓 Databento 歷史資料採逐 request fresh quote，max cost 固定 $0，正價 quote 停止；CFTC 使用公開免費來源，Theta 僅讀取現有 entitlement 範圍。

## 已有研究先驗與第一個樣本

1. `docs/VP_THETA_OI_5YR_STUDY_2026-09-23.md`：2021–2025 VP 基準 970 筆、PnL −$2,194.80、PF 0.9462；2026 holdout 141 筆、PnL −$817.34、PF 0.9144。OI 的描述性連接通過時點核對，特定 OI 門檻仍混入 2022 週二／週四到期制度改變與方向差異。
2. `docs/VP_THETA_OI_ENGINE_GATE_2026-09-23.md`：既有 OI gate 在 2021–2023 校準 n=32，14-tick PF 1.0104；2024–2025 n=172，stress PF 0.9365；2026 n=95，stress PF 0.9323。此 gate 留在研究層級。
3. `derived/research/cftc_tff_leveraged_money_weekly_change_replay_20260924.md`：週變化追隨策略在 MNQ、MES 全樣本與 walk-forward 均未通過成本壓力；5-session price momentum 也接近成本 break-even。Positioning 當背景變數的用途高於直接入場方向。
4. `docs/INSTITUTIONAL_BEHAVIOR_EXECUTION_2026-09-23.md`：近期被動觸價拒絕 × value reversion 有 25 個 evaluation dates，stress PF 1.2231，日期群組區間跨零，樣本短且部分日期已看過。它只適合作為候選假說來源。

### Rust 首輪研究 protocol

研究樣本使用已保存的 `VOLUME PROFILE` 70% previous-RTH value area、breakout/rejection episode 與現有 `BacktestEngine` 固定退出作基準。商品先以 MNQ 作主樣本、MES 作跨商品 replication。Rust 首輪逐筆重現既有口徑：2021–2025 reference、2026 YTD retrospective holdout。新 option-regime 參數在單獨 protocol 採 2021–2023 calibration、2024–2025 validation、2026 retrospective holdout；2026 已看過的診斷列為回顧樣本。凍結後新增日期作 prospective shadow holdout。

按資訊到達時間建立資料 join：

- 完成的 1-minute bar 以 bar end 作 `available_at`；訊號依 5-minute 完成區間建立，下一根 1-minute bar open 作研究 fill 基準。
- Theta OI 每一列保留 `published_at`、`effective_session`、symbol、expiration、strike、right、OI 與 chain coverage。決策時刻只連接 `published_at <= decision_time` 的列。
- OI 特徵首輪限於總量／標的自身歷史 rank、call-put 結構、DTE 分桶與 moneyness 集中度。門檻只在訓練期確定；按星期與 2022-11 週二／週四到期制度分層。
- 至少比較 frozen price-only baseline、range-rejection、breakout-acceptance 三種事先定義狀態。option context 的價值以相同日期、方向、觸發次數的 paired delta 衡量。
- 成本表包含 canonical commission/fees、14-tick 固定壓力與可調的滑價敏感度。績效同時報告 stress PnL/PF、每筆風險效率、每個 Topstep session 的虧損尾部、觸發頻率、最佳日移除與日期群組 bootstrap。

## Rust 架構

第一階段放在獨立 `research/rust_engine` workspace。Python `backend/` 保持既有 live/backtest 行為來源，研究核心取得 Python reference trade ledger 做 parity；完成 parity 與風險審核後，再獨立規劃 live adapter。

| crate | 職責 |
|---|---|
| `market-core` | UTC/ET/CME/Topstep session time、整數 tick 價格、MNQ/MES 規格、資料可用時間型別、特徵介面 |
| `market-data` | 原始 manifests/hash、Python pickle 一次性轉成研究用 Arrow/Parquet、Theta gzip CSV streaming、CFTC parser、point-in-time as-of join |
| `strategy-kernel` | 無 IO 的策略狀態機、episode、entry/exit intent、同一參數介面給 replay 與未來 live adapter |
| `simulator` | 單持倉、bar completion、next-bar fills、fees/slippage、stop/target 同棒歧義標記、逐筆 ledger |
| `prop-risk` | 版本化 Topstep profile：Combine/XFA 路徑、MLL、可選 DLL、position cap、session-day、consistency objective |
| `research-cli` | 有限、可審核的 `audit-inputs`、`run-study`、`compare-runs`、`explain-trade` JSON/TOML API |

研究輸入以日期／商品分割的 Arrow IPC 或 Parquet，保留 manifest：資料 SHA-256、時間邊界、coverage、source、轉換程式版本、roll flags。canonical pickle 保持原位；一次性轉換只寫 derived research。1.25B Theta rows 以 gzip stream 逐日處理，輸出小型 point-in-time feature tables；每次回測讀取 feature table，避免每次重解析期權全量逐筆資料。

### 首個 Rust 效能量測

新增的 `research/rust_engine` Release CLI 與 Python 3.10 使用相同 69 份 QQQ 月度 OI `.csv.gz`，逐列解壓解析並計數。兩者結果均為 **5,818,465 rows**、輸入 **36,941,613 compressed bytes**。三次交錯執行中，Rust 1.98.1 時間為 **1,114 / 687 / 664 ms**；Python `gzip` + `csv.reader` 為 **4,679.96 / 4,963.06 / 4,776.75 ms**。中位數約 **6.95×** Rust parser throughput。

此量測採本機 warm filesystem cache，排除 archive SHA-256 計算與策略回放；首輪 1,114 ms 顯示冷啟動附近的 parser 工作仍快於 Python 中位數。這提供 OI archive streaming 的直接效能證據，完整 BacktestEngine replay、memory profile、Arrow/Parquet 讀取和 live latency 尚待實測。

核心 API 以型別化設定由 CLI、Python adapter 或 AI tool wrapper 呼叫：

```text
audit_inputs(study_id) -> CoverageReport
run_study(study_id, config_path) -> RunManifest
compare_runs(reference_id, candidate_id) -> PairedReport
explain_trade(run_id, trade_id) -> DecisionTrace
```

每個 decision trace 保存 decision timestamp、輸入 feature `available_at`、原因 code、風險限制狀態、建議 entry/stop/target 與 fill model。AI 介面選擇既有 study/config/schema；研究範圍與成本由固定 protocol 控制，不讓每輪自行生成一份回測程式。

## Topstep 風險與獲利效率

Topstep rule profile 按 account type、account size、Combine/XFA 路徑和官方規則日期版本保存。每次研究先算原始市場策略，再用同一交易序列模擬 MLL、可選 DLL、micro/mini 數量限制、Topstep 17:00 CT trade-date 分組與一致性目標。使用者帳戶 dashboard 的實際參數作 live profile 輸入。

目標函數優先量測「達到目標的機率 × 不觸及 MLL 的機率 × 每單位 MLL 的淨利」，並設最大 drawdown、尾部單日虧損、最低有效觸發數與成本壓力門檻。報告同列 raw expectancy、stress expectancy、profit concentration、最佳日移除和完成目標所需 session 分布。Position sizing 先以 1 MNQ / 1 MES 固定口數測 edge，再用 risk profile 做獨立 sizing study。

Topstep 官方參考（規則會更新，研究 run 固定保存查證日期）：

- [Trading Combine Parameters](https://help.topstep.com/en/articles/8284197-trading-combine-parameters)
- [Consistency at Topstep](https://help.topstep.com/en/articles/8284208-consistency-at-topstep)
- [Express Funded Account Parameters](https://help.topstep.com/en/articles/8284215-express-funded-account-parameters)
- [CFTC About the COT Reports](https://www.cftc.gov/MarketReports/CommitmentsofTraders/AbouttheCOTReports/cot_about.html)

## 歷史 scripts 分類

不批次刪除歷史研究檔。建立一份 `research/catalog.toml` 登錄 `script_path`、研究問題、data hashes、run ids、輸出、status、canonical replacement 與退役理由。`active` 指唯一可重跑入口，`superseded` 指有明確 replacement 的重複實作，`archive_candidate` 指只供歷史查證。刪除前搜尋 imports、文件引用、job/automation 呼叫和輸出依賴；先由 catalog 取代重複用途，再逐檔審核。

## Acceptance gates

1. **Data**：五年 MNQ/MES 日期表、短盤與缺口分類、2026-08-31 seam 核對、Theta OI publication/effective-session、MBO coverage 和檔案 hash 全部封存。
2. **Parity**：Rust 對保存的 Python reference 逐筆比對 entry/exit time、side、price、quantity、fees、gross/net PnL；差異逐欄輸出，所有差異有解釋後才擴張研究。
3. **Causality**：禁止未完成 bar、未發布 OI、未來 Greeks/quotes 進入 decision snapshot；成交只使用預先指定的 bar/fill 模式。
4. **Research**：小型預先聲明候選集、chronological splits、paired count/return、14-tick stress、date-cluster uncertainty、best-day removal、年度/季度切片與 12 個月 coverage gate。
5. **Topstep**：按 trade-date、MLL、position cap、帳戶路徑 objective 重算通過率及尾部風險；規則 profile 隨 run hash 固定。
6. **Performance**：同機同輸入測 cold load、warm replay、peak RSS、研究輸出一致性。Rust 速度改善以實測結果報告。
7. **Live**：研究 core 經 parity 和 rule audit 後，才透過獨立 adapter 接入 live；adapter 保留現有 bracket/OCO、single-engine lease、manual-position ownership、warmup 與 session invariants。

## 當前執行狀態

已完成唯讀盤點、五年分鐘資料 RTH 分布計算、Theta 月檔月份缺口掃描、既有 OI/CFTC/MBO 研究彙整。Rust stable 1.98.1 與 Cargo 已安裝至使用者 profile；Visual Studio 2022 C++ x64 linker 已存在。隔離 Cargo workspace、Release archive-audit CLI 與 QQQ OI parser benchmark 已完成；資料轉換、VP/OI逐筆 parity、Topstep risk simulator、完整回測速度比較與新策略研究仍待完成。工作樹原先已有多項使用者改動，本輪未更動那些檔案。
