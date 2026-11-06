# ancserTPX 行為不變量

> **實作可以換。不變量不能悄悄消失。**

這份清單存在的理由:2026-08-08 一次區塊替換靜默刪掉了 `pi_listener.py` 的
兩個正規表示式,`ast.parse` 通過、`import` 通過、142 個測試全綠 ——
但實盤 listener 會在收到第一個訊號時 crash。**「測試綠」在沒有覆蓋時等於沒有資訊。**

## 怎麼讀這份文件

每一條的 **Test** 欄只有兩種值:

- 檔名 → 這條有自動化保護,可以放心重構
- `UNPROTECTED` → **只有這份文件記得它**。動到相關程式碼前先補測試,
  或至少在 PR 說明為什麼不會破壞它

**只有一個目錄、一個檔案。** 不建 `docs/invariants/` 樹狀結構 —— 這個 repo 的
`docs/` 已經有 15 個過期的版本快照,再開五個目錄只會多五份會爛的東西。
能寫成測試的就寫成測試;文件不執行會腐爛,測試不執行會大聲失敗。

---

## EXEC — 送單與券商

| ID | 不變量 | Test |
|---|---|---|
| EXEC-001 | 內部 `side 1=Buy/2=Sell` → API `0=Bid/1=Ask`;兩個方向**不得映射到同一個值**。寫反不會拋例外,會用正確價格下反方向的單 | `test_broker_order_mapping.py` |
| EXEC-002 | 內部 `type 3=Stop` → API `type 4`。API **沒有 type 3**,不得送出 | `test_broker_order_mapping.py` |
| ~~EXEC-003~~ | **2026-08-31 使用者明確決定退休 Practice-only 限制：Main／Express 實盤就是預期用途。** `verify_practice_account()` 僅保留為未接線 helper；live start、terminal 與 `place_order()` 不得突然接入它並阻擋實盤帳戶 | `test_broker_order_mapping.py`（helper only） |
| EXEC-004 | 每筆 live limit / market entry **必須同一個 request 附帶**策略的 `stopLossBracket` 與 `takeProfitBracket`,使用相對 entry 的有號 tick offset;不得等成交後才嘗試新增 Position bracket | `test_exec_protection_invariants.py` |
| EXEC-005 | 成交後只掃描 attached Auto OCO 建立的子單 ID,再以 `modify_order()` 校準到策略絕對價位;**不得另開第二組**獨立 SL/TP | `test_exec_protection_invariants.py` |
| EXEC-006 | 沒有市價參考時**必須拒絕下單**(return),不是只記 WARN | `test_exec_protection_invariants.py` |
| EXEC-007 | `/api/Order/search` 必須帶 bounded UTC `startTimestamp`(及 `endTimestamp`);查單 HTTP 400 不得令已成交 entry 永遠卡在 pending、跳過保護同步 | `test_broker_order_mapping.py` |
| EXEC-008 | `EXECUTE TRADES` 以 broker `Trade/search` 為真相；磁碟 cache 不得無限過期。打開分頁必須立即強制刷新，保持可見時要以有界頻率補抓，錯過 open→flat transition 也不得永久漏掉真實成交 | `test_trade_history_refresh.py` |
| EXEC-009 | 單筆口數只由 `backend/strategy/position_size.py::entry_contracts` 決定(`downsize_risk_dollars`:滿口規劃風險超過上限 → 1 口,不跳單、不改 SL);回測與 live 對同一訊號口數相同。live 一切觸及持倉的路徑(Auto OCO 子單辨識、改價、緊急平倉、5 分鐘口數安全檢查、紀錄)用 `_position_size`,不可用設定值 `contract_size`,否則 1 口持倉會被誤判而緊急平倉 | `test_position_size_downsize.py` |

## EXIT — 共用出場契約

| ID | 不變量 | Test |
|---|---|---|
| EXIT-001 | 每個被引擎接受的 `TradeSignal` 必須攜帶由唯一 `resolve_exit_policy()` 解析的 immutable `ExitPolicy`；模型不得在 Live／Backtest 內各自重算另一套 max-hold、hard TP 或 trailing 模式 | `test_exit_policy_unification.py` + `test_single_definition.py` |
| EXIT-002 | 持倉中的時間出場、單次 trailing 與 R-ladder 只能由唯一 pure `evaluate_exit_operation()` 回傳 `CLOSE`／`MOVE_SL`／`NONE`；相同 signal、market state 與 elapsed time 在 Live／Backtest 必須得到相同 operation 與 stop price | `test_exit_policy_unification.py` + `test_pi_directional_exits.py` + `test_option_wall_strategy.py` |
| EXIT-003 | Backtest 與 Live 保留不同 execution adapter：Backtest 對 K 棒模擬成交，Live 只把共用 operation 轉成既有 `flatten_now()` 或 attached Auto OCO `modify_order()`；共用 exit 不得管理手動／未追蹤倉位，也不得另造第二組保護單 | `test_exit_policy_unification.py` + `test_exec_protection_invariants.py` + `test_live_manual_guardian_integration.py` |

## LIVE — 執行期

