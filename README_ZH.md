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

### 1. 系統需求

- **Python 3.10 ~ 3.13**
- **Git**（可選，用於 clone 專案）

### 2. ProjectX API 設定

1. 前往 https://dashboard.projectx.com/dashboard
2. 建立 ProjectX 帳號
3. 開通 ProjectX API Access
4. 綁定 TopstepX 帳戶
5. 前往 https://topstepx.com/settings
6. 進入 **API** 頁面
7. 複製 API Key

### 3. 設定 `.env`

在專案根目錄建立 `.env`：

```env
TOPSTEPX_USERNAME=your_email@example.com
TOPSTEPX_API_KEY=your_api_key_here
TOPSTEPX_CONTRACT_ID=CON.F.US.ENQ.M26
```

---

## 安裝與啟動

請依照你的作業系統執行對應檔案：

### Windows

- 首次安裝：`install.bat`
- 啟動程式：`start.bat`

### macOS

- 首次安裝：`install.sh`
- 啟動程式：`start.sh`

---

## 使用方式

### Backtest

1. 載入歷史資料
2. 選擇策略或 preset
3. 點擊 **EXECUTE BACKTEST**

### Live Trading

1. 選擇交易帳戶
2. 點擊 **GO LIVE**
3. 需要停止或手動平倉時，使用 **STOP** 或 **FLATTEN**

---

## License

請參閱 [LICENSE](LICENSE)。
