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
| EXEC-004 | 進場單**不附帶** `stopLossBracket` / `takeProfitBracket`;TopstepX 帳戶的 Auto OCO 建立唯一 SL/TP 子單,引擎只掃描並以 `modify_order()` 套用策略價位。broker adapter 仍可轉送其他 caller 明確提供的 optional bracket | `test_exec_protection_invariants.py` |
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
| LIVE-010 | 同一帳號跨 web/terminal 只能有一個 live engine；啟動競態必須在產生第二個 PI callback 前拒絕 | `test_engine_lease.py` + `test_historical_range_cache.py` |

## PI — 外部訊號

| ID | 不變量 | Test |
|---|---|---|
| PI-001 | 美西 **07:00 之前**的訊號不得進入任何路徑(實盤 / 回測 / 圖表 / 研究)。那是 bot 對前一交易日標記的重播,佔 33% 訊息、49% 標記 | `test_pi_pre_session_filter.py` |
| PI-002 | 時區換算走 `zoneinfo`,PDT/PST 都要正確。寫死 UTC 偏移會讓冬令整段濾錯 | `test_pi_pre_session_filter.py` |
| PI-003 | `pi_long_only=True` 必須**壓過** `pi_signal_set` 與明確指定的 short kinds | `test_pi_pre_session_filter.py` |
| PI-004 | `size` 欄位是零資訊(圈類恆為「大」、π 的大小是視覺系統多餘分類),**不得用於任何進場決策**。強弱軸是 `kind` | `test_pi_pre_session_filter.py` |
| PI-005 | 時戳無法解析時**放行**而非崩潰 —— 寧可多一則訊號也不要 listener 掛掉 | `test_pi_pre_session_filter.py` |
| PI-006 | Normal historical backtest/research/chart data must share the same filtered history via `backend/data/pi_history.load_rows()`; Live and an explicit same-day PI Backtest may add their separately audited, run-scoped rows without rewriting that history | `test_pi_single_source.py` + `test_pi_live_audit.py` |
| PI-007 | 重複的外部事件不得產生重複的策略動作 | `test_pi_single_source.py` |
| PI-008 | Short circle bubbles (LEVEL 1/2) are parser/audit record-only and never enter the strategy queue; `size` remains non-decisional | `test_pi_pre_session_filter.py` |
| PI-009 | Web/terminal startup may backfill today/yesterday into the separate audit stream, but every parsed mark is record-only (no strategy callback); pre-session rows stay diagnostic-only and chart visibility ignores preset acceptance | `test_live_status_health.py` |
| PI-010 | The single active Discord listener's `received`/`recorded` audit rows may overlay both Live and Backtest charts; chart rendering never mutates the immutable history file | `test_ui_glass_repair_contracts.py` |
| PI-011 | An explicit PI Backtest may merge in-range audit rows into that run only (deduped and pre-session filtered); it never appends to `pi_history`, changes Live callbacks, or creates an order | `test_pi_live_audit.py` + `test_pi_pre_session_filter.py` |

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
| UI-004 | 每個 `.optical-surface` 複製的是**最近的 `[data-stage]`**,不是整頁。switch 的 `data-stage="switch"` 就在按鈕上,所以一顆開關的取樣複本只有那顆開關(實測 66×28、0 個巢狀 optical layer)。放多個開關的代價是 SVG filter 數量,不是頁面複本數 —— 但仍要確認 stage 標在控制項本身,標到面板上就真的會複製整個面板 | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
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
| UI-015 | **拇指就是鏡片,任何情況都不可以被遮蔽。** `visibility` 會繼承,遮住 `.switch-thumb` 等於一起關掉裡面那層 `.optical-layer` —— 開關照樣能拖能點,但 active 狀態整個消失(只剩一顆綠藥丸) | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-016 | Stage 複本沒有 `.optical-layer`(複本在 `buildOpticalSurfaces()` 掛 layer **之前**就做好了),但 class 與 inline style 會被原樣鏡射進去。所以複本**永遠只能畫靜止材質**,不得繼承 lens-up 狀態,否則 Precision 會在活的拇指上蓋一顆純 `--bg` 藥丸。同理,控制項自己舉起鏡片時 Precision 必須讓開 | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| UI-017 | lens-up 材質是一組**成對**的東西(`--bg` 底 + `.optical-layer`),兩半都必須由 `--switch-glass` 驅動。綁到 `.interacting` class 上就會變成兩個時鐘:`apply()` 在拇指停止移動那一幀就拿掉 class,彈簧卻還要再跑 ~70ms,結果鏡片已經淡出、底色還停在 `--bg` —— 每次放手都閃一下黑 | `test_glass_sampling_contract.py` + `tests/ui/glass-ui.spec.js` |
| RES-001 | **走查分段只能有一份定義。** `sweep.py` 的評分與 RESEARCH 面板的走查必須呼叫同一個 `robustness.segment_index()`。1.0.8g 起 `sweep.py` 內嵌一份、1.1 又在 `robustness.py` 寫了第二份,兩者只靠一個「比對原始碼字串」的測試宣稱一致 —— 從未拿實際數字對過。同一個詞在兩條路徑上可能是兩件事 | `test_robustness.py` |

---

## 目前的覆蓋缺口(誠實版)

**49 條目前都已有自動化保護。** UI-002…017 的 paint/timing 行為由
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

### R0 — 保護單所有權已釐清(2026-08-11)

實盤事件確認重構後同一筆 entry 同時產生 attached bracket 與 TopstepX
Auto OCO 子單,造成重複保護、殘留遠端 SL/TP,並在短距離策略訊號上快速出場。
使用者確認 Auto OCO 是帳戶端唯一保護來源。因此兩條進場路徑現在只送純
entry;成交後由 `_scan_auto_oco_order_ids()` 找到既有子單,再以
`modify_order()` 套用策略 SL/TP。adapter 對其他明確 caller 的 optional
bracket 轉送能力保留,但 live engine 不再使用它。

`test_exec_protection_invariants.py` 會鎖住「entry 不帶 bracket」與
「Auto OCO 只走 scan → modify」兩個契約。

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
