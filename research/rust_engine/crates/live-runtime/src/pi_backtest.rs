//! Offline PI fill and ledger replay.
//!
//! Signal generation and active-exit operations are supplied by the Python
//! parity oracle. This module owns only candle fills, position sequencing,
//! costs, and realized ledger accounting.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, HashSet};

#[derive(Debug, Deserialize)]
struct Fixture {
    schema: String,
    scenarios: Vec<Scenario>,
}

#[derive(Debug, Deserialize)]
struct Scenario {
    name: String,
    bars: Vec<Bar>,
    entries: Vec<Entry>,
    exit_operations: Vec<ExitOperation>,
    initial_capital: f64,
    point_value: f64,
    commission_rt: f64,
    fees_rt: f64,
    max_daily_loss: f64,
    #[serde(default = "yes")]
    force_exit_at_end: bool,
}

#[derive(Clone, Debug, Deserialize)]
struct Bar {
    timestamp: String,
    calendar_date: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    #[serde(default)]
    flatten_window: bool,
}

#[derive(Clone, Debug, Deserialize)]
struct Entry {
    entry_key: String,
    timestamp: String,
    direction: String,
    entry_price: f64,
    stop_loss: f64,
    take_profit: f64,
    quantity: u32,
    #[serde(default)]
    signal_reason: String,
    #[serde(default)]
    exit_policy: Value,
}

#[derive(Clone, Debug, Deserialize)]
struct ExitOperation {
    trade_index: usize,
    timestamp: String,
    action: String,
    reason: Option<String>,
    stop_price: Option<f64>,
    trail_triggered: bool,
    #[serde(default)]
    ladder_max_r: f64,
    ladder_lock_r: Option<f64>,
}

#[derive(Clone, Debug)]
struct Position {
    entry: Entry,
    trade_index: usize,
    entry_time: DateTime<Utc>,
    current_sl: f64,
    trail_triggered: bool,
    ladder_max_r: f64,
    ladder_lock_r: Option<f64>,
}

#[derive(Clone, Debug, Serialize)]
struct ClosedTrade {
    trade_index: usize,
    entry_key: String,
    entry_time: String,
    exit_time: String,
    direction: String,
    entry_price: f64,
    original_stop_loss: f64,
    final_stop_loss: f64,
    take_profit: f64,
    exit_price: f64,
    quantity: u32,
    point_value: f64,
    gross_pnl: f64,
    commission: f64,
    fees: f64,
    net_pnl: f64,
    exit_reason: String,
    signal_reason: String,
    exit_policy: Value,
}

fn yes() -> bool {
    true
}

fn parse_timestamp(value: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(value)
        .map(|value| value.with_timezone(&Utc))
        .map_err(|error| format!("invalid RFC3339 timestamp {value:?}: {error}"))
}

fn finite_price(value: f64, label: &str) -> Result<(), String> {
    if !value.is_finite() {
        return Err(format!("{label} must be finite"));
    }
    Ok(())
}

