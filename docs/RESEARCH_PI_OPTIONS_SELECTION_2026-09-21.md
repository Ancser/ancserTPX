# PI 訊號 → SPY/QQQ 期權回放：coverage checkpoint

日期：2026-09-21

## 結論

本機資料目前無法支持 1/3/7 calendar-DTE、下一交易日出場的真實期權績效統計。已取得的可成交 bid/ask P&L 計算數：0；可用於指定持倉測試的合格事件數：0。以下列出的 6 筆只有 QQQ 0DTE 報價日期重疊，尚未形成指定到期日及次日出場的合約回放。沒有計算或推估勝率、報酬、Profit Factor。

## 已查明的本機覆蓋

| 資料 | 已確認覆蓋 | 對本研究的限制 |
|---|---|---|
| PI 歷史訊號 | `pi_signals.json` 有 530 則訊息、527 個 signal marks，來源時間涵蓋 2026-03-05 至 2026-09-14 | 需把來源 `Level` 與顏色／kind 分開篩選；不能把 repost 時間當作下單時間 |
| PI runtime audit | 2026-09-10 至 09-18 的 received/callback 共 15 筆，queue accepted 3 筆、未接受 12 筆 | 沒有逐訊號 close-block 原因；這批資料不能標註成「被收盤擋住」或可成交時間 |
| QQQ 期權 | 208 個不重複交易日的 1 分鐘 0DTE CBBO，日期 2025-11-04 至 2026-09-02；同 208 日有 QQQ 1 分鐘 underlying bars；另有 210 個日檔 definitions | 只有 0DTE，無法回放持有至下一 RTH 的 1/3/7 calendar-DTE 合約。CBBO 是實際報價欄位；本次未逐筆配對事件、履約價、到期日及出場報價 |
| SPY 期權 | 在已檢視的本機 options 資料目錄中未找到 SPY OPRA/CBBO 報價檔 | SPY bid/ask P&L 無法計算 |
| QQQ 後續資料 | 來源 manifest 記錄 2026-09-03 CBBO 請求失敗：`403 license_not_found_unauthorized`；本機 QQQ chain 最後日期為 09-02 | 2026-09-14 至 09-20 沒有本機鏈資料；該週 runtime audit 也沒有深藍圈 source Level 2 訊號 |

## 已確認的事件日期交集

PI 歷史中 source Level 2 且 kind 為「深藍圈」共有 6 筆；這 6 個來源日期都落在本機 QQQ 0DTE CBBO 覆蓋日：

| 來源日期 | source Level | kind | QQQ 0DTE CBBO 日期檔 |
|---|---:|---|---|
| 2026-03-30 | 2 | 深藍圈 | 有 |
| 2026-06-05 | 2 | 深藍圈 | 有 |
| 2026-06-09 | 2 | 深藍圈 | 有 |
| 2026-07-02 | 2 | 深藍圈 | 有 |
| 2026-07-20 | 2 | 深藍圈 | 有 |
| 2026-07-24 | 2 | 深藍圈 | 有 |

這是日期層級的報價覆蓋交集，不代表已確認訊號後可成交的 option quote。原始 source `ts` 與 2026-09-09 的 repost `discord_timestamp` 是不同欄位；這 6 筆缺少可驗證的逐筆 runtime `received_at`，因此不能把 source-time 回放描述成當時 live 可成交結果。現有 0DTE 鏈也沒有下一交易日出場所需的非零 DTE 合約報價。故指定測試的合格樣本數仍為 0/6。

## 真實回放需要的最小資料與固定規則

1. **事件與時鐘：** 每筆 PI 原始 source timestamp、source Level、kind、標的映射，以及實際 `received_at`／audit 時間；保留原始時區。缺少 received time 的歷史訊號只能標為 retrospective，不能混進 live-parity 結果。
2. **期權與標的：** SPY、QQQ 在事件日的 OPRA 1 分鐘 CBBO（bid/ask、size、quote event/receive time）及完整合約 definitions（expiry、strike、put/call、multiplier），並有同步 underlying 價格。至少覆蓋入場後至下一 RTH 的 10:00 ET 和 15:30 ET 報價；expiry 要涵蓋各事件後 1、3、7 個 calendar days 的第一個可用到期日。週末／假日保留 calendar DTE 和實際 trading days，不能把兩者混用。
3. **預先固定的候選：** signal 後第一筆有效 quote 入場，quote age 最多 1 分鐘且 ask size 大於 0；買入按 ask、賣出按 bid。分別測試 1/3/7 calendar-DTE 與最近的 $3/$10 OTM call（以入場時 underlying 計算，選 strike 不低於 spot+offset）。10:00 與 15:30 是兩個配對的出場方案，各自統計，不能算成兩筆獨立訊號。使用 definitions 的 multiplier，費用另外列出。
4. **結果與風險：** 報告每一格的 quote coverage、拒絕原因、事件數、扣費勝率及 95% Wilson interval、Profit Factor、平均／中位 premium return、按 premium-risk 正規化的最大回撤。每個標的一次只持有一筆，SPY/QQQ 同時部位另做合併風險統計。張數按 `floor(每筆 premium-risk budget ÷ (entry ask × multiplier + 費用))`；若結果為 0 張則跳過。每筆 budget 目前待使用者提供。

這個選擇權損益回放只需同步 bid/ask、合約定義和標的價格；GEX、MBO、delta decay 不是計算真實買賣損益的必要欄位。Greeks 或 GEX 可在有覆蓋後加入分層研究，須和不含該欄位的基線固定比較。

## 待補輸入與完成狀態

- 使用者／主線尚待確認上週案例的精確日期、標的，以及「+$10」指標的點數、期權 premium 還是帳戶損益；目前本機上週 audit 沒有深藍圈 source Level 2 可供直接核對。
- 需要可查詢的 SPY 與 QQQ 多到期 OPRA 歷史 CBBO 和 definitions，並補齊相應 underlying、PI received-time audit。現有本機 QQQ 0DTE 檔不足以替代這些資料。
- 本 checkpoint 僅做既有本機 inventory 與已知事件日期交集整理；沒有跑回測、沒有做新的 quote-level 損益計算、沒有下載／購買資料，也沒有改 production code、preset 或 live 設定。沒有執行測試（本次只新增研究報告）。原有工作樹修改與未追蹤檔案均保留。
