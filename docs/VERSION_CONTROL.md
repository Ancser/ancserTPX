# ancserTPX 版本控制與工作紀錄

本文件參考 Age of Wealth 的 `Docs/VERSION_CONTROL.md`，集中記錄版本、待辦、
問題、修復紀錄與交接。交易行為以測試及現行程式為準；研究結果見
[`RESEARCH_REPORT.md`](RESEARCH_REPORT.md)，不變量見 [`INVARIANTS.md`](INVARIANTS.md)。
實際檢查與限制記在 [`VERIFICATION.md`](VERIFICATION.md)。

## 使用方式

1. 開工先讀本文件的待辦、負責人與交接，並依 `AGENTS.md` 和 `CLAUDE.md` 確認規則。
2. 每項需求或問題建立唯一編號，記錄使用者需求、狀態、優先級、證據、預期行為及驗收條件。
3. 修復完成後記錄修改檔案、具體行為、驗證結果、未完成的實盤驗證與提交版本；保留原問題與歷史紀錄。
4. 中途交接記錄已完成工作、未完成工作、風險與下一步。缺少驗證就標示「未驗證」。
5. 版本號與 commit 取自實際 Git 紀錄；未提交內容標示「工作區」。提交依專案及使用者指示執行。
6. 影響實盤的修改須遵守 `AGENTS.md` 的研究到 coding 交接格式，區分行為變更、允許修改的檔案與驗收。使用者批准的範圍優先。

## 已提交版本紀錄

| 版本／提交訊息 | commit | 日期 | 內容依據 |
|---|---|---|---|
| 1.5.1i CI skips private-data tests | `3bd1301` | 2026-11-09 | CI 沒有 ancserMarketData：Rust parity 與 Theta 回放測試缺資料時跳過（本機照跑）；GitHub 連續紅燈主因之一 |
| 1.5.1h Grok webhook forwarder | `79c4994` | 2026-11-09 | PI 事件轉發給 Grok Bot（只轉發、不過濾、不下單）；payload 標 received_at／catch_up／listener_role；測試無法打到真的 Bot |
| 1.5.1g UI renewal | `280578f` | 2026-11-09 | 大按鈕＋角記號；Live 主角損益、日虧損剩餘量表、異常才顯示；Backtest ADVANCED 收合；Research 候選卡片；動畫 |
| 1.5.1f live preset wide-stop 1 lot | `aa801c4` | 2026-11-06 | live preset `downsize_risk_dollars: 1000`；UI 欄位 |
| 1.5.1e UI polish | `5463bf2` | 2026-11-06 | 方形開關、主題 150ms、斑馬紋、移除 rail 品牌 |
| 1.5.1d downsize_risk_dollars | `2619296` | 2026-11-06 | 寬停損改 1 口（回測與 live 共用 `entry_contracts`，EXEC-009） |
| 1.5.1c trade history + chart speed | `976015e` | 2026-11-06 | DLL 鎖住的帳戶仍讀成交；30m 快取、5m/15m 初始 5000 根 |
| 1.5.1b test isolation + Rust stage 0 | `928f1f6` | 2026-11-06 | 測試不再碰真實 runtime；Rust parity 只釘 preset 內容；單一定義掃 .rs |
| 1.5.1a launcher keeps terminal | `3676a8b` | 2026-11-06 | 開 App 不再強殺 live terminal（LIVE-017） |
| 1.5 | `62eb95a` | 2026-11-05 | Git 提交訊息 |
| 1.4 | `7ab2829` | 2026-11-03 | Git 提交訊息 |
| 1.3.0 redo | `8985079` | 2026-09-27 | 本次審查的 Git HEAD；具體內容以提交為準 |
| 1.2.3 PI options and research engine checkpoint | `07aaeac` | 2026-09-25 | Git 提交訊息 |
| 1.3.1 something | `f63de66` | 2026-09-15 | Git 提交訊息；依實際提交順序列出 |

