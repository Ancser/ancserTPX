# ============================================================
# 文件: scripts/confluence_report.py
# 狀態: v1.0.6 (explainable confluence — per-trade report + chart)
# 用途: 把每一筆交易的「參數 + 實際結果 + 打分理由」匯出 CSV 並畫圖
# ============================================================
"""Per-trade explainability: a CSV where every row is one trade with its full
decision context (confluence labels, weight, score, win-prob, the top reasons)
AND its realised outcome (entry/SL/TP/exit/pnl). Plus a price chart marking
each trade's entry/exit and SL/TP, coloured by win/loss.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List

from backend.db.models import Trade, Direction, ExitReason
from backend.strategy.confluence_scorer import ConfluenceScorer


def export_trades_csv(trades: List[Trade], out_path, scorer: ConfluenceScorer) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "n", "entry_time", "exit_time", "direction", "mode", "side",
        "entry", "sl", "tp", "exit_price", "exit_reason",
        "risk_ticks", "rr_realized", "pnl",
        "score", "prob", "weight", "n_tf", "largest_tf",
        "top_reasons", "labels",
    ]
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for k, t in enumerate(trades, 1):
            meta = t.meta or {}
            feats = meta.get("features", {})
            tick = 0.25
            risk_ticks = abs((t.entry_price or 0) - (t.original_sl_price or t.sl_price or 0)) / tick
            reward = abs((t.exit_price or t.entry_price) - (t.entry_price or 0))
            rr_real = (reward / (risk_ticks * tick)) if risk_ticks else 0.0
            reasons = scorer.explain(feats)[:3] if feats else []
            top = "; ".join(f"{n}={v:.2f}x{wt:+.2f}" for n, v, wt, _ in reasons)
            w.writerow({
                "n": k,
                "entry_time": t.entry_time.isoformat() if t.entry_time else "",
                "exit_time": t.exit_time.isoformat() if t.exit_time else "",
                "direction": t.direction.value if t.direction else "",
                "mode": meta.get("mode", ""),
                "side": meta.get("side", ""),
                "entry": round(t.entry_price, 2) if t.entry_price else "",
                "sl": round(t.original_sl_price or t.sl_price, 2) if (t.original_sl_price or t.sl_price) else "",
                "tp": round(t.original_tp_price or t.tp_price, 2) if (t.original_tp_price or t.tp_price) else "",
                "exit_price": round(t.exit_price, 2) if t.exit_price else "",
                "exit_reason": t.exit_reason.value if t.exit_reason else "",
                "risk_ticks": round(risk_ticks, 1),
                "rr_realized": round(rr_real, 2),
                "pnl": round(t.pnl, 2) if t.pnl is not None else "",
                "score": meta.get("score", ""),
                "prob": meta.get("prob", ""),
                "weight": meta.get("weight", ""),
                "n_tf": len(meta.get("tfs", []) or []),
                "largest_tf": meta.get("largest_tf", ""),
                "top_reasons": top,
                "labels": ",".join(meta.get("labels", []) or []),
            })
    return out


def plot_trades(candles, trades: List[Trade], out_path, title: str = "") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    times = [c.timestamp for c in candles]
    closes = [c.close for c in candles]

    fig, ax = plt.subplots(figsize=(16, 7))
    ax.plot(times, closes, lw=0.6, color="#888", alpha=0.8, label="close")

    for t in trades:
        if not t.entry_time:
            continue
        win = (t.pnl or 0) > 0
        col = "#1a9850" if win else "#d73027"
        buy = t.direction == Direction.BUY
        ax.scatter([t.entry_time], [t.entry_price], marker="^" if buy else "v",
                   s=42, color=col, zorder=5, edgecolors="black", linewidths=0.3)
        if t.exit_time and t.exit_price:
            ax.scatter([t.exit_time], [t.exit_price], marker="o", s=20,
                       facecolors="none", edgecolors=col, zorder=5)
            ax.plot([t.entry_time, t.exit_time], [t.entry_price, t.exit_price],
                    color=col, lw=0.8, alpha=0.7)
            sl = t.original_sl_price or t.sl_price
            tp = t.original_tp_price or t.tp_price
            if sl:
                ax.plot([t.entry_time, t.exit_time], [sl, sl], color="#d73027",
                        lw=0.5, ls=":", alpha=0.5)
            if tp:
                ax.plot([t.entry_time, t.exit_time], [tp, tp], color="#1a9850",
                        lw=0.5, ls=":", alpha=0.5)

    n_win = sum(1 for t in trades if (t.pnl or 0) > 0)
    pnl = sum((t.pnl or 0) for t in trades)
    ax.set_title(f"{title}  |  {len(trades)} trades  {n_win} wins  pnl=${pnl:,.0f}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out
