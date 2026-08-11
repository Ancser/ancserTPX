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
| EXEC-003 | 非 Practice 帳戶必須拒絕;帳號查不到時**拒絕而非放行** | `test_broker_order_mapping.py` |
| EXEC-004 | 進場單**會**自帶 `stopLossBracket` / `takeProfitBracket`,內容是 `_entry_brackets_for_signal()` 依訊號價位算出的 tick 偏移。broker adapter 只在欄位非 None 時放進 payload | `test_broker_order_mapping.py` ⚠️ **語意待釐清,見 R0** |
| EXEC-005 | Auto OCO 括號**可以**用 `modify_order()` 改成策略價位。「不自己下 SL/TP」指的是不另開新單,不是不能改 | `test_exec_protection_invariants.py` |
| EXEC-006 | 沒有市價參考時**必須拒絕下單**(return),不是只記 WARN | `test_exec_protection_invariants.py` |

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

## PI — 外部訊號

| ID | 不變量 | Test |
|---|---|---|
| PI-001 | 美西 **07:00 之前**的訊號不得進入任何路徑(實盤 / 回測 / 圖表 / 研究)。那是 bot 對前一交易日標記的重播,佔 33% 訊息、49% 標記 | `test_pi_pre_session_filter.py` |
| PI-002 | 時區換算走 `zoneinfo`,PDT/PST 都要正確。寫死 UTC 偏移會讓冬令整段濾錯 | `test_pi_pre_session_filter.py` |
| PI-003 | `pi_long_only=True` 必須**壓過** `pi_signal_set` 與明確指定的 short kinds | `test_pi_pre_session_filter.py` |
| PI-004 | `size` 欄位是零資訊(圈類恆為「大」、π 的大小是視覺系統多餘分類),**不得用於任何進場決策**。強弱軸是 `kind` | `test_pi_pre_session_filter.py` |
| PI-005 | 時戳無法解析時**放行**而非崩潰 —— 寧可多一則訊號也不要 listener 掛掉 | `test_pi_pre_session_filter.py` |
| PI-006 | 回測、實盤、圖表、研究腳本必須看到**同一組**訊號。共用 `backend/data/pi_history.load_rows()` | `test_pi_single_source.py` |
| PI-007 | 重複的外部事件不得產生重複的策略動作 | `test_pi_single_source.py` |

## DATA — 蠟燭庫

| ID | 不變量 | Test |
|---|---|---|
| DATA-001 | Store 只增不減,永不截斷 | `test_candle_store.py` |
| DATA-002 | `merge()` 必須保留錨點:store 的錨點是權威,incoming 換了錨就平移 incoming,已存歷史一個字不動。且不得把正常 bar 修訂誤判成換錨 | `test_candle_store_anchor.py` |
| DATA-003 | 換月判準是 `instrument_id`,**不是跳幅大小**。用跳幅猜會把真實行情(2026-04-10 +69 點)當成換月抹平,而且不可逆 | `test_roll_detection.py` |
| DATA-004 | `load()` **兩條路徑**(快取命中與未命中)都要回傳淺拷貝。呼叫端會就地 `sort()`,共用 list 會污染快取 | `test_candle_store_anchor.py`(2026-08-08 修復:未命中路徑原本直接回傳快取本體) |
| DATA-005 | 缺口偵測不得把常態休市誤報:16:15–16:30 ET 收盤休止、盤外極短破洞、假日 | `test_candle_store.py` |
| DATA-006 | 完整 store(210MB/商品)不進版控;`data/store/seed/` 的開機種子只含自家 TopstepX 抓的資料,不含 Databento(授權) | `test_data_and_skin_policy.py` |
| DATA-007 | API 載入/寫入 store、缺口掃描、frozen 推進、百萬根 merge/sort、workset slice/copy/publish **不得阻塞 asyncio event loop**;券商 I/O 維持 async,CPU/磁碟工作走 worker thread | `test_historical_range_cache.py` + `test_backtest_data_lifecycle.py` |
| DATA-008 | 回測資料由 backend token 綁定單一 immutable workset 與 resolved contract economics。Live tail 不得改寫已解析的回測輸入;新選擇必須釋放舊的大型 generation;range cache 只能使用 observed/validated coverage,且 store/seed generation 變更後不得命中舊快取 | `test_historical_range_cache.py` + `test_backtest_data_lifecycle.py` |

## BACKTEST — 回測生命週期

| ID | 不變量 | Test |
|---|---|---|
| BT-001 | 單次與 sweep 無論成功或失敗都必須發布 terminal `done`/`error`;歷史結果只保留 bounded scalar summary,不得常駐完整 trades/zones/equity;equity response 最多 5000 點且保留首尾 | `test_backtest_data_lifecycle.py` |

## CONFIG — 策略設定

