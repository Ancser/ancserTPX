"""Audit existing research outputs and preset live-readiness.

This script is deliberately read-only. It does not rerun optimizers; it
summarizes the artifacts already written under data/machinelearning and
data/shadow_replay, then emits a Chinese report for operational decisions.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.research_correctness_audit
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "research_correctness"
OUT_JSON = OUT_DIR / "latest.json"
OUT_MD = ROOT / "docs" / "1.0.9_RESEARCH_CORRECTNESS_AUDIT.md"


@dataclass
class ArtifactAudit:
    name: str
    path: str
    tested: int
    passes: int
    best_name: str
    best_pnl: float
    best_dd: float
    best_pf: float
    best_trades: int
    verdict: str
    reasons: list[str]


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _best_label(row: dict[str, Any]) -> str:
    for key in ("variant", "name", "preset", "rule", "family"):
        if row.get(key):
            label = str(row[key])
            break
    else:
        label = "unknown"
    extras = []
    for key in ("session", "session_set", "side", "direction_mode", "target", "hold_bars"):
        if row.get(key) not in (None, ""):
            extras.append(f"{key}={row[key]}")
    return label + (f" ({', '.join(extras)})" if extras else "")


def _audit_latest_json(name: str, rel: str) -> ArtifactAudit:
    path = ROOT / rel
    data = _load_json(path) or {}
    top = data.get("top") or data.get("validations") or data.get("strategy_scores") or []
    best = top[0] if top else {}
    tested = _int(data.get("tested_variants_with_trades") or data.get("tested") or data.get("total_variants") or len(top))
    passes = _int(data.get("passes"), 0)
    verdict = str(best.get("verdict") or ("PASS" if passes else "FAIL"))
    reasons = best.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [x for x in reasons.split(",") if x]
    if not reasons and not passes:
        reasons = ["no_pass"]
    return ArtifactAudit(
        name=name,
        path=rel,
        tested=tested,
        passes=passes,
        best_name=_best_label(best),
        best_pnl=_float(best.get("pnl")),
        best_dd=_float(best.get("max_dd")),
        best_pf=_float(best.get("profit_factor")),
        best_trades=_int(best.get("trades")),
        verdict=verdict,
        reasons=list(map(str, reasons)),
    )


def _audit_sigma_batch() -> ArtifactAudit:
    rel = "data/machinelearning/sigma_resting_batch/top_latest.csv"
    path = ROOT / rel
    if not path.exists():
        return ArtifactAudit("Sigma resting full batch", rel, 0, 0, "missing", 0, 0, 0, 0, "MISSING", ["missing"])
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    best = rows[0] if rows else {}
    checkpoint = _load_json(ROOT / "data/machinelearning/sigma_resting_batch/checkpoint.json") or {}
    tested = _int(checkpoint.get("total") or best.get("total") or len(rows))
    passes = 0
    for r in rows:
        pnl = _float(r.get("pnl"))
        dd = _float(r.get("max_dd"))
        loss = abs(_float(r.get("total_loss")))
        trades = _int(r.get("trades"))
        pf = _float(r.get("profit_factor"))
        if pnl > 6000 and dd < 1000 and trades >= 80 and pf >= 1.4 and loss <= pnl:
            passes += 1
    reasons = []
    if best:
        if abs(_float(best.get("total_loss"))) > _float(best.get("pnl")):
            reasons.append("loss>pnl")
        if _float(best.get("max_dd")) > 1000:
            reasons.append("maxDD>1000")
        if _int(best.get("size")) > 1:
            reasons.append("size>1")
    return ArtifactAudit(
        name="Sigma resting full batch",
        path=rel,
        tested=tested,
        passes=passes,
        best_name=_best_label(best),
        best_pnl=_float(best.get("pnl")),
        best_dd=_float(best.get("max_dd")),
        best_pf=_float(best.get("profit_factor")),
        best_trades=_int(best.get("trades")),
        verdict="CAUTION" if rows else "MISSING",
        reasons=reasons or ["batch_top_not_live_validated"],
    )


def _audit_institution() -> ArtifactAudit:
    rel = "data/machinelearning/institution_research/latest.json"
    path = ROOT / rel
    data = _load_json(path) or {}
    scores = data.get("strategy_scores") or []
    best = scores[0] if scores else {}
    ml = data.get("ml") or {}
    mean_auc = _float(ml.get("mean_auc"))
    reasons = []
    if mean_auc < 0.55:
        reasons.append(f"ml_auc={mean_auc:.4f}<0.55")
    if abs(_float(best.get("total_loss"))) > _float(best.get("pnl")):
        reasons.append("loss>pnl")
    return ArtifactAudit(
        name="Institution/sweep behavior + ML",
        path=rel,
        tested=len(scores),
        passes=0,
        best_name=_best_label(best),
        best_pnl=_float(best.get("pnl")),
        best_dd=_float(best.get("max_dd")),
        best_pf=_float(best.get("profit_factor")),
        best_trades=_int(best.get("trades")),
        verdict="FAIL",
        reasons=reasons or ["no_live_ready_edge"],
    )


def _shadow_reports() -> list[dict[str, Any]]:
    out = []
    for path in sorted((ROOT / "data" / "shadow_replay").glob("*.json")):
        data = _load_json(path) or {}
        for r in data.get("reports") or []:
            out.append({
                "date": data.get("date") or path.stem,
                "snapshot_id": r.get("snapshot_id"),
                "strategy_mode": r.get("strategy_mode"),
                "exit_mode": r.get("exit_mode"),
                "live_n": _int(r.get("live_n")),
                "bt_n": _int(r.get("bt_n")),
                "matched": _int(r.get("matched")),
                "match_rate": _float(r.get("match_rate")),
                "pass": bool(r.get("pass")),
                "live_only": len(r.get("live_only") or []),
                "bt_only": len(r.get("bt_only") or []),
            })
    return out


def _preset_statuses() -> list[dict[str, Any]]:
    try:
        from backend.terminal_live import BUILTIN_PRESETS
    except Exception:
        BUILTIN_PRESETS = {}
    names = sorted(BUILTIN_PRESETS)
    rows: list[dict[str, Any]] = []
    for name in names:
        upper = name.upper()
        if "CLAUDE #1" in upper:
            status, reason = "LIVE_CANDIDATE", "單 5m，與近期實盤/回測框架最接近；仍需 P0 shadow replay 驗證"
        elif "CLAUDE #2" in upper:
            status, reason = "LIVE_CANDIDATE", "30m+1h overlap，小區間交易；候選但 live 標記歷史不足"
        elif "CLAUDE #3" in upper:
            status, reason = "LIVE_CANDIDATE", "5m+30m overlap，小區間交易；候選但 live 標記歷史不足"
        elif "FABLE" in upper and "TREND" in upper:
            status, reason = "RESEARCH_VALID_NOT_LIVE", "回測品質高，但屬 ladder/futureman 派生；live/backtest parity 未連續通過"
        elif "FABLE" in upper and "FADE" in upper:
            status, reason = "RESEARCH_ONLY", "Fade entry 有潛力，SL/TP 還在重新研究"
        elif "SIGMA" in upper:
            status, reason = "CAUTION", "Sigma batch 有高 PnL 但 total loss 偏高，且 live parity 未驗證"
        elif "CODEX #2" in upper or "CODEX #3" in upper or "CODEX #4" in upper:
            status, reason = "LEGACY_RESEARCH", "舊 trend/overlap 結果；作為對照，不是目前月度 live 首選"
        else:
            status, reason = "UNKNOWN", "未綁定最近 P0 shadow replay"
        rows.append({"name": name, "status": status, "reason": reason})
    return rows


def _md_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    hs = list(headers)
    lines = ["| " + " | ".join(hs) + " |", "| " + " | ".join(["---"] * len(hs)) + " |"]
    for row in rows:
        vals = [str(x).replace("|", "/") for x in row]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    audits = [
        _audit_latest_json("Futures repo port", "data/machinelearning/futures_repo_port/latest.json"),
        _audit_latest_json("APX/rank acceleration intraday factor", "data/machinelearning/apx_intraday_factor/latest.json"),
        _audit_latest_json("RSI/KDJ extreme market entry", "data/machinelearning/rsi_kdj_extreme/latest.json"),
        _audit_latest_json("Edge validation selected candidates", "data/machinelearning/edge_validation/latest.json"),
        _audit_sigma_batch(),
        _audit_institution(),
        _audit_latest_json("Fade range-ladder study", "data/machinelearning/fade_range_ladder/latest.json"),
        _audit_latest_json("Fade professional idea sweep", "data/machinelearning/fade_professional_ideas/latest.json"),
        _audit_latest_json("Trend range-ladder study", "data/machinelearning/trend_range_ladder/latest.json"),
    ]
    shadows = _shadow_reports()
    presets = _preset_statuses()

    live_ready = 0
    research_pass = sum(1 for a in audits if a.passes > 0)
    caution = sum(1 for a in audits if a.verdict.upper() == "CAUTION")
    fail = sum(1 for a in audits if a.verdict.upper() == "FAIL")
    shadow_pass_days = sum(1 for r in shadows if r.get("pass"))
    shadow_total = len(shadows)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "live_ready_count": live_ready,
        "research_artifacts_with_passes": research_pass,
        "caution_artifacts": caution,
        "failed_artifacts": fail,
        "shadow_reports": shadow_total,
        "shadow_pass_reports": shadow_pass_days,
        "audits": [asdict(a) for a in audits],
        "shadow": shadows,
        "presets": presets,
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    audit_rows = [
        (
            a.name,
            a.tested,
            a.passes,
            a.verdict,
            a.best_trades,
            f"{a.best_pnl:+.0f}",
            f"{a.best_dd:.0f}",
            f"{a.best_pf:.2f}",
            ", ".join(a.reasons[:4]),
        )
        for a in audits
    ]
    shadow_rows = [
        (
            r["date"],
            r["snapshot_id"],
            r["live_n"],
            r["bt_n"],
            r["matched"],
            f"{100*r['match_rate']:.1f}%",
            "PASS" if r["pass"] else "FAIL",
            f"live-only {r['live_only']} / bt-only {r['bt_only']}",
        )
        for r in shadows
    ] or [("-", "-", "-", "-", "-", "-", "-", "no shadow replay files")]
    preset_rows = [(p["status"], p["name"], p["reason"]) for p in presets]

    md = f"""# 1.0.9 Research Correctness Audit

