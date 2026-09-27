# 量化研究與資料狀態 — 2026-09-26

## 現況

目前沒有策略通過穩定性與實際執行門檻。MES live 維持關閉；下一階段以固定規則的紙上前瞻資料收集和風險路徑驗證為主。

F 槽目前剩餘 **392.42 GB（約 365.47 GiB）**。現有容量可繼續保存原始資料、研究輸出及新資料；目前無需清理資料。

2026-09-26 取得 QQQ 與 SPY 最新已完成交易日（2026-09-25）的 EOD 和 OI 日檔：共 **23,070 列、373,260 壓縮 bytes**，每個原始檔已寫入 SHA-256 manifest。OI 檔的原始時間戳約為 06:30 ET；該日 OI 表示 2026-09-24 收盤未平倉量，符合 ThetaData 文件定義。[ThetaData OI 說明](https://docs.thetadata.us/operations_python/option_history_open_interest.html)

這批日檔新增每日期權結構快照。當天沒有對應的 LV2/LV3 訊號標記與進出場結果，因此它們尚未增加該策略的可比交易樣本。

另從 ThetaData 補取 QQQ 2026-09-24、09-25 的 0DTE 成交/報價及 1 分鐘一階 Greeks：4 個壓縮檔共 **2,021,169 列、41,181,609 bytes**，列數、gzip 解壓及 SHA-256 均已核對。帳戶 Standard 可取得一階 Greeks；all-Greeks 歷史端點回報需 Professional 權限。新檔保存在獨立 extension，原先截至 09-23 的已審核 0DTE archive 保持原樣。這兩天仍沒有 PI 標記與配對交易結果。

截至 09-26（週六），最近完成的交易日為 09-25。MNQ canonical 1 分 K 到 09-23，另存 09-24 prospective snapshot；MES canonical 1 分 K 到 09-14，09-15 至 09-25 尚待 futures feed 回補。Theta extension 補的是 QQQ 期權微結構；PI 標記與 MNQ/MES futures bars 仍由各自來源提供。新增期權列因此維持資料擴充用途，現有交易績效數字不變。

## 可用資料年限

| 資料 | 已核對覆蓋 | 核心限制 |
|---|---|---|
| MNQ continuous 1 分 K | 2020-01-01 至 2026-09-23；2,376,404 根 | 2026-08-31 連續合約接縫仍隔離；2026-09-24 另存 prospective snapshot |
| MES continuous 1 分 K | 2020-01-01 至 2026-09-14；2,365,689 根 | 2023-07-04、2023-11-23 的 RTH 分鐘缺口已標記；需要補齊 2026-09-15 之後資料 |
| QQQ/SPY 月度 EOD 與 OI | 2021-01 至 2026-09；各 dataset/symbol 69 個月分區 | 2026-09 月檔截至 09-21；另有 09-25 EOD/OI 日檔。鏈範圍為 0–60 DTE |
| QQQ 0DTE 成交、報價與一階 Greeks | 原始 archive：2020-01-03 至 2026-09-23、1,270 個到期日、449,467,397 列；獨立 extension：09-24、09-25 增加 2,021,169 列 | 原始 archive 9.14 GiB 壓縮；新 extension 4 檔、41,181,609 bytes。Standard 可用一階 Greeks；all-Greeks 端點受 Professional 權限限制 |
| QQQ/SPY 分鐘期權 quote/Greeks | 2026-03-05 至 2026-09-15；QQQ 131 日、SPY 128 日 | 約 7 個月，尚缺 12 個月 option path 覆蓋 |
| PI/期權 cohort | 240 個標的日期組、960 個下載請求；2026-03 至 2026-09 | 473 個訊號、125 個市場日期；短期歷史與人工標記限制泛化 |
| 原始 PI 標記 | 481 個有效 mark、126 個日期；2026-03-05 至 2026-09-14 | cohort 的 473 訊號/125 日期套用額外 option path 覆蓋條件；原始標記目前沒有 09-14 之後的日期 |
| CFTC TFF | E-mini 股票指數自 2006 年；micro 指數 proxy 自 2020 年 | 星期資料僅能在官方發布後使用；lagged TFF 策略門檻未通過 |
| MNQ MBO | 2026-08-07 至 2026-09-18 的 31 個策略 RTH 日，另 2 個交叉核對日 | 樣本期短，適合執行診斷和持續收集 |

期貨策略研究範圍目前包含完整 2022–2025 與部分 2026。連續合約與標註品質旗標應隨每次 run 一起保存。完整 2026 年度資料要等全年結束及 freeze 後再做年度判斷。

## 策略研究結果

| 候選 | 可核對結果 | 研究判斷 |
|---|---|---|
| 目前 PI preset | 約 6 個月樣本。MNQ n=76、PnL +$2,710.76、PF 1.5169、14-tick stress PF 1.3935；三段 walk-forward PF 為 1.0831、0.8832、4.6549。MES n=55、PnL +$201.80、PF 1.1163、stress PF 0.6712 | MNQ 表現集中於最新區段，持續性檢查未過；MES 成本壓力測試未過，保持 live 關閉 |
| MNQ strict PI（青π＋粉π） | n=74、PF 1.80、14-tick stress PF 1.64；三段 walk-forward 均為正 | 目前最適合凍結後做 paper/holdout 的策略候選。歷史約 6 個月，屬研究候選，尚待至少 12 個月固定規則、獨立樣本外與前瞻成交資料 |
| MNQ VP breakout/retest 基線 | 2021–2025 共 970 筆，PnL −$2,194.80、PF 0.9462；2026 至 09-23 共 141 筆，PnL −$817.34、PF 0.9144 | 期權 OI 描述性連接尚未轉化成通過成本與樣本外驗證的入場閘門 |
| 60-session TSM，vol20 sizing | 246 個 MNQ price-only 配對區間，TSM 相對同區間 passive long 的淨差 −$28,091；244 個 MES 區間淨差 −$8,092.50。14-tick 壓力仍為負差，shared walk-forward 與 Monte Carlo 均未通過 | TSM/TFF 的方向選擇力目前弱於配對長倉基準；不列為 live 候選 |
| TSM + lagged TFF | 85 個 MNQ 配對區間淨差 −$24,666；68 個 MES 區間淨差 −$13,517.50；robustness gates 均未通過 | TFF 僅保留為發布後的背景特徵研究 |
| 1DTE PI 退出規則 | 473 個訊號、125 個日期、7 個月。若用 ask 進場與 60 分鐘 RTH timer/50% premium stop，淨總額 +$7,592.10、PF 1.3287；date-cluster 95% CI 為 −$6.60 至 +$39.50／訊號，包含 0。12 個月、共享 walk-forward/Monte Carlo 及實際成交驗證仍待完成 | 可作為事先凍結的 paper-shadow 候選之一，研究樣本尚未形成穩定策略證據 |
| LV2 取代 LV3 | `pi_lv2_replace_pi` 已提供 Backtest/Live opt-in，預設關閉；觸發條件為新鮮 BUY 深藍圈、結構化 source Level 2，取代 bot 持有的 BUY 青π、source Level 3。QQQ source Level 2 多方與深藍圈交集為 6 個 marks | 機制已具備，績效仍需獨立重播及前瞻驗證；六筆交集屬小樣本 |
| 深藍圈重新進場／收盤後延續 | 1R gap cap：MNQ baseline 6 筆、3 筆重新進場；續倉 +$410.78，策略總額 −$520.66。MES baseline 7 筆、5 筆重新進場；續倉 +$1,006.30，策略總額 +$946.37。樣本少於一年，walk-forward 未通過 | 只涵蓋 15:45 ET 仍持有的 bot 部位，並於 18:00 同日重新進場；數字按「深藍圈 kind」篩選。新訊號的 LV2→LV3 觸發、收盤新訊號重驗及次日開盤延續仍需獨立前瞻驗證 |

### PI 標記來源敏感度

目前 canonical `pi_signals.json` 有 481 個有效 marks、126 個日期（2026-03-05 至 2026-09-14）。`pi_signals.before_full_channel_20260914T171903Z.json` 沒有增加 canonical 以外的有效事件。`pi_signals.before_ny_channel_20260909.json` 經同一 loader 解析後有 166 個額外 marks，集中於 2026-06-11 至 2026-08-07 的 40 個日期，含 86 個 MES、80 個 MNQ；kind 分布為紫圈 72、淡藍圈 43、粉π 27、青π 20、深藍圈 4。事件 key（時間、標的、kind）與 canonical 樣本沒有重疊。合併敏感度池為 647 個事件。

目前 `pi_history.load_rows()` 以 canonical 檔作為回測來源，因此主要績效仍以 481 個 marks 計算。較早 NY-channel 快照缺少 `channel_id` 及結構化 source level，166 個額外 marks 無法還原來源 channel 或分類 Level 2；它們保留為獨立來源敏感度池。QQQ canonical 的 source Level 2 有 21 個 marks，其中 6 個為深藍圈多方、15 個為紫圈空方；深藍圈 kind 共 7 個，其中 6 個屬 source Level 2、1 個屬 source Level 1。來源 Level 與 mark kind 在資料及報告中分開保存。

Theta OI 五年分析顯示，OI 門檻在 2024–2025 與 2026 的分組方向不一致；OI、正負 GEX proxy、MBO 或 order flow 尚未證明能改善 VP 突破／盤整判斷。OI 是 unsigned open interest，dealer 方向仍未知。

目前最有根據的研究路徑是凍結 MNQ「青π＋粉π」作為單口 paper/holdout 候選，並同步補齊 signal provenance 與成交紀錄。MES 維持 paper/shadow；現有 preset 與已測 MES exit cells 都未通過 14-tick 成本壓力。候選再通過跨年、日期群集、最佳日移除、walk-forward、Monte Carlo、Topstep MLL path 及前瞻成交門檻後，才評估 live。

## 資料缺口與下一輪

1. 每個交易日保存 LV2/LV3 訊號方向、level、產生時間、觸發時間、取消條件、broker acknowledgement、quote/NBBO、成交價、部分成交、滑價與退出原因。固定版本後累積至少 12 個月 option path；目前起點為 2026-03，覆蓋目標延伸至 2027-03。
2. 補齊 MES 2026-09-15 至最新交易日的 1 分資料，核驗日期連續性及每日 RTH bar 數；隔離的 MNQ 2026-08-31 接縫保留人工核對旗標。
3. 維持 NY-channel 歸檔 166 個 marks 為獨立敏感度池；若要用於 source-level 研究，先補回可核驗的 channel 與 source-level provenance。
4. 對 LV2→LV3 與收盤／開盤後延續預先定義訊號、隔夜持倉、停損、滑價與比較基準，再以相同日期樣本測試。新取得的 09-25 OI/EOD 快照可加入未來的 point-in-time 特徵，不回填到更早決策。
5. 使用已審核的 2020–2026 QQQ 0DTE archive 做獨立特徵研究；該 archive 提供 option tape 特徵，PI 訊號標記與 1DTE option exit path 仍由各自資料集提供。
6. 候選通過固定交易成本、14-tick 壓力、年份／日期穩健性、Topstep MLL path 及前瞻紙上成交後，再評估 MES live eligibility。

## Luna Max 與 Astra High 審查

Luna Max 彙整離線實證及 live readiness。Astra High 對抗式審查記錄 **7 項 High、5 項 Medium、0 項確認 Critical**。主要 High finding 已有離線處理：未標記手動 stop 不納入管理；cancel/modify/close 採單次請求與不確定結果鎖；部分 entry remainder 保留閘門；late fill 不覆寫 terminal state；合成 order ID 不作 broker position ID；partial exit 按淨持倉量管理；journal 使用獨佔寫入鎖。

本輪再按官方 TopstepX schema 修正 OCO 子單剩餘量判定，pending 子單不會解除保護單 blocker，stop modify 省略可選 `size` 參數。Rust compile、paper lifecycle demo、Python syntax 與 JSONL exit-kernel 操作均通過。沒有建立 TopstepX 帳戶連線、送出訂單、重啟服務、執行測試套件或 commit。

## 資料位置與重現

- Theta 原始資料及 coverage manifest：`F:\ancserQuant\ancserMarketData\source\options\thetadata\`
- QQQ 0DTE 09-24/25 extension 與 SHA-256 manifest：`F:\ancserQuant\ancserMarketData\derived\research\theta_0dte_recent_extension_20260926\`
- PI cohort ingest status：`F:\ancserQuant\ancserTPX\docs\THETA_PI_COHORT_INGEST_STATUS_2026-09-22.md`
- 五年 MNQ VP × QQQ OI 研究：`F:\ancserQuant\ancserTPX\docs\VP_THETA_OI_5YR_STUDY_2026-09-23.md`
- TSM 配對與穩健性結果：`F:\ancserQuant\ancserMarketData\derived\research\tsm_volscale_pair_review_20260925T1708Z\report.md`
- 2022–2026 資料完整度盤點：`F:\ancserQuant\ancserMarketData\derived\research\rust_data_readiness_20260925T191101Z\report.md`
- Theta OI 文件：[Python API reference](https://docs.thetadata.us/operations_python/option_history_open_interest.html)
- TopstepX 下單欄位：[Search orders](https://gateway.docs.projectx.com/docs/api-reference/order/order-search/)、[realtime events](https://gateway.docs.projectx.com/docs/realtime/)、[modify order](https://gateway.docs.projectx.com/docs/api-reference/order/order-modify/)