| ID | 不變量 | Test |
|---|---|---|
| LIVE-001 | 熱身**不得下單**:餵歷史 K 棒只更新 detector / breakout 狀態,不得產生任何進場。歷史 K 棒必須先依時間排序(API 回傳是新到舊),否則 breakout 狀態是亂序建出來的 | `test_live_warmup_safety.py` |
| LIVE-002 | 沒有歷史 K 棒時熱身以 **error** 記錄並跳過,不得當成正常路徑靜默通過 | `test_live_warmup_safety.py` |
| LIVE-007 | session 不允許交易的時段,熱身必須重置 breakout 確認狀態(`_reset_breakout_confirmation`),不得讓跨 session 的殘留狀態影響第一根實盤 K 棒 | `test_live_warmup_safety.py` |
| ~~LIVE-003~~ | ~~Zone 超過 12 小時視為過期,不得用來下單~~ **這條從來不存在**。0.17.0 有過一段 `zone_age` 程式碼,但它是 **>24h 的 logger.warning,不是 block**,而且在 1.0 重寫時刪掉了。我把記憶裡「zone 老化」與「檢查要 BLOCK 不要 WARN」兩件事混成一條不存在的不變量。**現行架構沒有任何 zone 時效閘門** —— 那是待評估的提案,不是既有保護 | *不適用(見下)* |
| LIVE-004 | 每日風控休息(虧損單數 / 贏單數 / PDPT)= **只停開新單**。既有部位由 SL/TP/trail 自然了結,**不得強制平倉**。閘門是 `>=`,0 代表停用 | `test_live_daily_rest.py` |
| LIVE-005 | 交易日邊界走 `zoneinfo` 的真實 CME 日曆(週日 18:00 ET → 週五 17:00 ET,每日 17:00–18:00 維護),不是寫死的 UTC 22:00 | `test_live_daily_locks.py` |
| LIVE-006 | 每日虧損鎖觸發後不得再開新倉 | `test_live_daily_locks.py` |
| LIVE-008 | `/live/stop` 未確認成功時不得顯示 STOPPED 或清除 live loop;舊 RUNNING response 不得越過 stop generation,失敗必須標示 STATUS STALE 並恢復 bounded polling | `test_live_status_frontend_contract.py` |
| LIVE-009 | 歷史資料 fetch 不得 disconnect/replace 已被 running 或 starting live engine 擁有的 client/contract;每個 start reservation 必須 ref-counted,history-only client 必須關閉 | `test_historical_range_cache.py` |
| LIVE-010 | 同一帳號跨 web/terminal 只能有一個 live engine；啟動競態必須在產生第二個 PI callback 前拒絕 | `test_engine_lease.py` + `test_historical_range_cache.py` |
| LIVE-011 | 手動／未追蹤倉位屬於外部所有權：只阻擋 Bot 新開倉；Bot 不得替它掛、改、撤 SL/TP，不得因盤末、max-hold、trailing 或缺 Auto OCO 自動平倉。倉位消失後可恢復訊號；明確 `/live/flatten` 仍由使用者控制 | `test_live_manual_guardian_integration.py` |
| LIVE-012 | The live engine persists each completed execution in `trades.json` with execution, order, cost, and parameter-snapshot fields, but does not persist decision-reason/explain fields there. `strategy_snapshots.jsonl` remains the canonical strategy-parameter record; broker trade history and `live_exits` remain operational sources for execution, PnL, and exit correlation. | `test_live_daily_locks.py` + `test_shadow_replay.py` |
| LIVE-013 | 獨立 live 程序(opt-in:`.env` 的 `ANCSERTPX_SEPARATE_LIVE_PROCESS=true`):UI 只轉送 `/live/start|stop|cancel-pending|flatten|status|status-all`,轉送在 UI 自己的 localhost/CSRF 檢查**之後**;host 只綁 127.0.0.1、每次啟動隨機 token、帶 `Origin` 的請求一律拒絕;host 用的是 routes 同一組 handler(lease、warm-up、安全檢查不分叉)。host 不寫 candle store、不跑 accumulator/MBO recorder/shadow replay,並以 `ANCSERTPX_PI_RECORDER=off` 關掉 PI record-only worker;UI 依 host 的 PI 引擎暫停/恢復自己的 recorder,讀不到 host 時維持原狀態(同一時間只有一個 Discord poller)。`delta_absorption` 在 host 被拒(會開第二條 Databento 付費串流)。狀態讀取不會啟動 host;沒有 host 時回應與「無引擎」相同。host 在 UI 結束後繼續運作(Windows 上子程序本來就不隨父程序結束,此測試釘的是性質),沒有引擎在跑且 10 分鐘無請求才自行結束;`stop_legacy_instances.ps1` 的樣式不得匹配 `backend.live_host` | `test_live_host.py`(含兩個真實程序測試) |
| LIVE-014 | O1：Daily start 必須回報啟動狀態和錯誤；引擎啟動前的失敗限時最多三次，已進入引擎啟動或結果未知時不自動重試；STOP 與設定 revision 使舊啟動無效 | `test_daily_start_reliability.py` |
| LIVE-015 | O2：terminal 控制狀態無法確認時暫停新進場並保留既有引擎與持倉管理，持續重試控制連線，確認 STOP 後停止；已送出的待成交單仍需以 broker 狀態確認 | `test_daily_start_reliability.py` |
| LIVE-016 | O3：每日自動更換 terminal 前，舊引擎須確認暫停進場及完成當前 tick；broker 持倉和委託查詢成功且為空才能停止，停止後再核對 broker；STOP、帳號歧義、逾時及狀態未知必須阻止新程序 | `test_daily_start_reliability.py` |
| LIVE-017 | 打開 App/Web 不得停掉 live terminal:`stop_legacy_instances.ps1` 只有帶 `-IncludeTerminal` 才比對 `backend.terminal_live`,且只有 `windows terminal.bat` 帶;`macOS web.command` 不 pkill terminal。(1.5.1:11-05 01:38、11-06 00:34 兩次被 App 啟動器強殺,無平倉、無停止狀態) | `test_windows_launchers.py`(含 `-DryRun` 對真實程序的測試) |

## CLOCK — 市場時鐘

| ID | 不變量 | Test |
|---|---|---|
| CLOCK-001 | Candle／order／trade timestamp 保存 UTC instant；ASIA/EURO/PRE/RTH/AH 的牆上時間必須用 `America/New_York` + `zoneinfo` 換算，不得寫死夏令 UTC 偏移 | `test_market_clock.py` + `tests/ui/market-clock.spec.js` |
| CLOCK-002 | Pending cancel 固定 15:30 ET、Bot-owned flatten 固定 15:45–18:00 ET；EST/EDT、12/31→1/1 都必須得到相同紐約規則 | `test_market_clock.py` |
| CLOCK-003 | 市場盤段時鐘不得改變其他來源的日界線：Topstep 風控日仍是 `America/Chicago` 17:00，PI replay filter 仍是 `America/Los_Angeles` 07:00 | `test_live_daily_locks.py` + `test_pi_pre_session_filter.py` |

## WEB — 本機控制平面

