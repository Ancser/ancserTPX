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
| EXEC-004 | 進場單不得自帶 `stopLossBracket` / `takeProfitBracket` —— 帳戶端 Auto OCO 會自建,重送會變成重複保護單 | `test_broker_order_mapping.py` |
| EXEC-005 | Auto OCO 括號**可以**用 `modify_order()` 改成策略價位。「不自己下 SL/TP」指的是不另開新單,不是不能改 | `UNPROTECTED` |
| EXEC-006 | 沒有市價參考時**必須拒絕下單**(return),不是只記 WARN | `UNPROTECTED` |

## LIVE — 執行期

| ID | 不變量 | Test |
|---|---|---|
| LIVE-001 | 熱身**不得下單**:餵歷史 K 棒只更新 detector / breakout 狀態,不得產生任何進場。歷史 K 棒必須先依時間排序(API 回傳是新到舊),否則 breakout 狀態是亂序建出來的 | `test_live_warmup_safety.py` |
| LIVE-002 | 沒有歷史 K 棒時熱身以 **error** 記錄並跳過,不得當成正常路徑靜默通過 | `test_live_warmup_safety.py` |
| LIVE-007 | session 不允許交易的時段,熱身必須重置 breakout 確認狀態(`_reset_breakout_confirmation`),不得讓跨 session 的殘留狀態影響第一根實盤 K 棒 | `test_live_warmup_safety.py` |
| LIVE-003 | Zone 資料超過 12 小時視為過期,不得用來下單 | `UNPROTECTED` |
| LIVE-004 | 每日獲利休息 = **只停開新單**。既有部位由 SL/TP/trail 自然了結,**不得強制平倉** | `UNPROTECTED` ⚠️ |
| LIVE-005 | 交易日邊界走 `zoneinfo` 的真實 CME 日曆(週日 18:00 ET → 週五 17:00 ET,每日 17:00–18:00 維護),不是寫死的 UTC 22:00 | `test_live_daily_locks.py` |
| LIVE-006 | 每日虧損鎖觸發後不得再開新倉 | `test_live_daily_locks.py` |

## PI — 外部訊號

| ID | 不變量 | Test |
|---|---|---|
| PI-001 | 美西 **07:00 之前**的訊號不得進入任何路徑(實盤 / 回測 / 圖表 / 研究)。那是 bot 對前一交易日標記的重播,佔 33% 訊息、49% 標記 | `test_pi_pre_session_filter.py` |
| PI-002 | 時區換算走 `zoneinfo`,PDT/PST 都要正確。寫死 UTC 偏移會讓冬令整段濾錯 | `test_pi_pre_session_filter.py` |
| PI-003 | `pi_long_only=True` 必須**壓過** `pi_signal_set` 與明確指定的 short kinds | `test_pi_pre_session_filter.py` |
| PI-004 | `size` 欄位是零資訊(圈類恆為「大」、π 的大小是視覺系統多餘分類),**不得用於任何進場決策**。強弱軸是 `kind` | `test_pi_pre_session_filter.py` |
| PI-005 | 時戳無法解析時**放行**而非崩潰 —— 寧可多一則訊號也不要 listener 掛掉 | `test_pi_pre_session_filter.py` |
| PI-006 | 回測、實盤、圖表、研究腳本必須看到**同一組**訊號。共用 `backend/data/pi_history.load_rows()` | `UNPROTECTED` |
| PI-007 | 重複的外部事件不得產生重複的策略動作 | `UNPROTECTED` |

## DATA — 蠟燭庫

| ID | 不變量 | Test |
|---|---|---|
| DATA-001 | Store 只增不減,永不截斷 | `test_candle_store.py` |
| DATA-002 | `merge()` 必須保留錨點,防止換月接縫(9 月 U26→Z26)產生假跳空 | `UNPROTECTED` ⚠️ |
| DATA-003 | 換月判準是 `instrument_id`,**不是跳幅大小**。用跳幅猜會把真實行情(2026-04-10 CPI +69 點)當成換月抹平 | `UNPROTECTED` ⚠️ |
| DATA-004 | `load()` 回傳**淺拷貝** —— 呼叫端會對結果就地 `sort()`,共用 list 會污染快取 | `UNPROTECTED` |
| DATA-005 | 缺口偵測不得把常態休市誤報:16:15–16:30 ET 收盤休止、盤外極短破洞、假日 | `test_candle_store.py` |
| DATA-006 | 完整 store(210MB/商品)不進版控;`data/store/seed/` 的開機種子只含自家 TopstepX 抓的資料,不含 Databento(授權) | `UNPROTECTED` |

