# UI renewal 交接：Age of Dev v2 線條與版面語言 → ancserTPX

> 來源：Age of Dev（`F:/ancserEntertatinment/Age of Wealth`）的新介面 v2——`Assets/AgeOfDev/Scripts/UiTokens.cs`、
> `Docs/UI_STANDARD.md` §9、`RELAYOUT/RELAYOUT_SPEC.md`、`RELAYOUT/MOTION_SPEC.md`。2026-11-08 由 Age of Dev 那邊的 Claude 整理。
> 使用者要求：**除了光之外**的線條與版面指引交給 ancserTPX，由 ancserTPX 的對話接著做 UI renewal——
> 標題中英、按鈕與背景文字、套到 BACKTEST／LIVE 等大按鈕、類似書名號「」的按鈕角落、動畫。
>
> 這份是**設計輸入**，不是實作。照 ancserTPX 自己的 `CLAUDE.md`／`AGENTS.md`／`docs/VERSION_CONTROL.md` 規則做，
> 和 `docs/ui-design-system.md` 的「Relayout v2 progress」對齊（那份已經排除所有光效、模糊背景；這份也一樣）。

## 0. 不做的（光）

全部排除：外發光（glow／outer glow）、按鈕按下的光環 ripple、進度條掃光 shimmer、底部光條／漸層光、呼吸或脈動的光、
揭曉頁光束、模糊背景。**狀態一律用文字顏色、淡底色、≤ 11 px 的小圓點／小方塊表示，不用光。**

## 1. 基本規則（Age of Dev 使用者明確要求過的）

1. **不畫左側色條**（任何一邊的色條都不要）：列表、卡片、按鈕、晶片、選中狀態、通知都不畫。（ancserTPX 1.5.1e 已經拿掉 rail 的品牌條，方向一致。）
2. **不畫框線**：卡片、列、按鈕不畫外框，靠亮度分層與留白分開。唯一的「線」是 §4 的角落「」。
3. **每個畫面只有一個橘色主要按鈕**（`.btn-primary`）；選中狀態是白色亮度，不是橘色。
4. **數字一律等寬字**（JetBrains Mono／Consolas），表格數字靠右對齊、tabular。
5. 介面上不寫括號規則說明；規則寫在文件，介面只有名稱與數字（懸停提示可以解釋）。

## 2. Tokens（深色，對照 Age of Dev UiTokens）

| 用途 | 值 | CSS 建議 |
|---|---|---|
| 底色 | `#0E1116` | `--bg` |
| 面 1／2／3（亮度分層） | 白 5%／9%／12% | `--surface-1: rgba(255,255,255,.05)` … |
| 選中 | 白 14% | `--surface-selected` |
| 頂光線（卡片上緣 1 px） | 白 8% | `--highlight-top`（**這是 1 px 的亮度線，不是光效**） |
| 凹下（輸入框） | 黑 25% | `--sunken` |
| 文字 主／次／小標／不能用 | 白 92%／64%／40%／30% | `--text-1/2/3/disabled` |
| 主要按鈕 | `#F5A623`，hover `#FFB84D`，按下 `#E68C14`，字 `#0E1116` | `--accent*` |
| 狀態（只用在文字與小點） | 好 `#7FBF9A`、警告 `#C9A35F`、壞 `#C27272`、資訊 `#A9B8CC` | `--pos/--warn/--neg/--info` |
| 數字跳動的增減閃色 | 增 `#3DDC97`、減 `#FF5A5F` | 只在數字變動的那一下 |
| 角落記號 | 白 35% | `--tick` |
| 字級 | Display 40／H1 28／H2 20／數字 32／內文 15／說明 13／**Micro 10** | |
| 間距 | 4 px 格線：4／8／12／16／24／32／48 | |

ancserTPX 已有自己的 v2 tokens：**用 ancserTPX 的變數名，數值以這張表為參考**，不要另外再定義一套。

## 3. 標題：中文主標＋英文微標（Micro）

- **英文微標**：全大寫、等寬字、10–11 px、字距 `0.12em`、白 40%，放在中文標題**上方**（或同一行左邊）。
  例：`BACKTEST` 上、`回測` 下；`LIVE` 上、`實盤` 下。
