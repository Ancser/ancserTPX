"""Can a simple, causal filter rescue the Volume Profile breakout baseline?

Pre-declared before any outcome was read (2026-09-27).  The baseline is the
frozen VP reference ledger produced by the production BacktestEngine
(``vp_baseline_python_reference_2022_2026_*``: breakout/retest, 70% prior-RTH
value area, 2-bar confirm, 2-tick buffers, ATR SL 1.5 / TP 2, max 2/day).

Every feature is known before the entry bar:

  H1 open_outside   RTH open already beyond the prior edge in the trade's
                    direction (long: open > prior VAH, short: open < prior VAL).
                    Dalton "open outside value" = initiative, breakouts extend.
  H3 narrow_value   prior VA width / mean RTH range of the prior 20 sessions is
                    below its 2022-2023 median (balance -> imbalance).
  H4 trend_aligned  long only when the prior RTH close is above the mean of the
                    prior 20 RTH closes; short only when below.
  H5 morning        entry before 11:30 ET.
  H6 first_trade    first trade of that RTH session only.
  H7 vol_expansion  prior-session RTH range / mean of the prior 20 ranges > 1.
  H8 open_outside AND trend_aligned (the only pre-declared conjunction).

Periods: train 2022-2023, validation 2024-2025, 2026 (retrospective: the 2026
baseline has been looked at before, these filters have not).  Symbols: MNQ
and MES (cross-instrument replication).

A filter PASSES only if, for BOTH symbols and ALL three periods: n >= 20,
PF > 1 after costs and PF above the unfiltered baseline of that cell; stress
PF > 1 in validation and 2026 (MNQ 14 ticks = $7, MES 4 ticks = $5, the
per-symbol thresholds in the 1.0.9 research record); and the full-period
three-segment walk-forward passes for both symbols.

Filtering a ledger assumes a skipped trade would not have been replaced by a
later one.  The 2026-09-23 OI-gate engine replay found zero replacement
entries for this strategy; a passing filter must still be re-run through
BacktestEngine before any preset changes.

Run:  python scripts/vp_filter_study.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.robustness import series_stats, tick_value, walk_forward

RESEARCH = Path(r"F:\ancserQuant\ancserMarketData\derived\research")
LEDGER_DIR = RESEARCH / "vp_baseline_python_reference_2022_2026_20260925T0645Z"
LEDGERS = {
    "MNQ": LEDGER_DIR / "mnq_vp_baseline_trades_2022-01-01_2026-09-23.csv.gz",
    "MES": LEDGER_DIR / "mes_vp_baseline_trades_2022-01-01_2026-09-14.csv.gz",
}
PROFILES = RESEARCH / "vp_rust_profile_parity_20260925T0655Z" / "python_daily_profiles.csv"
BARS_DIR = RESEARCH / "rust_arrow_full_export_2022_2026_20260925T0632Z"
STRESS_TICKS = {"MNQ": 14, "MES": 4}
PERIODS = (("train", 2022, 2023), ("validation", 2024, 2025), ("2026", 2026, 2026))
LOOKBACK = 20
MORNING_CUTOFF_MIN = 11 * 60 + 30
MIN_N = 20

FILTERS = ("open_outside", "narrow_value", "trend_aligned", "morning",
           "first_trade", "vol_expansion", "open_outside_and_trend")


def rth_sessions(symbol: str) -> pd.DataFrame:
    """RTH open/high/low/close per ET session from the export the ledger used.

    ``is_rth`` and ``timestamp_date_et`` were stamped by the exporter from the
    repository session functions; this reads them instead of redefining RTH.
    """
    frames = []
    for path in sorted(BARS_DIR.glob(f"{symbol.lower()}_*.parquet")):
        table = pq.read_table(path, columns=[
            "source_timestamp_utc", "timestamp_date_et", "is_rth",
            "open", "high", "low", "close"])
        frame = table.to_pandas()
        frames.append(frame[frame["is_rth"]])
    bars = pd.concat(frames).sort_values("source_timestamp_utc")
    grouped = bars.groupby("timestamp_date_et", sort=True)
    sessions = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "bars": grouped.size(),
    })
    sessions.index = pd.to_datetime(sessions.index).date
    return sessions


def session_features(symbol: str) -> pd.DataFrame:
    sessions = rth_sessions(symbol)
    profiles = pd.read_csv(PROFILES)
    profiles = profiles[profiles["symbol"] == symbol].copy()
    profiles["date"] = pd.to_datetime(profiles["source_session_date_et"]).dt.date
    profiles = profiles.set_index("date").sort_index()
    frame = sessions.join(profiles[["poc", "vah", "val"]], how="left")
    frame["range"] = frame["high"] - frame["low"]
    # Everything below is shifted one session: the value for day D uses only
    # sessions strictly before D (the profile row of D-1 is D's prior value).
    prior = pd.DataFrame(index=frame.index)
    prior["prior_vah"] = frame["vah"].shift(1)
    prior["prior_val"] = frame["val"].shift(1)
    prior["prior_close"] = frame["close"].shift(1)
    prior["prior_range"] = frame["range"].shift(1)
    prior["mean_close_20"] = frame["close"].shift(1).rolling(LOOKBACK).mean()
    prior["mean_range_20"] = frame["range"].shift(1).rolling(LOOKBACK).mean()
    prior["rth_open"] = frame["open"]            # known at 09:30, before any entry
    prior["va_width_ratio"] = (prior["prior_vah"] - prior["prior_val"]) / prior["mean_range_20"]
    prior["range_expansion"] = prior["prior_range"] / prior["mean_range_20"]
    return prior


def load_trades(symbol: str) -> pd.DataFrame:
    with gzip.open(LEDGERS[symbol], "rt", encoding="utf-8") as handle:
        trades = pd.read_csv(handle)
    trades["date"] = pd.to_datetime(trades["rth_session_date_et"]).dt.date
    entry_et = pd.to_datetime(trades["entry_time_et"].str.slice(0, 19))
    trades["entry_minute_et"] = entry_et.dt.hour * 60 + entry_et.dt.minute
    trades["year"] = entry_et.dt.year
    trades["is_long"] = trades["direction"].str.lower().eq("buy")
    trades = trades.sort_values("entry_time_utc").reset_index(drop=True)
    trades["trade_of_day"] = trades.groupby("date").cumcount() + 1
    return trades


def attach(symbol: str) -> pd.DataFrame:
    trades = load_trades(symbol)
    features = session_features(symbol)
    joined = trades.join(features, on="date")
    missing = joined["prior_vah"].isna() | joined["mean_range_20"].isna()
    joined = joined[~missing].copy()
    # Positive assertion that the join lines up: a VAH breakout long should
    # enter near the prior VAH (and a VAL short near the prior VAL).
    edge = joined["prior_vah"].where(joined["is_long"], joined["prior_val"])
    near = (joined["entry_price"] - edge).abs() <= 0.5 * joined["mean_range_20"]
    joined.attrs["edge_alignment"] = float(near.mean())
    joined.attrs["dropped_for_warmup"] = int(missing.sum())
    return joined


def masks(frame: pd.DataFrame, narrow_cut: float) -> dict:
    long_ = frame["is_long"]
    open_outside = (long_ & (frame["rth_open"] > frame["prior_vah"])) | (
        ~long_ & (frame["rth_open"] < frame["prior_val"]))
    trend = (long_ & (frame["prior_close"] > frame["mean_close_20"])) | (
        ~long_ & (frame["prior_close"] < frame["mean_close_20"]))
    return {
        "open_outside": open_outside,
        "narrow_value": frame["va_width_ratio"] < narrow_cut,
        "trend_aligned": trend,
        "morning": frame["entry_minute_et"] < MORNING_CUTOFF_MIN,
        "first_trade": frame["trade_of_day"] == 1,
        "vol_expansion": frame["range_expansion"] > 1.0,
        "open_outside_and_trend": open_outside & trend,
    }


def cell(frame: pd.DataFrame, symbol: str) -> dict:
    pnls = frame["net_pnl"].astype(float).tolist()
    base = series_stats(pnls)
    charge = STRESS_TICKS[symbol] * tick_value(symbol)
    stress = series_stats([p - charge for p in pnls])
    return {"n": base["n"], "pnl": round(base["pnl"], 2), "pf": round(base["pf"], 4),
            "max_dd": round(base["max_dd"], 2), "stress_pf": round(stress["pf"], 4),
            "stress_pnl": round(stress["pnl"], 2), "dates": int(frame["date"].nunique())}


def period(frame: pd.DataFrame, first: int, last: int) -> pd.DataFrame:
    return frame[(frame["year"] >= first) & (frame["year"] <= last)]


def run() -> dict:
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    verdicts = {name: True for name in FILTERS}
    for symbol in ("MNQ", "MES"):
        frame = attach(symbol)
        train = period(frame, 2022, 2023)
        narrow_cut = float(train["va_width_ratio"].median())
        flags = masks(frame, narrow_cut)
        sym = {"edge_alignment": frame.attrs["edge_alignment"],
               "dropped_for_warmup": frame.attrs["dropped_for_warmup"],
               "narrow_value_cut": narrow_cut, "baseline": {}, "filters": {}}
        for label, first, last in PERIODS:
            sym["baseline"][label] = cell(period(frame, first, last), symbol)
        sym["baseline"]["all"] = cell(frame, symbol)
        for name in FILTERS:
            kept = frame[flags[name]]
            dropped = frame[~flags[name]]
            out = {"kept": {}, "dropped": {}}
            for label, first, last in PERIODS:
                out["kept"][label] = cell(period(kept, first, last), symbol)
                out["dropped"][label] = cell(period(dropped, first, last), symbol)
            out["kept"]["all"] = cell(kept, symbol)
            out["dropped"]["all"] = cell(dropped, symbol)
            wf = walk_forward([{"entry_time": t, "pnl": p} for t, p in
                               zip(kept["entry_time_utc"], kept["net_pnl"])])
            out["walk_forward"] = None if wf is None else {
                "pass": wf["pass"],
                "segments": [{k: round(v, 4) if isinstance(v, float) else v
                              for k, v in seg.items()} for seg in wf["segments"]]}
            reasons = []
            for label, _first, _last in PERIODS:
                got, base = out["kept"][label], sym["baseline"][label]
                if got["n"] < MIN_N:
                    reasons.append(f"{label}: n {got['n']} < {MIN_N}")
                if got["pf"] <= 1.0:
                    reasons.append(f"{label}: PF {got['pf']:.2f} <= 1")
                if got["pf"] <= base["pf"]:
                    reasons.append(f"{label}: PF not above baseline {base['pf']:.2f}")
                if label != "train" and got["stress_pf"] <= 1.0:
                    reasons.append(f"{label}: stress PF {got['stress_pf']:.2f} <= 1")
            if not (wf and wf["pass"]):
                reasons.append("walk-forward failed")
            out["reasons"] = reasons
            if reasons:
                verdicts[name] = False
            sym["filters"][name] = out
        report["symbols"][symbol] = sym
    report["verdicts"] = verdicts
    return report


def markdown(report: dict) -> str:
    lines = ["# VP 過濾研究(預先宣告)", "",
             f"產生時間:{report['generated_utc']}。基準:VP 參考帳本(production BacktestEngine)。", ""]
    for symbol, sym in report["symbols"].items():
        lines += [f"## {symbol}", "",
                  f"進場價落在前日邊緣 0.5×平均區間內的比例:{sym['edge_alignment']:.1%}"
                  f"(join 正確性檢查);暖身丟棄 {sym['dropped_for_warmup']} 筆;"
                  f"narrow_value 門檻 {sym['narrow_value_cut']:.4f}(僅用 2022–2023)。", "",
                  "| 組別 | train n / PF | validation n / PF / stress | 2026 n / PF / stress | 全期 PnL | WF |",
                  "|---|---:|---:|---:|---:|:---:|"]
        base = sym["baseline"]
        lines.append(
            f"| 基準 | {base['train']['n']} / {base['train']['pf']:.2f} | "
            f"{base['validation']['n']} / {base['validation']['pf']:.2f} / {base['validation']['stress_pf']:.2f} | "
            f"{base['2026']['n']} / {base['2026']['pf']:.2f} / {base['2026']['stress_pf']:.2f} | "
            f"{base['all']['pnl']:+,.0f} | — |")
        for name, out in sym["filters"].items():
            k = out["kept"]
            wf = out["walk_forward"]
            lines.append(
                f"| {name} | {k['train']['n']} / {k['train']['pf']:.2f} | "
                f"{k['validation']['n']} / {k['validation']['pf']:.2f} / {k['validation']['stress_pf']:.2f} | "
                f"{k['2026']['n']} / {k['2026']['pf']:.2f} / {k['2026']['stress_pf']:.2f} | "
                f"{k['all']['pnl']:+,.0f} | {'✓' if wf and wf['pass'] else '✗'} |")
        lines.append("")
    lines += ["## 判決", ""]
    for name, ok in report["verdicts"].items():
        lines.append(f"- {name}: {'PASS' if ok else 'REJECT'}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    report = run()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = args.out or RESEARCH / f"vp_filter_study_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"written: {out}")


if __name__ == "__main__":
    main()
