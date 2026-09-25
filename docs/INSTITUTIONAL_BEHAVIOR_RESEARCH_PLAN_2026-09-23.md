# 現有資料：機構行為代理與策略研究規劃

日期：2026-09-23。交付範圍：資料盤點與離線研究設計。以下候選與驗收門檻屬提案；本次執行內容為讀取資料、研究報告和程式，新增本文件。

執行進度：已用 31 個 MNQ RTH MBO 日期完成 frozen VP engine 情境回放，以及被動觸價／價值區與 MBO turnover 首輪回測。摘要和再現輸出見[首輪執行結果](INSTITUTIONAL_BEHAVIOR_EXECUTION_2026-09-23.md)。前瞻 30 個 session 尚待候選規則凍結後開始記錄。

## 1. 研究目標

建立「期權持倉背景 → 關鍵價位 → 買賣壓力／流動性反應 → 價格確認 → 執行」的可追溯模型。回答兩個問題：哪些可觀察行為預示延續或反轉；這些資訊加入現有策略後，扣成本的期望值改善多少。

「機構行為」以可量測的行為代理表示：被動吸收、主動推進、補單、撤單、持續成交壓力與期權持倉集中。CME MBO 的 OrderID 匿名；交易者身分及跨訂單共同所有權保持 unknown。MNQ/MES 訂單流反映各自微型期貨市場，機構在 NQ/ES 的活動需要對應資料另作研究。[CME MBO 說明](https://www.cmegroup.com/articles/faqs/market-by-order-mbo.html)

OI 量測未平倉合約；每張合約同時有多空雙方。OI 變化、call/put 比及履約價集中度先作結構特徵，dealer 持倉符號保持假設標籤。[OIC OI 說明](https://www.optionseducation.org/referencelibrary/faq/general-information)

## 2. 本次確認的可用資料

根目錄：`F:/ancserQuant/ancserMarketData`。

| 資料 | 本次盤點／既有驗收 | 可支持的研究 |
|---|---|---|
| MNQ／MES 多年 1m | `source/futures/continuous_1m`；既有 VP 重播涵蓋 2021–2026 | 價格狀態、時段、ATR、VP、多年背景驗證 |
| QQQ／SPY Theta 每日 EOD＋OI | 2021-01 至 2026-09-21；0–60 DTE；138 EOD＋138 OI 月檔 | 持倉集中、到期結構、同合約 OI 變化；屬部分到期範圍 |
| Theta 次日到期期權分鐘 quote＋一階 Greeks | 240 標的／事件日期 cohort；960 請求；另有 16 個延長時段檔 | PI 事件及次日的 bid/ask、IV、Delta、Theta、Vega 與標的路徑 |
| Canonical PI | 481 標記、126 市場日期；QQQ 251／SPY 230 | 分開研究 kind、source level、方向、時段與執行延遲 |
| MNQ RTH footprint | 36 個日期檔，31 個非空；2026-08-07 至 09-18；5 個空檔為星期日日期 | 吸收、主動成交、OFI、價格層補單／撤單、實際 volume-at-price |
| MNQ ALL footprint | 29 個非空 UTC 日期檔；2026-08-16 至 09-18 | 依交易 session 重組後的跨時段研究 |
| MES ALL footprint | 25 個非空 UTC 日期檔；2026-08-16 至 09-13 | MES 獨立研究及跨商品敏感度 |

本次逐一讀取上述 footprint gzip，確認非空檔數；非空日期仍需逐 session 驗收完整性。UTC 日期檔數和獨立交易日數分開統計。最新 footprint metadata 為 schema v5，包含 buy/sell、delta、OFI、refill、cancel、depth、逐價 cells 及成交量級別。

Theta manifest 最新去重結果共 1,262 個 `downloaded` 請求；本次核對 manifest 分類，既有完整雜湊驗收引用下列報告。期權分鐘覆蓋集中於 PI cohort 的指定到期日；3DTE／7DTE 比較及跨到期 IV 曲線列入後續資料缺口。

期貨資料需固定來源／合約切換與不可變快照；目前累積庫和 pending tail 分開處理。最早期的 MNQ 連續合约拼接問題以既有 source audit 與凍結基準核對。

## 3. 已有結果如何決定優先級

1. **吸收行為優先研究。** 既存 2026-08-15 至 09-14 時段報告，MNQ RTH 20 個完整 session、31 筆，PF 2.2143，14-tick 成本壓力 PF 1.9404；MES RTH 35 筆 PF 0.5084，壓力 PF 0.2479。商品及時段交互作用值得追查；樣本已被觀察，擴展結果仍屬回顧研究。
2. **OI 作背景增量。** 最新完整引擎 gate：2024–2025 PF 1.0652、成本壓力 0.9365；2026 PF 1.0419、成本壓力 0.9323。先保留此失敗對照，下一候選聚焦結構與狀態交互作用。
3. **PI 優先重播可執行持倉。** 包含延長時段的 481 標記、每標記一張、次日 15:30 結果為 +$13,106.70；移除 6/4 整個市場日期後 +$1,515.20。單持倉、固定資金、延遲、零 bid 處理決定實際可用性。各標記 payoff 加總與組合 equity curve 分別保存。

證據：[時段報告](F:/ancserQuant/ancserMarketData/derived/research/mbo_session_comparison_2026-08-15_2026-09-14.md)、[VP 引擎 gate](VP_THETA_OI_ENGINE_GATE_2026-09-23.md)、[PI 全樣本](PI_THETA_FULL_COHORT_REPLAY_2026-09-23.md)、[Theta QC](THETA_ARCHIVE_QC_2026-09-22.md)、[cohort 完成紀錄](THETA_PI_COHORT_INGEST_STATUS_2026-09-22.md)。

## 4. 四個預先登記的研究方向

### A. 被動吸收後收回關鍵價位：第一優先

事件位置固定為前一完整同類 session 的 VAH／VAL；RTH 另記當時已知 VWAP 距離。每次接觸建立獨立 episode。

觀察三類證據：

- 主動壓力：`(buy-sell)/(buy+sell)`、每分鐘 delta 強度及同向成交持續性；零成交時記 unknown。
- 價格效率：同方向 tick 位移，相對已完成視窗的主動成交量；按商品、時段和歷史成交量分布標準化。
- 被動承接：接觸價帶的 passive fills、near-fill refill、相反側撤單與深度變化。先重用既有 cells/refill 定義，於少量 raw MBO 視窗核對順序。

候選假設：大量主動賣出、向下價格進展有限、買盤持續補回，且完成 K 線收回 VAL，之後向 value area 內部的路徑改善；做空鏡像獨立報告。

策略比較保留現有 Delta Absorption 的成本、單持倉與風險規則，一次加入一組確認。補單模式標為 absorption/refill proxy；原生 iceberg 的同 OrderID 刷新可另作 raw 資料核對，合成拆單的共同來源保持未知。

### B. 突破接受與流動性消耗：第二優先

同一組 VAH／VAL episodes 量測：完成 K 線在外側的停留、外側實際成交量占比、回測守住、同向 OFI/CVD、對手側深度消耗及 refill 減弱。

輸出三種狀態：`BREAKOUT_ACCEPT`、`RANGE_REJECT`、`WAIT`。狀態只能隨已完成資料更新。首次候選沿用既有兩根 K 線確認和風險距離；價格確認帶與停損參數分輪研究。

未來 5／15／30 分鐘方向調整報酬、MFE/MAE、先碰 ±1 個決策時 ATR 的順序作 outcome。未來路徑欄位只進 labels；雙邊同棒觸及保留 ambiguous。重疊 episodes 以同一市場日期群組處理。

VP 現有每日 edge lock 會影響可交易候選。先重播現行行為；episode 重試設計列為獨立行為變更研究，分別統計新增獲利、新增虧損和交易次數。

### C. 期權持倉集中與價格狀態：第三優先

首輪選兩組無方向特徵：履約價 OI 集中度／距集中區的正規化距離，以及近到期 OI 占比。保留舊 total-OI／put-call gate 作對照。後續才增加同合約 ΔOI 和成交量／前日 OI。

同合約 ΔOI 需匹配 symbol、expiration、strike、right，分開列出新上市、到期移出與真正持倉變化。0–60 DTE 滾動集合的增減另外記錄。

資料時點依供應商每列 publication timestamp；OI 一般於約 06:30 ET 發布前日收盤值。前日 EOD 加入已公布背景，當日 EOD 留給收盤後分析。[Theta OI 定義](https://docs.thetadata.us/operations/option_history_open_interest.html)

門檻使用過去資料的滾動分位數，並按星期、到期上市制度、方向與波動分組。QQQ→MNQ、SPY→MES 使用同時點正規化距離／報酬，ETF 履約價維持 ETF 座標。多年 OI 分析和短期 MBO 分析分開報告，兩者相交日期才可評估聯合增益。

### D. PI 方向與執行／隔夜延續：第四優先

先完成兩個獨立 portfolio：現有期貨 PI baseline；現有次日到期期權 ATM baseline。各自固定資金／風險預算、單持倉、相同成本，保留訊號互斥原因。

- 期貨：LV2 替換 LV3 與 reopen continuation 各自開關對照；kind 與 structured source level 分開，依 PI-014／PI-015 精確資格。
- 期權：先固定 next-session 10:00／15:30 兩種出場；ask 買、bid 賣，零 bid 保留經濟損失和成交失敗狀態；權利金不足時為零張。
- 有實收時間用實收時間；source-only 樣本加 1／5 分鐘延遲敏感度。期貨 18:00 重開與 ETF 期權開市各依自己的市場時鐘處理。
- 每日群組排除及最大獲利日集中度必報。3DTE／7DTE、跨到期 IV 及 tick 成交細節在覆蓋計畫完成後另列實驗。

## 5. 執行順序與產物

| 階段 | 工作 | 產物／完成條件 |
|---|---|---|
| P0 | 固定資料／preset／程式版本；檢查交易日、session、來源、timestamps；重現當前基準 | coverage matrix、source hashes、逐筆 parity；unknown／缺口有原因 |
| P1 | 建立 A/B 共用 episode 表，重用 footprint 與現有吸收計算 | 每個 episode 有決策時間、可用特徵、狀態、outcome、資料來源 |
| P2 | 同期比較價格確認、加入 MBO、加入 OI、聯合版本 | 配對淨損益、日期數、漏掉贏家／避開虧損、成本壓力、所有嘗試清單 |
| P3 | PI 期貨與期權各自單持倉重播 | 實際可執行 equity curve、延遲敏感度、零 bid／資金限制紀錄 |
| P4 | 凍結候選並前瞻模擬 | 第一輪 30 個完整 session 作操作檢查，再依獨立事件數／不確定性决定延長 |

P1 第一輪集中 MNQ RTH；MES、其他 session 作獨立對照。所有分支保留報告，參數調整與實驗次數明確記錄。P2 的比較在同一有效日期集合進行，同時展示全 baseline 和 unknown 資料覆蓋，防止將資料選樣效果歸入訊號效果。

## 6. 研究驗收

- 先逐筆 parity，再比較變更；使用共用 `robustness.segment_index()` 與資料載入器。
- 多年價格／OI 可重用歷史分段作回顧對照；已閱讀的 2024–2026 結果標示 retrospective。新驗證期從候選凍結後開始。
- 依市場日期聚類估計不確定性；跨日 outcome 重疊時使用日期區塊，分段邊界 purge 到最長持有視窗。QQQ／SPY 同市場日期保持同 fold。
- 報告交易數、獨立日期、淨期望值、PF、最大回撤、尾損、最大單日貢獻及逐年／逐月結果。
- 候選進入前瞻模擬的提案門檻：成本壓力後仍有正期望、配對增量跨多個時段／月份同方向、移除最大獲利日後保有正期望，鄰近參數結果穩定。樣本不足或信賴區間太寬時繼續研究。
- 期貨沿用已明確記錄的 14-tick 壓力情境並核對其費用定義；期權使用 bid/ask、每張費用及額外不利成交敏感度。兩者成本模型分開。
- 模擬中保留既有交易／風險約束。實盤候選另需逐事件可用性、行情 freshness、收訊延遲與執行路徑驗收。

## 7. 後續研究票範圍

OBSERVED：`backend/data/orderflow.py:175` 有 refill 等 bar 欄位，`:486` 輸出 refill，`:502` 輸出 schema；既有 `scripts/orderflow_context_combination_study.py` 與 `scripts/orderflow_microburst_study.py` 已有 imbalance、passive-rejection 和 absorption 研究。

INTENDED：在相同事件及成本下量測吸收／接受特徵的增量，再加期權背景。

EVIDENCE：本文件盤點、既有時段報告、VP 引擎 gate、PI 全 cohort 回放。

INVARIANTS：DATA-008、DATA-011、DATA-012、DATA-013、RES-001；PI 對照遵循 PI-014／PI-015。實作票開始前核對當時 invariant 內容。

BEHAVIOUR CHANGE：本規劃文件無交易行為變更。episode 重試及任何策略執行變更由獨立票定義和審批。

FILES ALLOWED：本輪只有本文件；後續研究腳本／測試以狹窄 allowlist 另列，production allowlist 為空。

TESTS：後續實作需覆蓋時間因果、同棒 ambiguous、unknown coverage、單持倉、真實正向事件及 baseline parity；本次文件交付採讀回核對。

ACCEPTANCE：P0–P2 可交付可重現的事件表與配對結果；獲利與否均保留完整結果，據此決定候選去留。
