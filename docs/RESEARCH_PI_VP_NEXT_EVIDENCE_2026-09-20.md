# PI / Volume Profile 後續證據盤點 — 2026-09-20

## 本輪範圍

沿用 `docs/RESEARCH_FOCUS_PI_VP_2026-09-20.md`，只讀現有程式、研究產物與本機紀錄，並完成一輪基準測試。沒有跑新回測、下載資料、購買資料或改策略程式。工作區原有的未提交變更保持原狀；本輪只新增本報告。

## 先看可確認的數字

### PI source level、深藍圈與 close-window 日誌

`F:\ancserQuant\ancserMarketData\source\discord\pi\pi_signals.json` 有 **530 則訊息、527 個 mark rows**，source 時間覆蓋 2026-03-05 至 2026-09-14。QQQ 的 Level 2 共 **21 個 marks**：**6 深藍圈多方、15 紫圈空方**。QQQ 深藍圈 kind 共 **7 個**，其中 **6 個是 source Level 2，1 個是 source Level 1**。因此此資料集裡「source Level 2 多方」與「深藍圈 kind」的重疊數是 6；兩個條件仍須分開標記。

PI 回測規格 `SignalSpec` 以 `long_kinds=("深藍圈",)` 選多方，沒有套用 long source-level 條件（`scripts/pi_reopen_continuation_study.py` 的 `LV2_LONG`；`scripts/pi_exhaustive_exit_study.py:111`）。既有續倉研究的「Level 2 bullish bubble」因此實際代表深藍圈 kind 篩選。原始 6 個 MNQ trades 與 source-level 2 的逐筆連結還未提供；報告應以「深藍圈 kind 樣本」描述，直到逐筆 join 完成。

`F:\ancserQuant\ancserMarketData\runtime\logs\pi_live_signals.jsonl` 截至 9 月 20 日有 **34,641 筆紀錄**，其中 **15 received、15 callback、5 recorded**；15 個 callback 裡有 **3 個入列、12 個未入列**。檔案沒有記錄這 12 個未入列訊號的最終策略決策，也沒有專用的 close-block / close-revalidation event row。當前可確認的 close-blocked 新訊號數為 **尚無可計數的逐筆決策資料**；不能把 callback 未入列數當成收盤攔截數。

現有 reopen continuation 回測只涵蓋已持有至 15:45 ET 強制平倉的 bot 部位。1R gap cap 的研究結果為：MNQ baseline 6 筆、3 筆重新入場；MES baseline 7 筆、5 筆重新入場。MNQ continuation 合計 +$410.78，總策略仍為 -$520.66；MES continuation 合計 +$1,006.30，總策略為 +$946.37。這些數字來自少於一年樣本，walk-forward gate 未通過（`pi_reopen_continuation_lv2_20260919_135830.md`）。

程式觀察支持兩種路徑分開研究：PI 來源訊號超過預設 5 分鐘會過期（`backend/strategy/pi_signal.py:36,289,516`）；15:45–18:00 ET 的 close handler 在一般評估前返回（`backend/live/engine.py:3912`）；目前 reopen ticket 由 15:45 仍持有的選定 bot 部位建立，支援一次 18:00 同日重新入場（`backend/live/engine.py:1420-1560`; `docs/INVARIANTS.md:98`）。

### Volume Profile 與 delta decay

既有 MNQ 代表候選在 2021–2025 train 為 n=970、PF 0.9462、-$2,194.80；2026 holdout 為 n=141、PF 0.9144、-$817.34。候選設定為 breakout、2 根 close 確認、2-tick break/touch buffer、ATR SL 1.5 / TP 2.0；未通過整體 robustness gate（`volume_profile_research_mnq_2021_2025.md`; `docs/HANDOFF.md`）。

在 18 筆可配對 MBO 的 VP trades 上，已保存的 decay ratio 是「最後 2 分鐘絕對 delta 總和 ÷ 最初 3 分鐘絕對 delta 總和」。按每分鐘平均正規化後：

```text
normalized = (late_abs_sum / 2) / (early_abs_sum / 3)
           = raw_ratio * 1.5
```

