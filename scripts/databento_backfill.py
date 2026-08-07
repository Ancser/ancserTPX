"""1.0.10: 用 Databento 補齊 2026 年初到本機資料起點的 1m K 棒。

券商只保留約 60 天,本機 MNQ 從 2026-05-10 才開始,1–4 月完全空白 ——
那段用 TopstepX 永遠補不回來。Databento 只作為**歷史訓練資料**來源;
live 與日常累積仍然只靠 TopstepX,補進來的 bar 標記 source="databento"。

════════════════════════════════════════════════════════════════
為什麼不能只套一個偏移(這是本腳本的核心)
════════════════════════════════════════════════════════════════
兩邊的價格基準完全不同,而且各自有各自的接縫:

  Databento MNQ.v.0   未調整原始價。官方明說不做回溯調整,所以這條序列
                      **自己在三月的 H26→M26 換月處就有一個原始跳空**。

  本機 TopstepX       已回溯調整。實測 2026-05-13 與 06-05 兩天各 30 根,
                      本機 = MNQM6 + 269.25,偏移完全固定。

所以要接得上,必須做三步,少一步都會留下假跳空:

  1. 把 Databento 序列**內部**的換月跳空補平(回溯調整到最新段)
  2. 用 05-10~05-20 的**重疊區**量出整條相對本機的偏移
  3. 套用該偏移 → 現在才跟本機同一個錨點

第 2 步是唯一能證明接縫正確的方法,重疊區的錢不能省。

════════════════════════════════════════════════════════════════
絕不修改既有資料
════════════════════════════════════════════════════════════════
使用者的決定:六月至今的價格不得變動。本腳本只寫入**嚴格早於**本機
起始時間的 bar;重疊區只用來量偏移與驗證,不寫入。

另見 backend/data/candle_store.py 的 detect_reanchor():那裡修的是
**未來**的換月(2026-09 的 U26→Z26),讓 store 自己成為錨點。
已知的 2026-06-11 接縫記在 meta 的 known_seams,不修改資料。

用法:
    python scripts/databento_backfill.py --symbol MNQ --dry-run
    python scripts/databento_backfill.py --symbol MNQ --verify-only
    python scripts/databento_backfill.py --symbol MNQ --merge
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
ROLL = "v"          # 成交量排名 —— 與本機引擎的換月規則一致(非日曆換月)
RANK = 0

def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def load_local(symbol):
    from backend.data import candle_store
    bars = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    return {_utc(c.timestamp): c for c in bars}, bars


def find_rolls(df):
    """換月點 = instrument_id 改變。

    **不要用跳幅大小去猜。** 第一版就是那樣寫的,結果把 2026-04-10 12:30
    (美東 8:30 數據發布)的 +69.00 點真實行情誤判成換月抹平了 —— 那一分鐘
    成交量從 2,690 暴增到 3,881 且價格站穩不回補,原始 MNQM6 合約本身有
    完全相同的跳空。真換月時 instrument_id 一定變,行情波動時一定不變。
    """
    rolls = []
    prev_iid = None
    prev_close = None
    for ts, r in df.iterrows():
        iid = int(r["instrument_id"])
        if prev_iid is not None and iid != prev_iid and prev_close is not None:
            rolls.append((_utc(ts.to_pydatetime()),
                          float(r["open"]) - prev_close, prev_iid, iid))
        prev_iid = iid
        prev_close = float(r["close"])
    return rolls


def flatten_rolls(bars, rolls):
    """把換月造成的跳空補平,錨定到**最新**的那一段。

    每個接縫之前的所有 bar 都加上該跳幅,使序列連續。
    """
    out = list(bars)
    for seam_ts, jump, _a, _b in rolls:
        out = [replace(c, open=c.open + jump, high=c.high + jump,
                       low=c.low + jump, close=c.close + jump)
               if _utc(c.timestamp) < seam_ts else c
               for c in out]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--overlap-days", type=int, default=10,
                    help="超出本機起點的重疊天數,用來量偏移並驗證接縫")
    ap.add_argument("--dry-run", action="store_true", help="只估算,不呼叫 API")
    ap.add_argument("--verify-only", action="store_true", help="下載並驗證,不寫入")
    ap.add_argument("--merge", action="store_true", help="驗證通過才寫入 store")
    ap.add_argument("--max-cost", type=float, default=2.00,
                    help="報價超過此金額就中止,避免打錯參數噴額度")
    a = ap.parse_args()

    local, local_bars = load_local(a.symbol)
    if not local_bars:
        print(f"✘ [{a.symbol}] 本機無資料 —— 無從量測偏移,拒絕執行", file=sys.stderr)
        sys.exit(1)
    first = _utc(local_bars[0].timestamp)
    last = _utc(local_bars[-1].timestamp)
    print(f"[{a.symbol}] 本機 {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}"
          f"  {len(local_bars):,} 根")

    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    end = first + timedelta(days=a.overlap_days)
    print(f"  下載 {start:%Y-%m-%d} → {end:%Y-%m-%d}"
          f"  (其中約 {a.overlap_days} 天與本機重疊)")
    print(f"  符號 {a.symbol}.{ROLL}.{RANK}  stype_in=continuous  schema={SCHEMA}")
    print(f"  只會寫入嚴格早於 {first:%Y-%m-%d %H:%M} 的 bar;既有資料一律不動")

    if a.dry_run:
        print("\n--dry-run:未呼叫 API,未產生任何費用")
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        print("\n✘ .env 裡的 DATABENTO_API_KEY 是空的", file=sys.stderr)
        sys.exit(1)

    try:
        import databento as db
    except ImportError:
        print("\n✘ 請先 pip install databento", file=sys.stderr)
        sys.exit(1)

    client = db.Historical(key)
    kw = dict(dataset=DATASET, symbols=[f"{a.symbol}.{ROLL}.{RANK}"],
              schema=SCHEMA, stype_in="continuous",
              start=start.isoformat(), end=end.isoformat())

    cost = client.metadata.get_cost(**kw)        # 純查價,不扣額度
    cnt = client.metadata.get_record_count(**kw)
    print(f"\n  官方報價 ${cost:.4f}   筆數 {cnt:,}")
    if cost > a.max_cost:
        print(f"✘ 超過上限 ${a.max_cost:.2f},中止", file=sys.stderr)
        sys.exit(1)
    if not (a.verify_only or a.merge):
        print("  未指定 --verify-only / --merge,到此為止(未下載)")
        return

    df = client.timeseries.get_range(**kw).to_df().sort_index()
    print(f"  下載完成 {len(df):,} 筆")
    if "instrument_id" not in df.columns:
        print("✘ 回應缺少 instrument_id,無法可靠辨識換月,中止", file=sys.stderr)
        sys.exit(1)
    rolls = find_rolls(df)

    from backend.db.models import Candle
    bars = []
    for ts, row in df.iterrows():
        bars.append(Candle(
            timestamp=_utc(ts.to_pydatetime()),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=int(row["volume"]), symbol=a.symbol, interval="1m",
            source="databento",
        ))
    bars.sort(key=lambda c: c.timestamp)

    # ── 步驟 1:補平 Databento 序列內部的換月跳空 ──────────────
    print("\n═══ 步驟 1:補平內部換月(依 instrument_id) ═══")
    if rolls:
        for ts, jump, a_id, b_id in rolls:
            print(f"  {ts:%Y-%m-%d %H:%M}  iid {a_id} → {b_id}"
                  f"  跳空 {jump:+.2f} 點 → 已補平")
    else:
        print("  區間內未發生換月")
    bars = flatten_rolls(bars, rolls)

    # ── 步驟 2:用重疊區量偏移 ──────────────────────────────
    print("\n═══ 步驟 2:量測相對本機的偏移 ═══")
    diffs = {}
    n_ov = 0
    for b in bars:
        old = local.get(b.timestamp)
        if old is not None:
            n_ov += 1
            d = round(old.close - b.close, 4)
            diffs[d] = diffs.get(d, 0) + 1
    if n_ov < 100:
        print(f"  ✘ 重疊只有 {n_ov} 根,不足以可靠量測偏移。中止。", file=sys.stderr)
        sys.exit(2)
    offset, hits = max(diffs.items(), key=lambda kv: kv[1])
    agree = hits / n_ov
    print(f"  重疊 {n_ov:,} 根,主導偏移 {offset:+.2f} 點,一致度 {agree*100:.1f}%")
    if agree < 0.90:
        top = sorted(diffs.items(), key=lambda kv: -kv[1])[:5]
        print(f"  ✘ 一致度不足九成 —— 兩邊不是同一條序列。前五名偏移:{top}",
              file=sys.stderr)
        sys.exit(2)

    bars = [replace(c, open=c.open + offset, high=c.high + offset,
                    low=c.low + offset, close=c.close + offset) for c in bars]

    # ── 步驟 3:套用後重新驗證 ──────────────────────────────
    print("\n═══ 步驟 3:套用後驗證 ═══")
    bad = 0
    worst = 0.0
    for b in bars:
        old = local.get(b.timestamp)
        if old is not None:
            d = abs(b.close - old.close)
            worst = max(worst, d)
            if d > 1e-6:
                bad += 1
    print(f"  重疊區殘差 >0 的 bar:{bad} / {n_ov} = {bad/n_ov*100:.2f}%")
    print(f"  最大殘差:{worst:.2f} 點")
    if bad / n_ov > 0.01:
        print("  ✘ 殘差過大,拒絕合併", file=sys.stderr)
        sys.exit(2)
    print("  ✔ 接縫一致")

    # ── 步驟 4:只寫入早於本機起點的 bar ─────────────────────
    fill = [b for b in bars if b.timestamp < first]
    print(f"\n═══ 步驟 4:待寫入 ═══")
    print(f"  下載 {len(bars):,} 根,其中早於本機起點的 {len(fill):,} 根")
    if fill:
        print(f"  範圍 {fill[0].timestamp:%Y-%m-%d %H:%M}"
              f" → {fill[-1].timestamp:%Y-%m-%d %H:%M}")

    if not a.merge:
        print("\n(--verify-only:未寫入)")
        return

    from backend.data import candle_store
    merged = local_bars + fill
    candle_store.save(merged, a.symbol, 1)
    candle_store._record_seam(a.symbol, 1, {
        "timestamp": first.isoformat(),
        "kind": "databento_backfill_join",
        "offset_applied": offset,
        "internal_rolls_flattened": [[str(t), j, a, b] for t, j, a, b in rolls],
        "overlap_bars": n_ov,
        "detail": f"{fill[0].timestamp:%Y-%m-%d}~{fill[-1].timestamp:%Y-%m-%d} "
                  f"來自 Databento,已對齊本機錨點,重疊區殘差 {worst:.2f} 點",
        "action": "接縫已驗證;此點之前的 bar source='databento'",
    })
    print(f"\n✔ 已寫入:{len(local_bars):,} → {len(merged):,} 根")
    print(f"  接縫已登記到 meta 的 known_seams")


if __name__ == "__main__":
    main()
