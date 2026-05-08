# ancserTPX

繁體中文｜[English](README.md)

ancserTPX 是一套運行於 **TopstepX（ProjectX API）** 的 NQ（Nasdaq 100 E-mini）期貨自動交易系統。

系統以 **1 分鐘 K 線** 為核心，支援回測與即時監控 / 交易。

---

## 策略概要

目前主要策略為 **Session Trend Follow**，依照不同市場時段建立 Volume Profile 區間（VAH / VAL / POC），再在區間突破後尋找進場機會。

| Session | 美東時間（ET） | UTC |
|---------|----------------|-----|
| ASIA | 6:00 PM - 3:00 AM | 22:00 - 07:00 |
| EURO | 3:00 AM - 7:00 AM | 07:00 - 11:00 |
| PRE | 7:00 AM - 9:30 AM | 11:00 - 13:30 |
| RTH | 9:30 AM - 4:00 PM | 13:30 - 20:00 |
| AH | 4:00 PM - 6:00 PM | 20:00 - 22:00 |

目前預設交易參數如下：

- **Entry**：連續 5 根 1 分鐘 K 線突破 VAH / VAL 後，以 50% retracement 掛限價單
- **Stop Loss**：50 ticks
- **Take Profit**：150 ticks
- **Trail SL**：5 ticks
- **Flatten**：每日美西時間 12:45 PM 強制平倉

---

## 安裝前準備

### 1. ProjectX API 設定

1. 前往 https://dashboard.projectx.com/dashboard 並開通 ProjectX API Access
2. 綁定 TopstepX 帳戶
3. 前往 https://topstepx.com/settings
4. 進入 **API** 頁面
5. 複製 API Key

### 2. 帳號設定

在網頁右上角 **CONNECT** 面板輸入 **郵箱** 和 **API Key**，首次連線成功後會自動保存到 `.env`，無需手動編輯檔案。

### 3. TopstepX Auto OCO Preset

即時交易的保護單依賴 TopstepX 的 **Auto OCO Bracket** preset。Bot 不會在入場單裡送 API bracket 欄位。

建議在 TopstepX 這樣設定：

- 開啟 [TopstepX Risk Settings](https://topstepx.com/settings?tab=risk-settings)
- 啟用 **Auto OCO Brackets**
- 建立一個給本 bot 使用的 preset
- **Stop Loss Order Type**：`Stop Market`
- **Take Profit Order Type**：`Limit`
- 預設 ticks 距離設大一點，讓成交後帳戶立刻有保護單
- 不要把 preset SL 設成 `Trailing Stop Market`；trail 由 bot 透過修改既有 Auto OCO stop order 來完成

實際下單流程：

1. Bot 只送普通入場單。
2. 入場成交後，由 TopstepX 自動生成 Auto OCO SL/TP 子單。
3. Bot 等待子單出現，篩選正確平倉方向，然後把 SL/TP 修改成策略算法計算出的價格。
4. Trail SL 觸發後，Bot 會修改同一張 Auto OCO SL，不會另外新掛一張 stop order。

如果 Auto OCO 子單沒有生成，live log 會出現 `[AUTO OCO]` 警告，Bot 不會退回到手動 bracket 下單。如果成交後 5 分鐘仍沒有 SL/TP，Bot 會先平倉、暫停 engine，並在 log 裡留下上面的 Risk Settings 連結。

---

## 安裝與啟動

請依照你的作業系統執行對應檔案：

### Windows 11

- 首次安裝：雙擊 `install-Win11.bat`
- 啟動程式：雙擊 `start-Win11.bat`

> 若 Windows 跳出 SmartScreen 警告，點 **「其他資訊」→「仍要執行」**。

### macOS

- 首次安裝：雙擊 `install-Mac.command`
- 啟動程式：雙擊 `start-Mac.command`

> **第一次執行**：macOS 會擋下，跳出「無法打開，因為來自未識別的開發者」。
> 解法：**對檔案按右鍵 → 打開 → 在對話框再點「打開」**。每個檔案只需做一次。
>
> 接著會自動跳出 Terminal 視窗執行，不用輸入任何指令。
> 使用 ancserTPX 期間請保持 Terminal 視窗打開，關掉就停掉伺服器了。

---

## 使用方式

### Backtest

1. 選擇策略或 preset
2. 點擊 **EXECUTE BACKTEST**

### Live Trading

1. 選擇交易帳戶
2. 確認 TopstepX Auto OCO preset 已啟用
3. 點擊 **GO LIVE**
4. 需要停止或手動平倉時，使用 **STOP** 或 **FLATTEN**

---

## License

請參閱 [LICENSE](LICENSE)。