## 進行中版本：工作區（版本號待定）

**使用者原話（2026-10-27）：**「Open ticket for 1 2 3 Plan on reason how to fix and Just like ancserEntertainment age of wealth project document system to show all problem and patch fix note Do the same for this」。

| 編號 | 問題／範圍 | 優先級 | 狀態 | 負責／日期 |
|---|---|---|---|---|
| O1（原 OPS1） | Daily start 的啟動失敗、回報與安全重試 | 高 | 程式已修改；隔離驗證通過，Windows 排程及實盤待驗 | Codex，2026-10-27 |
| D1（原 DATA1） | 換月帳本與 K 線寫入的中斷一致性 | 高 | 程式已修改；隔離故障注入通過，正式換月待驗 | Codex，2026-10-27 |
| O2（原 OPS2） | Terminal 狀態監測失敗時的交易引擎處理 | 高 | 程式已修改；隔離驗證通過，實盤故障狀態待驗 | Codex，2026-10-27 |
| O3 | 每個交易日自動更換 terminal | 高 | 程式已載入；隔離驗證與運行中握手通過，首次排程切換待驗 | Codex，2026-10-27 |

### O3 — 每日更換 terminal（2026-10-27）

**使用者原話**：「Let every day open new one repalce one one」。

**現況**：Daily start 遇到已有 terminal 就跳過。**目標**：06:30 PT 有效交易日先使舊程序停止新進場並確認其空閒，核對券商持倉／委託為空後正常停止，再由排程開啟新預設；資料不明或有倉位／委託時保留原程序並顯示原因。使用者 STOP、單帳號 lease 和原有出場管理保持有效。

**影響**：實盤程序啟動時序變更（使用者已要求）；候選檔案 `backend/daily_start.py`、`backend/terminal_live.py`、相關隔離測試與說明。驗收涵蓋握手、未確認／持倉／掛單、API 失敗、同時 STOP、停機逾時、每日去重、正常啟動及原程式保持。

### O1 — Daily start 失敗可見性與安全恢復

**現況與原因**

- `backend/daily_start.py:233` 在啟動子程序前把 `last_day` 標記為當天；子程序啟動、認證或熱身失敗後，當天不再自動領取排程。排程已設 10 分鐘窗口與每日一次，原意包含防重複。
- `backend/terminal_live.py:801` 的缺少憑證分支在排程錯誤記錄區塊之前返回，可能留下空的 `last_error`。Windows task 成功呼叫 launcher 並不能單獨證明交易引擎已啟動。
- 既有程序存活或身分無法排除時，`terminal_statuses()` 會阻止新程序。啟動必須保留帳號 lease、防重複、使用者 STOP 持續關閉 Daily start 及 preset／account 驗證。

**預期修復方案（提案）**

1. 將排程狀態分為已領取、啟動中、引擎確認運行、失敗／結果未知，保留每次嘗試時間與簡短原因；憑證缺失等所有早期退出都回報。
2. 研究有限次數、有限時間的恢復：僅在舊程序及帳號 lease 已確認不存在，且仍在使用者啟用的當日窗口時嘗試；身分或訂單狀態不明時顯示待處理並保持單一引擎。STOP、設定 revision 或 preset／account 變更必須使舊嘗試失效。
3. App／Web 與 terminal 日誌顯示同一狀態；明確區分排程執行和交易引擎就緒。保持原時間、時區、假日與使用者停用語義。

**驗收**

- 隔離環境模擬成功、缺憑證、認證失敗、熱身失敗、子程序無回應、重複 tick、PID 重用、lease 被占、STOP／重設的競態，以及重新開啟 Daily start。
- 確認錯誤可見、同帳號最多一個引擎、使用者停止後持續停用；檢查新 preset 只在實際啟動並完成解析後標為生效。
- Windows 排程及 broker 端到端以隔離模擬或授權的非交易環境另做，記錄實際結果。不得用成功的排程呼叫代替引擎啟動證據。

