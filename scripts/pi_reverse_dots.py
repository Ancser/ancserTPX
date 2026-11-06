"""Probe: can the π chart's yellow/gray dots be explained by a simple price feature?

Research only. Reads the PI Discord channel (same read-only user-token access as
scripts/pi_collect_history.py), downloads the chart image attached to each
recent signal, extracts the dot markers inside the shaded current-session band,
converts their x position to New York time, and ranks candidate features of the
mapped future (SPY->MES, QQQ->MNQ) at those minutes against every other minute
of the same session.

Time axis (verified on the 2026-11-05 SPY chart against its own signals):
shaded band left edge = 09:30 NY, rightmost price pixel = chart title time.

Output: ancserMarketData/derived/research/pi_reverse_dots_<UTC>/
    images/<message_id>.jpg, dots.csv, features.csv, REPORT.md

    python scripts/pi_reverse_dots.py --images 20
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from dotenv import load_dotenv
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.broker.topstepx import TopstepXClient  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import BarUnit  # noqa: E402
from backend.live.pi_listener import (  # noqa: E402
    API,
    message_is_pre_session,
    message_source_timestamp,
    parse_message,
)

NY = ZoneInfo("America/New_York")
CHANNEL_ID = "1547062725060993066"
FUTURE = {"SPY": "MES", "QQQ": "MNQ"}
# Front month for the probe window (the 2026-09 roll put Z26 in front).
CONTRACT = {"MES": "CON.F.US.MES.Z26", "MNQ": "CON.F.US.MNQ.Z26"}
EARLIEST = datetime(2026, 9, 15, tzinfo=timezone.utc)

# Plot area of the 1800x1500 FutureMan template.
PLOT_X = (230, 1620)
PLOT_Y = (185, 1335)
TEMPLATE_END_X = 1503
# A chart counts only if its drawn price line tracks the future's closes.
MIN_AXIS_ABS_R = 0.9


# ── Discord ────────────────────────────────────────────────────────────────
def fetch_pairs(n_images: int, max_pages: int = 10) -> list[dict]:
    """Recent (image message, parsed signal message) pairs, newest first."""
    headers = {"Authorization": os.environ["DISCORD_TOKEN"].strip()}
    url = f"{API}/channels/{CHANNEL_ID}/messages"
    msgs, before = [], None
    for _ in range(max_pages):
        params = {"limit": 100} | ({"before": before} if before else {})
        r = httpx.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        msgs.extend(batch)
        before = batch[-1]["id"]
        if message_source_timestamp(batch[-1]) and message_source_timestamp(batch[-1]) < EARLIEST:
            break
    msgs.sort(key=lambda m: int(m["id"]))
    pairs = []
    for i, m in enumerate(msgs):
        sigs = parse_message(m, allow_historical=False)
        if not sigs or message_is_pre_session(m):
            continue
        ts = message_source_timestamp(m)
        if ts is None or ts < EARLIEST:
            continue
        sym = sigs[0].equity
        # The chart is posted as a separate message just before the text.
        for prev in reversed(msgs[max(0, i - 3):i]):
            url_m = re.search(r"https://cdn\.discordapp\.com/attachments/\S+?\.(?:jpg|png)\S*",
                              prev.get("content") or "")
            atts = [a["url"] for a in prev.get("attachments") or []]
            img = atts[0] if atts else (url_m.group(0) if url_m else None)
            if img and f"/{sym}_" in img:
                pairs.append({"message_id": m["id"], "ts": ts, "equity": sym,
                              "kinds": [s.kind for s in sigs], "image_url": img})
                break
    pairs.sort(key=lambda p: p["ts"], reverse=True)
    return pairs[:n_images]


def refresh_url(url: str) -> str:
    """Re-sign an expired cross-posted CDN link (Discord refresh-urls endpoint)."""
    r = httpx.post(f"{API}/attachments/refresh-urls", json={"attachment_urls": [url]},
                   headers={"Authorization": os.environ["DISCORD_TOKEN"].strip()}, timeout=30)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        return refresh_url(url)
    r.raise_for_status()
    return r.json()["refreshed_urls"][0]["refreshed"]


# ── Image ─────────────────────────────────────────────────────────────────
def _components(mask: np.ndarray, min_area: int, max_wh: int) -> list[tuple[float, float, int]]:
    lab, n = ndimage.label(mask)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        area = int((lab[sl] == i).sum())
        if area < min_area or w > max_wh or h > max_wh or max(w, h) > 2.5 * min(w, h):
            continue
        cy, cx = ndimage.center_of_mass(lab[sl] == i)
        out.append((sl[1].start + cx, sl[0].start + cy, area))
    return out


def extract(path: Path) -> dict:
    im = np.asarray(Image.open(path).convert("RGB")).astype(int)
    R, G, B = im[..., 0], im[..., 1], im[..., 2]
    inside = np.zeros(R.shape, bool)
    inside[PLOT_Y[0]:PLOT_Y[1], PLOT_X[0]:PLOT_X[1]] = True
    # The shaded band alternates by day (it is NOT "today"), so the session
    # boundary comes from the price line itself: overnight is drawn as a flat
    # segment, and the rightmost long flat run ends at today's 09:30 open.
    # The 1px line is anti-aliased and JPEG-dimmed: (80,20) covers ~96% of
    # columns on the 11-04 chart, (150,45) only 16%.
    # B - G < 15 excludes the magenta π glyph (B-G ≈ 33) sitting on the
    # overnight flat; the salmon price line has G ≈ B.
    line = inside & (R > 80) & (R - G > 20) & (R - B > 20) & (B - G < 15)
    # FutureMap levels are dashed lines in the same salmon colour; ignore rows
    # that span much of the plot unless a column has nothing else.
    level_rows = line.sum(1) > 0.2 * (PLOT_X[1] - PLOT_X[0])
    price = line & ~level_rows[:, None]
    cols = np.where(price.any(0))[0]
    if cols.size == 0:
        raise ValueError("no price line")
    # The template always ends the data at x≈1503 (26 of 40 charts within
    # ±2px); detection undershoots when a marker circle covers the last bars.
    price_end = TEMPLATE_END_X
    trace ={int(x): float(np.median(np.where(price[:, x])[0])) for x in cols}
    runs, start = [], None
    xs = sorted(trace)
    for prev, x in zip(xs, xs[1:]):
        flat = x == prev + 1 and abs(trace[x] - trace[prev]) <= 1.0
        if flat and start is None:
            start = prev
        if not flat and start is not None:
            runs.append((start, prev))
            start = None
    runs = [r for r in runs if r[1] - r[0] >= 15 and r[1] < price_end - 20]
    if not runs:
        raise ValueError("no overnight flat segment")
    # Glyphs on the flat can split it; walk on while y stays at the flat level.
    level, x = trace[runs[-1][1]], runs[-1][1]
    while x + 1 <= price_end and (x + 1 not in trace or abs(trace[x + 1] - level) <= 3):
        x += 1
    band_l = x + 1
    yellow = inside & (R > 140) & (G > 115) & (B < 105) & (R - G > 12) & (R - G < 55) & (G - B > 40)
    gray = inside & (R > 140) & (R < 215) & (G > 120) & (B > 115) & (abs(G - B) < 20) & (R - G > 8) & (R - G < 40)
    # Price-line y per column inside the band (for the time-axis check).
    line_y = {x: y for x, y in trace.items() if band_l <= x <= price_end}
    return {
        "band_l": band_l, "price_end": price_end, "line_y": line_y,
        "yellow": _components(yellow, 15, 30),
        "gray": _components(gray, 15, 16),
    }


def x_to_ny(x: float, band_l: int, price_end: int, title: datetime) -> datetime:
    open_ny = title.astimezone(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    span = (title - open_ny).total_seconds() / 60
    minutes = (x - band_l) / max(1, price_end - band_l) * span
    return open_ny + timedelta(minutes=round(minutes))


# ── Features (exploratory; a winning feature gets one shared definition) ──
def session_features(bars) -> dict[datetime, dict]:
    o = np.array([b.open for b in bars]); h = np.array([b.high for b in bars])
    lo = np.array([b.low for b in bars]); c = np.array([b.close for b in bars])
    v = np.array([max(b.volume, 1) for b in bars], float)
    tp = (h + lo + c) / 3
    vwap = np.cumsum(tp * v) / np.cumsum(v)
    dev = c - vwap
    sd = np.array([dev[: i + 1].std() if i >= 9 else np.nan for i in range(len(c))])
    prev_c = np.r_[o[0], c[:-1]]
    tr = np.maximum(h - lo, np.maximum(abs(h - prev_c), abs(lo - prev_c)))
    atr = np.array([tr[max(0, i - 13): i + 1].mean() for i in range(len(c))])
    diff = np.diff(c, prepend=c[0])
    up = np.where(diff > 0, diff, 0); dn = np.where(diff < 0, -diff, 0)
    rsi = np.array([100 * up[max(0, i - 13): i + 1].sum() /
                    max(1e-9, (up + dn)[max(0, i - 13): i + 1].sum()) for i in range(len(c))])
    run_hi = np.maximum.accumulate(h); run_lo = np.minimum.accumulate(lo)
    out = {}
    for i, b in enumerate(bars):
        def ret(n):
            return (c[i] - c[max(0, i - n)]) / atr[i] if atr[i] > 0 else np.nan
        out[b.timestamp.astimezone(NY).replace(second=0, microsecond=0)] = {
            "vwap_z": dev[i] / sd[i] if sd[i] and sd[i] > 0 else np.nan,
            "ret5_atr": ret(5), "ret15_atr": ret(15), "ret30_atr": ret(30),
            "rsi14": rsi[i],
            "off_low_atr": (c[i] - run_lo[i]) / atr[i] if atr[i] > 0 else np.nan,
            "off_high_atr": (run_hi[i] - c[i]) / atr[i] if atr[i] > 0 else np.nan,
            "tr_atr": tr[i] / atr[i] if atr[i] > 0 else np.nan,
            "vol_rel": v[i] / v[max(0, i - 29): i + 1].mean(),
        }
    return out


async def fetch_bars(client, future: str, day_open_ny: datetime, end_ny: datetime):
    bars = await client.get_historical_bars(
        CONTRACT[future], BarUnit.MINUTE, 1,
        start_time=day_open_ny.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=end_ny.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return sorted(bars, key=lambda b: b.timestamp)


def percentile_of(value: float, population: np.ndarray) -> float:
    pop = population[~np.isnan(population)]
    return float((pop < value).mean() * 100) if pop.size and not np.isnan(value) else np.nan


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=20)
    a = ap.parse_args()

    out = market_data.derived_path(
        "research", "pi_reverse_dots_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ"))
    (out / "images").mkdir(parents=True, exist_ok=True)

    pairs = fetch_pairs(a.images)
    print(f"{len(pairs)} chart/signal pairs")
    client = TopstepXClient(username=os.environ["TOPSTEPX_USERNAME"],
                            api_key=os.environ["TOPSTEPX_API_KEY"], use_demo=False)
    await client.authenticate()

    dot_rows, feat_rows = [], []
    for p in pairs:
        img = out / "images" / f"{p['message_id']}.jpg"
        if not img.exists():
            r = httpx.get(p["image_url"], timeout=30)
            if r.status_code == 404:
                r = httpx.get(refresh_url(p["image_url"]), timeout=30)
            if r.status_code != 200:
                # Cross-posted CDN links carry an expiry (?ex=); old ones 404.
                print(f"  skip {p['message_id']}: image HTTP {r.status_code}")
                continue
            img.write_bytes(r.content)
        try:
            ex = extract(img)
        except ValueError as exc:
            print(f"  skip {p['message_id']}: {exc}")
            continue
        title = p["ts"]
        future = FUTURE[p["equity"]]
        open_ny = title.astimezone(NY).replace(hour=9, minute=30, second=0, microsecond=0)
        bars = await fetch_bars(client, future, open_ny, title + timedelta(minutes=1))
        feats = session_features(bars)
        if not feats:
            continue
        # Time-axis check: the drawn price line must track the future's close.
        # Pixel y grows downward, so a correct axis gives r close to -1.
        close_at = {b.timestamp.astimezone(NY).replace(second=0, microsecond=0): b.close for b in bars}
        pairs_xy = [(y, close_at.get(x_to_ny(x, ex["band_l"], ex["price_end"], title)))
                    for x, y in ex["line_y"].items()]
        pairs_xy = [(y, c) for y, c in pairs_xy if c is not None]
        axis_r = (float(np.corrcoef(*zip(*pairs_xy))[0, 1])
                  if len(pairs_xy) > 10 else float("nan"))
        if not axis_r <= -MIN_AXIS_ABS_R:
            print(f"  gate {title.astimezone(NY):%m-%d %H:%M} {p['equity']}: axis_r={axis_r:.3f} → excluded")
            feat_rows.append({"message_id": p["message_id"], "title_utc": title.isoformat(),
                              "equity": p["equity"], "kinds": "/".join(p["kinds"]),
                              "band_l": ex["band_l"], "price_end": ex["price_end"],
                              "bars": len(bars), "axis_r": round(axis_r, 4),
                              "n_yellow": "", "n_gray": ""})
            continue
        keys = list(feats[next(iter(feats))])
        pop = {k: np.array([f[k] for f in feats.values()], float) for k in keys}
        n_dots = {"yellow": 0, "gray": 0}
        for colour in ("yellow", "gray"):
            for x, y, area in ex[colour]:
                if x < ex["band_l"] - 2 or x > ex["price_end"] + 2:
                    continue  # previous session: different x scale
                t = x_to_ny(x, ex["band_l"], ex["price_end"], title)
                f = feats.get(t)
                if f is None:
                    continue
                n_dots[colour] += 1
                row = {"message_id": p["message_id"], "equity": p["equity"],
                       "colour": colour, "ny_time": t.isoformat(), "x": round(x), "y": round(y),
                       "area": area}
                for k in keys:
                    row[k] = round(float(f[k]), 4) if not np.isnan(f[k]) else ""
                    row[k + "_pct"] = round(percentile_of(f[k], pop[k]), 1)
                dot_rows.append(row)
        feat_rows.append({"message_id": p["message_id"], "title_utc": title.isoformat(),
                          "equity": p["equity"], "kinds": "/".join(p["kinds"]),
                          "band_l": ex["band_l"], "price_end": ex["price_end"],
                          "bars": len(bars), "axis_r": round(axis_r, 4),
                          **{f"n_{k}": v for k, v in n_dots.items()}})
        print(f"  {title.astimezone(NY):%m-%d %H:%M} {p['equity']} {'/'.join(p['kinds'])}: "
              f"yellow={n_dots['yellow']} gray={n_dots['gray']} bars={len(bars)} axis_r={axis_r:.3f}")

    for name, rows in (("dots.csv", dot_rows), ("charts.csv", feat_rows)):
        if rows:
            with open(out / name, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader(); w.writerows(rows)

    # Summary: where do dots sit in each feature's within-session distribution?
    passed = sum(1 for r in feat_rows if r["n_yellow"] != "")
    lines = [f"# π dot probe ({passed}/{len(feat_rows)} charts pass the time-axis gate "
             f"|r|≥{MIN_AXIS_ABS_R}, {len(dot_rows)} dots)\n",
             "Median within-session percentile of each feature at dot minutes "
             "(50 = ordinary; near 0 or 100 = extreme). Share = dots in the outer 10%.\n",
             "| feature | yellow median pct | yellow share ≤10 / ≥90 | gray median pct | gray share ≤10 / ≥90 |",
             "|---|---|---|---|---|"]
    if dot_rows:
        for k in keys:
            cells = []
            for colour in ("yellow", "gray"):
                v = np.array([r[k + "_pct"] for r in dot_rows if r["colour"] == colour], float)
                v = v[~np.isnan(v)]
                if v.size:
                    cells += [f"{np.median(v):.0f}",
                              f"{(v <= 10).mean():.0%} / {(v >= 90).mean():.0%} (n={v.size})"]
                else:
                    cells += ["–", "–"]
            lines.append(f"| {k} | " + " | ".join(cells) + " |")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