| ID | 不變量 | Test |
|---|---|---|
| CONFIG-001 | 新策略**必須**加進 `FACTOR_PIPELINE_STRATEGIES` / `ZONELESS_STRATEGIES`(models.py)。漏加 = 回測與實盤靜默不一致(實測 PnL 差 33%)且慢 20 倍 | `test_strategy_pipeline_classification.py` |
| CONFIG-002 | dataclass 欄位預設會**蓋過**策略端的 `getattr(..., fallback)`。兩邊必須一致,只改一邊等於沒改 | `test_param_default_consistency.py` |
| CONFIG-005 | backtest 與 live 兩個引擎認得的 strategy_mode 必須**完全相同**;白名單裡不得有引擎不認得的殘骸;`FACTOR_PIPELINE_STRATEGIES` 成員必須實作 `observe()` | `test_strategy_pipeline_classification.py` |
| CONFIG-003 | 時間出場對 FACTOR/PMO preset 永久關閉(`factor_max_hold_bars=0`),不要重新加回這個控制項 | `test_param_default_consistency.py` |
| CONFIG-004 | 策略預設值與 preset 的往返序列化必須無損 | `test_strategy_defaults.py` |

## UI — 前端與 Glass

| ID | 不變量 | Test |
|---|---|---|
| UI-001 | 套用任何皮膚**不得移除語言切換**。皮膚是外觀,不該拿掉功能 | `test_data_and_skin_policy.py` |
| UI-002 | Stage 版面變動必須讓被取樣的 glass scene 一起重新布局;只更新 surface 幾何不夠 | `tests/ui/glass-ui.spec.js` |
| UI-003 | 同一個邏輯控制項的所有呈現(來源 + 光學複本)必須顯示相同狀態 | `tests/ui/glass-ui.spec.js` |
| UI-004 | 每個 `.optical-surface` 會建一份整頁 DOM 複本。**不要在同一個面板裡放很多個** —— 12 個開關實測直接把 renderer 卡死 | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-005 | 隱藏面板中的 DOM 量測回傳 0×0。動畫依賴 rAF,在背景分頁不會前進 —— 兩者都會讓驗證出現偽陽性 | `tests/ui/glass-ui.spec.js` |
| UI-006 | Precision Lens 下方可取樣的 Tier-1 Glass(含 Chart Layers 與附近控制項)必須以已解析的材質/狀態出現在 Lens 內 | `tests/ui/glass-ui.spec.js` |
| UI-007 | 光學輸出不得取樣自己:Tier-1 只取 Tier-0;Tier-2 可取 Tier-0 + 合格 Tier-1,但不得取 Tier-2/self | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-008 | Research→Backtest/Live 在目的 workspace 可量測後,不得讓可見 Glass 留在無有效來源/1×1 canvas 的狀態 | `tests/ui/glass-ui.spec.js` |
| UI-009 | switch/button 的本地狀態更新不得觸發無關的大型 scene 結構重建 | `tests/ui/glass-ui.spec.js` |
| UI-010 | 策略 canonical identity 固定為 `FADE/SIGMA/FACTOR/MOMENTUM/BETAFIB/PI`;說明是分離且本地化的 presentation | `test_ui_glass_repair_contracts.py` + `tests/ui/glass-ui.spec.js` |
| UI-011 | 語言 Glass presentation 必須沿用唯一的 `UI_LANG`/storage/event 路徑,thumb 顯示目前語言且同步 `<html lang>` | `test_ui_glass_repair_contracts.py` + `tests/ui/glass-ui.spec.js` |
| UI-012 | 共用/分層來源不得改變各 component 原有的 shrink/refraction/motion 與本地材質所有權 | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-013 | 可見來源正確性有 bounded high-priority 路徑;背景結構 churn 仍走原本 deferred/batched scheduler | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-014 | Chart Tools 只保留跳到最新與 Chart Layers;已退役的 auto-center provider/drag/latch 不得回流,且預設圖表 framing 必須保留 | `test_ui_glass_repair_contracts.py` + `tests/ui/glass-ui.spec.js` |

---

## 目前的覆蓋缺口(誠實版)

**45 條目前都已有自動化保護。** UI-002…014 的 paint/timing 行為由
`tests/ui/glass-ui.spec.js` 在 Chromium 驗證;小型架構接縫另由 pytest static
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

### R0 — 保護單的所有權未釐清(2026-08-08,Codex 交接審查發現)

**初版的 EXEC-004 寫反了。** 它說「進場單不得自帶 bracket」,但實際上
`engine.py:3914 / 3999` 兩條進場路徑都帶:

```python
stop_loss_bracket, take_profit_bracket = self._entry_brackets_for_signal(signal)
OrderRequest(..., stop_loss_bracket=..., take_profit_bracket=...)
```

而 `_entry_brackets_for_signal()` 用的是**策略自己算的** SL/TP 價位換成
tick 偏移,不是帳戶預設值。ProjectX API 也確實支援這兩個 optional 欄位。

我當初那條測試是稻草人:它只斷言「OrderRequest 的欄位是 None 時 payload
不含 bracket」—— 那恆真,而且從來沒碰到引擎的實際路徑。

**同時**,`_scan_auto_oco_order_ids()` + `modify_order()` 那條路徑也還在
(engine.py:878–930),而且會等 Auto OCO 子單出現、超時就報 error。

所以現在有**兩套保護機制並存**,而沒有任何文件說明它們的關係:

- 進場時附帶的 bracket 是主要保護,scan+modify 是校正?
- 還是 attached bracket 是備援,Auto OCO 才是主要?
- 兩者同時生效會不會產生重複的子單?

**在釐清之前,不要依 EXEC-004 去刪任何 bracket 程式碼。** 刪掉附帶
bracket 會讓進場後到 modify 完成之間出現一段**沒有保護的裸倉**。

這需要對照實盤證據(實際的子單數量與價位)才能判定,不是讀程式碼能得出的。

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
