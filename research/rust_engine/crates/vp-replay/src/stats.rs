use crate::model::TradeRow;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn summarize(values: &[f64]) -> Value {
    let net: f64 = values.iter().sum();
    let wins: f64 = values.iter().copied().filter(|value| *value > 0.0).sum();
    let losses: f64 = values.iter().copied().filter(|value| *value < 0.0).sum();
    let pf = if losses < 0.0 {
        wins / losses.abs()
    } else if wins > 0.0 {
        f64::INFINITY
    } else {
        0.0
    };
    let mut cumulative: f64 = 0.0;
    let mut peak: f64 = 0.0;
    let mut max_dd: f64 = 0.0;
    for value in values {
        cumulative += value;
        peak = peak.max(cumulative);
        max_dd = max_dd.max(peak - cumulative);
    }
    json!({
        "n": values.len(),
        "pnl": round2(net),
        "pf": if pf.is_finite() { round4(pf) } else { 999.0 },
        "win_rate": if values.is_empty() { 0.0 } else { values.iter().filter(|value| **value > 0.0).count() as f64 / values.len() as f64 },
        "max_drawdown": round2(max_dd),
        "avg_trade": if values.is_empty() { 0.0 } else { round2(net / values.len() as f64) }
    })
}

/// Summarize a strategy-neutral P&L sequence with the shared ledger metrics.
pub fn summarize_pnl(values: &[f64]) -> Value {
    summarize(values)
}

fn shared_pf(gain: f64, loss: f64) -> f64 {
    if loss > 0.0 {
        gain / loss
    } else if gain > 0.0 {
        999.0
    } else {
        0.0
    }
}

fn shared_series_stats(values: &[f64]) -> Value {
    let mut gain = 0.0;
    let mut loss = 0.0;
    let mut equity = 0.0;
    let mut peak = 0.0_f64;
    let mut max_dd = 0.0_f64;
    let mut wins = 0usize;
    for pnl in values {
        if *pnl > 0.0 {
            gain += pnl;
            wins += 1;
        } else {
            loss -= pnl;
        }
        equity += pnl;
        peak = peak.max(equity);
        max_dd = max_dd.max(peak - equity);
    }
    json!({
        "n": values.len(),
        "pnl": equity,
        "pf": shared_pf(gain, loss),
        "max_dd": max_dd,
        "win": if values.is_empty() { 0.0 } else { wins as f64 / values.len() as f64 }
    })
}

/// Match backend.backtest.robustness.walk_forward's equal-time-span buckets and gate.
pub fn shared_walk_forward(points: &[(f64, f64)], segments: usize) -> Option<Value> {
    if segments == 0 || points.len() < 2 * segments {
        return None;
    }
    let start = points.iter().map(|point| point.0).fold(f64::INFINITY, f64::min);
    let end = points.iter().map(|point| point.0).fold(f64::NEG_INFINITY, f64::max);
    let span = (end - start).max(1.0);
    let mut buckets = vec![Vec::<f64>::new(); segments];
    for (timestamp, pnl) in points {
        let offset = timestamp - start;
        let index = (((offset * segments as f64) / span) as usize).min(segments - 1);
        buckets[index].push(*pnl);
    }
    let summaries: Vec<Value> = buckets.iter().map(|values| shared_series_stats(values)).collect();
    let passed = summaries.iter().all(|summary| {
        summary["n"].as_u64().unwrap_or(0) > 0
            && summary["pnl"].as_f64().unwrap_or(0.0) > 0.0
            && summary["pf"].as_f64().unwrap_or(0.0) > 1.0
    });
    Some(json!({"segments": summaries, "pass": passed}))
}

struct PythonMt19937 {
    state: [u32; 624],
    index: usize,
}

