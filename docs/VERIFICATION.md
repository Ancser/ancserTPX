# ancserTPX 驗證紀錄

本文件記錄實際執行的檢查、環境與限制，與 [`VERSION_CONTROL.md`](VERSION_CONTROL.md) 的待辦和 patch 對照。未執行的驗收標為待驗；實盤成交、券商保護單、Windows 自動啟動與 mock 測試分開記載。

## 2026-10-27 02:55 PT — 每日切換 terminal（O3／RUN2）

- 在隔離資料根目錄進行 O3、PI、安全單、風控與監聽回歸：**145 passed**；包含 SQLite 與監測程式的握手、broker 回應檢查、持倉／掛單保留、STOP 競態、啟動視窗和未知狀態。
- 正式帳戶重啟前後以 broker read-only 查詢確認當時無持倉或未成交單。舊 PID `35632` 正常結束，新 PID `23396` 載入 `PI 2MNQ LV3 REOPEN BEST`、2 MNQ、5 秒 listener，狀態 healthy；Daily start 維持 06:30 ON。
- 盤前空倉時向新程序發出短暫暫停請求，觀察 `restart_pause_ack=true`；撤回請求後恢復 healthy。沒有送出交易委託。
- 以正式設定執行 O3 broker 查詢函式，確認帳戶存在且當時持倉／未成交委託均為空；最後 145 項隔離回歸通過。下一次 Windows 排程為 2026-10-27 06:30 PT。
- 真正的 06:30 Windows 排程完整切換與盤中成交仍待驗。當時 PID 與帳戶狀態只是快照；未知 broker／terminal 狀態會阻止自動切換。

## 2026-10-27 02:49 PT — 使用者要求切換 terminal（RUN1）

- 切換前以 TopstepX read-only 查詢，帳戶 `22373660` 的持倉及未成交委託均為空；舊程序正常停止。
- 保留 Daily start 啟用及 06:30 加州時間；新程序啟動後日誌顯示 `PI 2MNQ LV3 REOPEN BEST`、`CON.F.US.MNQ.Z26`、2 口，2089 根熱身 K 線完成，PI listener 每 5 秒，狀態 `running`／`health=ok`。
- 策略快照 `e7b429b6b680` 顯示 LV2／LV3 多單、LV3 空單、LV3 多單續倉、每日虧損單數限制 1。當時 listener 在訊號時段外，尚未觀察盤中收訊與新訂單。
- 舊 PID `34736` 已退出；新 PID `35632` 為當時快照。Daily start 下一次檢查為 06:30；若新 terminal 仍運行，會跳過重複啟動。O1/O2 的實際故障情境及 D1 正式換月仍待驗。

## 2026-10-27 — O1／D1／O2 修復（工作區）

- 在獨立 `ANCSER_MARKET_DATA_ROOT`／備份目錄，關閉 pytest cache 與 Python bytecode。最後完整套件 `python -B -m pytest -p no:cacheprovider tests/ -q`：**933 passed、5 failed、11 skipped、8 subtests passed**。
- 5 個失敗：一項 `test_live_host.py` 的 request 假物件欠 `query_params`；兩項 Rust PI 測試硬編碼預設為 `PI 2MES BOTH BEST`，現行預設為使用者選擇的 MNQ；兩項實際歷史資料測試在隔離資料根目錄缺少 PI／MES 資料。這些測試需在合適的隔離資料及明確 preset 條件下另行處理。
- O1/O2 隔離案例：啟動錯誤、重試次數、並發 claim、STOP、引擎啟動後未知狀態、缺憑證、健康確認、控制故障恢復及封鎖新單。
- D1 隔離案例：各寫入階段注入失敗、子程序中途退出、資料／metadata／pending 復原、損壞備份拒絕讀取、重試冪等、跨程序鎖；既有合約歸屬、增量 journal 與跨執行緒回歸也有執行。
- 第一輪完整檢查還發現兩項既有時區契約衝突；已改用共用 `LOS_ANGELES` 並更新前端顯示，最後完整檢查中這兩項通過。
- Windows 實際喚醒及排程啟動、broker 真實下單和持倉監測故障、正式歷史資料換月、突然斷電與磁碟耗盡仍待驗。跨程序保護只覆蓋已載入本次程式的參與者；舊程序要先依安全程序處理。
- 本輪修復結束時尚未重啟 terminal；隨後依使用者要求切換，結果見上方 RUN1。

## 2026-10-27 — 未提交工作區風險審查（OPS1／DATA1／OPS2 建單前）

- Git 基準：`8985079`；工作區含未提交及未追蹤變更，檢查前後 `git status` 分類數量一致。
- 使用暫存目錄作 `ANCSER_MARKET_DATA_ROOT`，停用 pytest cache 與 bytecode，執行兩組相關測試：
  - `test_exec_protection_invariants.py`、`test_broker_order_mapping.py`、`test_pi_reopen_continuation.py`、`test_pi_directional_exits.py`、`test_pi_p1_latency.py`、`test_contract_ledger.py`、`test_market_clock.py`、`test_engine_lease.py`：**112 passed**。
  - `test_live_daily_locks.py`、`test_live_daily_rest.py`、`test_live_warmup_safety.py`、`test_live_manual_guardian_integration.py`、`test_exit_policy_unification.py`、`test_pi_replacement.py`、`test_live_status_health.py`、`test_preset_strategy_normalization.py`、`test_timeframe_aggregation.py`、`test_columnar_store.py`：**103 passed**。
- 合計 **215 passed**；這些測試並未覆蓋本次建單所列的全部中斷、並行及排程失敗情境。
- 只讀檢查確認當時 Windows task 設定 06:30、狀態 Ready；terminal 狀態顯示運行，啟動日誌的 preset 為 `PI 2MNQ BOTH BEST`。這是當時的快照，之後應重新查證。
- 本次未執行完整測試套件、未重啟 terminal、未發送 broker 訂單，亦未驗證 Windows 實際喚醒或實盤成交。

## 待補：每次 patch

記錄日期、ticket、commit／工作區、具體測試命令與結果、測試資料位置、實際執行環境、錯誤與待驗事項；交易訊號和成交以實際時間及帳號狀態記載，排除憑證與 token。