**涉及檔案（候選，開工前確認 allowlist）**：`backend/daily_start.py`、`backend/terminal_live.py`、相關 API／UI 和隔離測試。**行為變更：是。**

### D1 — 換月資料一致性與中斷恢復

**現況與原因**

- `backend/data/candle_store.py:926` 先寫 `contract_rolls` metadata，再保存回調後的 K 線與清空 pending journal；進程或磁碟故障若出現在兩步之間，可能留下已換月帳本和舊價格資料。
- 既有 idempotency 檢查看到目標合約已在帳本中會跳過；不能單憑該欄位確定全部資料已完成。
- `save_meta` 直接寫檔；`_store_lock` 為程序內鎖。多程序同時寫入、換月與 append 時的排序需要明確保護。這些是程式碼所示的失敗情境；本次沒有發現實際損壞證據。

**預期修復方案（提案）**

1. 對帳本、K 線與 pending journal 設計可恢復的換月交易：先準備帶版本／校驗資料的候選檔與備份、記錄階段，再提交一致版本；每次啟動或讀取檢查未完成交易並安全恢復。具體提交順序與回滾策略在實作前確認。
2. 針對同商品的跨程序寫入加入可驗證的互斥及 generation 檢查；合約判定與實際寫入要受同一一致性保護，保留 append／merge 的既有去重與校價規則。
3. 在資料副本驗證原有歷史、合約歸屬、價差及 pending 對帳；保存可追溯備份與操作結果。避免對使用者正式 K 線做試驗。

**驗收**

- 使用臨時副本，在 metadata、K 線、pending、sidecar 每個寫入階段注入中斷／例外／空間不足；恢復後資料與帳本一致、操作可重試、價格不重複調整。
- 驗證跨程序並行換月／append／merge、同一分鐘不同合約、舊資料缺少合約標記、重複執行及舊格式相容。
- 對照回測資料切片及既有 DATA-001/002/003/004/009，不直接覆寫 canonical 資料做測試。

**涉及檔案（候選，開工前確認 allowlist）**：`backend/data/candle_store.py`、`contract_ledger.py`、`accumulator.py` 與隔離測試。**行為變更：是；修改正式歷史資料的操作需另行明確確認。**

### O2 — Terminal 監測故障與持倉管理

**現況與原因**

- `backend/terminal_live.py:815` 的控制監測每秒發布引擎狀態；遇到未處理例外會設定停止事件。
- `LiveTradingEngine.stop()` 停止 listener 與主循環並嘗試撤銷待成交單。持倉仍依賴券商端已有的保護單；自動時間出場、移動停損等引擎管理會停止。保護單是否存在及停止時是否成功須以實際 broker 回報核對。

**預期修復方案（提案，故障政策待確定）**

1. 區分短暫狀態庫故障、持續不可用與使用者 STOP。以有限退避恢復監測，顯示 stale／故障原因；不得把讀取失敗當成已確認 STOP。
2. 在控制狀態不確定期間，評估暫停新的自動進場並保留現有 bot 部位管理；用 broker 實際倉位和附帶保護單確認保護狀態。故障持續時的升級、人工介入與停止策略在實作前明確定義。
3. 真正的使用者 STOP 必須持續生效，並按照既有停止契約處理待成交單；不得重複下單或接管手動倉位。監測恢復與重啟需核對帳號 lease、待成交單和券商倉位。

**驗收**

- 注入 SQLite busy、短暫 I/O 錯誤、持續故障、狀態序列化錯誤及恢復；分別覆蓋空倉、bot 持倉、待成交與手動倉位。
- 驗證真實 STOP、STOP 與故障競態、保護單查詢失敗、進程重啟；確認無重複進場、無手動倉位接管，且狀態與日誌明確。
- 用 broker mock 和隔離狀態資料先驗證；實際券商與長時間運行狀態另行驗收。