impl PythonMt19937 {
    /// CPython Random.seed(int) initialization for nonnegative 32-bit seeds.
    fn new(seed: u32) -> Self {
        let mut state = [0u32; 624];
        state[0] = 19_650_218;
        for index in 1..624 {
            state[index] = 1_812_433_253u32
                .wrapping_mul(state[index - 1] ^ (state[index - 1] >> 30))
                .wrapping_add(index as u32);
        }
        let mut i = 1usize;
        let mut j = 0usize;
        for _ in 0..624 {
            state[i] = (state[i]
                ^ (state[i - 1] ^ (state[i - 1] >> 30)).wrapping_mul(1_664_525))
                .wrapping_add(seed)
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                state[0] = state[623];
                i = 1;
            }
            if j >= 1 {
                j = 0;
            }
        }
        for _ in 0..623 {
            state[i] = (state[i]
                ^ (state[i - 1] ^ (state[i - 1] >> 30)).wrapping_mul(1_566_083_941))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                state[0] = state[623];
                i = 1;
            }
        }
        state[0] = 0x8000_0000;
        Self { state, index: 624 }
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= 624 {
            for index in 0..624 {
                let y = (self.state[index] & 0x8000_0000)
                    | (self.state[(index + 1) % 624] & 0x7fff_ffff);
                let twist = if y & 1 == 0 { 0 } else { 0x9908_b0df };
                self.state[index] = self.state[(index + 397) % 624] ^ (y >> 1) ^ twist;
            }
            self.index = 0;
        }
        let mut value = self.state[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c_5680;
        value ^= (value << 15) & 0xefc6_0000;
        value ^= value >> 18;
        value
    }

    fn getrandbits(&mut self, bits: u32) -> u32 {
        self.next_u32() >> (32 - bits)
    }

    /// CPython's _randbelow_with_getrandbits, including its rejection step.
    fn randbelow(&mut self, upper: usize) -> usize {
        let bits = usize::BITS - upper.leading_zeros();
        loop {
            let candidate = self.getrandbits(bits) as usize;
            if candidate < upper {
                return candidate;
            }
        }
    }
}

fn nearest_rank(values: &[f64], percentile: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let position = (percentile * (values.len() - 1) as f64).round_ties_even() as usize;
    values[position.min(values.len() - 1)]
}

/// Match the shared Python Monte Carlo gate, including CPython MT19937 sampling,
/// `randrange` rejection, nearest-even percentile ranks, and gate thresholds.
pub fn shared_monte_carlo(
    pnls: &[f64],
    iterations: usize,
    seed: u32,
    drawdown_threshold: f64,
) -> Option<Value> {
    if pnls.len() < 10 || iterations == 0 {
        return None;
    }
    let mut rng = PythonMt19937::new(seed);
    let mut totals = Vec::with_capacity(iterations);
    let mut drawdowns = Vec::with_capacity(iterations);
    let mut profit_factors = Vec::with_capacity(iterations);
    let mut losses = 0usize;
    let mut drawdown_breaches = 0usize;
    for _ in 0..iterations {
        let mut equity = 0.0;
        let mut peak = 0.0_f64;
        let mut max_dd = 0.0_f64;
        let mut gain = 0.0;
        let mut loss = 0.0;
        for _ in 0..pnls.len() {
            let pnl = pnls[rng.randbelow(pnls.len())];
            if pnl > 0.0 {
                gain += pnl;
            } else {
                loss -= pnl;
            }
            equity += pnl;
            peak = peak.max(equity);
            max_dd = max_dd.max(peak - equity);
        }
        totals.push(equity);
        drawdowns.push(max_dd);
        profit_factors.push(shared_pf(gain, loss));
        losses += usize::from(equity <= 0.0);
        drawdown_breaches += usize::from(max_dd > drawdown_threshold);
    }
    totals.sort_by(f64::total_cmp);
    drawdowns.sort_by(f64::total_cmp);
    profit_factors.sort_by(f64::total_cmp);
    let p_loss = losses as f64 / iterations as f64;
    let dd_p95 = nearest_rank(&drawdowns, 0.95);
    let pf_p5 = nearest_rank(&profit_factors, 0.05);
    Some(json!({
        "iters": iterations,
        "seed": seed,
        "n": pnls.len(),
        "pnl_p5": nearest_rank(&totals, 0.05),
        "pnl_p25": nearest_rank(&totals, 0.25),
        "pnl_p50": nearest_rank(&totals, 0.50),
        "pnl_p75": nearest_rank(&totals, 0.75),
        "pnl_p95": nearest_rank(&totals, 0.95),
        "p_loss": p_loss,
        "dd_p5": nearest_rank(&drawdowns, 0.05),
        "dd_p25": nearest_rank(&drawdowns, 0.25),
        "dd_p50": nearest_rank(&drawdowns, 0.50),
        "dd_p75": nearest_rank(&drawdowns, 0.75),
        "dd_p95": dd_p95,
        "p_dd_breach": drawdown_breaches as f64 / iterations as f64,
        "dd_threshold": drawdown_threshold,
        "pf_p5": pf_p5,
        "shared_gate_pass": p_loss <= 0.05 && dd_p95 < drawdown_threshold && pf_p5 > 1.0
    }))
}