| ID | 不變量 | Test |
|---|---|---|
| WEB-001 | Production `backend.main` 與 Windows/macOS Web launcher 只可 bind `127.0.0.1`；不得監聽 `0.0.0.0` 或 LAN interface | `test_web_security.py` |
| WEB-002 | Web UI 與 API 是 same-origin；不得恢復 `CORS *`。非 localhost／loopback Host 必須在 route 前拒絕，production 白名單不得為測試 host 放寬 | `test_web_security.py` |
| WEB-003 | 所有 `/api` POST/PUT/PATCH/DELETE 必須同時具備 process-local HttpOnly session cookie、port-scoped CSRF cookie 及相同 CSRF header；跨 Origin 或缺 token 不得執行 route | `test_web_security.py` + `tests/ui/web-security.spec.js` |
| WEB-004 | Web 回應必須拒絕 framing、禁止 MIME sniff、限制 referrer/resource；OpenAPI/Swagger 預設關閉，只能以本機 development env 明確啟用 | `test_web_security.py` |
| WEB-005 | Root、API 與 `/static/` 前端資產必須回傳 `Cache-Control: no-store`；後端重啟後重新整理同一端口即可取得目前程式，不得依賴換 port 逃避舊快取 | `test_web_security.py` + `tests/ui/web-security.spec.js` |
| WEB-006 | Windows native launcher 以單一程序擁有 Uvicorn 與 WebView2；固定使用 loopback port、單實例鎖，關閉視窗會 graceful-stop engine/client；Windows cleanup 不得 broad-kill desktop App 或任意 loopback port | `test_desktop_launcher.py` + `test_windows_launchers.py` |
| WEB-007 | Native UI 必須週期性檢查自己的 `/api/health`；後端埠消失時保留頁面並把帳戶 avatar 標為紅色 error，恢復後還原先前連線狀態 | `tests/ui/live-status-health.spec.js` |

## PI — 外部訊號

| ID | 不變量 | Test |
|---|---|---|
| PI-001 | 美西 **07:00 之前**的訊號不得進入任何路徑(實盤 / 回測 / 圖表 / 研究)。那是 bot 對前一交易日標記的重播,佔 33% 訊息、49% 標記 | `test_pi_pre_session_filter.py` |
| PI-002 | 時區換算走 `zoneinfo`,PDT/PST 都要正確。寫死 UTC 偏移會讓冬令整段濾錯 | `test_pi_pre_session_filter.py` |
| PI-003 | `pi_long_only=True` 必須**壓過** `pi_signal_set` 與明確指定的 short kinds | `test_pi_pre_session_filter.py` |
| PI-004 | `size` 欄位是視覺資訊,**不得用於任何進場決策**。新格式的 `Level N` 必須先解析成獨立的 `level` 欄位;強弱軸仍是 `kind` + 明確 source level,不能把視覺大小當級別 | `test_pi_pre_session_filter.py` |
| PI-005 | 時戳無法解析時**放行**而非崩潰 —— 寧可多一則訊號也不要 listener 掛掉 | `test_pi_pre_session_filter.py` |
| PI-006 | Normal historical backtest/research/chart data must share the same filtered history via `backend/data/pi_history.load_rows()`; Live and an explicit same-day PI Backtest may add separately audited, **new-channel-only**, run-scoped rows without rewriting that history | `test_pi_single_source.py` + `test_pi_live_audit.py` |
| PI-007 | 重複的外部事件不得產生重複的策略動作；跨歷史／audit／repost 來源以「1m 來源時間 + 商品 + kind + source level」去重，不得只依賴 Discord message ID | `test_pi_single_source.py` + `test_pi_live_audit.py` |
| PI-008 | Short circle bubbles (LEVEL 1/2) are parsed/audited and may enter the strategy queue only when explicitly selected by `pi_short_levels`; legacy kind-only `紫圈` selection keeps both levels, while `size` remains non-decisional | `test_pi_pre_session_filter.py` |
| PI-009 | Web/terminal startup may backfill today/yesterday into the separate audit stream, but every parsed mark is record-only (no strategy callback); pre-session rows stay diagnostic-only and chart visibility ignores preset acceptance | `test_live_status_health.py` |
| PI-010 | The single active **new-channel** Discord listener's `received`/`recorded` rows may overlay Live and Backtest charts; the overlay must be deduped against history by 1m source time + symbol + kind + source level, and rendering never mutates immutable history | `test_ui_contracts.py` + `test_pi_live_audit.py` |
| PI-011 | An explicit PI Backtest may merge in-range **new-channel** audit rows into that run only (deduped by 1m source time + symbol + kind + source level and pre-session filtered); it never appends to `pi_history`, changes Live callbacks, or creates an order | `test_pi_live_audit.py` + `test_pi_pre_session_filter.py` |
| PI-012 | A single Discord message that parses to 2+ supported PI marks is an invalid aggregate/summary: no mark from it may reach the live callback, normal history loader, replay input, or chart audit overlay; the raw source may remain audit-only | `test_live_status_health.py` + `test_pi_live_audit.py` + `test_pi_pre_session_filter.py` |
| PI-013 | PI 的多／空時間出場必須各讀自己的參數並保持 live/backtest 一致:多單預設 `0=OFF`,空單預設 `60m`,明確的 `0` 不得被 API fallback 改回非零。Live 只可對 `_active_signal` 所有的 bot 倉位觸發既有 `flatten_now()`;不得碰手動倉位或另造 OCO 路徑 | `test_pi_directional_exits.py` + `test_param_default_consistency.py` |
| PI-014 | Reopen continuation is opt-in per selected PI kind/short level and defaults off. Only a bot-owned selected PI position reaching the 15:45 ET flatten arms one ticket; flat-window touch of its original SL/TP, a missed same-day 18:00 ET reopen, or expiry of the first five minutes cancels it. A claimed ticket permits at most one entry, rebuilds protection from the current ATR blend, and continues through standard entry/risk/OCO gates; Live persists the ticket per account/contract. The shipped `PI STRICT MNQ REOPEN` preset continues only 青π through both live paths | `test_pi_reopen_continuation.py` + `test_market_clock.py` + `test_param_default_consistency.py` + `test_ui_contracts.py` + `tests/ui/design-system.spec.js`(PI STRICT MNQ REOPEN loads into the LIVE panel) |
| PI-015 | LV2 replacement is an explicit opt-in. Only a fresh selected BUY 深蓝圈 with structured source Level 2 may replace an active bot-owned BUY 青π whose structured source level is exactly 3; visual `size`, missing levels, Level 1/2 active sources, shorts, manual positions, prior replacements, and reopen continuations are ineligible. The original position closes first and Live must confirm the configured contract flat before one fresh ATR-blend market entry uses the ordinary daily-risk and attached-OCO path; only the already-consumed same-session direction lock is bypassed | `test_pi_replacement.py` + `test_param_default_consistency.py` + `test_ui_contracts.py` |
| PI-016 | PI OPTIONS(QQQ/SPY 次一交易日到期 call)的成交與出場規則只有一份定義:`backend/options/pi_options.py` 的 `limit_price` / `limit_fill_index` / `exit_after`。MARKET = 進場分鐘的 ask;LIMIT −x% = ask×(1−x) 無條件捨去到 $0.01,之後第一個可買且 ask ≤ 限價的分鐘成交;停損以**實際觀察到的 bid** 賣出(含跳空),目標以目標價成交,否則在次一交易日 10:00 / 15:30 的 bid 出場;每口來回 $1.30;口數預設**每個訊號固定 1 口**(`contracts`,可選 N 口),`contracts=null` 才依預算取整口(權利金高過預算就跳過)。Backtest 頁的 `pi_options` 模型與 `scripts/pi_option_limit_entry_study.py` / `pi_option_exit_grid_study.py` 都呼叫這幾個函式,真實快取必須逐位重現已發表數字。期貨的 `/backtest/run` 與 `/live/start` 必須拒絕 `pi_options`(未知名稱會被正規化成 FACTOR,不能讓期權 preset 開出期貨引擎)。回測的 `option_rows` 列出**每一個被考慮的訊號**(成交與未成交,未成交附原因與限價),各列淨額加總等於總額;BACKTEST TRADES 表的表頭由各自的 renderer 設定(`renderOptionTrades` 用期權欄位,TIME = 訊號→出場,掛單成交時間跟著 ENTRY;`renderTrades` 還原期貨欄位) | `test_pi_options.py`(含 25605/17613/9353/57035/18098 與限價成交 45/38/29 的重現)+ `test_single_definition.py` + `tests/ui/design-system.spec.js`(PI OPTIONS model) |
| PI-017 | Robinhood 期權帳戶目前**只有 ALERT 模式,不送任何單**:讀 PI audit 串流(`recorded`/`received`),多單 深蓝圈(LV2)/青π(LV3)、QQQ/SPY、美東 09:30–16:00、每個商品每天第一則,產生一張 ticket(寫 `logs/pi_options_tickets.jsonl`);只收 5 分鐘內且在 runner 啟動後收到的訊號(重啟不得把歷史重播成 ticket)。到期日 = 下一個期權交易日(週末、共用美國假日表、**Good Friday**)。`save_account` 對 PAPER/LIVE 一律拒絕,直到 Robinhood MCP 連線做好並經使用者確認;這個帳戶與 TopstepX 期貨引擎互不共用狀態 | `test_pi_options.py` + `tests/ui/design-system.spec.js`(options account arms ALERT mode) |