**涉及檔案（候選，開工前確認 allowlist）**：`backend/terminal_live.py`、`backend/daily_start.py`、必要的既有風控介面及隔離測試。**行為變更：是。**

### U1 — UI renewal：Age of Dev v2 線條語言（2026-11-08，工作區）

**來源**：Age of Dev session 交接 `docs/UI_RENEWAL_FROM_AGEOFDEV.md`（使用者要求「除了光之外」的線條與版面語言）。

**使用者決定**：介面維持全英文（UI-026）；大按鈕灰底、中文名稱只當背景浮水印（約 20% 被底邊切掉）；主色維持青色；rail 維持圖示列。

**已完成（Phase 2a）**：共用契約 `.micro`／`.ticks`／`.hero-btn` 套到 EXECUTE BACKTEST 與 GO LIVE；狀態只寫在 `.hero-sub`（`setHeroButtonState`）；移除回測進度條發光；Phase 1 的 Playwright 舊斷言改為 v2；主題轉場檢查改為同步取樣。UI-026 加入浮水印例外並由 `test_ui_contracts.py` 限定範圍。

**驗證**：Playwright 截圖（深色／淺色、hover、角記號未被裁切）；design-system.spec.js 35/35；`overflow:hidden` 放回時斷言變紅。
既有紅燈（1.5 起即紅，非本項造成）：`chart-catchup` order-flow research layers、`chart-timeframe` switching to 1H。

**Phase 2–6（1.5.1h，工作區）**：使用者要求「do all fix」並採用公認設計方法（NN/g 漸進揭露、Few 儀表板例外凸顯、Tufte 小倍數、dataviz skill）。Live：FUTURES／OPTIONS 分頁、主角當日損益、日虧損上限剩餘量表（`dailyLossLimit` 單一定義於 topstep-eval.js）、異常才顯示的狀態標籤、Daily start 狀態標籤、狀態點去光。Backtest：ADVANCED 收合（計算隱藏數與相對 preset 的變更數）、`?` 懸停才顯示、PERFORMANCE 主角＋4 關鍵＋MORE METRICS。Research：候選卡片（判定標籤、3 關鍵數字、共用刻度的淨利／壓力長條）、Topstep 表中性化並標亮目前口數。動畫：分頁滑動選中塊、數字逐位滾動、工作區面板錯開淡入，皆遵守 reduced motion。

**未完成**：Phase 2b 頁面／彈窗四角、頁面標題（微標＋標題＋麵包屑）；Phase 5 Settings；訊號卡片。

### G1 — Grok Bot 訊號轉發（2026-11-09，`79c4994` 1.5.1h）

**使用者需求**：ancserTPX「只負責順便擔任數據轉發功能」，Grok Bot 自己決定怎麼用。官方唯一的程式觸發方式是 Grok Bot 例行任務的 webhook（回 200 只代表已排入，無法讀回結果）。

**行為**：新 PI 事件（含被拒的多標記訊息）POST 到 `GROK_PI_WEBHOOK_URL`；URL 空白即不動作。在 listener 迴圈外的執行緒送出，不影響輪詢與下單。每筆附 `received_at`、`catch_up`（啟動回補，可能是數小時前）、`listener_role`（`recorder`=App、`trader`=terminal，兩者同時開會送同一個 `message_id`）。

**修正的缺陷**：URL 一旦填入，跑一次 listener 測試就會送出 5 筆假訊號（4 筆 QQQ 青π 多單）。`tests/conftest.py` 讓三個變數在測試中存在但為空，`load_dotenv(override=False)` 無法回填。

**驗證**：5 個新測試（各自放回 bug 會紅）；本機假 webhook 探測 5→0；全套 986 passed。

**啟用**：把 Grok Bot 例行任務面板的 URL 與 Authorization 填進 `.env`，下次 terminal 啟動（每日 06:30 自動）生效；App 的 recorder 要重啟 App。