小樣本結果：raw `>= 0.75` 有 **8/18**；normalized 仍用數值門檻 `>= 0.75` 有 **9/18**；把同一經濟門檻等比例轉成 normalized `>= 1.125`，仍為 **8/18**。18 筆中只有 **1 筆** 5 分鐘 volume ratio `>=1.25`；它的 raw / normalized decay 分別是 **0.075 / 0.113**，所以原 breakout-flow 組合仍是 **0/18**。正規化修正時間窗長度偏差；樣本結果尚未檢驗任何盈利提升。raw ratio 最大 27.63，後續 event study 還需記錄分母與有效分鐘數，標示低分母比率。

VP 目前 26 個 RTH MBO 日，22 筆候選策略交易、18 個完整 MBO join、9 個 GEX join；這份 overlap 適合建立逐事件標籤，尚不足以驗證長期 MBO/GEX gate。

## 優先研究單 A — PI 收盤訊號生命週期

- **OBSERVED:** PI 訊號 history 有 530 則訊息。QQQ source Level 2 多方與深藍圈的交集為 6 marks，深藍圈 kind 共 7 marks。runtime log 有 15 個 received/callback pairs、3 入列、12 未入列，0 筆專用 close-block outcome。既有 continuation 是已持倉後重新入場；它沒有重驗一筆已排隊但未進場的新 source signal。
- **INTENDED:** 逐筆保留 source timestamp、received timestamp、source level、kind、selector 結果、queue 結果、close phase、價格失效條件、entry/exit outcome。固定比較：A. 現行 close/5-minute expiry；B. 既有已持倉 continuation；C. 15:30–16:00 ET 收到的新訊號於當日 18:00 ET 前五分鐘重新驗證；D. 同一批訊號於下一個 RTH open 重新驗證。C/D 使用同一組 0.5R、1R gap cap 與原 SL/TP 失效條件；另以 `source level=2` 與 `kind=深藍圈` 分開分組。
- **EVIDENCE:** 上述 source、runtime log、既有續倉報告；PI queue callback 在 `backend/live/engine.py:3042-3064` 只記錄入列結果。LIVE-012 記錄 `trades.json` 不保存 strategy decision-reason 欄位。
- **INVARIANTS:** PI-001–PI-012、PI-014、CLOCK-002/003、LIVE-004、LIVE-012。
- **BEHAVIOUR CHANGE?** 否；本 ticket 為研究及 decision-audit 規格。任何延長訊號有效期或新增重開路徑都屬策略行為變更，需另行核准。
- **FILES ALLOWED:** 新增 `scripts/pi_lifecycle_replay_study.py`、`tests/test_pi_lifecycle_replay_study.py`、外部 derived research report。Production allowlist 空白；保留既有 dirty `scripts/pi_reopen_continuation_study.py` 與其他未提交檔。
- **TESTS:** 新研究解析器的 source-level/kind 分離、事件時間窗、失效 barrier、重複去重；既有 PI 行為測試只讀使用。
- **ACCEPTANCE:** 先逐筆重現現行 baseline trades，再以相同 source events 和成本跑 A–D。Backtest 與 shadow-live 在相同已完成 candle tape 上逐項輸出一致的 state、entry eligibility、SL/TP。每筆被拒與入場都帶原因碼；同一 source mark 不重複產生交易。若決策 parity 或逐筆 baseline 有一筆差異，先修研究模型，不比較績效。報告樣本數、缺失 join 數、RTH 分段、PnL/PF、14-tick stress、日群組 bootstrap；不足 30 個獨立事件的分組列為 inconclusive，不作 live promotion。

## 優先研究單 B — VP 三狀態事件測試