## DATA — 蠟燭庫

| ID | 不變量 | Test |
|---|---|---|
| DATA-001 | Store 只增不減,永不截斷 | `test_candle_store.py` |
| DATA-002 | `merge()` 必須保留錨點:store 的錨點是權威,incoming 換了錨就平移 incoming,已存歷史一個字不動。且不得把正常 bar 修訂誤判成換錨 | `test_candle_store_anchor.py` |
| DATA-003 | 換月判準是 `instrument_id`,**不是跳幅大小**。用跳幅猜會把真實行情(2026-04-10 +69 點)當成換月抹平,而且不可逆 | `test_roll_detection.py` |
| DATA-004 | `load()` **兩條路徑**(快取命中與未命中)都要回傳淺拷貝。呼叫端會就地 `sort()`,共用 list 會污染快取 | `test_candle_store_anchor.py`(2026-08-08 修復:未命中路徑原本直接回傳快取本體) |
| DATA-005 | 缺口偵測不得把常態休市誤報:16:15–16:30 ET 收盤休止、盤外極短破洞、假日 | `test_candle_store.py` |
| DATA-006 | 完整 store(210MB/商品)不進版控；唯一正式資料根目錄是 sibling `ancserMarketData/source/futures/continuous_1m/`，`data/store/seed/` 只保留自家 TopstepX 開機種子，不含 Databento(授權) | `test_data_and_skin_policy.py` + `test_market_data_paths.py` |
| DATA-007 | API 載入/寫入 store、缺口掃描、frozen 推進、百萬根 merge/sort、workset slice/copy/publish **不得阻塞 asyncio event loop**;券商 I/O 維持 async,CPU/磁碟工作走 worker thread | `test_historical_range_cache.py` + `test_backtest_data_lifecycle.py` |
| DATA-008 | 回測資料由 backend token 綁定單一 immutable workset 與 resolved contract economics。Live tail 不得改寫已解析的回測輸入;新選擇必須釋放舊的大型 generation;range cache 只能使用 observed/validated coverage,且 store/seed generation 變更後不得命中舊快取 | `test_historical_range_cache.py` + `test_backtest_data_lifecycle.py` |
| DATA-009 | 啟動與 CONNECT 只能抓取最近增量；背景自動保存寫入去重 pending journal，不得為了保存而解包完整 MNQ/MES store。只有 backtest、store-only 的資料載入才合併 pending(寫回 pkl);圖表讀取(時間週期、拖到左界、前日 value area)只用 `candle_store.load_view` 把**比 store 最後一根更新**的 pending bar 疊在唯讀視圖上,不合併、不寫檔;未使用 MES 不加入背景追蹤 | `test_lazy_candle_store.py` + `test_accumulator_event_loop.py` + `test_historical_range_cache.py` + `test_columnar_store.py`(view 不寫檔)+ `test_chart_timeframes_endpoint.py` / `test_backtest_data_lifecycle.py`(圖表讀取呼叫 merge 即失敗) |
| DATA-010 | 所有 canonical MarketData 穩定檔案以 `F:\ancserQuant\ancserMarketData` 為唯一主來源；Windows installer 不得建立 E: 自動同步排程，`windows_market_data_sync.bat` 只供明確手動執行。同步使用暫存檔後原子替換且不刪除備份多出的檔案，只有完成驗證的遷移才可明確 `--prune`。大量 orderflow source/cache 是明確例外，只存 F 且任何手動 tree sync 都必須排除 | `test_market_data_paths.py` |
| DATA-011 | Databento MNQ order flow 的 canonical source 是逐 UTC 日的 MBO DBN，另保留不可由 MBO 重建的小型 definition/statistics/status；下載前每個請求必須重新報價，預設拒絕任何非零 usage cost。Chart/API 不得即時 replay raw DBN，只可按可見時間窗讀取預先產生的 RTH 1m gzip footprint cache；另有獨立 ALL-session 1m archive 供研究與稽核。MBO book state 只由 A/M/C/R 更新：F 只記被動成交、不得再扣一次深度，部分 C 只扣其 size，深度只能在 F_LAST 完整事件邊界取樣。schema v5 的每個價格 cell 額外保留買／賣 50–99、100–149、150+ 的實際成交量。全部 orderflow source/cache 均只存 F，不鏡像 E | `test_orderflow.py` + `test_market_data_paths.py` |
| DATA-012 | 每日 MBO settlement 只選上一個已完成的 Topstep Chicago 17:00 交易日，跳過週末／共用假日；以 DST-aware RTH 轉成 UTC 日請求。完整日檔必須通過 metadata、大小、SHA-256 manifest 及 schema v5 的 390 根連續 RTH bar 驗證；缺檔／失效檔才可報價下載，替換前舊檔必須保留 `.replaced-*` 可恢復副本。Live checkpoint 只作對帳，不得覆寫 historical truth；settlement source/cache/report 只存 F | `test_databento_orderflow_settlement.py` |
| DATA-013 | App-owned MBO recorder 對同一條訂閱保留所有收到的 ASIA/EURO/PRE/RTH/AH 事件，寫入獨立 UTC-day compact ALL cache 並在每根 bar 標記 New-York session；RTH cache 與 Delta 的 RTH 進場規則不變，資料保留不得偷偷擴大策略交易時段 | `test_orderflow.py` + `test_databento_mbo_recorder.py` |
| DATA-014 | 連續期貨 store 的每一分鐘只屬於一個合約:每根 bar 帶 `contract_id` 與 `price_adjustment`(原始交易所價 = 價格 − 調整值);換月帳本存在 sidecar `meta["contract_rolls"]`,由 `backend/data/contract_ledger.py` 決定分鐘歸屬。所有寫入(pending journal、save、merge)都先經過 `enforce_store_contract`,不屬於該分鐘合約的 bar 一律丟棄,不得拼接。換月採日曆規則(第三個星期五前 8 天,與 live 相同),偏移量只能由兩個合約同分鐘的實價量測,不得推估;`apply_contract_roll` 對同一次換月冪等。live 5 秒輪詢只寫入連線合約擁有的分鐘 | `test_contract_ledger.py` + `test_live_tail_contract_guard.py` |
| DATA-015 | D1：換月前建立校驗備份與交易紀錄；中途失敗或程序退出後，在下次讀寫前復原主資料、帳本與 pending；同版本的資料讀寫使用跨程序鎖；備份失效時拒絕讀寫 | `test_candle_roll_recovery.py` |
| DATA-015 | `Candle` 使用 slots(完整 MNQ store 238 萬根:每根約 545 B,原本約 1.7 KB)但 pickle 格式不變:`__getstate__` 仍輸出完整欄位 dict,舊版程式可讀新 store;缺欄位的舊 pickle 由 `__setstate__` 補回原本的預設值(`source="topstepx"`、`contract_id/price_adjustment=None`)。低基數欄位(symbol/interval/source/contract_id/price_adjustment)載入時共用物件,int 與 float 不得合併。不得往 Candle 實例動態掛屬性 | `test_candle_memory_layout.py` |
| DATA-016 | pkl 仍是 canonical store;`<store>.columns.npz` 是衍生 sidecar(`backend/data/columnar_store.py`),蓋上它所對應 pkl 的 generation(mtime_ns, size)。generation 不符、檔案損毀或 schema 不同一律忽略並從 pkl 重建,所以 sidecar 只可能慢、不可能給出與 pkl 不同的 bar;所有 Candle 欄位逐欄精確還原(真實 MNQ 238 萬根 0 差異)。快取的 generation 是唯讀 `ColumnarBars`:每次存取都產生新的 Candle,範圍讀取以 searchsorted 只物件化所選列;全歷史時間週期由 `aggregate_columns` 直接讀陣列,結果與 `aggregate_many` 相同。欄位載入進記憶體(不 mmap:Windows 不允許替換被 map 的檔) | `test_columnar_store.py` + `test_historical_range_cache.py` |
| DATA-017 | 換月規則維持日曆(第三個星期五前 8 天,使用者 2026-09-27 決定)。圖表的即時資料與 live 下單跟 TopstepX 的 active 旗標(`get_front_month_contract_id` 優先旗標;`get_active_contract_id` 只讀旗標、不退回日曆)。`/connection/status` 的 `contract` 回報圖表合約、日曆合約、Topstep active 三者;任一不一致時,圖表在時間週期按鈕左側、日期軸上方顯示共用的黃色 `tpx-warn` ⚠,hover 顯示明細。Topstep 旗標最多每 10 分鐘在背景查一次,狀態輪詢本身不打券商;查詢失敗不得讓狀態 API 失敗 | `test_contract_sync_hint.py` + `tests/ui/design-system.spec.js`(contract out of sync) |

