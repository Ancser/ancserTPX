# 1 分鐘價格行為 setup 研究

日期：2026-09-13

這是一份研究結果，不是 live 上線批准，也沒有改動 production strategy。

## 先回答「之前怎麼測」

之前並沒有把每一個 pin bar、inside bar、outside bar、engulfing、failed
breakout 都各自用同一套因果規則完整跑過一次。既有研究分成幾類：

| 既有研究 | 覆蓋內容 | 主要限制 |
|---|---|---|
| `public_strategies_MNQ.json` / `MES.json` | 716 個變體；ORB、VWAPREV、IBS、RSI2、GAPFADE、INTRAMOM、DONCHIAN、BBREV、ONCONT、SESSFIB | 是公開技術策略大類，不等於逐一 candle PA 形態；結果是當時的研究快照 |
| `prop_intraday_research_current.json` | 30 個 ORB、VWAP pullback、mean-reversion 設定，MNQ/MES | 主要是 prop intraday idea，不是完整 PA 形態庫 |
| `1.0.9_FADE_PROFESSIONAL_IDEA_SWEEP_REPORT.md` | 12 個 fade idea、2,304 個變體 | 只有約 269 個 2026 春夏 session，且部分是研究用輕量 simulator |
| `1.0.9_OPENING_SIGMA_FADE_REPORT.md` | 6,912 個 sigma/fade 變體 | 同一短資料窗大量掃描，正結果仍需未看過的資料驗證 |

所以先前的答案應該說「測過多個策略族」，不能說「所有 PA setup 都試過」。
這次才把 candle 型態、結構突破、回踩和前日極值 rejection 統一成同一個
可重現 harness。

## 為什麼採用這個研究框架

我沒有把任何書中的主觀描述直接當成訊號，而是採取以下原則：

1. Al Brooks 的價格行為材料把 prior highs/lows、breakout、failed breakout
   與 5 分鐘圖列為核心觀察對象；本研究把它們改成明確的 OHLC 條件，而不
   把「看起來像」留給回測者自行判斷。
2. Adam Grimes 的框架強調型態要放在買賣壓力／市場結構背景中，且需要
   統計驗證；因此本研究把「前方反向延伸」「局部極值」「20/50 EMA 趨勢」
   分別寫進規則，不只測孤立蠟燭。
3. David Aronson 的 *Evidence-Based Technical Analysis* 將 scientific
   method、統計推論和 data-mining bias 放在核心；所以結果不以最高 PnL
   單一排行決定。
4. Lo、Mamaysky、Wang 的研究指出，圖形若要研究就要先有自動化 pattern
   recognition 與 conditional distribution，而不是事後看圖挑例子。
5. Sullivan、Timmermann、White 的 Reality Check 工作說明，同時挑很多
   technical rules 時，必須把 data-snooping 影響算進去。本次因此預先固定
   setup library、退出倍數、成本和 acceptance gate。

書本／原始研究來源：