## CONFIG — 策略設定

| ID | 不變量 | Test |
|---|---|---|
| CONFIG-001 | 新策略**必須**加進 `FACTOR_PIPELINE_STRATEGIES` / `ZONELESS_STRATEGIES`(models.py)。漏加 = 回測與實盤靜默不一致(實測 PnL 差 33%)且慢 20 倍 | `test_strategy_pipeline_classification.py` |
| CONFIG-002 | dataclass 欄位預設會**蓋過**策略端的 `getattr(..., fallback)`。兩邊必須一致,只改一邊等於沒改 | `UNPROTECTED` |
| CONFIG-005 | backtest 與 live 兩個引擎認得的 strategy_mode 必須**完全相同**;白名單裡不得有引擎不認得的殘骸;`FACTOR_PIPELINE_STRATEGIES` 成員必須實作 `observe()` | `test_strategy_pipeline_classification.py` |
| CONFIG-003 | 時間出場對 FACTOR/PMO preset 永久關閉(`factor_max_hold_bars=0`),不要重新加回這個控制項 | `UNPROTECTED` |
| CONFIG-004 | 策略預設值與 preset 的往返序列化必須無損 | `test_strategy_defaults.py` |

## UI — 前端與 Glass

| ID | 不變量 | Test |
|---|---|---|
| UI-001 | 套用任何皮膚**不得移除語言切換**。皮膚是外觀,不該拿掉功能 | `UNPROTECTED`(2026-08-08 修復:skin 原本直接 `remove()`) |
| UI-002 | Stage 版面變動必須讓被取樣的 glass scene 一起重新布局;只更新 surface 幾何不夠 | `UNPROTECTED` |
| UI-003 | 同一個邏輯控制項的所有呈現(來源 + 光學複本)必須顯示相同狀態 | `UNPROTECTED` |
| UI-004 | 每個 `.optical-surface` 會建一份整頁 DOM 複本。**不要在同一個面板裡放很多個** —— 12 個開關實測直接把 renderer 卡死 | `UNPROTECTED` |
| UI-005 | 隱藏面板中的 DOM 量測回傳 0×0。動畫依賴 rAF,在背景分頁不會前進 —— 兩者都會讓驗證出現偽陽性 | `UNPROTECTED` |

---

## 目前的覆蓋缺口(誠實版)

**36 條裡有 17 條是 `UNPROTECTED`。** 標 ⚠️ 的 3 條是我認為最該優先補的,
因為它們的失敗模式是**靜默的**且直接影響金錢或資料正確性:

```
LIVE-004  休息時強制平倉 → 砍掉本來會獲利的部位
DATA-002  接縫未保留錨點 → 回測資料出現假跳空
DATA-003  用跳幅猜換月 → 把真實行情抹平(已發生過一次)
```

### ⚠️ 這份文件本身也會過期

初版的 LIVE-001 寫「熱身後必須呼叫 `reset_to_safe_state()`」—— 那個函式
**在現行程式碼裡不存在**,只存在於 `0.17.0` 那一代。我從舊筆記抄過來卻沒
先 grep 驗證。一份指名不存在函式的不變量清單比沒有更糟,因為它看起來權威。

**加任何一條之前,先確認它指名的函式/旗標/檔案現在真的存在。**

`backend/live/engine.py` 5,088 行、只有 2 個測試檔碰到;
`frontend/static/ancserTPX.js` 9,333 行、0 個測試。這兩個數字是這份清單裡
大部分 `UNPROTECTED` 的來源。

## 維護規則

1. 修好一個歷史 bug → 在這裡加一條,並把 Test 欄填上
2. 補了測試 → 把 `UNPROTECTED` 換成檔名
3. 刪掉一段看起來多餘的程式碼前 → 先在這裡搜一遍
4. **不要**把這份文件變成實作細節的複本。只記:**什麼必須為真、哪個測試證明它**