- **中文主標**：粗體，頁標題 28 px、區塊標題 20 px、卡片標題 15–16 px，白 92%。
- 區塊標題：`01 · CONFIG` 這種編號微標＋中文標題（ancserTPX 已經是「numbered micro labels with no bar」，沿用）。
- 頁面標題旁可加麵包屑（`主畫面 / 回測`，13 px、白 40%）。

```css
.micro { font: 400 10px/1.2 "JetBrains Mono", Consolas, monospace; letter-spacing: .12em;
         text-transform: uppercase; color: var(--text-3); }
.title-zh { font-weight: 700; font-size: 20px; color: var(--text-1); }
```

## 4. 角落「」記號（書名號角落）

Age of Dev 用 `UiTokens.CornerTicks`：**四個 L 形角**，每邊長 8 px、粗 1 px、白 35%，**畫在框外**——
頁面內容區外擴 16 px，彈窗外擴 12 px，鎖住的提示框外擴 8 px。沒有其他框線。

給 ancserTPX 的大按鈕／面板：

- **頁面與主面板**：四角（同上）。
- **大按鈕（BACKTEST、LIVE、EXECUTE BACKTEST…）**：像書名號——**左上「＋右下」兩個角**，外擴 4–6 px；
  滑過時兩個角各往外移 2 px（120 ms，Standard），按下時回到 0。小按鈕不加角。
- 角只是 1 px 線，**不發光**。

```css
.ticks { position: relative; }
.ticks::before, .ticks::after { content: ""; position: absolute; width: 8px; height: 8px; pointer-events: none;
  border-color: var(--tick); border-style: solid; transition: transform 120ms cubic-bezier(.2,0,0,1); }
.ticks::before { left: -6px; top: -6px;   border-width: 1px 0 0 1px; }   /* 「 */
.ticks::after  { right: -6px; bottom: -6px; border-width: 0 1px 1px 0; } /* 」 */
.ticks:hover::before { transform: translate(-2px, -2px); }
.ticks:hover::after  { transform: translate(2px, 2px); }
/* 四角版本：外層容器再加一組 ::before/::after，或用 4 個 span／背景漸層畫 */
```

## 5. 大按鈕＋背景文字（Age of Dev 主畫面入口按鈕的做法）

結構（每顆大按鈕三層字）：

1. 左上：英文微標（§3），例 `BACKTEST`。
2. 左下：中文名稱，粗體 26 px（主要按鈕 34 px），例 `回測`。
3. **背景大字（watermark）**：同一個英文字，粗體、字高約按鈕高度的 95%，靠**右下**、超出的部分被按鈕裁掉；
   顏色：一般按鈕白 **4.5%**，橘色主要按鈕上用黑 **6%**。不動、不發光。

尺寸參考（1920 寬）：主要大按鈕 520×140，其他 440×92，間距 16／12；`overflow: hidden`。
底色：一般按鈕 = `--bg` 59%（hover 75%）＋上緣 1 px 白 8%；主要按鈕 = 橘色實心，字用 `#0E1116`。
狀態（進行中、要注意）只寫在中文名稱下面的一行小字（14 px），用文字顏色表示——**不加進度條、色條、光**
（Age of Dev Z402：入口按鈕只靠副標文字顏色）。

```html
<button class="hero-btn ticks" data-wm="BACKTEST">
  <span class="micro">BACKTEST</span>
  <span class="hero-zh">回測</span>
  <span class="hero-sub">上次 2026-11-06 · 214 筆</span>
</button>
```
```css
.hero-btn { position: relative; overflow: visible; isolation: isolate; background: rgba(14,17,22,.59);
  box-shadow: inset 0 1px 0 var(--highlight-top); border: 0; text-align: left; padding: 14px 28px; }
.hero-btn > .wm-clip { position: absolute; inset: 0; overflow: hidden; z-index: -1; }   /* 背景字只在按鈕裡裁切；角記號在外面 */
.hero-btn .wm-clip::after { content: attr(data-wm); position: absolute; right: -2%; bottom: -18%;
  font: 800 calc(var(--h) * .95) / 1 Inter, sans-serif; color: rgba(255,255,255,.045); white-space: nowrap; }
.hero-btn.btn-primary { background: var(--accent); color: var(--ink); }
.hero-btn.btn-primary .wm-clip::after { color: rgba(0,0,0,.06); }
.hero-btn:hover { background: rgba(14,17,22,.75); }
```
（`.ticks` 的角要在按鈕外面，所以背景字的裁切放在內層 `.wm-clip`，按鈕本身不要 `overflow:hidden`。）