## BACKTEST — 回測生命週期

| ID | 不變量 | Test |
|---|---|---|
| BT-001 | 單次 backtest 無論成功或失敗都必須發布 terminal `done`/`error`;歷史結果只保留 bounded scalar summary,不得常駐完整 trades/zones/equity;equity response 最多 5000 點且保留首尾。產品不再提供 cross-model sweep lifecycle | `test_backtest_data_lifecycle.py` + `test_product_scope.py` |

## CONFIG — 策略設定

| ID | 不變量 | Test |
|---|---|---|
| CONFIG-001 | 新策略**必須**加進 `FACTOR_PIPELINE_STRATEGIES` / `ZONELESS_STRATEGIES`(models.py)。漏加 = 回測與實盤靜默不一致(實測 PnL 差 33%)且慢 20 倍 | `test_strategy_pipeline_classification.py` |
| CONFIG-002 | dataclass 欄位預設會**蓋過**策略端的 `getattr(..., fallback)`。兩邊必須一致,只改一邊等於沒改 | `test_param_default_consistency.py` |
| CONFIG-005 | backtest 與 live 兩個引擎認得的 strategy_mode 必須**完全相同**;白名單裡不得有引擎不認得的殘骸;`FACTOR_PIPELINE_STRATEGIES` 成員必須實作 `observe()` | `test_strategy_pipeline_classification.py` |
| CONFIG-003 | 時間出場對 FACTOR/PMO preset 永久關閉(`factor_max_hold_bars=0`),不要重新加回這個控制項 | `test_param_default_consistency.py` |
| CONFIG-004 | 策略預設值與 preset 的往返序列化必須無損 | `test_strategy_defaults.py` |
| CONFIG-006 | `OPTION WALL / PRIMARY STRICT` 只可讀 entry-time 因果欄位與固定 gate，不得載入研究 artifact 的 PnL／未來路徑；v1 僅允許 MNQ、固定 RTH historical replay，沒有 causal live option feed 時 `/live/start` 必須明確拒絕 | `test_option_wall_strategy.py` + `test_strategy_pipeline_classification.py` |
| CONFIG-007 | 儲存、計算與 API instant 一律以 UTC 為基準；市場 session 使用 DST-aware New York、Topstep 日界使用 Chicago 17:00、PI source 使用 Los Angeles。前端圖表只在顯示時依 configured market timezone (America/New_York) 位移，不能受瀏覽器／作業系統時區影響。Production contract default 必須動態解析 front month，不得寫死 M26/U26 | `test_timebase_contract.py` + `test_market_clock.py` + `tests/ui/chart-catchup.spec.js` |