**未做（使用者決定不需要）**：轉發健康度上 Live 監控、伺服器端去重或過濾。

## 已完成與 patch 紀錄

| 日期 | 編號 | 內容 | 驗證／版本 |
|---|---|---|---|
| 2026-10-27 | DOC1 | 建立共用工作紀錄與 OPS1、DATA1、OPS2 待辦；整理原因、方案和驗收 | 文件建置；交易程式修復尚未開始。工作區，未提交。 |
| 2026-10-27 | O1 | 記錄啟動階段、缺少憑證及錯誤；引擎啟動前的已結束失敗在 10 分鐘內最多三次；啟動後或狀態未知保持單一程序並提示 | 隔離測試見 `VERIFICATION.md`；Windows 排程與 broker 實際啟動待驗。工作區，未提交。 |
| 2026-10-27 | D1 | 換月前校驗備份和建立交易紀錄；讀寫使用跨程序鎖；中途錯誤或程序退出後下次讀寫恢復，失效備份使讀寫停止 | 隔離故障注入及跨程序鎖檢查見 `VERIFICATION.md`；正式資料未執行換月。工作區，未提交。 |
| 2026-10-27 | O2 | 控制資料庫失效期間保留持倉管理、暫停新進場、持續嘗試恢復，確認 STOP 後停止 | 隔離測試見 `VERIFICATION.md`；券商已有待成交單可能成交，實盤故障處理待驗。工作區，未提交。 |
| 2026-10-27 02:49 PT | RUN1 | 使用者要求關閉舊 terminal、啟動新版本；先查券商倉位及委託，再正常切換並保留 Daily start | 新程序載入 `PI 2MNQ LV3 REOPEN BEST`、2 MNQ、5 秒 PI listener，啟動狀態 healthy；見 `VERIFICATION.md`。盤中訊號及實際執行待驗。 |
| 2026-10-27 02:55 PT | O3／RUN2 | 加入每日切換握手、停止前後券商核對及 STOP 保護；空倉重啟 terminal 載入握手程式 | 145 項隔離回歸通過，運行中的 terminal 已確認暫停及恢復握手；首次 06:30 實際排程切換待驗。 |

未來每個 patch 應在此增加一行，並在 `VERIFICATION.md` 記錄實際命令、結果、限制與相關 ticket。未驗證的項目保持待驗狀態。

## 交接（Codex，2026-10-27）

- O1、D1、O2 的工作區程式修復已完成隔離檢查；Windows 排程、正式資料換月與 broker 實盤情境待驗。
- 程式修復後、RUN1 切換前的 terminal 使用舊程式與舊 preset；受控重啟前先確認 terminal、帳號、部位、保護單及使用者設定。
- D1 跨程序鎖要求所有讀寫者載入本次修改；已運行的舊程序未受新鎖保護。備份會增加磁碟用量；實際斷電及檔案系統持久性仍待驗。
- O2 封鎖新送單；已送出的限價單可能在故障期間成交，需查詢 broker。故障期間 STOP 無法寫入控制資料庫時，其結果要保持未知直到核對。
- 2026-10-27 審查時運行中的 terminal 載入 `PI 2MNQ BOTH BEST`；儲存預設及 Daily start 選用 `PI 2MNQ LV3 REOPEN BEST`。程序啟動後的實際載入情況應重新查證。
- 2026-10-27 02:49 PT 使用者要求切換；舊 terminal 已正常停止，新 terminal 已確認載入新 preset 及 O1／O2 程式；Daily start 維持啟用 06:30。此為當時快照，之後狀態需重新查證。
- 2026-10-27 02:55 PT 再次正常重啟以載入 O3 握手；已在空倉時確認暫停與恢復，首次實際 06:30 自動替換待驗。未知狀態需檢查 broker 和 terminal，再由使用者重新開啟 Daily start。
- 既有工作區含大量未提交及未追蹤檔案，保留全部使用者變更；修改尚未提交。
