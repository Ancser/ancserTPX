# Rust 核心標準 —— 一個計算只有一份定義

狀態:**規劃,尚未動工**(2026-11-05)。本文件是標準與路線圖;每一步實作
前仍須使用者確認,live 下單路徑的切換另有閘門(§6)。

## 0. 為什麼要有這份標準

現況(2026-11-05 查證):

- 回測與 live **100% 走 Python**。`research/rust_engine/` 沒有接到任何
  production 入口;`backend/rust_live_bridge.py` 是 Rust → Python 方向的
  JSONL 橋,Python 不呼叫 Rust。
- 同一個計算已經有兩份以上:
  - `round_to_tick`:Python 3 份(`live/engine.py`、`strategy/exit_policy.py`、
    `strategy/volume_profile.py`),Rust 3 份(`pi.rs`、`vp-replay/profile.rs`、
    `vp-replay/strategy.rs`)。**而且已經不一致**:`exit_policy.py` 最後
    `round(..., 10)`、`volume_profile.py` 是 `round(..., 4)`;`tick_size <= 0`
    時 `exit_policy.py` / `pi.rs` 回傳原價,其他幾份會除以零;`live/engine.py`
    固定用全域 `TICK_SIZE` 不收參數。六份「同一個概念」,至少三種行為。
  - PI trail 解析:Rust `exit.rs::resolve_pi_exit` 是 Python
    `exit_policy.py::_resolved_trail_ticks` + `resolve_exit_policy` 的逐行翻譯,
    而且帶著一條註解解釋「為什麼這裡跟 Python 不一樣」—— CLAUDE.md 判準:
    **解釋為什麼需要自己一份的註解,就代表不需要。**
- `tests/test_single_definition.py` 只掃 `backend/**/*.py` 與
  `frontend/static/*.js`。`resolve_exit_policy` 已註冊為單一定義,但 Rust 的
  第二份它看不見。
- 雙份之間靠 parity 測試對齊。`test_rust_pi_backtest_parity.py` 釘的是整份
  `data/presets.json` 的 SHA256,並經 `_load_default_preset()` 讀
  `last_used_live` —— 使用者在 UI 點一下 preset,測試就紅。這是雙份並存的利息。

結論:現在是「兩邊成本都付,好處只拿一半」。Rust 的編譯期保證只保護 Rust
那份,production 的 Python 沒受益。

## 1. 標準(規則)

**R1. 一個計算概念只有一份可執行定義。** 這份定義在 Rust 或 Python 擇一,
不允許「Python 一份 + Rust 一份 + parity 測試對齊」作為**穩態**。parity
只是**切換那一刻**的驗收工具(§4 第 3 步),切換完成後被取代的那份要刪掉。

**R2. Rust 是函式庫,不是第二個引擎。** Rust 核心以 PyO3 編成 Python 擴充
模組(`tpx_core`),由現有 Python 引擎呼叫。Python 保留:IO、券商、FastAPI、
排程、preset 載入、UI 契約、研究腳本。Rust 負責:純計算(沒有 IO、沒有時鐘、
沒有全域狀態)。

**R3. Rust 不讀 `presets.json`。** preset 解析、`last_used_bt` /
`last_used_live`、每帳號指定(`account_roles.json`)都只在 Python
(`routes.py` / `terminal_live.py::_load_default_preset`)。Rust 只接收
**已解析好的型別化參數**。因此「最近使用的參數」不需要「同步到 Rust」——
它本來就只存在一處,換核心不影響它。

**R4. 介面型別在 Rust 定義一次,Python 端不重寫。** 例如 `ExitPolicy`、
`Side`、`TrailMode` 以 `#[pyclass]` 匯出;Python 端 `from tpx_core import ...`,
不再有平行的 `@dataclass`。

**R5. 不允許靜默 fallback 到舊實作。** `import tpx_core` 失敗 = 啟動失敗,
大聲報錯。保留「Rust 掛了就用 Python 版」等於保留第二份定義,而且是平常
沒人跑、壞了也沒人知道的那份。