## UI — 前端

| ID | 不變量 | Test |
|---|---|---|
| ~~UI-001~~ | **2026-09-06 使用者明確決定退休語言切換：產品 UI 固定使用英文。** 舊的「皮膚不得移除語言切換」契約不再適用；English-only 行為由 UI-026 保護 | `test_data_and_skin_policy.py`（retirement contract） |
| ~~UI-002~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束stage 版面與 glass scene 重新布局;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-003~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束來源與光學複本狀態一致;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-004~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束optical-surface 只複製最近的 data-stage;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| UI-005 | 隱藏面板中的 DOM 量測回傳 0×0。動畫依賴 rAF,在背景分頁不會前進 —— 兩者都會讓驗證出現偽陽性 | 驗證紀律(CLAUDE.md「驗證的標準」);瀏覽器測試先斷言 `document.hidden === false`,例如 `tests/ui/chart-catchup.spec.js` session-divider |
| ~~UI-006~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束Precision Lens 內 Tier-1 材質;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-007~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束光學輸出不得取樣自己;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-008~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束切換 workspace 後 Glass 不得留在無效來源;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-009~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束switch 本地狀態不得觸發 scene 重建;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| UI-010 | 策略 canonical identity 固定為 `FADE/SIGMA/FACTOR/MOMENTUM/BETAFIB/PI/OPTION WALL/DELTA ABSORPTION/VOLUME PROFILE`;說明是分離且固定英文的 presentation | `test_ui_contracts.py` |
| ~~UI-011~~ | **2026-09-06 使用者明確決定退休語言 Glass presentation。** `UI_LANG`、locale storage/event、語言 thumb 與切換同步路徑全部移除；產品不再需要這條雙語不變量 | `test_ui_contracts.py`（retirement contract） |
| ~~UI-012~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束分層來源不得改變 shrink/refraction/motion;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-013~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束可見來源的高優先重取樣路徑;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| UI-014 | Chart Tools 只保留時間週期選擇(UI-033)、跳到最新與 Chart Layers;已退役的 auto-center provider/drag/latch 不得回流。預設圖表水平 framing 只在沒有既有 viewport 的首次資料載入時以最近 200 根 smooth window 的 logical midpoint 置中;回測結果、背景刷新與 resize 不得重新接管水平位置。垂直價格範圍不得使用固定 provider 鎖定。圖面拖曳(1.3.0 起,TradingView 式):時間走 Lightweight Charts 原生 pan(1:1 跟游標、原生慣性);水平為主的拖曳/雙指橫滑保持 auto-fit(上下自動調整);明確垂直的拖曳或雙指直滑(≥12px 且 ≥0.5×水平)把價格窗交給使用者(manual,經 autoscale provider 提供),之後跟游標移動;雙擊價格軸或「跳到最新」回到 auto。滑鼠滾輪與 pinch 仍是縮放。不得再依方向鎖單軸 | `test_ui_contracts.py` + `tests/ui/design-system.spec.js`(auto-fit / trackpad / mouse-wheel / double-click / diagonal / panning free / monotonic / holding) |
| ~~UI-015~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束拇指鏡片不得被遮蔽;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-016~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束Stage 複本只畫靜止材質;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-017~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束lens-up 材質共用 --switch-glass 時鐘;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| ~~UI-018~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束光學複本鏡像表單 property 狀態;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| UI-019 | RESEARCH robustness 的 Monte Carlo 百分位只能來自後端同一個 seeded bootstrap，並完整呈現 P5/P25/P50/P75/P95；摘要六欄等寬、Monte Carlo 必須排第一、P25–P75 內帶與外帶必須可區分，風險警示與動態 `?` 說明不得退回長篇行內文字 | `test_robustness.py` + `tests/ui/research-robustness.spec.js` |
| UI-020 | Research robustness charts use the backend's seeded path data: Monte Carlo exposes P5/P25/P50/P75/P95 at every replay step; Walk-Forward exposes separate 1/3, 2/3, and 3/3 paths in both charts with $1,000/$2,000 maxDD guides; Slippage is rendered as an organized table. The Topstep/XFA tables show sizes 1/2/3/5/10 and explain column parameters with help dots. Calendar BT/LIVE labels stay in normal HTML rows so responsive SVG scaling cannot squash the text. | `test_robustness.py` + `tests/ui/research-robustness.spec.js` |
| UI-021 | Workspace navigation is the native left icon rail (the Liquid Glass dock was removed in 1.3.0). The lower panel navigation remains the flat `.bottom-tabs` underline bar for Backtest Trades, Execute Trades, and System Log. The full realized PNL Curve belongs in Research and replaces the old month-only BT/LIVE curve. | `tests/ui/design-system.spec.js` + `tests/ui/research-robustness.spec.js` |
| UI-022 | Chart Layer choices persist in browser-local storage and survive reload. Stored values may override only known layer keys with real booleans; unseen/new keys keep their declared defaults. Restored state must seed the live switch markup so the first interaction after reload toggles from the visible state. | `test_ui_contracts.py` + `tests/ui/design-system.spec.js`(toggle/close/reopen + `chart layer choices survive a reload…`;restore 路徑放回 bug 會紅) |
| UI-023 | QQQ Option Wall, Put Wall, and Gamma Flip are continuous step lines within one New York RTH date but must start a new subpath after the 16:00 ET close; no wall may connect overnight into the next open. Per-session metadata and shared X coordinates are computed once per snapshot rather than once per series on every paint. The feed prefers real 5-minute demo payloads and may fill otherwise-missing purchased sessions only from point-in-time hourly feature rows, which must remain explicitly labelled hourly and use their supplied cadence rather than being presented or truncated as 5-minute data. A time gap between consecutive segments (a row clamped at the 16:00 close followed by an after-close snapshot) also starts a new subpath; only a same-time level change draws a (vertical) connector, never a diagonal (`_optionWallSeriesPath`). | `test_option_wall_demo.py` + `test_ui_contracts.py` + `tests/ui/chart-catchup.spec.js`(含不依賴時鐘的 diagonal 測試;全圖測試只在紐約 16:00 後幾分鐘執行時才會跨收盤) |
| UI-024 | The product UI/API must not expose cross-model sweep controls, result tabs, sweep preset generation, or the `/backtest/sweep` route. Normal preset save/load/use/delete controls and routes remain available. | `test_product_scope.py` + `test_ui_contracts.py` |
| ~~UI-025~~ | **1.3.0 退休:Liquid Glass 永久移除(使用者決定)。** 原本約束Glass 光學表面不得有裝飾框/specular;該引擎與其 DOM/CSS 已刪除,`test_ui_contracts.py::test_liquid_glass_is_fully_removed` 防止回流 | `test_ui_contracts.py`(retirement contract) |
| UI-026 | Product-rendered UI copy is canonical English only: no language toggle, translation map, locale observer, or alternate Chinese presentation; the document root remains `lang="en"`. | `test_data_and_skin_policy.py` + `test_ui_contracts.py` |
| UI-027 | Backtest and Execute Trades tables combine entry and exit into one TIME column, show a shared New York date once when both events share a day, use compact `10:30PM` times and `3hr 2m` durations, and display neither ID nor WHY; each complete trade row retains the 10 useful columns from SIZE through DIR. | `test_ui_contracts.py` |
| UI-028 | FOOTPRINT / LEVEL 2 is off by default. Enabling it requests only the visible chart window from the compact order-flow endpoint. When footprint data is available, the original candle series is hidden and each visible price cell renders one signed delta bar: positive (buy−sell) extends right in white, negative extends left in red; bar length follows `|delta|`, passive activity remains encoded by fill opacity, and the strongest bounded depth levels plus imbalance markers remain available. Overview view aggregates cells per screen bucket, keeps one dominant delta cell per screen column, applies a 50-contract delta minimum when available, and defers detailed labels. Panning is debounced; disabling the layer clears its canvas and restores candles | `test_orderflow.py`;**瀏覽器繪製檢查隨 glass-ui.spec.js 刪除,UNPROTECTED**(`tests/ui/chart-performance.spec.js` opt-in 仍量測繪製) |
| UI-029 | CVD / DELTA is off by default and reuses the visible-window order-flow request. The backend returns RTH cumulative aggressive delta (reset per RTH date), so panning does not restart CVD at zero; disabling the layer clears its canvas and it remains chart-only | `test_orderflow.py` |
| UI-030 | A stable footprint repaint must not reapply an unchanged candle visibility option. Price-coordinate reuse is paint-local only, preserving exact footprint pixels and refreshing after every pan/zoom/scale change; disabling footprint still restores the current candle series | **UNPROTECTED**:瀏覽器測試隨 glass-ui.spec.js 刪除;`tests/ui/chart-performance.spec.js`(opt-in)仍記錄 visibility writes |
| UI-031 | The native workstation design layer keeps a stable icon workspace rail/page/rail/panel/control hierarchy and shared semantic tokens; Research/Backtest/Live are icon-only vertical navigation, account connection and theme controls stay at the rail bottom, account status/time stay at chart upper-right, the legacy deep-blue palette is the persisted default dark palette, Apple Light is the persisted light palette, chart primitives and watermark follow the selection, production surfaces use straight edges, the empty title strip is absent, the default sidebar is expanded by 10% without horizontal page/panel overflow, buttons and inputs are borderless with lighter button/darker input surfaces, section titles keep only the left accent bar, trade-limit controls stack on separate lines, the main chart legend and minor-account surface are absent, and Chart Layers switches share one right-aligned column | `tests/ui/design-system.spec.js` |
| UI-032 | Delta Absorption chart evidence is model-scoped and chart-only: the response/live status exposes bounded `value_areas` plus typed `touched` (VAH/VAL), `delta_decay`, and `stalled` events; the UI renders them through independent persisted layers and does not mix the generic EMAPMO/PI/MREV signal canvas into an active Delta chart | `tests/test_delta_absorption.py` + `test_ui_contracts.py` |
| UI-033 | 圖表時間週期(1m/5m/15m/30m/1H/4H/D/W):非 1m 的 bar 一律由後端 `backend/data/timeframes.py` 單一定義聚合(4H 錨定紐約 18:00 開盤、D 為 Topstep trade date、W 為 trade-date ISO 週),前端不得自行聚合;1m 維持原本 live-append 路徑。高週期下 overlay/marker 經 `_chartTimeToCoordinate`/`_chartSnapTime` 落在包含它的 bar 內,live 只更新最後一根 bucket,不得把 1m bar 接進高週期序列;footprint/CVD 只在 1m 繪製;session 分隔線只在 ≤1H 顯示 | `test_timeframe_aggregation.py` + `test_chart_timeframes_endpoint.py` + `tests/ui/chart-timeframe.spec.js` |
| UI-034 | 訂單流研究圖層 `PASSIVE REJECT (RESEARCH)` / `MBO TURNOVER (RESEARCH)` 預設關閉,只**重播**最新 `institutional_behavior_frozen_refresh_*_trades.csv.gz` 的存檔列(`backend/data/research_events.py`,每個事件取 `split=all` 一次,另標 discovery/evaluation/latest),不計算、不修改任何訊號,也不進回測或 live。開啟時畫在 indicator 訊號層(箭頭 + 該筆淨損益),log 一定同時寫出該候選的評估期筆數、PF、14-tick PF;Delta 圖表模式下仍可顯示 | `test_research_events.py`(含對已發表數字 195 筆/PF 1.2173 的逐位重現)+ `tests/ui/chart-catchup.spec.js` |
| RES-001 | **走查分段只能有一份定義。** 所有保留的研究／RESEARCH 面板走查都必須使用 `robustness.segment_index()`；不得在 script、前端或另一個 backend 模組重建相似的三分段公式 | `test_robustness.py` + `test_single_definition.py` |