套用範圍建議：工作區頁籤 **BACKTEST／LIVE**（rail 的大入口）、主要動作 **EXECUTE BACKTEST**（唯一橘色）、Research 的主要入口。
頁面標題區也可以放一個頁面級的背景大字（例 `RESEARCH`，白 3–4%，靠右，被內容區裁切）。

## 6. 動畫（MOTION_SPEC，去掉光的部分）

**時間**：instant 80／fast 120／base 200／medium 320／slow 480／xslow 800 ms。
**曲線**：Standard `cubic-bezier(.2,0,0,1)`；進場 EmphDecel `(.05,.7,.1,1)`；離場 EmphAccel `(.3,0,.8,.15)`；按下 `(.4,0,1,1)`；彈回 Pop（overshoot 1.4）。
**原則**：進場減速、離場加速，離場時間約進場的 60–70%；一組元素一起進場、每個錯開 40 ms、最多 8 個；離場不錯開；**輸入最多鎖 400 ms**；尊重 `prefers-reduced-motion`。
**距離**：4（按下位移）／12（內容淡入上移）／24（標題、頁籤切換）／40（通知、抽屜）／80（大按鈕離場）。

| 互動 | 規格 |
|---|---|
| 大按鈕按下 | scale 1→.96（80 ms Press），放開 Pop 160 ms 回 1；**不要光環／外發光** |
| 換頁（大按鈕 → 頁面） | 其他大按鈕往右 80 px、淡出，錯開 30 ms（EmphAccel）；頁面標題（英文微標＋中文）從 x+24 淡入（EmphDecel 200）；內容 y+12 淡入、錯開 40 ms；400 ms 可操作 |
| 返回 | 反過來、壓縮到約 360 ms、不錯開；大按鈕從右邊 80 px 回來，錯開 30 ms |
| 頁籤切換 | 選中的是一塊白 14% 的**長方形滑過去**（200 ms Standard），**不是底線、也不是左條**；舊內容往反方向 24 px 淡出 120 ms，新內容 24 px 淡入 200 ms |
| 列表列 hover | 底色→白 9%（120 ms），內容往右 4 px |
| 數字（PnL、勝率、筆數） | 等寬字逐位滾動（odometer）400 ms（變化 ≥ 10 倍時 800 ms），增減那一下用增／減色，然後回到一般色；快速連續更新時合併 |
| 通知 | 從右邊 40 px 淡入（200），最多 3 則，4 秒後淡出（160） |
| 彈窗 | 開：scale .96→1、y+12→0、淡入 200；關：淡出、scale→.98（120） |
| 角落「」 | hover 外移 2 px（120），按下回 0 |

## 7. 對照圖（不進 ancserTPX 的 git）

Age of Dev QA 截圖在 `F:/ancserEntertatinment/ancserAssets/AgeOfDev/ArtSource/Previews/`：
- `ui_dark_hub_status_1440x900.png`：入口大按鈕（英文微標＋中文＋背景大字，狀態只用文字顏色）
- `ui_dark_market_ghost_1440x900.png`：市場頁（頁面角記號、英文微標＋中文表頭、品牌色只用在條本身）
- `ui_dark_cards_1440x900.png`：策略桌彈窗（彈窗外擴 12 px 的四角）
- `ui_dark_tech_vertical_1440x900.png`：頁籤（白色選中塊）

> 注意：這些截圖裡市場頁的卡片底部還有淡淡的顏色漸層——那是光，**ancserTPX 不要做**。

## 8. 建議做法（給 ancserTPX 的對話）

1. 先讀 ancserTPX 的 `CLAUDE.md`、`AGENTS.md`、`docs/HANDOFF.md`、`docs/ui-design-system.md`，把這份的項目排進
   「Relayout v2 progress」的 Phase 2～6（大按鈕與角記號→Phase 2/3；動畫→Phase 6）。
2. 共用元件（`.micro`、`.ticks`、`.hero-btn`）做成**一份契約**套到所有實例，不要為某一顆按鈕加專屬選擇器（ancserTPX CLAUDE.md 的 1.0.10o 教訓）。
3. Playwright 的 design-system.spec.js 要跟著改（目前還釘著舊的 Orbitron watermark、title left border、#btn-backtest outline）。
4. 不動策略、行情、下單邏輯；這是純介面工作。
