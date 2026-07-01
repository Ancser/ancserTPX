"""1.0.8 研究:成交量/波動率體制分析 — 為什麼回測好、實盤翻車?

四個區塊,對應使用者提問:
  S1. 獲利天 vs 虧損天:當天 + 前一天的總成交量 / 波動率對比(回測 & 實盤各一份)。
  S2. 突破成交量 delta:進場前 10 根 vs 突破 3 根 vs 進場後 10 根;
      驗證「突破 3 根量 > 之前 → 更高成功率」假說(回測,有乾淨勝負)。
  S3. 實盤 vs 回測體制對比:兩者進場當下的量/波動分佈是否不同 →
      直接回答「回測好、實盤壞」是不是體制差異造成。
  S4. RR 實現 / 保本困難:回測 reached_tp vs 先中 SL;實盤 exit_reason + 持倉時長。

資料來源:
  - 回測:BacktestEngine(preset #3).run(candles).trades
  - 實盤:data/trade_history.json(去重多帳號 copy)
  - K 線:candle_store.load('MNQ',1) 真實成交量

Run:  PYTHONIOENCODING=utf-8 python -m scripts.volume_regime_study
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS,
    CODEX_630_PRESET_3,
    _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
LIVE_FILE = Path("data/trade_history.json")


# ─────────────────────────── candle index ───────────────────────────

class CandleIndex:
    def __init__(self, candles):
        self.c = candles
        self.ts = [x.timestamp for x in candles]
        self.vol = [float(x.volume) for x in candles]
        self.hi = [x.high for x in candles]
        self.lo = [x.low for x in candles]
        self.close = [x.close for x in candles]
        # per-candle trade date + daily aggregates
        self.tdate = [_topstep_trade_date(x.timestamp) for x in candles]
        self._daily = self._build_daily()
        self._dates_sorted = sorted(self._daily.keys())
        self._prev = {
            d: (self._dates_sorted[i - 1] if i > 0 else None)
            for i, d in enumerate(self._dates_sorted)
        }

    def _build_daily(self):
        by = defaultdict(list)
        for i, d in enumerate(self.tdate):
            by[d].append(i)
        out = {}
        for d, idxs in by.items():
            vols = [self.vol[i] for i in idxs]
            ranges = [self.hi[i] - self.lo[i] for i in idxs]
            closes = [self.close[i] for i in idxs]
            rets = [
                math.log(closes[k] / closes[k - 1])
                for k in range(1, len(closes))
                if closes[k - 1] > 0 and closes[k] > 0
            ]
            out[d] = {
                "total_vol": sum(vols),
                "range_sum": sum(ranges),
                "realized_vol": (statistics.pstdev(rets) if len(rets) > 1 else 0.0),
                "n": len(idxs),
            }
        return out

    def idx_at(self, ts: datetime) -> int:
        """Index of the 1m candle containing ts (last candle with ts_c <= ts)."""
        j = bisect.bisect_right(self.ts, ts) - 1
        return max(0, min(j, len(self.c) - 1))

    def daily(self, tdate):
        return self._daily.get(tdate)

    def prev_daily(self, tdate):
        p = self._prev.get(tdate)
        return self._daily.get(p) if p else None

    def window_mean_vol(self, lo_i, hi_i):
        lo_i = max(0, lo_i)
        hi_i = min(len(self.vol), hi_i)
        if hi_i <= lo_i:
            return None
        seg = self.vol[lo_i:hi_i]
        return sum(seg) / len(seg)


# ─────────────────────────── helpers ───────────────────────────

def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_live():
    """Dedupe multi-account copies → one record per (entry-second, price, dir)."""
    recs = json.load(open(LIVE_FILE, encoding="utf-8"))
    seen = {}
    for r in recs:
        et = r.get("entry_time", "")
        key = (et[:19], r.get("entry_price"), r.get("direction"))
        et_dt = parse_ts(et)
        if key in seen or et_dt is None:
            continue
        seen[key] = {
            "entry_time": et_dt,
            "exit_time": parse_ts(r.get("exit_time")),
            "direction": r.get("direction"),
            "entry_price": r.get("entry_price"),
            "pnl": float(r.get("pnl") or 0.0),
            "exit_reason": r.get("exit_reason"),
        }
    return sorted(seen.values(), key=lambda x: x["entry_time"])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


# ─────────────────────────── feature extraction ───────────────────────────

def trade_features(ci: CandleIndex, entry_ts, entry_price=None):
    """pre10 / brk3 / post10 mean volume + daily & prev-day regime for one trade."""
    i = ci.idx_at(entry_ts)
    brk3 = ci.window_mean_vol(i - 2, i + 1)
    pre10 = ci.window_mean_vol(i - 12, i - 2)
    post10 = ci.window_mean_vol(i + 1, i + 11)
    td = ci.tdate[i]
    d = ci.daily(td) or {}
    pd = ci.prev_daily(td) or {}
    return {
        "pre10": pre10,
        "brk3": brk3,
        "post10": post10,
        "expansion": (brk3 / pre10) if (brk3 and pre10) else None,
        "follow": (post10 / brk3) if (post10 and brk3) else None,
        "day_vol": d.get("total_vol"),
        "day_range": d.get("range_sum"),
        "day_rv": d.get("realized_vol"),
        "prev_vol": pd.get("total_vol"),
        "prev_range": pd.get("range_sum"),
        "prev_rv": pd.get("realized_vol"),
        "tdate": td,
    }


# ─────────────────────────── report blocks ───────────────────────────

def _daysplit(day_pnl, ci):
    win_days = {d: p for d, p in day_pnl.items() if p > 0}
    loss_days = {d: p for d, p in day_pnl.items() if p < 0}

    def agg(days):
        return {
            "n": len(days),
            "vol": mean([ci.daily(d)["total_vol"] for d in days if ci.daily(d)]),
            "range": mean([ci.daily(d)["range_sum"] for d in days if ci.daily(d)]),
            "rv": mean([ci.daily(d)["realized_vol"] for d in days if ci.daily(d)]),
            "prev_vol": mean([ci.prev_daily(d)["total_vol"] for d in days if ci.prev_daily(d)]),
            "prev_rv": mean([ci.prev_daily(d)["realized_vol"] for d in days if ci.prev_daily(d)]),
        }
    return agg(win_days), agg(loss_days)


def block_days(title, day_pnl, ci):
    w, l = _daysplit(day_pnl, ci)
    print(f"\n== S1 {title}:獲利天 vs 虧損天 (當天+前一天量/波動) ==", flush=True)
    print(f"{'':<10}{'days':>6}{'dayVol':>12}{'dayRange':>10}{'dayRV%':>9}{'prevVol':>12}{'prevRV%':>9}", flush=True)
    for lab, a in (("WIN days", w), ("LOSS days", l)):
        print(
            f"{lab:<10}{a['n']:>6}{a['vol']:>12,.0f}{a['range']:>10.1f}"
            f"{100*a['rv']:>9.3f}{a['prev_vol']:>12,.0f}{100*a['prev_rv']:>9.3f}",
            flush=True,
        )


def block_expansion(title, feats_wins, feats_loss):
    """S2: does breakout-3-candle volume expansion predict success?"""
    print(f"\n== S2 {title}:突破量 delta 與勝負 ==", flush=True)
    print(f"{'':<8}{'n':>6}{'pre10':>10}{'brk3':>10}{'post10':>10}{'brk3/pre':>10}{'post/brk':>10}", flush=True)
    for lab, fs in (("WIN", feats_wins), ("LOSS", feats_loss)):
        print(
            f"{lab:<8}{len(fs):>6}{mean([f['pre10'] for f in fs]):>10,.0f}"
            f"{mean([f['brk3'] for f in fs]):>10,.0f}{mean([f['post10'] for f in fs]):>10,.0f}"
            f"{mean([f['expansion'] for f in fs]):>10.2f}{mean([f['follow'] for f in fs]):>10.2f}",
            flush=True,
        )
    # bucket by expansion > 1.0
    allf = [("W", f) for f in feats_wins] + [("L", f) for f in feats_loss]
    for lo, hi, name in ((0, 1.0, "brk3<=pre10 (無放量)"),
                         (1.0, 1.5, "1.0-1.5x (小放量)"),
                         (1.5, 99, ">1.5x (大放量)")):
        sub = [tag for tag, f in allf if f["expansion"] is not None and lo <= f["expansion"] < hi]
        if sub:
            wr = 100 * sub.count("W") / len(sub)
            print(f"  {name:<22} n={len(sub):>4}  win%={wr:>5.1f}", flush=True)


def block_regime(ci, bt_feats, live_feats):
    """S3: are live entries in a different volume/volatility regime than backtest?"""
    print("\n== S3 實盤 vs 回測:進場當下體制對比 ==", flush=True)
    print(f"{'':<10}{'n':>6}{'brk3(med)':>11}{'pre10(med)':>11}{'expansion':>11}{'dayVol(med)':>13}{'dayRV%':>9}", flush=True)
    for lab, fs in (("BACKTEST", bt_feats), ("LIVE", live_feats)):
        print(
            f"{lab:<10}{len(fs):>6}{med([f['brk3'] for f in fs]):>11,.0f}"
            f"{med([f['pre10'] for f in fs]):>11,.0f}{med([f['expansion'] for f in fs]):>11.2f}"
            f"{med([f['day_vol'] for f in fs]):>13,.0f}{100*mean([f['day_rv'] for f in fs]):>9.3f}",
            flush=True,
        )


def block_rr(bt_trades, live):
    """S4: RR realization + hold time — 保本難 / RR 太大一直止損 / 拖很久."""
    print("\n== S4 RR 實現 / 保本 / 持倉時長 ==", flush=True)
    # backtest post-breakout reachability
    reached = [t for t in bt_trades if getattr(t, "post_breakout_reached_tp", None) is True]
    sl_first = [t for t in bt_trades if getattr(t, "post_breakout_broke_sl_first", None) is True]
    print(f"回測 n={len(bt_trades)}  reached_TP(60m內)={len(reached)}  先中SL={len(sl_first)}", flush=True)
    bt_dur = [t.duration_minutes for t in bt_trades if t.duration_minutes is not None]
    print(f"回測 持倉分鐘 median={med(bt_dur):.0f} mean={mean(bt_dur):.0f}", flush=True)
    from collections import Counter
    bt_reasons = Counter(t.exit_reason.value if t.exit_reason else "?" for t in bt_trades)
    print(f"回測 exit_reason={dict(bt_reasons)}", flush=True)

    # live
    live_dur = [(x["exit_time"] - x["entry_time"]).total_seconds() / 60
                for x in live if x["exit_time"] and x["entry_time"]]
    lr = Counter(x["exit_reason"] for x in live)
    print(f"\n實盤 n={len(live)}  exit_reason={dict(lr)}", flush=True)
    print(f"實盤 持倉分鐘 median={med(live_dur):.0f} mean={mean(live_dur):.0f}", flush=True)
    # hold time split by outcome
    win_dur = [(x["exit_time"] - x["entry_time"]).total_seconds() / 60
               for x in live if x["pnl"] > 0 and x["exit_time"]]
    loss_dur = [(x["exit_time"] - x["entry_time"]).total_seconds() / 60
                for x in live if x["pnl"] <= 0 and x["exit_time"]]
    print(f"實盤 WIN持倉median={med(win_dur):.0f}  LOSS持倉median={med(loss_dur):.0f}", flush=True)


# ─────────────────────────── main ───────────────────────────

def main():
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    ci = CandleIndex(candles)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    # ── backtest (preset #3) ──
    preset = BUILTIN_PRESETS[CODEX_630_PRESET_3]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    params = _build_strategy_params(preset, cid)
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid), fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    bt = BacktestEngine(config=config, strategy_params=params,
                        zone_timeline=None, record_equity=False).run(candles)
    bt_trades = [t for t in bt.trades if t.exit_time is not None]
    print(f"\nbacktest trades={len(bt_trades)}  pnl={bt.metrics.total_pnl:+.0f}  "
          f"win%={100*bt.metrics.win_rate:.1f}", flush=True)

    # backtest per-day pnl (by CT-17:00 trade date)
    bt_day = defaultdict(float)
    for t in bt_trades:
        bt_day[_topstep_trade_date(t.entry_time)] += (t.pnl or 0.0)

    bt_feats = [trade_features(ci, t.entry_time, t.entry_price) for t in bt_trades]
    bt_win_f = [f for t, f in zip(bt_trades, bt_feats) if (t.pnl or 0) > 0]
    bt_loss_f = [f for t, f in zip(bt_trades, bt_feats) if (t.pnl or 0) <= 0]

    # ── live ──
    live = load_live()
    live = [x for x in live if x["entry_time"]]
    live_day = defaultdict(float)
    for x in live:
        live_day[_topstep_trade_date(x["entry_time"])] += x["pnl"]
    live_feats = [trade_features(ci, x["entry_time"], x["entry_price"]) for x in live]
    live_win_f = [f for x, f in zip(live, live_feats) if x["pnl"] > 0]
    live_loss_f = [f for x, f in zip(live, live_feats) if x["pnl"] <= 0]
    lwin = sum(1 for x in live if x["pnl"] > 0)
    print(f"live trades (deduped)={len(live)}  pnl={sum(x['pnl'] for x in live):+.0f}  "
          f"win%={100*lwin/len(live):.1f}", flush=True)

    # ── blocks ──
    block_days("回測", bt_day, ci)
    block_days("實盤", live_day, ci)
    block_expansion("回測", bt_win_f, bt_loss_f)
    block_expansion("實盤", live_win_f, live_loss_f)
    block_regime(ci, bt_feats, live_feats)
    block_rr(bt_trades, live)


if __name__ == "__main__":
    main()