fn linear_percentile(sorted: &[f64], percentile: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let position = percentile * (sorted.len() - 1) as f64;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    if lower == upper {
        sorted[lower]
    } else {
        sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower as f64)
    }
}

/// Circular moving-block bootstrap over ordered weekly-event P&L values.
pub fn circular_block_bootstrap(
    values: &[f64],
    draws: usize,
    block_events: usize,
    seed: u32,
) -> Value {
    if values.is_empty() || draws == 0 || block_events == 0 {
        return json!({"draws": draws, "block_events": block_events, "p_positive": null});
    }
    let mut rng = PythonMt19937::new(seed);
    let blocks = values.len().div_ceil(block_events);
    let mut totals = Vec::with_capacity(draws);
    for _ in 0..draws {
        let mut total = 0.0;
        let mut sampled = 0usize;
        for _ in 0..blocks {
            let start = rng.randbelow(values.len());
            for offset in 0..block_events {
                if sampled == values.len() {
                    break;
                }
                total += values[(start + offset) % values.len()];
                sampled += 1;
            }
        }
        totals.push(total);
    }
    totals.sort_by(f64::total_cmp);
    json!({
        "draws": draws,
        "block_events": block_events,
        "seed": seed,
        "p_positive": totals.iter().filter(|value| **value > 0.0).count() as f64 / draws as f64,
        "p05": linear_percentile(&totals, 0.05),
        "p50": linear_percentile(&totals, 0.50),
        "p95": linear_percentile(&totals, 0.95)
    })
}

fn round2(value: f64) -> f64 {
    (value * 100.0).round_ties_even() / 100.0
}

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round_ties_even() / 10_000.0
}

pub fn summarize_ledger(rows: &[TradeRow], tick_value: f64, stress_ticks: f64) -> Value {
    let pnl: Vec<f64> = rows.iter().map(|row| row.net_pnl).collect();
    let stress: Vec<f64> = rows
        .iter()
        .map(|row| row.net_pnl - stress_ticks * tick_value * row.contracts as f64)
        .collect();
    let entry_dates: BTreeSet<String> = rows.iter().map(|row| row.trade_date_key()).collect();

    let mut by_year = BTreeMap::<String, Vec<f64>>::new();
    let mut by_entry_day = BTreeMap::<String, Vec<f64>>::new();
    for (row, value) in rows.iter().zip(pnl.iter()) {
        by_year
            .entry(row.entry_time_utc[..4].to_owned())
            .or_default()
            .push(*value);
        by_entry_day
            .entry(row.rth_session_date_et.clone())
            .or_default()
            .push(*value);
    }

    let best_day = by_entry_day.iter().max_by(|left, right| {
        left.1
            .iter()
            .sum::<f64>()
            .total_cmp(&right.1.iter().sum::<f64>())
    });
    let best_day_removed = best_day
        .map(|(day, day_values)| {
            let removed: Vec<f64> = by_entry_day
                .iter()
                .filter(|(other_day, _)| *other_day != day)
                .flat_map(|(_, values)| values.iter().copied())
                .collect();
            json!({
                "date_et": day,
                "net_removed": round2(day_values.iter().sum()),
                "net_after_removal": round2(removed.iter().sum()),
                "remaining_pf": summarize(&removed)["pf"]
            })
        })
        .unwrap_or_else(|| {
            json!({
                "date_et": null,
                "net_removed": 0.0,
                "net_after_removal": 0.0,
                "remaining_pf": 0.0
            })
        });

    let year_summary: BTreeMap<String, Value> = by_year
        .iter()
        .map(|(year, values)| (year.clone(), summarize(values)))
        .collect();

    json!({
        "trade_count": rows.len(),
        "independent_entry_dates": entry_dates.len(),
        "baseline": summarize(&pnl),
        "stress_14t": summarize(&stress),
        "best_day_removal": best_day_removed,
        "by_year": year_summary
    })
}

trait TradeRowDate {
    fn trade_date_key(&self) -> String;
}

impl TradeRowDate for TradeRow {
    fn trade_date_key(&self) -> String {
        self.rth_session_date_et.clone()
    }
}