---

## 目前的覆蓋缺口(誠實版)

**93 條 active invariant(另 18 條已退休);其中 2 條標記 UNPROTECTED。** 1.3.0 刪除 Liquid Glass 時,
`tests/ui/glass-ui.spec.js` 一起刪除;它原本也蓋到 UI-022 reload(已在 design-system.spec.js 補回)、UI-028/030 footprint 繪製(目前只剩靜態或 opt-in 檢查)。
其餘瀏覽器行為由 `tests/ui/design-system.spec.js`、`chart-catchup.spec.js`、`chart-timeframe.spec.js`、
`research-robustness.spec.js` 在 Chromium 驗證;小型架構接縫另由 pytest static
contracts 快速擋回歸。CI 的 browser job 以 `--lifespan off` 啟動 app,不得啟動
candle accumulator / shadow replay / broker 連線。

補測試的過程本身抓到五個真問題:

```
routes.py            PI 預設的第三份真相 —— API 沒帶欄位時做空照樣開著(已修)
candle_store.load()  快取未命中時回傳快取本體,不是淺拷貝(已修)
pi_signal / routes   仍各自讀 json,PI-006 的「單一真相」名不副實(已收斂)
INVARIANTS.md        LIVE-001 指名一個不存在的函式(已修)
INVARIANTS.md        LIVE-003 整條是幻覺 —— 見下(已改為提案)
```