fn validate_scenario(scenario: &Scenario) -> Result<(), String> {
    if scenario.name.trim().is_empty() {
        return Err("scenario name is empty".to_owned());
    }
    if scenario.point_value <= 0.0 || !scenario.point_value.is_finite() {
        return Err(format!("{}: point_value must be positive", scenario.name));
    }
    for (label, value) in [
        ("initial_capital", scenario.initial_capital),
        ("commission_rt", scenario.commission_rt),
        ("fees_rt", scenario.fees_rt),
        ("max_daily_loss", scenario.max_daily_loss),
    ] {
        finite_price(value, label)?;
        if value < 0.0 {
            return Err(format!("{}: {label} must be non-negative", scenario.name));
        }
    }

    let mut previous = None;
    for bar in &scenario.bars {
        let timestamp = parse_timestamp(&bar.timestamp)?;
        if previous.is_some_and(|value| timestamp <= value) {
            return Err(format!("{}: bars must be strictly chronological", scenario.name));
        }
        previous = Some(timestamp);
        if bar.calendar_date.len() != 10 {
            return Err(format!("{}: invalid calendar_date", scenario.name));
        }
        for (label, price) in [
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
        ] {
            finite_price(price, label)?;
        }
        if bar.high < bar.low
            || bar.high < bar.open
            || bar.high < bar.close
            || bar.low > bar.open
            || bar.low > bar.close
        {
            return Err(format!("{}: invalid OHLC range at {}", scenario.name, bar.timestamp));
        }
    }
    if scenario.bars.is_empty() {
        return Err(format!("{}: bars are empty", scenario.name));
    }

    let bars: HashSet<String> = scenario.bars.iter().map(|bar| bar.timestamp.clone()).collect();
    let mut previous_entry = None;
    for entry in &scenario.entries {
        let timestamp = parse_timestamp(&entry.timestamp)?;
        if previous_entry.is_some_and(|value| timestamp < value) {
            return Err(format!("{}: entries must be chronological", scenario.name));
        }
        previous_entry = Some(timestamp);
        if !bars.contains(&entry.timestamp) {
            return Err(format!("{}: entry timestamp has no bar", scenario.name));
        }
        if entry.entry_key.trim().is_empty() || entry.quantity == 0 {
            return Err(format!("{}: entry key and quantity are required", scenario.name));
        }
        if entry.direction != "buy" && entry.direction != "sell" {
            return Err(format!("{}: unsupported entry direction", scenario.name));
        }
        for (label, price) in [
            ("entry_price", entry.entry_price),
            ("stop_loss", entry.stop_loss),
            ("take_profit", entry.take_profit),
        ] {
            finite_price(price, label)?;
        }
    }
    for operation in &scenario.exit_operations {
        parse_timestamp(&operation.timestamp)?;
        if !bars.contains(&operation.timestamp) {
            return Err(format!("{}: exit operation timestamp has no bar", scenario.name));
        }
        if operation.action != "none" && operation.action != "move_sl" && operation.action != "close" {
            return Err(format!("{}: unsupported exit operation", scenario.name));
        }
        if let Some(price) = operation.stop_price {
            finite_price(price, "stop_price")?;
        }
    }
    Ok(())
}

fn close_position(
    position: Position,
    bar: &Bar,
    exit_price: f64,
    requested_reason: &str,
    scenario: &Scenario,
    daily_net: &mut HashMap<String, f64>,
) -> ClosedTrade {
    let reason = if requested_reason == "flatten" && position.trail_triggered {
        "trail_sl"
    } else {
        requested_reason
    };
    let signed_points = if position.entry.direction == "buy" {
        exit_price - position.entry.entry_price
    } else {
        position.entry.entry_price - exit_price
    };
    let gross = signed_points * scenario.point_value * f64::from(position.entry.quantity);
    let commission = scenario.commission_rt * f64::from(position.entry.quantity);
    let fees = scenario.fees_rt * f64::from(position.entry.quantity);
    let net = gross - commission - fees;
    *daily_net.entry(bar.calendar_date.clone()).or_default() += net;
    ClosedTrade {
        trade_index: position.trade_index,
        entry_key: position.entry.entry_key,
        entry_time: position.entry_time.to_rfc3339(),
        exit_time: bar.timestamp.clone(),
        direction: position.entry.direction,
        entry_price: position.entry.entry_price,
        original_stop_loss: position.entry.stop_loss,
        final_stop_loss: position.current_sl,
        take_profit: position.entry.take_profit,
        exit_price,
        quantity: position.entry.quantity,
        point_value: scenario.point_value,
        gross_pnl: gross,
        commission,
        fees,
        net_pnl: net,
        exit_reason: reason.to_owned(),
        signal_reason: position.entry.signal_reason,
        exit_policy: position.entry.exit_policy,
    }
}

fn close_reason(position: &Position, level: &str) -> &'static str {
    match level {
        "tp" => "tp",
        "sl" if position.trail_triggered => "trail_sl",
        "sl" => "sl",
        _ => "flatten",
    }
}

