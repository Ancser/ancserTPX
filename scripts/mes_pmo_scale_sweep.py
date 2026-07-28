"""1.0.9: BEST 在 MES 上掃 factor_pmo_threshold_scale,驗證波動校準是否有效。

校準研究(scripts/emapmo_vol_calibration.py)給出 MES 的等效門檻 scale ≈ 0.51
(std 等比)~ 0.56(分位數對齊)。這裡實測掃一圈,順便確認 scale=1.0 在 MNQ
上與校準前完全一致(回歸保護)。

用法: python scripts/mes_pmo_scale_sweep.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # best_mes_parity_study

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from best_mes_parity_study import (  # noqa: E402
    footprint, mc_test, run_variant, series_stats, slip_table, volume_index,
    walk_forward,
)
from backend.data import candle_store  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_point_value  # noqa: E402

SCALES = (1.0, 0.8, 0.65, 0.56, 0.51, 0.45, 0.35)
LOG = lambda *a: (print(*a), sys.stdout.flush())


def main() -> None:
    presets = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))["presets"]
    best = dict(presets["BEST"])

    rows = []
    for sym in ("MNQ", "MES"):
        bars = sorted(candle_store.load(sym, 1), key=lambda c: c.timestamp)
        if not bars:
            continue
        vidx = volume_index(bars)
        tick_value = 0.25 * get_point_value(current_quarterly_contract_id(sym))
        scales = (1.0,) if sym == "MNQ" else SCALES
        LOG(f"\n########## {sym} ({len(bars)} bars, 1t=${tick_value:.2f}) ##########")
        for sc in scales:
            p = dict(best)
            p["factor_pmo_threshold_scale"] = sc
            run = run_variant(p, bars, sym)
            trades = run["trades"]
            pnls = [t["pnl"] for t in trades]
            if not pnls:
                LOG(f"  scale {sc:<5} → 0 trades")
                continue
            base = series_stats(pnls)
            mc = mc_test(pnls)
            wf = walk_forward(trades)
            sl = slip_table(pnls, tick_value)
            fp = footprint(trades, vidx, 18)
            rows.append({"symbol": sym, "scale": sc, **base, **mc, **wf,
                         "slip": sl, "footprint": fp})
            LOG(f"  scale {sc:<5} n={base['n']:<4} PF={base['pf']:<6} "
                f"pnl=${base['pnl']:<9} dd=${base['max_dd']:<8} "
                f"win={base['win_rate']:<6} MC={'Y' if mc.get('mc_pass') else 'n'} "
                f"WF={'Y' if wf.get('wf_pass') else 'n'} "
                f"PF@14t={sl['14']['pf']}")

    out = Path("data/research/mes_pmo_scale_sweep.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    LOG(f"\nreport: {out}")


if __name__ == "__main__":
    main()