### ⚠️ 這份文件本身也會過期

初版的 LIVE-001 寫「熱身後必須呼叫 `reset_to_safe_state()`」—— 那個函式
**在現行程式碼裡不存在**,只存在於 `0.17.0` 那一代。我從舊筆記抄過來卻沒
先 grep 驗證。一份指名不存在函式的不變量清單比沒有更糟,因為它看起來權威。

**加任何一條之前,先確認它指名的函式/旗標/檔案現在真的存在。**

第二次:LIVE-003(zone 12h 過期)也是幽靈。查證後發現 0.17.0 只有
`if zone_age > 86400: logger.warning(...)` —— **24 小時、而且只是警告**。
兩條幽靈都來自「憑舊筆記寫不變量」。

### R0 — 保護單所有權更正(2026-08-17;取代 2026-08-11 結論)

2026-08-11 在尚未理解 bracket 參數時,把重複／遠端保護單歸因於 entry
attached bracket,並在 1.0.10n 改成純 entry。2026-08-17 的實盤證據推翻了
這個結論:PI BEST 已算出 SL/TP,但純 API entry 成交後沒有任何保護子單;
TopstepX 畫面同時明示 Position brackets 已停用、只能使用 Auto OCO brackets。

現行所有權是:live engine 在 limit 與 market entry request 內附帶唯一一組
Auto OCO bracket。成交後 `_scan_auto_oco_order_ids()` 只找這組 attached
children,再以 `modify_order()` 校準到策略 SL/TP;不得另開第二組獨立保護單。
這恢復 1.0.10n 以前的 entry 契約,但保留後來加入的 scan → modify 校準。

`test_exec_protection_invariants.py` 會執行兩條真實 entry path,鎖住
「entry 必帶 bracket」與「attached children 只走 scan → modify」兩個契約。

### 提案(不是不變量,尚未實作)

- **zone 時效閘門**:目前沒有任何機制阻止用很舊的 zone 下單。
  0.17.0 只警告不擋,1.0 連警告都沒了。要不要擋、門檻多少,是未決的設計問題。
  在決定之前不該假裝它是既有保護。

`backend/live/engine.py` 5,088 行、只有 2 個測試檔碰到;
`frontend/static/ancserTPX.js` 9,333 行、0 個測試。這兩個數字是這份清單裡
大部分 `UNPROTECTED` 的來源。

## 維護規則

1. 修好一個歷史 bug → 在這裡加一條,並把 Test 欄填上
2. 補了測試 → 把 `UNPROTECTED` 換成檔名
3. 刪掉一段看起來多餘的程式碼前 → 先在這裡搜一遍
4. **不要**把這份文件變成實作細節的複本。只記:**什麼必須為真、哪個測試證明它**