fn replay_scenario(scenario: Scenario) -> Result<Value, String> {
    validate_scenario(&scenario)?;
    let mut operations = BTreeMap::new();
    for (index, operation) in scenario.exit_operations.iter().cloned().enumerate() {
        let key = (operation.trade_index, operation.timestamp.clone());
        if operations.insert(key, (index, operation)).is_some() {
            return Err(format!("{}: duplicate exit operation", scenario.name));
        }
    }
    let mut operation_used = HashSet::new();
    let entries_by_time: BTreeMap<String, Vec<(usize, Entry)>> = scenario
        .entries
        .iter()
        .cloned()
        .enumerate()
        .fold(BTreeMap::new(), |mut out, (index, entry)| {
            out.entry(entry.timestamp.clone()).or_default().push((index, entry));
            out
        });
    let mut position: Option<Position> = None;
    let mut trades = Vec::new();
    let mut daily_net: HashMap<String, f64> = HashMap::new();
    let mut ending_capital = scenario.initial_capital;
    let mut processed_entries = 0usize;

    for bar in &scenario.bars {
        let daily_locked = daily_net.get(&bar.calendar_date).copied().unwrap_or(0.0)
            <= -scenario.max_daily_loss;
        if daily_locked {
            if let Some(open) = position.take() {
                let closed = close_position(
                    open,
                    bar,
                    bar.close,
                    "flatten",
                    &scenario,
                    &mut daily_net,
                );
                ending_capital += closed.net_pnl;
                trades.push(closed);
            }
            continue;
        }
        if bar.flatten_window {
            if let Some(open) = position.take() {
                let closed = close_position(
                    open,
                    bar,
                    bar.close,
                    "flatten",
                    &scenario,
                    &mut daily_net,
                );
                ending_capital += closed.net_pnl;
                trades.push(closed);
            }
            continue;
        }

        let mut exit_operation_closed = false;
        if let Some(open) = position.as_ref() {
            let hit_sl = if open.entry.direction == "buy" {
                bar.low <= open.current_sl
            } else {
                bar.high >= open.current_sl
            };
            let hit_tp = if open.entry.direction == "buy" {
                bar.high >= open.entry.take_profit
            } else {
                bar.low <= open.entry.take_profit
            };
            let touched = match (hit_sl, hit_tp) {
                (true, true) => {
                    let sl_distance = (bar.open - open.current_sl).abs();
                    let tp_distance = (bar.open - open.entry.take_profit).abs();
                    Some(if sl_distance <= tp_distance { "sl" } else { "tp" })
                }
                (true, false) => Some("sl"),
                (false, true) => Some("tp"),
                (false, false) => None,
            };
            if let Some(level) = touched {
                let open = position.take().expect("position was checked above");
                let reason = close_reason(&open, level);
                let price = if level == "sl" { open.current_sl } else { open.entry.take_profit };
                let closed = close_position(open, bar, price, reason, &scenario, &mut daily_net);
                ending_capital += closed.net_pnl;
                trades.push(closed);
            } else {
                let key = (open.trade_index, bar.timestamp.clone());
                let found = operations.get(&key);
                if let Some((op_index, operation)) = found {
                    operation_used.insert(*op_index);
                    match operation.action.as_str() {
                        "none" => {
                            let open = position.as_mut().expect("position remains open");
                            open.trail_triggered = operation.trail_triggered;
                            open.ladder_max_r = operation.ladder_max_r;
                            open.ladder_lock_r = operation.ladder_lock_r;
                        }
                        "move_sl" => {
                            let open = position.as_mut().expect("position remains open");
                            open.current_sl = operation
                                .stop_price
                                .ok_or_else(|| format!("{}: move_sl requires stop_price", scenario.name))?;
                            open.trail_triggered = operation.trail_triggered;
                            open.ladder_max_r = operation.ladder_max_r;
                            open.ladder_lock_r = operation.ladder_lock_r;
                        }
                        "close" => {
                            let open = position.take().expect("position remains open");
                            let reason = operation.reason.as_deref().unwrap_or("flatten");
                            let closed = close_position(
                                open,
                                bar,
                                bar.close,
                                reason,
                                &scenario,
                                &mut daily_net,
                            );
                            ending_capital += closed.net_pnl;
                            trades.push(closed);
                            exit_operation_closed = true;
                        }
                        _ => unreachable!("actions are validated"),
                    }
                } else {
                    return Err(format!(
                        "{}: missing shared exit operation for trade {} at {}",
                        scenario.name, open.trade_index, bar.timestamp
                    ));
                }
            }
        }

        if exit_operation_closed {
            continue;
        }
        if position.is_some() {
            continue;
        }
        if let Some(entries) = entries_by_time.get(&bar.timestamp) {
            if entries.len() != 1 {
                return Err(format!("{}: at most one entry per candle is supported", scenario.name));
            }
            let (trade_index, entry) = entries[0].clone();
            if entry.direction == "buy" && entry.stop_loss >= entry.entry_price
                || entry.direction == "sell" && entry.stop_loss <= entry.entry_price
            {
                return Err(format!("{}: stop loss is on the wrong side of entry", scenario.name));
            }
            let entry_time = parse_timestamp(&entry.timestamp)?;
            position = Some(Position {
                current_sl: entry.stop_loss,
                entry,
                trade_index,
                entry_time,
                trail_triggered: false,
                ladder_max_r: 0.0,
                ladder_lock_r: None,
            });
            processed_entries += 1;

            // Python's market-entry candle checks only the stop; its high may
            // have printed before the entry was accepted at the close.
            let hit_sl = position.as_ref().is_some_and(|open| {
                if open.entry.direction == "buy" {
                    bar.low <= open.current_sl
                } else {
                    bar.high >= open.current_sl
                }
            });
            if hit_sl {
                let open = position.take().expect("entry position exists");
                let price = open.current_sl;
                let reason = close_reason(&open, "sl");
                let closed = close_position(open, bar, price, reason, &scenario, &mut daily_net);
                ending_capital += closed.net_pnl;
                trades.push(closed);
            }
        }
    }

    if scenario.force_exit_at_end {
        if let (Some(open), Some(last_bar)) = (position.take(), scenario.bars.last()) {
            let closed = close_position(
                open,
                last_bar,
                last_bar.close,
                "flatten",
                &scenario,
                &mut daily_net,
            );
            ending_capital += closed.net_pnl;
            trades.push(closed);
        }
    }
    if operation_used.len() != scenario.exit_operations.len() {
        let unused = scenario.exit_operations.len() - operation_used.len();
        return Err(format!("{}: {unused} exit operation(s) were not consumed", scenario.name));
    }
    if processed_entries != scenario.entries.len() {
        return Err(format!(
            "{}: processed {processed_entries} of {} frozen entries",
            scenario.name,
            scenario.entries.len()
        ));
    }

    let realized_net_pnl: f64 = trades.iter().map(|trade| trade.net_pnl).sum();
    Ok(json!({
        "name": scenario.name,
        "bars": scenario.bars.len(),
        "frozen_entries": scenario.entries.len(),
        "trades": trades,
        "realized_net_pnl": realized_net_pnl,
        "ending_capital": ending_capital,
        "final_position_open": position.is_some(),
        "final_open_trade_index": position.map(|open| open.trade_index),
    }))
}

pub fn replay_json(input: &[u8]) -> Result<Value, String> {
    let fixture: Fixture = serde_json::from_slice(input).map_err(|error| error.to_string())?;
    if fixture.schema != "ancsertpx.live-runtime-pi-backtest.v1" {
        return Err("unsupported PI Backtest fixture schema".to_owned());
    }
    if fixture.scenarios.is_empty() {
        return Err("PI Backtest fixture requires at least one scenario".to_owned());
    }
    let scenarios = fixture
        .scenarios
        .into_iter()
        .map(replay_scenario)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(json!({
        "schema": "ancsertpx.live-runtime-pi-backtest-result.v1",
        "scenarios": scenarios,
    }))
}