- **OBSERVED:** current profile 將每根 K 棒成交量平均配置到 high-low tick 範圍，屬 OHLCV proxy（`backend/strategy/volume_profile.py:215-246`）。既有代表候選尚未盈利通過。MBO context 僅有 18 個 trade joins；delta ratio 的固定 2/3 分鐘窗口需要 per-minute normalization。
- **INTENDED:** 把 VAH/VAL 作為位置，依已完成 bars 將每個 edge episode 分成三種狀態：`BREAKOUT_ACCEPT`（2 根 close 穿越既有 2-tick buffer，之後 2-tick retest 守住）、`RANGE_REJECT`（穿越後在預先固定期限內收回 value，並以 POC 為固定目標）、`WAIT`（期限內未確認、或同一 episode 多次反覆穿越）。一個 episode 只給一個分類和一次 entry decision；超過期限後才重新 arm。breakout 及 range entry 共用已凍結的 SL 1.5 ATR / TP 2 ATR，先以目前 two-close/2-tick baseline 作比較。
- **EVIDENCE:** 五年 VP baseline/holdout 報告；26 日 MBO overlap；18 行 delta decay CSV。現存 CSV 保存五分鐘總量與 decay ratio，沒有完整逐分鐘 early/late vectors，所以本輪只做了代數正規化敏感度，完整事件窗口仍須由 MBO cache 重建。
- **INVARIANTS:** CLOCK-001/002、LIVE-004、DATA-003/006/011–013、CONFIG-001/002/004/005；研究階段保持 VP live/backtest 共用決策與現有成交保護契約。
- **BEHAVIOUR CHANGE?** 否；本 ticket 為研究。任何 entry/edge-lock 變更都需另行核准。
- **FILES ALLOWED:** 新增 `scripts/volume_profile_event_study.py`、`tests/test_volume_profile_event_study.py`、外部 derived research report。Production allowlist 空白；不改既有策略或 preset。
- **TESTS:** edge episode 去重與三態互斥；bar 完成後才可決策；profile 交易量總和守恆；MBO per-minute ratio 算術；缺失分鐘與近零分母標籤；live/backtest 相同 tape 的決策輸出 parity。
- **ACCEPTANCE:** 先在固定日期與輸入逐筆重現現行 candidate trades，再於同一 MBO overlap 比較 OHLCV profile 與 trades-at-price profile；固定 A. baseline、B. 三態 VP、C. B+normalized delta、D. B+GEX、E. B+兩者，全部共用日期、成本、單持倉與 bracket 假設。每個 state 的事件數、no-trade 數、false-break 率、入場後最大有利/不利移動皆列出。至少 30 個獨立事件/state 且涵蓋 3 個 chronological folds 才能進入下一道評估；候選還須在 holdout 成本後及 14-tick stress 後 PF>1，day-cluster 95% CI 下界大於 0，並通過 live/backtest decision parity。樣本門檻不足時保持 exploratory。

## 數據預算備註

US$40 / US$80 對應 ThetaData Options Value / Standard。Value 可先測歷史 1-minute quotes 與 OI；Standard 加歷史 IV、first-order Greeks、trades，first-order schema 同時列 IV error、underlying price/time，適合建立經同步 spot、利率、股息、到期時間計算的 proxy gamma。模型結果需與 Pro 的歷史 second-order gamma 在重疊樣本比對。ThetaData 最新 Greek 方法採 real TTE 並設 1 小時下限；舊方法使用固定 0.15 DTE，0DTE 回測需鎖定版本與 TTE 設定。若目標是現成歷史 gamma，Pro US$160 可省下自建 gamma 計算與驗證工作。

Massive Options 標價為 Starter US$29、Developer US$79、Advanced US$199。Starter 可先做延遲快照的前向資料收集；Developer 對歷史資料與 trades 較有用。Chain snapshot 端點把 Starter/Developer 標為 15 分鐘延遲，Advanced 為即時，該端點沒有歷史 snapshot；snapshot OI 是前一交易日數值。價格頁面 Greeks/IV 的即時描述與 snapshot endpoint 的延遲標籤需要以實際方案欄位樣本確認。第一步仍採用既有 QQQ/MBO 檔；購買前先確認合約、歷史時間戳與 OI 可知時間。

官方資料：[ThetaData pricing](https://www.thetadata.net/pricing)、[ThetaData subscription matrix](https://docs.thetadata.us/Articles/Getting-Started/Subscriptions.html)、[ThetaData first-order Greeks schema](https://docs.thetadata.us/operations/option_history_greeks_first_order.html)、[ThetaData second-order Greeks](https://docs.thetadata.us/operations/option_history_greeks_second_order.html)、[Massive options pricing](https://massive.com/pricing?product=options)、[Massive option-chain snapshot plan access](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)。

## 本輪驗證

執行 `python -m pytest tests/ -q`：**759 passed、8 subtests passed、1 failed，61.31s**。失敗為 `tests/test_volume_profile_research.py::test_volume_profile_research_preset_round_trips_through_terminal_builder`，實際結果 `params.strategy == "factor"`，預期 `"volume_profile"`。`docs/HANDOFF.md` 先前已記錄一項 VP preset round-trip failure；本輪沒有改動該路徑。沒有新增程式碼，也沒有宣稱 live 修復或策略獲利驗證。