- [Brooks — Reading Price Charts Bar by Bar, Wiley](https://uat.store.wiley.com/en-us/reading-price-charts-bar-by-bar-the-technical-analysis-of-price-action-for-the-serious-trader-p-9780470464274)
- [Grimes — The Art and Science of Technical Analysis, Wiley](https://uat.store.wiley.com/en-us/the-art-and-science-of-technical-analysis-market-structure-price-action-and-trading-strategies-p-9781118238141)
- [Aronson — Evidence-Based Technical Analysis, Wiley](https://onlinelibrary.wiley.com/doi/book/10.1002/9781118268315)
- [Lo, Mamaysky & Wang — Foundations of Technical Analysis, Journal of Finance](https://onlinelibrary.wiley.com/doi/abs/10.1111/0022-1082.00265)
- [Sullivan, Timmermann & White — Data-Snooping and the Bootstrap, Journal of Finance](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00163)
- [Marshall, Young & Rose — Candlestick technical trading strategies, Journal of Banking & Finance](https://doi.org/10.1016/j.jbankfin.2005.08.001)

最後一篇很重要：它在 DJIA 股票資料上以 bootstrap 測試傳統 candlestick，
結果沒有發現 candlestick strategy 能創造價值。那不代表 MNQ 必然一樣，卻
代表「形態名稱本身」不能當成先驗 edge。

## 本次測試的 setup

資料是 canonical 1-minute store，先在 `America/New_York` RTH 09:30–16:00
內聚合 5 分鐘 K 棒。訊號只使用已完成的 5m K；下一根可用的 1m open
成交；同一 setup 的模擬同時只持有一筆，最多兩筆／日；15:50 ET 強制平倉。

每個 setup 同時測 long、short，以及固定 1R、1.5R、2R 三個 target。停損
放在結構極值外 2 ticks，單筆風險預算 $200，成本沿用程式的
commission + fees（每口 round trip = $1.24）。另外用 14-tick round-trip
滑價壓力測試。所有同一根 K 同時碰到 stop/target 的情況，沿用共用
`backend/backtest/intrabar.py` 的保守解法。

測試的 9 個 setup：

- pin rejection：反向延伸後掃過前 10 根局部極值，長影線收回。
- engulfing reversal：反向實體被當前實體包住，並位於局部極值。
- outside reversal：高低點同時包住上一根，收在反轉端。
- inside breakout：inside bar 後收盤突破 inside 邊界。
- NR7 breakout：最近七根中最窄一根後突破。
- failed breakout：刺穿前 20 根區間後收回區間。
- trend pullback：20 EMA/50 EMA 同向，回撤後重新越過前一根高／低。
- opening-range retest：09:30–09:45 ET 區間突破後回踩邊界。
- prior-day rejection：觸碰前一完整 RTH high/low 後收回。

## Acceptance gate

「rare」和「stable」是兩個不同標籤：

- rare：每 session 平均最多 0.20 筆，且至少 30 筆，避免把只有幾筆的
  偶然交易叫作少見優勢。
- stable：至少 40 筆；3 段等時間 walk-forward 每段 PnL/PF 都正；bootstrap
  的虧損機率 ≤5%、DD P95 < $2,000、PF P5 > 1；14t stress PF > 1；至少
  60% 的年度為正。

這是研究篩選門檻，不是統計學上的證明，也沒有把 108 個候選的最佳結果
冒充成事前顯著性。

## 結果摘要

資料覆蓋 2020-01 至 2026-09 的完整 RTH session：MNQ 1,668 個、MES 1,664
個。每個商品 54 個變體，合計 108 個；**108/108 都是 REJECT，0 個
RARE_STABLE，0 個 STABLE_NOT_RARE。**

以下每個 setup 顯示該商品中 14t stress PF 最高的版本；不是挑最高總 PnL。

### MNQ

| Setup | 最佳 side/R | n | 成本後 PnL | PF | 14t PF | 正年度 | WF | 判定 |
|---|---:|---:|---:|---:|---:|---:|:---:|---|
| pin rejection | Long / 2R | 235 | -$4,174 | 0.856 | 0.650 | 1/7 | No | REJECT |
| engulfing reversal | Long / 2R | 403 | -$7,038 | 0.847 | 0.665 | 1/7 | No | REJECT |
| outside reversal | Long / 2R | 1,045 | -$4,798 | 0.958 | 0.745 | 3/7 | No | REJECT |
| inside breakout | Short / 2R | 2,409 | -$21,446 | 0.920 | 0.718 | 1/7 | No | REJECT |
| NR7 breakout | Long / 2R | 2,957 | -$7,716 | 0.976 | 0.758 | 2/7 | No | REJECT |
| failed breakout | Long / 2R | 1,330 | -$17,482 | 0.890 | 0.676 | 1/7 | No | REJECT |
| trend pullback | Long / 2R | 1,914 | +$8,673 | 1.048 | 0.832 | 4/7 | No | REJECT |
| opening-range retest | Short / 2R | 295 | -$2,741 | 0.925 | 0.634 | 4/7 | No | REJECT |
| prior-day rejection | Long / 1R | 755 | -$10 | 1.000 | 0.758 | 3/7 | No | REJECT |

MNQ 的兩個比較少見 setup 是 pin rejection（Long 235 筆、Short 214 筆）
和 opening-range retest（Long 356 筆、Short 295 筆）。它們沒有通過成本和
時間穩健性。trend-pullback 2R 是表面上最接近可用的版本，但第三段
walk-forward PF 只有 0.896，bootstrap PF P5 約 0.968，14t PF 0.832，
不是穩定 edge。

### MES

MES 的 14t stress PF 最高的 setup 也沒有通過：

| Setup | 最佳 side/R | n | 成本後 PnL | PF | 14t PF | 正年度 | WF | 判定 |
|---|---:|---:|---:|---:|---:|---:|:---:|---|
| pin rejection | Long / 2R | 274 | -$9,483 | 0.741 | 0.233 | 1/7 | No | REJECT |
| engulfing reversal | Long / 2R | 588 | -$10,148 | 0.859 | 0.296 | 2/7 | No | REJECT |
| outside reversal | Short / 2R | 925 | -$4,002 | 0.963 | 0.390 | 4/7 | No | REJECT |
| inside breakout | Long / 2R | 2,752 | -$23,591 | 0.925 | 0.352 | 0/7 | No | REJECT |
| NR7 breakout | Long / 2R | 2,959 | -$21,057 | 0.940 | 0.344 | 2/7 | No | REJECT |
| failed breakout | Long / 2R | 1,307 | -$29,770 | 0.823 | 0.284 | 0/7 | No | REJECT |
| trend pullback | Short / 2R | 1,627 | -$14,318 | 0.917 | 0.424 | 2/7 | No | REJECT |
| opening-range retest | Short / 2R | 754 | -$3,721 | 0.961 | 0.327 | 4/7 | No | REJECT |
| prior-day rejection | Long / 2R | 797 | -$17,051 | 0.821 | 0.363 | 1/7 | No | REJECT |

## 判斷

### 1. 可以試很多種，但不應無限試到有漂亮結果

技術上可以把任何能由歷史資訊定義的 setup 寫成規則；但「9 個形態 ×
多空 × 3 個 target」已經是 108 次候選比較。若之後再把時間窗、停損、
session、濾網、EMA 長度、成交方式一起自由掃描，最佳值很容易只是
data-snooping 的產物。下一輪若要擴充，應先封存本次結果，再只在新的
holdout 日期測試預先寫好的假說。

### 2. 這批 OHLC 純 PA 沒找到可直接升級的 setup

「少見」不等於「穩定」：pin rejection 和 opening retest 確實較少出現，
但成本後與滑價後都不成立。反過來，MNQ trend pullback 2R 有正全樣本
PnL，但交易太頻繁、年度只 4/7 正、最後 WF 區段轉負，因此也不能作為
策略。

### 3. 舊研究中看起來很好的 PA idea 需要降級解讀

舊的 OR15 false-break、VA resting、opening sigma fade 在短期 2026 春夏窗
看過正結果；它們可作為假說，但不是已證實的跨 regime edge。它們的樣本窗
較短、部分變體數很多，而且尚未用本次完整 2020–2026 的同一執行規則
重新驗證。這和本次全樣本 108 組全部未過，並不矛盾，反而顯示舊結果很
可能包含 regime 或 selection effect。

## 資料限制

- MNQ sidecar 記錄 2026-06-11 的 268.5 點合約接縫，MES 記錄 2026-06-15
  的 63 點接縫；資料未被私自修正。跨接縫的研究結果要保守解讀。
- 每個商品有少數 2020-03 的 RTH 內 14–15 分鐘資料缺口；5m aggregation
  會跳過不完整的 5m signal bar，但持倉路徑仍是 OHLC 近似。
- OHLC 不知道一根 K 內 stop 和 target 的真實先後，且不能建模限價排隊；
  這是為什麼本研究採下一根 1m open 和保守同 K 解法，仍不能取代 tick／
  MBO replay。
- MNQ accumulated store 的 raw candle label 同時包含 MNQ/NQ source label；
  研究沿用 canonical continuous store 與現有 loader，沒有另外拼接合約。

## 產物

- 執行器：[scripts/price_action_study.py](../scripts/price_action_study.py)
- 可讀摘要：[price_action_study_current.csv](F:/ancserQuant/ancserMarketData/derived/research/price_action_study_current.csv)
- 完整 scalar／年度／WF／MC 結果：[price_action_study_current.json](F:/ancserQuant/ancserMarketData/derived/research/price_action_study_current.json)

下一步若要繼續，我建議不是把更多 candle 名稱塞進 live，而是挑一個事前
假說：「PA 只作 entry trigger，必須與既有 prior-value／PI／MBO absorption
context 同向」，在未看過日期上做 nested walk-forward；PA 形態單獨在目前
MNQ/MES OHLC 資料上沒有足夠證據。