**R6. 單一定義檢查涵蓋 Rust。** `test_single_definition.py` 的 `SOURCES`
加入 `research/rust_engine/crates/**/*.rs`(與之後的 `tpx_core`),已註冊
概念的 Rust 定義也要列在 `home`。

**R7. 架構重構與策略行為變更不混在同一次改動**(CLAUDE.md 既有規則)。
搬到 Rust 的那次 commit,任何回測數字都不得改變。

## 2. 為什麼是 PyO3 而不是 CLI / JSONL / 全面改寫

| 做法 | 定義份數 | 問題 |
|---|---|---|
| 現況:Rust CLI + JSONL + parity 測試 | 2 | 每加一個參數改約 5 處;parity 測試本身也會漂移 |
| 全面改寫(Rust 引擎取代 Python 引擎) | 切換期 2,之後 1 | 一次搬 46k 行;FastAPI、券商、Discord listener 都要重寫;已修好的 `1.0.x` bug 會在新路徑重演 |
| **PyO3 函式庫,逐模組取代** | **每個模組只在切換那次 commit 短暫有 2 份** | 安裝流程需要編譯好的 wheel(§7) |

PyO3 讓 Rust 函式在 Python 看起來就是一個普通函式。呼叫端不用改,所以
**取代的單位可以小到一個函式**,每一步都能用既有的整套 Python 測試(`tests/`,約 930 個)驗證。

## 3. 架構

```
frontend (UI)  ──HTTP──▶  backend/api/routes.py   (Python,不動)
                             │  preset 解析 / last_used (Python,唯一一份)
                             ▼
                          backend/backtest/engine.py, backend/live/engine.py  (Python,協調)
                             │  呼叫
                             ▼
                          tpx_core  (Rust + PyO3,純計算,唯一一份)
                             ├─ ticks      round_to_tick, tick_floor
                             ├─ exit       resolve_exit_policy, evaluate_exit_operation
                             ├─ bracket    PI 下單括號建構
                             ├─ profile    VP profile
                             └─ ledger     手續費/點值/淨損益
```

`research/rust_engine/` 的研究 CLI(vp-replay、research-api)改成**依賴
`tpx_core`**,不再自帶 `round_tick` / `round_price`。研究與 production 用同一份。

## 4. 每個模組的搬遷流程(5 步)

以 `round_to_tick` 為例,示範最小單位。

**步驟 1 —— 在 `tpx_core` 寫 Rust 版並匯出:**

```rust
// crates/tpx-core/src/ticks.rs
use pyo3::prelude::*;

/// The only definition of "snap a price to the exchange tick".
/// Mirrors backend/strategy/exit_policy.py::_round_to_tick exactly:
/// Python round() is ties-to-even, so f64::round (ties-away) would be a
/// behaviour change hidden inside a "refactor".
#[pyfunction]
pub fn round_to_tick(price: f64, tick_size: f64) -> f64 {
    if tick_size <= 0.0 {
        return price;
    }
    let snapped = (price / tick_size).round_ties_even() * tick_size;
    (snapped * 1e10).round_ties_even() / 1e10
}
```

注意:搬的是**其中一份**(`exit_policy.py`),不是「發明一份更好的」。
其他幾份的差異(小數 4 位 vs 10 位、`tick_size <= 0` 的處理)要在**另一次**
改動裡逐一確認合併後數字不變,或由使用者決定接受的行為差異(R7)。
這一步最容易出事:看起來只是換語言,實際上悄悄改了捨入規則。

**步驟 2 —— Python 原地換成呼叫,保留同樣的名字與簽名:**

```python
# backend/strategy/exit_policy.py
from tpx_core import round_to_tick as _round_to_tick   # 原本的 def 刪掉
```

呼叫端(`live/engine.py`、`volume_profile.py`)改成 import 這一份,
刪掉各自的 `_round_to_tick`。