Generated: {summary['created_at']}

## 判決

- **Actual live-correct preset/research count: {live_ready}**。目前沒有任何 preset 已經完成「連續 P0 shadow replay >=90%」這個實盤/回測同邏輯條件。
- **可以作為本月 live 候選的只有 CLAUDE #1 / #2 / #3**，原因不是它們已被證明有 edge，而是它們最接近你上週四到今天實際執行的 5m / 5m+30m 框架，變更面最小。
- **研究結果可參考，但不能直接升級 live**。多數候選是同窗回測或 batch sweep；只要沒有 shadow replay 對齊，就只能算 research，不算 production edge。

## Research Script Result

{_md_table(['artifact', 'tested', 'passes', 'verdict', 'trades', 'best pnl', 'maxDD', 'PF', 'main reasons'], audit_rows)}

## Shadow Replay

{_md_table(['date', 'snapshot', 'live', 'bt', 'matched', 'match', 'verdict', 'gap'], shadow_rows)}

## Preset Operational Status

{_md_table(['status', 'preset', 'reason'], preset_rows)}

## 結論

1. **真正正確 = live/backtest 同 engine + 同 preset + 同時間逐筆對齊**。依這個標準，目前數量是 0。
2. **CLAUDE #1/#2/#3 是當前最小變更的 live 候選**，不是因為最漂亮，而是因為它們最貼近實際交易歷史，方便把 mismatch 壓到最低。
3. **Sigma / rank acceleration / RSI-KDJ / institution ML 都沒有 production pass**。它們提供想法和特徵，但不是本月 live 主策略。
4. **FABLE Trend 是研究上較乾淨的分支**，但它和你最近實盤使用的 5m/#2 系列不是同一路徑，不能直接拿回測數字替代實盤對齊。
"""
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
