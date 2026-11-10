# QA — 三層檢查

> 測試綠燈只證明「被測到的路徑」沒壞。這個專案的錢在 live terminal 上,
> 而 live 路徑有一半不在 CI 裡(券商、Discord、排程、DLL 鎖)。
> 所以 QA 分三層,每層抓不同的東西,誰也取代不了誰。

| 層 | 什麼時候跑 | 抓什麼 | 抓不到什麼 |
|---|---|---|---|
| 1. CI(自動) | 每次 push 到 main | 回歸:邏輯、契約、單一定義、UI 互動 | 真實券商、真實訊號、排程、私有資料 |
| 2. UI 截圖 | 改了 `frontend/static/**` | 版面、對齊、截字、深淺色 | 互動中途的狀態(那歸 Playwright spec) |
| 3. Live 首日 | 影響實盤的改動後,下一個交易日 | 啟動、口數、下單、出場、PnL 同步 | — 這一層就是最後一道 |

---

## 1. CI(GitHub Actions)

`.github/workflows/tests.yml` 有三個 job:`pytest`、`syntax`、`ui`(Playwright)。

**收到 "Run failed" 信要不要處理?要。** 長期紅燈會訓練人忽略紅燈,
那封信真的有問題時就沒人看了。處理順序:

1. 開 GitHub → Actions → 那次 run → 看是哪個 job 紅。
2. 本機重現(用乾淨的資料根,模擬 CI 沒有 `ancserMarketData`):
   ```bash
   ANCSER_MARKET_DATA_ROOT= python -m pytest tests/ -q
   ```
   ```bash
   node tests/ui/playwright-cli.cjs test
   ```
3. 判斷是哪一類:
   - **真回歸** → 修程式碼。
   - **測試依賴本機私有資料**(蠟燭庫、PI 歷史、Theta 快取)→ 缺資料時
     `pytest.skip`,本機照跑。範例:`tests/rust_parity_inputs.py` 的
     `require_local_parity_data()`(1.5.1i)。
   - **測試依賴「現在幾點」** → 改成固定日期。範例:
     `tests/ui/chart-timeframe.spec.js` 的 1H 視窗跨過 11/1 夏令時間結束夜,
     兩根 01:00 被合併成一根,400 變 399(1.5.1j)。
   - **測試釘了舊 UI 文字/格式** → 照新契約更新,不是刪掉斷言。

**已知未修**:`chart-timeframe.spec.js` 的 "live poll on 1H" 仍用 `now - 99h`,
夏令時間切換後約 4 天內可能紅。紅了就照上面 1.5.1j 的做法改成固定日期。

CI 綠燈的前提是**已經 push**。本機 commit 但沒 push,GitHub 上跑的還是舊碼,
信會繼續寄。

## 2. UI 截圖 QA

改了版面就跑一次,前後對照看,不靠描述:

```bash
TPX_UI_SHOTS=1 TPX_UI_SHOTS_TAG=after node tests/ui/playwright-cli.cjs test tests/ui/ui-relayout-shots.spec.js
```

- 預設存到 `.playwright-output/ui-shots/`(已在 `.gitignore`,不進版控)。
- 改之前先用 `TPX_UI_SHOTS_TAG=before` 拍一組,才有得對照。
- 要看的:截字、溢出、對齊、深淺色兩種主題、空狀態。
- 截圖只看靜止狀態。拖曳、切換、動畫中途的 bug 要寫 Playwright spec
  在中途取樣(見 `CLAUDE.md`「互動的 bug 必須驅動互動才看得見」)。

## 3. Live 首日檢查

**觸發條件**:動到以下任何一處,下一個交易日就要照表檢查一次:
`backend/live/**`、`backend/terminal_live.py`、`backend/daily_start.py`、
`backend/strategy/**`、`data/presets.json` 的 live preset、
`scripts/install_daily_start.ps1`、啟動器(`*.bat` / `stop_legacy_instances.ps1`)。

全部是**唯讀**檢查。不要替使用者下單、平倉、改設定。

| # | 檢查 | 怎麼看 | 通過 |
|---|---|---|---|
| L1 | 06:30 PT 自動啟動 | `python -c "from backend import daily_start as d; print(d.settings())"`(在 repo 根目錄跑) | `last_day` = 今天、`attempt_state` = `running`、`last_error` 空 |
| L2 | terminal 活著 | `python -c "from backend import daily_start as d; print([(t['run_id'][:8], t['status'].get('health'), t['status'].get('phase')) for t in d.terminal_statuses()])"` | 恰好一個 terminal,`ok`、phase 不是錯誤 |
| L3 | preset / 帳戶 / 口數 | `runtime/logs/terminal_daily.log` 開頭的 `Preset:`、`Account:`、`Params:` | 是預期的 preset 與帳戶;`size=` 對 |
| L4 | 訊號有進來 | 同 log 的 `[PI] 訊號`;稽核流 `runtime/logs/pi_live_signals.jsonl` | 07:00 PT 後的訊號有出現;07:00 前的重播被濾掉 |
| L5 | 路由正確 | `[PI] 訊號入列` vs `訊號已接收但未入列` | 只有 preset 交易的商品會入列 |
| L6 | 下單口數 | `[ORDER SEND] … size=` | 等於 preset 口數;風險 > `downsize_risk_dollars` 時應先看到 `[SIZE] planned risk …` 並降成 1 口 |
| L7 | 出場路徑 | `max hold`、`SL`、`TP`、`flatten` 行 | 照 preset 的出場規則發生,殘單有被取消 |
| L8 | PnL 同步 | 出場後幾分鐘內的 `daily_pnl=` | 與 TopstepX 成交紀錄一致 |
| L9 | 沒被誤殺 | 開關 App 之後 terminal 還在 | App 啟動器不會停掉 terminal(1.5.1a) |
| L10 | Grok 轉發(有設 `.env` 才檢查) | Grok Bot 那邊有收到 | 收到即可;ancserTPX 只負責轉發,失敗不影響交易 |

`runtime/` 在資料根底下(`ANCSER_MARKET_DATA_ROOT`,本機是
`F:/ancserQuant/ancserMarketData/runtime/`)。

看到不通過:先記錄在下面,再決定要不要停。**停 terminal 是使用者的決定。**

### 紀錄

| 日期 | 觸發的改動 | 結果 | 備註 |
|---|---|---|---|
| 2026-11-09 | 1.5.1a–1.5.1h(DST 排程修正、啟動器不殺 terminal、downsize 規則、取消續倉、Grok 轉發) | L1–L8 通過;L9、L10 未驗 | 06:30:06 自動啟動(DST 修正後第一次)。preset `PI 2MNQ LV3 REOPEN BEST`,size=2。07:24 SPY→MES 粉π 正確未入列;07:57 QQQ→MNQ 粉π Level 3 空單入列,07:58 送出 SELL 2 MNQ @27974.5;08:59 60 分最長持有平倉,殘留 SL/TP 已取消;daily_pnl **+$146**。沒有 `[SIZE]` 行 = 規劃風險未超過 $1,000,維持 2 口(與規則一致)。Grok `.env` 尚未填,轉發未啟用 |

下一次要特別看:
- 第一次出現**寬停損**的訊號時,`[SIZE]` 行有出現且真的只下 1 口。
- `.env` 填好 `GROK_PI_WEBHOOK_URL` 後的第一個訊號(terminal 下次 06:30 啟動才生效;
  App recorder 要重開 App)。