**步驟 3 —— 切換那一刻的 parity(唯一一次):** 在刪掉 Python 版**之前**,
用真實輸入(六年蠟燭的所有價格 × MNQ/MES tick)比對兩版逐位元相等,記錄
結果到 commit message。必須包含 `.5` tick 邊界、`tick_size <= 0`、負價與
極大值。parity 程式碼放在一次性的腳本,不進 `tests/` 長期維護。

**步驟 4 —— 刪掉被取代的那份,把概念註冊進 `SINGLE_DEFINITION`,`home`
指向 Rust 檔。** 然後**把舊實作放回去,確認測試會紅**(CLAUDE.md 驗證標準)。

**步驟 5 —— 全套回測數字不變:** 跑 `python -m pytest tests/ -q` 與既有
preset 回測,PnL/筆數必須與搬遷前完全相同(R7)。

## 5. 搬遷順序

由「沒有狀態、影響面小、容易驗證」到「有狀態、碰錢」:

| 階段 | 模組 | 現有 Rust 可沿用 | 為什麼排這裡 |
|---|---|---|---|
| 0 | 修 parity 測試(§0)+ R6 擴充單一定義檢查 | — | 先讓尺量得準 |
| 1 | `ticks`(round_to_tick 6 份 → 1) | `pi.rs` | 純函式,最小練習 |
| 2 | `ledger`(點值、手續費、淨損益) | `pi_backtest.rs` 帳本 | 純函式,影響所有報表 |
| 3 | `exit`(resolve_exit_policy / evaluate_exit_operation) | `exit.rs`(需補齊非 PI 模型) | 已在單一定義清單;live 與回測共用 |
| 4 | `bracket`(PI 括號建構) | `pi_signal_builders.rs` | 直接決定下單價,§6 閘門 |
| 5 | `profile`(VP)、ATR | `vp-replay` | 研究為主,速度收益最大 |
| — | 券商、Discord listener、FastAPI、排程 | 不搬 | IO 綁定,Rust 沒有收益(RESEARCH_REPORT §7) |

`live-runtime` crate 的 coordinator / journal / TopstepX adapter **凍結,
不擴充**。它們若未來要用,也必須改成依賴 `tpx_core`,不能自帶計算。

## 6. 交易安全閘門(階段 3、4 之前必過)

碰到下單價、出場決策的模組,在 live 啟用前:

1. **影子模式**:live 引擎同時算 Python 版與 `tpx_core` 版,下單只用 Python
   版,兩者不一致就寫 log 並告警。至少跑滿 **10 個交易日**,不一致 = 0。
   (這是唯一允許兩份並存的情境,且有到期日,到期刪除。)
2. **重播**:`backend/backtest/shadow_replay.py` 用真實 live 紀錄重播,逐筆一致。
3. **Paper/練習帳戶**:先在非資金帳戶跑完整交易日。
4. 使用者明確同意後才切換。切換那次 commit 只做切換,不改任何參數(R7)。

## 7. 成本與代價(要先說清楚)

- **安裝**:`windows install.bat` / `macOS install.command` 需要能拿到
  `tpx_core`。兩個選擇:(a) 本機裝 rustup + `maturin develop`;
  (b) 在 repo 放預先編譯好的 wheel。建議 (a),並在 install 腳本檢查。
- **伺服器重啟**:沒有 `--reload` 的規則不變;Rust 改動要重新
  `maturin develop` 再重啟伺服器。
- **除錯**:Python traceback 到 Rust 邊界就停了;Rust 端要回傳有內容的錯誤,
  不能 panic(panic 會被 PyO3 轉成 `PanicException`,但訊息很差)。
- **型別邊界**:`StrategyParams` 欄位很多,不要整包搬過去;每個函式只傳它
  需要的欄位(這同時迫使依賴變明確)。

## 8. 不做清單

- 不讓 Rust 讀 `presets.json` 或任何設定檔(R3)。
- 不寫「Rust 失敗就退回 Python」的 fallback(R5)。
- 不在同一次 commit 搬模組又調參數(R7)。
- 不把 live 下單、券商連線搬到 Rust。
- 不再擴充 `live-runtime` 的平行引擎。
