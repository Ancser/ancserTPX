"""Validate selected Sigma and hunter/sweep research edges.

This is a research-only second pass.  It takes the best-looking aggregate
results and asks whether they survive stability checks:
  - chronological thirds
  - monthly/session breakdown
  - bootstrap mean-PnL confidence interval
  - Monte Carlo drawdown from shuffled trade order

Run:
  python -m scripts.edge_validation_report
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import numpy as np
import pandas as pd

from backend.backtest.engine import _topstep_trade_date
from backend.backtest.metrics import MetricsCalculator
from backend.db.models import Trade
from scripts import sigma_resting_batch_sweep as sigma


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "edge_validation"
REPORT_JSON = OUT_DIR / "latest.json"
REPORT_MD = OUT_DIR / "report.md"
SIGMA_TRADES_CSV = OUT_DIR / "sigma_selected_trades.csv"
HUNTER_TRADES_CSV = OUT_DIR / "hunter_selected_trades.csv"

SIGMA_RESULTS = ROOT / "data" / "machinelearning" / "sigma_resting_batch" / "results.jsonl"
HUNTER_FEATURES = ROOT / "data" / "machinelearning" / "institution_research" / "features.csv"
HUNTER_SCORES = ROOT / "data" / "machinelearning" / "institution_research" / "strategy_scores.csv"

INITIAL_CAPITAL = 50_000.0
MNQ_TICK_VALUE = 0.50
ROUND_TURN_COST = 2.48
TICK = 0.25


@dataclass
class Validation:
    name: str
    family: str
    trades: int
    pnl: float
    max_dd: float
    profit_factor: float
    win_rate: float
    total_loss: float
    avg_trade: float
    thirds: list[dict]
    months: list[dict]
    bootstrap_mean_ci: list[float]
    bootstrap_p_mean_positive: float
    monte_carlo_dd_p50: float
    monte_carlo_dd_p95: float
    verdict: str
    reasons: list[str]


def _max_dd(values: list[float]) -> float:
    peak = 0.0
    dd = 0.0
    equity = 0.0
    for v in values:
        equity += float(v)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def _metrics(values: list[float]) -> dict:
    arr = np.asarray(values, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gain = float(wins.sum())
    loss = float(losses.sum())
    return {
        "trades": int(len(arr)),
        "pnl": round(float(arr.sum()), 2),
        "max_dd": round(_max_dd(arr.tolist()), 2),
        "profit_factor": round(gain / abs(loss), 4) if loss < 0 else 999.0,
        "win_rate": round(float((arr > 0).mean()), 4) if len(arr) else 0.0,
        "total_loss": round(loss, 2),
        "avg_trade": round(float(arr.mean()), 3) if len(arr) else 0.0,
    }


def _bootstrap(values: list[float], seed: int = 42, n: int = 5000) -> tuple[list[float], float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 5:
        return [0.0, 0.0], 0.0
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n, len(arr)), replace=True).mean(axis=1)
    ci = [round(float(np.quantile(means, 0.025)), 3), round(float(np.quantile(means, 0.975)), 3)]
    return ci, round(float((means > 0).mean()), 4)


def _mc_dd(values: list[float], seed: int = 7, n: int = 5000) -> tuple[float, float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 5:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    dds = []
    for _ in range(n):
        sample = rng.permutation(arr)
        dds.append(_max_dd(sample.tolist()))
    return round(float(np.quantile(dds, 0.50)), 2), round(float(np.quantile(dds, 0.95)), 2)


def _split_thirds(rows: list[dict]) -> list[dict]:
    out = []
    if not rows:
        return out
    chunks = np.array_split(np.arange(len(rows)), 3)
    for i, idxs in enumerate(chunks, start=1):
        vals = [rows[int(j)]["pnl"] for j in idxs]
        item = _metrics(vals)
        item["part"] = i
        item["start"] = rows[int(idxs[0])]["time"][:10] if len(idxs) else ""
        item["end"] = rows[int(idxs[-1])]["time"][:10] if len(idxs) else ""
        out.append(item)
    return out


def _by_month(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        key = str(row["time"])[:7]
        groups.setdefault(key, []).append(float(row["pnl"]))
    out = []
    for key in sorted(groups):
        item = _metrics(groups[key])
        item["month"] = key
        out.append(item)
    return out


def _validate_rows(name: str, family: str, rows: list[dict]) -> Validation:
    rows = sorted(rows, key=lambda r: r["time"])
    vals = [float(r["pnl"]) for r in rows]
    m = _metrics(vals)
    thirds = _split_thirds(rows)
    months = _by_month(rows)
    ci, p_pos = _bootstrap(vals)
    mc50, mc95 = _mc_dd(vals)
    hard_reasons = []
    soft_reasons = []
    if m["trades"] < 80:
        hard_reasons.append("sample<80")
    if ci[0] <= 0:
        hard_reasons.append("bootstrap_lower<=0")
    if any(part["pnl"] <= 0 for part in thirds):
        hard_reasons.append("walk_forward_third_negative")
    if sum(1 for item in months if item["pnl"] > 0) < max(2, len(months) - 1):
        hard_reasons.append("month_stability_weak")
    if m["profit_factor"] < 1.5:
        hard_reasons.append("pf<1.5")
    if abs(m["total_loss"]) > abs(m["pnl"]):
        soft_reasons.append("loss_greater_than_pnl")
    if abs(m["total_loss"]) > 10_000:
        soft_reasons.append("loss_over_10k")
    verdict = "FAIL" if hard_reasons else ("CAUTION" if soft_reasons else "PASS")
    reasons = hard_reasons + soft_reasons
    return Validation(
        name=name,
        family=family,
        trades=m["trades"],
        pnl=m["pnl"],
        max_dd=m["max_dd"],
        profit_factor=m["profit_factor"],
        win_rate=m["win_rate"],
        total_loss=m["total_loss"],
        avg_trade=m["avg_trade"],
        thirds=thirds,
        months=months,
        bootstrap_mean_ci=ci,
        bootstrap_p_mean_positive=p_pos,
        monte_carlo_dd_p50=mc50,
        monte_carlo_dd_p95=mc95,
        verdict=verdict,
        reasons=reasons,
    )


def _load_sigma_results() -> list[dict]:
    rows = []
    with SIGMA_RESULTS.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _variant_from_row(row: dict) -> sigma.Variant:
    return sigma.Variant(
        session_set=str(row["session_set"]),
        window=int(row["window"]),
        method=str(row["method"]),
        layout=str(row["layout"]),
        level_set=str(row["level_set"]),
        target=str(row["target"]),
        stop_span=float(row["stop_span"]),
        accept_mode=str(row["accept_mode"]),
        daily_loss_stop=int(row["daily_loss_stop"]),
    )


def _scale_trade_rows(trades: list[Trade], size: int, name: str) -> list[dict]:
    rows = []
    for t in sorted(trades, key=lambda x: x.entry_time):
        rows.append(
            {
                "name": name,
                "time": t.exit_time.isoformat() if t.exit_time else t.entry_time.isoformat(),
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat() if t.exit_time else "",
                "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
                "entry": t.entry_price,
                "exit": t.exit_price,
                "pnl": round(float(t.pnl or 0.0) * size, 2),
                "size": size,
            }
        )
    return rows


def validate_sigma() -> tuple[list[Validation], list[dict]]:
    all_rows = _load_sigma_results()
    candidates = []
    passes = [r for r in all_rows if r["pnl"] > 6000 and r["max_dd"] < 1000]
    if passes:
        candidates.append(min(passes, key=lambda r: abs(r["total_loss"])))
        candidates.append(max(passes, key=lambda r: r["pnl"] / max(r["max_dd"], 1)))
    pf_pool = [r for r in all_rows if r["pnl"] > 0 and r["trades"] >= 50 and r["max_dd"] < 1000]
    if pf_pool:
        candidates.append(max(pf_pool, key=lambda r: r["profit_factor"]))

    dedup: dict[tuple[str, int], dict] = {}
    for row in candidates:
        dedup[(row["variant"], int(row["size"]))] = row

    sessions = sigma._build_sessions(include_mad=False)
    calc = MetricsCalculator()
    validations: list[Validation] = []
    trade_rows: list[dict] = []
    for row in dedup.values():
        variant = _variant_from_row(row)
        trades, _extra = sigma.simulate_variant(sessions, variant)
        # Force metric calculation once to ensure trade PnL is populated consistently.
        calc.calculate_all(trades, INITIAL_CAPITAL)
        name = f"sigma x{row['size']} {variant.key}"
        rows = _scale_trade_rows(trades, int(row["size"]), name)
        trade_rows.extend(rows)
        validations.append(_validate_rows(name, "sigma", rows))
    return validations, trade_rows


def _hunter_rule_rows(rule: dict, df: pd.DataFrame) -> list[dict]:
    work = df.copy()
    hp75 = work["hunter_pressure"].quantile(0.75)
    hp90 = work["hunter_pressure"].quantile(0.90)
    rule_name = rule["rule"]
    direction = 1 if rule["direction"] == "long" else -1
    hold = int(rule["hold_bars"])
    stop_ticks = int(rule["stop_ticks"])
    target_ticks = int(rule["target_ticks"])
    session = rule["session"]
    filter_name = rule["filter"]

    if rule_name == "reject_session_high":
        mask = work["sweep_session_high"].fillna(False)
    elif rule_name == "reject_session_low":
        mask = work["sweep_session_low"].fillna(False)
    elif rule_name == "reject_prev_high":
        mask = work["sweep_prev_high"].fillna(False)
    elif rule_name == "reject_prev_low":
        mask = work["sweep_prev_low"].fillna(False)
    elif rule_name == "reject_open15_high":
        mask = work["sweep_open15_high"].fillna(False)
    elif rule_name == "reject_open15_low":
        mask = work["sweep_open15_low"].fillna(False)
    else:
        return []

    if session != "ALL":
        mask = mask & (work["session"] == session)
    if filter_name == "vol_z2":
        mask = mask & (work["volume_z_60"] >= 2)
    elif filter_name == "profile_void":
        mask = mask & (work["profile_void_score"] >= 0.70)
    elif filter_name == "hunter_p75":
        mask = mask & (work["hunter_pressure"] >= hp75)
    elif filter_name == "hunter_p90":
        mask = mask & (work["hunter_pressure"] >= hp90)

    idxs = np.flatnonzero(mask.to_numpy(bool))
    picked = []
    last = -10**9
    for idx in idxs:
        if idx - last >= max(10, min(hold, 30)):
            picked.append(int(idx))
            last = int(idx)

    opens = work["open"].to_numpy(float)
    highs = work["high"].to_numpy(float)
    lows = work["low"].to_numpy(float)
    closes = work["close"].to_numpy(float)
    times = pd.to_datetime(work["timestamp"], utc=True)
    rows = []
    for idx in picked:
        if idx + 1 >= len(work):
            continue
        entry = opens[idx + 1]
        stop_price = entry - direction * stop_ticks * TICK
        target_price = entry + direction * target_ticks * TICK
        end = min(len(work) - 1, idx + hold)
        exit_idx = end
        tick_result = direction * (closes[end] - entry) / TICK
        for j in range(idx + 1, end + 1):
            if direction > 0:
                stop_hit = lows[j] <= stop_price
                target_hit = highs[j] >= target_price
            else:
                stop_hit = highs[j] >= stop_price
                target_hit = lows[j] <= target_price
            if stop_hit:
                tick_result = -stop_ticks
                exit_idx = j
                break
            if target_hit:
                tick_result = target_ticks
                exit_idx = j
                break
        rows.append(
            {
                "name": f"hunter {rule_name} {session} {filter_name} TP{target_ticks} SL{stop_ticks} H{hold}",
                "time": times.iloc[exit_idx].isoformat(),
                "entry_time": times.iloc[idx + 1].isoformat(),
                "exit_time": times.iloc[exit_idx].isoformat(),
                "direction": rule["direction"],
                "entry": round(float(entry), 2),
                "exit": "",
                "pnl": round(float(tick_result) * MNQ_TICK_VALUE - ROUND_TURN_COST, 2),
                "size": 1,
            }
        )
    return rows


def validate_hunter() -> tuple[list[Validation], list[dict]]:
    scores = pd.read_csv(HUNTER_SCORES)
    features = pd.read_csv(HUNTER_FEATURES)
    candidates = scores.head(3).to_dict("records")
    trade_rows: list[dict] = []
    validations: list[Validation] = []
    for rule in candidates:
        rows = _hunter_rule_rows(rule, features)
        trade_rows.extend(rows)
        validations.append(_validate_rows(rows[0]["name"] if rows else str(rule["rule"]), "hunter", rows))
    return validations, trade_rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sigma_validations, sigma_trades = validate_sigma()
    hunter_validations, hunter_trades = validate_hunter()
    validations = sigma_validations + hunter_validations
    _write_csv(SIGMA_TRADES_CSV, sigma_trades)
    _write_csv(HUNTER_TRADES_CSV, hunter_trades)

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validations": [asdict(v) for v in validations],
        "files": {
            "report_md": str(REPORT_MD),
            "sigma_trades_csv": str(SIGMA_TRADES_CSV),
            "hunter_trades_csv": str(HUNTER_TRADES_CSV),
        },
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Edge Validation Report",
        "",
        f"Generated: {payload['created_at']}",
        "",
        "| verdict | family | name | trades | pnl | dd | pf | win | loss | bootstrap mean CI | MC DD p95 | reasons |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for v in validations:
        lines.append(
            f"| {v.verdict} | {v.family} | {v.name} | {v.trades} | {v.pnl} | {v.max_dd} | "
            f"{v.profit_factor} | {v.win_rate} | {v.total_loss} | "
            f"{v.bootstrap_mean_ci} | {v.monte_carlo_dd_p95} | {', '.join(v.reasons)} |"
        )
    lines += ["", "## Thirds", ""]
    for v in validations:
        lines.append(f"### {v.name}")
        for row in v.thirds:
            lines.append(
                f"- part {row['part']} {row['start']}..{row['end']}: "
                f"pnl={row['pnl']} dd={row['max_dd']} pf={row['profit_factor']} trades={row['trades']}"
            )
        lines.append("")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {REPORT_MD}")
    for v in validations:
        print(v.verdict, v.family, v.name, "pnl", v.pnl, "dd", v.max_dd, "pf", v.profit_factor, "reasons", v.reasons)


if __name__ == "__main__":
    main()
