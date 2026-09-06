"""Create a compact MNQ chart comparing Astra raw events with PI BEST fills."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.data import candle_store  # noqa: E402

DATASET = Path(r"F:\ancserData\astra_2026\astra_event_dataset.csv")
BACKTEST = Path(r"F:\ancserData\astra_2026\astra_pi_best_backtest.json")
OUT = Path(r"F:\ancserData\astra_2026\astra_vs_pi_best_mnq.png")


def main() -> None:
    ev = pd.read_csv(DATASET, parse_dates=["ts", "entry_ts"])
    ev = ev[ev["future"] == "MNQ"].copy().sort_values("entry_ts")
    ev["entry_ts"] = pd.to_datetime(ev["entry_ts"], utc=True)
    bt = json.loads(BACKTEST.read_text(encoding="utf-8"))
    long_trades = pd.DataFrame(bt["results"]["current_pi_best_long_only"]["trades"])
    both_trades = pd.DataFrame(bt["results"]["pi_best_with_short"]["trades"])
    # The PI BEST preset is an MNQ contract; discard MES rows when plotting
    # the QQQ→MNQ comparison even if the all-symbol replay was run last.
    for x in (long_trades, both_trades):
        if not x.empty and "future" in x:
            x.drop(x[x["future"] != "MNQ"].index, inplace=True)
    for x in (long_trades, both_trades):
        if not x.empty:
            x["ts"] = pd.to_datetime(x["ts"], utc=True)
            x["exit_ts"] = pd.to_datetime(x["exit_ts"], utc=True)

    snap = candle_store.load_snapshot("MNQ", 1, use_cache=False)
    start = ev["entry_ts"].min() - pd.Timedelta(hours=2)
    end = ev["entry_ts"].max() + pd.Timedelta(hours=2)
    bars = candle_store.select_range(snap, start=start.to_pydatetime(), end=end.to_pydatetime())
    raw = pd.DataFrame({
        "ts": [pd.Timestamp(b.timestamp, tz="UTC") if pd.Timestamp(b.timestamp).tz is None else pd.Timestamp(b.timestamp).tz_convert("UTC") for b in bars],
        "open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars], "close": [float(b.close) for b in bars],
    }).drop_duplicates("ts").set_index("ts").sort_index()
    ohlc = raw.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

    plt.style.use("dark_background")
    fig, (ax, pnl) = plt.subplots(2, 1, figsize=(18, 10), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]},
                                  facecolor="#070b12")
    for a in (ax, pnl):
        a.set_facecolor("#070b12")
        a.grid(True, alpha=.16, linewidth=.6)

    ax.plot(ohlc.index, ohlc["close"], color="#aeb8c7", linewidth=.75, label="MNQ 15m close", zorder=1)

    # All Astra raw MNQ signals, including ones rejected by PI BEST.
    longs = ev[ev["direction"] > 0]
    shorts = ev[ev["direction"] < 0]
    ax.scatter(longs["entry_ts"], longs["entry"], s=15, marker="o", color="#28b9ff",
               alpha=.28, label="Astra raw long", zorder=2)
    ax.scatter(shorts["entry_ts"], shorts["entry"], s=15, marker="o", color="#ff5d78",
               alpha=.28, label="Astra raw short", zorder=2)

    def marks(df, label, color, marker):
        if df.empty:
            return
        ax.scatter(df["ts"], df["entry"], s=68, marker=marker, color=color,
                   edgecolors="#f5f7fa", linewidths=.5, label=label, zorder=4)
        exit_px = df["entry"] + df["direction"] * df["points"]
        ax.scatter(df["exit_ts"], exit_px, s=26, marker="x", color=color,
                   linewidths=1.1, zorder=4)
        for _, r in df.iterrows():
            ax.plot([r["ts"], r["exit_ts"]], [r["entry"], r["entry"] + r["direction"] * r["points"]],
                    color=color, alpha=.22, linewidth=.65, zorder=3)

    marks(long_trades, "PI BEST long fill", "#48e28a", "^")
    # The all-direction overlay is shown only for entries not already in the
    # long-only set, making the extra Astra/short decisions visible.
    if not both_trades.empty:
        extra = both_trades[both_trades["direction"] < 0]
        marks(extra, "PI BEST short fill", "#ffae57", "v")

    # Cumulative net PnL, same $7 round-turn cost used by the replay.
    for df, label, color, ls in ((long_trades, "PI BEST long-only", "#48e28a", "-"),
                                 (both_trades, "PI BEST long+short", "#ffae57", "--")):
        if df.empty:
            continue
        s = df.sort_values("ts")[["ts", "usd"]].copy()
        s["cum"] = s["usd"].cumsum()
        pnl.step(s["ts"], s["cum"], where="post", color=color, linewidth=1.8, linestyle=ls, label=label)
    pnl.axhline(0, color="#9aa4b2", linewidth=.7)
    pnl.set_ylabel("Net PnL ($)")
    pnl.set_xlabel("UTC time")
    pnl.legend(loc="upper left", ncol=2, frameon=False, fontsize=9)

    ax.set_ylabel("MNQ price")
    ax.set_title("QQQ→MNQ：Astra 原始事件 vs PI BEST 實際成交", loc="left", fontsize=15, weight="bold")
    ax.text(0.01, 0.97,
            "淡色圓點=Astra全部事件；實心三角=PI BEST實際採用；×=出場\n"
            "綠=只做多；橙=加入粉π空方；PnL已扣$7/趟",
            transform=ax.transAxes, va="top", color="#c4ccd8", fontsize=9)
    ax.legend(loc="lower left", ncol=3, frameon=False, fontsize=9)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=8, maxticks=14))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d", tz=pd.Timestamp.now(tz="UTC").tz))
    fig.autofmt_xdate(rotation=0)
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print(OUT)


if __name__ == "__main__":
    main()
