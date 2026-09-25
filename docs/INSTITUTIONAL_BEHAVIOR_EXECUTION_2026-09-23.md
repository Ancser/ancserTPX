# 機構行為研究規劃：首輪執行結果

日期：2026-09-23。工作範圍：MNQ MBO／Volume Profile 離線回放。只讀取行情與研究輸入；無 production、preset、live state 或原始資料修改。本文件記錄研究結果與下一輪可執行條件。

## 執行項目

- 使用既有 `scripts/orderflow_context_combination_study.py` 重播 31 個 schema v5 MNQ RTH footprint 日期（2026-08-07 至 09-18）。切分沿用既有 `DISCOVERY_END=2026-08-14`；8/17 至 9/18 共 25 個 evaluation 日期。
- 對六組預選 stream/context 重跑同一固定退出、費用、單持倉及 cooldown 模擬；每筆另外加 14 tick round-trip 壓力，即 MNQ 每筆 $7。
- 使用既有 `scripts/volume_profile_context_study.py`，把凍結 VP 研究候選送入 production `BacktestEngine`，重播同一段 MBO 有效日期，並只連接 entry-time 已可見的五分鐘 MBO features。
- 輸出另存於外部 research data；不覆寫歷史報告。

MBO 行為欄位含 aggressor buy/sell、OFI、passive fills、near-fill refill、depth 與逐價 cells。OrderID 為匿名識別碼，持有人身分保持 unknown。[CME MBO 文件](https://www.cmegroup.com/articles/faqs/market-by-order-mbo.html)

## P1：被動觸價拒絕 × 價值區位置

研究腳本以最活躍的被動價位作候選：深度加權 passive fills／refills；價格觸及價位一個 tick 內，完成 K 線收回 bid 上方或 ask 下方，下一根 1m open 進場。`value_reversion` 按方向要求當下收盤位於前一 RTH value area 外側、朝 value 內回歸。固定退出沿用現有研究的 ATR blend：多單 SL/TP = 4/12 ATR，空單 1.5/4.5 ATR，最多 60 分鐘；同向 10 分鐘 cooldown 並限制單一持倉。費用使用 MNQ canonical commission + fees。

| Stream 狀態 | 樣本期間 | 筆數 | 淨 PnL | PF | 加 14 tick 後 PnL / PF |
|---|---|---:|---:|---:|---:|
| 所有被動觸價拒絕 | Discovery | 45 | −$1,323.80 | 0.4871 | −$1,638.80 / 0.4123 |
| 價值區回歸 | Discovery | 35 | −$72.40 | 0.9512 | −$317.40 / 0.8038 |
| 所有被動觸價拒絕 | Evaluation | 215 | −$933.60 | 0.9144 | −$2,438.60 / 0.7931 |
| 價值區回歸 | Evaluation | 171 | +$2,823.46 | 1.4214 | +$1,626.46 / 1.2231 |
| 價值區突破 | Evaluation | 168 | −$4,294.32 | 0.5721 | −$5,470.32 / 0.4942 |

價值區回歸組移除最佳 evaluation 日期 2026-09-16 後仍有 +$2,319.36；25 個 evaluation 日期的日期群組 bootstrap 95% 區間為 **−$69.04 至 +$5,551.98**，跨零。Discovery 的結果也明顯弱於 evaluation。方向支持「觸價拒絕在回歸狀態較有利」，穩定性仍未建立。

## MBO turnover 延伸檢查

| Stream 狀態 | Evaluation 筆數 | 淨 PnL / PF | 加 14 tick 後 PnL / PF | 移除最佳日後 PnL | 日期群組 bootstrap 95% 區間 |
|---|---:|---:|---:|---:|---:|
| 全部 turnover | 146 | +$1,076.46 / 1.1893 | +$54.46 / 1.0088 | +$616.90 | −$1,263.20 至 +$3,326.76 |
| CVD aligned | 45 | +$646.20 / 1.4963 | +$331.20 / 1.2242 | +$241.66 | −$506.36 至 +$1,981.94 |
| Passive rejection + imbalance | 128 | +$1,086.78 / 1.2195 | +$190.78 / 1.0355 | +$739.72 | −$553.60 至 +$2,663.72 |

CVD aligned 的 discovery n=10、PnL −$101.90、PF 0.7286；evaluation 指標轉正，日期群組不確定區間仍跨零。三組候選的區間都跨零。14 tick 為額外 slippage 情境，base PnL 已扣 canonical 費用。

Bootstrap 固定 seed 20260923，5,000 次，以 25 個 evaluation 日期的每日淨 PnL 為抽樣單位。這些歷史日期在本專案其他研究中已有部分觀察，結果全數標示 retrospective；多組既存 context 的同時比較也增加選樣風險。

## P0：VP engine／MBO 因果連接

重播凍結 MNQ VP 研究候選：70% previous-RTH value area、breakout、2-bar confirmation、2-tick buffer、ATR SL 1.5／TP 2、每日最多兩筆。production `BacktestEngine` 產出：31 個 MBO 日期中 27 筆，PnL −$580.48、PF 0.6406；14 tick 壓力後 −$769.48、PF 0.5563。按既有 8/27 discovery 切分，9 筆為 −$27.16／PF 0.9492，後段 14 筆為 −$444.86／PF 0.4713。

23 筆可因果連接 MBO；當前 `mbo_breakout_flow` 門檻通過 0 筆，`mbo_consolidation_flow` 8 筆、PnL −$432.92、PF 0.2371。QQQ OI gamma proxy 僅 9 筆可連接，其中正／負標籤各 5／4 筆；此子樣本維持描述用途，標籤不等於 dealer 真實持倉方向。[OIC 對 OI 的說明](https://www.optionseducation.org/referencelibrary/faq/general-information)

VP edge 分組為 VAH 11 筆、+$169.36、PF 1.3458；VAL 12 筆、−$641.38、PF 0.2766。edge 與多空方向重合，必須和同方向市場狀態配對後再估計 edge 的額外資訊。

## 判斷與下一輪凍結規則

第一輪支持把 MNQ RTH 的「被動觸價拒絕 × value reversion」列為前瞻模擬候選；結果呈現 discovery／evaluation 差異且信賴區間跨零，故目前只屬探索性證據。MBO turnover 的成本壓力餘裕有限。現有 VP OI gate 結果在成本壓力後亦低於 PF 1；OI／GEX 維持背景特徵。

下一輪在規則凍結後收集 30 個完整 MNQ RTH session；每個交易日期保存候選事件、當時可用特徵、時間戳、下一根開盤模擬成交、固定退出、canonical 成本與 14 tick 壓力。同步保留無條件被動觸價 stream 作對照，報告 missed winners、避開的 losers、每日群組區間、單日貢獻與方向分層。30 日只作操作覆蓋檢查；樣本不確定性仍決定是否延長。策略引擎實作另需逐筆重現候選與風險規則。

## 可重現輸出

- [31 日 order-flow context 回放](F:/ancserQuant/ancserMarketData/derived/research/institutional_behavior_orderflow_context_20260923.md)
- [成本壓力、最佳日排除與日期群組 bootstrap](F:/ancserQuant/ancserMarketData/derived/research/institutional_behavior_matched_gate_20260923.md)
- [Volume Profile engine／MBO 因果情境重播](F:/ancserQuant/ancserMarketData/derived/research/institutional_behavior_vp_mbo_engine_20260923.md)

本輪讀回報告與 JSON 摘要核對結果；未新增或執行測試，也未修改交易程式、preset、live 帳戶狀態或原始行情。
