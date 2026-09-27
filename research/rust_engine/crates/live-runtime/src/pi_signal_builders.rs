use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::topstep_trade_date;
use crate::exit::Side;
use crate::pi::{
    PiMark, PiParams, atr_bracket, parse_level, python_datetime_str, python_iso_timestamp,
    round_to_tick,
};
use crate::pi_lifecycle::{
    PiLifecycleConfig, PiSource, ReopenTicket, continuation_enabled,
    replacement_mark_selected, replacement_source_active, ticket_source,
};

#[derive(Clone, Debug, Deserialize)]
pub struct PiSignalBuilderFixture {
    pub schema: String,
    pub params: PiParams,
    pub lifecycle: PiLifecycleConfig,
    pub cases: Vec<PiSignalBuilderCase>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PiSignalBuilderCase {
    pub id: String,
    pub action: String,
    pub candle: crate::pi::PiBar,
    #[serde(default)]
    pub atr_blend: Option<f64>,
    #[serde(default)]
    pub trades_used_today: u32,
    #[serde(default)]
    pub max_gap_r: Option<f64>,
    #[serde(default)]
    pub ticket: Option<ReopenTicket>,
    #[serde(default)]
    pub mark: Option<PiMark>,
    #[serde(default)]
    pub active_source: Option<PiSource>,
    #[serde(default)]
    pub active_pi: Value,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiSignalBuilderReport {
    pub schema: String,
    pub results: Vec<PiSignalBuilderResult>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiSignalBuilderResult {
    pub id: String,
    pub ticket_consumed: bool,
    pub mark_consumed: bool,
    pub trades_used_today_after: u32,
    pub signal: Option<PiBuiltSignal>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiBuiltSignal {
    pub direction: String,
    pub quantity: u32,
    pub entry: f64,
    pub stop_loss: f64,
    pub take_profit: f64,
    pub atr_blend: f64,
    pub risk_width: f64,
    pub reason: String,
    pub zone_id: String,
    pub trade_date: String,
    pub meta: Value,
}

pub fn replay(fixture: PiSignalBuilderFixture) -> Result<PiSignalBuilderReport, String> {
    if fixture.schema != "ancsertpx.live-runtime-pi-signal-builders.v1" {
        return Err("unsupported PI signal builder fixture schema".to_owned());
    }
    if fixture.params.tick_size <= 0.0 || fixture.params.contract_size == 0 {
        return Err("PI signal builder fixture has invalid tick or quantity".to_owned());
    }
    let results = fixture
        .cases
        .iter()
        .map(|case| build_case(&fixture, case))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(PiSignalBuilderReport {
        schema: "ancsertpx.live-runtime-pi-signal-builders.result.v1".to_owned(),
        results,
    })
}

fn build_case(
    fixture: &PiSignalBuilderFixture,
    case: &PiSignalBuilderCase,
) -> Result<PiSignalBuilderResult, String> {
    match case.action.as_str() {
        "reopen" => build_reopen_case(fixture, case),
        "replacement" => build_replacement_case(fixture, case),
        other => Err(format!("unsupported PI signal builder action: {other}")),
    }
}

fn build_reopen_case(
    fixture: &PiSignalBuilderFixture,
    case: &PiSignalBuilderCase,
) -> Result<PiSignalBuilderResult, String> {
    let Some(ticket) = case.ticket.as_ref() else {
        return Ok(result(case, false, false, None));
    };
    let timestamp = parse_utc(&case.candle.ts)?;
    let side = match ticket.direction.trim().to_ascii_lowercase().as_str() {
        "buy" | "long" | "1" => Side::Buy,
        "sell" | "short" | "-1" => Side::Sell,
        _ => return Ok(result(case, true, false, None)),
    };
    let selected_source = ticket_source(ticket);
    if !continuation_enabled(&fixture.lifecycle, &selected_source) {
        return Ok(result(case, true, false, None));
    }
    let source_risk = (ticket.source_entry - ticket.original_sl).abs();
    if source_risk <= 0.0 {
        return Ok(result(case, true, false, None));
    }
    let entry = round_to_tick(case.candle.close, fixture.params.tick_size);
    let gap_r = (entry - ticket.flat_price).abs() / source_risk;
    let gap_cap = case.max_gap_r.unwrap_or(1.0).max(0.0);
    if gap_cap > 0.0 && gap_r > gap_cap {
        return Ok(result(case, true, false, None));
    }
    let Some(atr_blend) = case.atr_blend.filter(|value| *value > 0.0) else {
        return Ok(result(case, true, false, None));
    };
    let source_reason = if ticket.source_reason.is_empty() {
        "PI continuation"
    } else {
        &ticket.source_reason
    };
    let reason = format!("{source_reason} | REOPEN CONTINUATION");
    let Some(signal) = construct_signal(
        &fixture.params,
        &case.candle,
        timestamp,
        side,
        atr_blend,
        reason,
        case.trades_used_today,
        reopen_meta(ticket, timestamp, gap_r, side, &fixture.params, atr_blend),
    ) else {
        return Ok(result(case, true, false, None));
    };
    Ok(result(case, true, false, Some(signal)))
}

fn build_replacement_case(
    fixture: &PiSignalBuilderFixture,
    case: &PiSignalBuilderCase,
) -> Result<PiSignalBuilderResult, String> {
    let Some(active_source) = case.active_source.as_ref() else {
        return Ok(result(case, false, false, None));
    };
    let selected_source = active_pi_source(active_source, &case.active_pi);
    if !replacement_source_active(&fixture.lifecycle, &selected_source) {
        return Ok(result(case, false, false, None));
    }
    let Some(mark) = case.mark.as_ref() else {
        return Ok(result(case, false, false, None));
    };
    if !mark_would_enter_queue(mark, &fixture.params) {
        return Ok(result(case, false, false, None));
    }
    let mark_consumed = true;
    if !replacement_mark_selected(&PiSource {
        direction: if mark.direction > 0 { "buy" } else { "sell" }.to_owned(),
        kind: mark.kind.clone(),
        level: mark.level,
        continuation: false,
        replacement: false,
    }) {
        return Ok(result(case, false, mark_consumed, None));
    }
    let now = parse_utc(&case.candle.ts)?;
    let mark_time = parse_utc(&mark.ts)?;
    let age_minutes = (now - mark_time).num_milliseconds() as f64 / 60_000.0;
    if age_minutes > fixture.params.max_signal_age_minutes as f64 {
        return Ok(result(case, false, mark_consumed, None));
    }
    let Some(atr_blend) = case.atr_blend.filter(|value| *value > 0.0) else {
        return Ok(result(case, false, mark_consumed, None));
    };
    let Some(level) = mark.level.or_else(|| parse_level(&mark.size)) else {
        return Ok(result(case, false, mark_consumed, None));
    };
    let source_reason = format!(
        "PI {} {}/{}{}",
        mark.equity,
        mark.kind,
        mark.size,
        mark.pos
            .as_deref()
            .filter(|value| !value.is_empty())
            .map(|value| format!("/{value}"))
            .unwrap_or_default()
    );
    let meta = json!({
        "pi": {
            "message_id": mark.message_id,
            "equity": mark.equity,
            "kind": mark.kind,
            "level": level,
            "size": mark.size,
            "pos": mark.pos,
            "signal_ts": python_datetime_str(mark_time),
        },
        "pi_replacement": {
            "replaces_kind": "青π",
            "source_level": 2,
            "replacement_time": python_iso_timestamp(now),
            "original_pi": case.active_pi,
        }
    });
    let Some(signal) = construct_signal(
        &fixture.params,
        &case.candle,
        now,
        Side::Buy,
        atr_blend,
        source_reason,
        case.trades_used_today,
        meta,
    ) else {
        return Ok(result(case, false, mark_consumed, None));
    };
    Ok(result(case, false, mark_consumed, Some(signal)))
}

fn active_pi_source(active_source: &PiSource, active_pi: &Value) -> PiSource {
    let kind = active_pi
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let level = active_pi.get("level").and_then(|value| {
        value
            .as_i64()
            .and_then(|level| i32::try_from(level).ok())
            .or_else(|| value.as_str().and_then(|level| level.parse().ok()))
    });
    PiSource {
        direction: active_source.direction.clone(),
        kind,
        level,
        continuation: active_source.continuation,
        replacement: active_source.replacement,
    }
}

fn mark_would_enter_queue(mark: &PiMark, params: &PiParams) -> bool {
    if mark.future != params.future {
        return false;
    }
    if params.long_only && mark.direction <= 0 {
        return false;
    }
    if mark.direction < 0
        && mark.kind == "紫圈"
        && params.short_levels.as_ref().is_some_and(|levels| {
            mark.level.is_none_or(|level| !levels.contains(&level))
        })
    {
        return false;
    }
    let allowed = if mark.direction > 0 {
        &params.long_kinds
    } else {
        &params.short_kinds
    };
    allowed.contains(&mark.kind)
}

#[allow(clippy::too_many_arguments)]
fn construct_signal(
    params: &PiParams,
    candle: &crate::pi::PiBar,
    timestamp: DateTime<Utc>,
    side: Side,
    atr_blend: f64,
    source_reason: String,
    trades_used_today: u32,
    meta: Value,
) -> Option<PiBuiltSignal> {
    if (params.side_mode.eq_ignore_ascii_case("long_only") && side != Side::Buy)
        || (params.side_mode.eq_ignore_ascii_case("short_only") && side != Side::Sell)
        || (params.max_trades_per_day > 0 && trades_used_today >= params.max_trades_per_day)
    {
        return None;
    }
    let bracket = atr_bracket(candle.close, atr_blend, side, params)?;
    let trade_date = topstep_trade_date(timestamp).to_string();
    Some(PiBuiltSignal {
        direction: match side {
            Side::Buy => "buy",
            Side::Sell => "sell",
        }
        .to_owned(),
        quantity: params.contract_size,
        entry: bracket.entry,
        stop_loss: bracket.stop_loss,
        take_profit: bracket.take_profit,
        atr_blend,
        risk_width: bracket.risk_width,
        reason: format!("PI | {source_reason}"),
        zone_id: format!("PI:{trade_date}:{}", python_iso_timestamp(timestamp)),
        trade_date,
        meta,
    })
}

fn reopen_meta(
    ticket: &ReopenTicket,
    timestamp: DateTime<Utc>,
    gap_r: f64,
    side: Side,
    params: &PiParams,
    atr_blend: f64,
) -> Value {
    let width = if side == Side::Sell && params.sl_atr > 0.0 {
        atr_blend * params.short_sl / params.sl_atr
    } else {
        atr_blend
    };
    let pi = if ticket.pi.is_null() {
        json!({})
    } else {
        ticket.pi.clone()
    };
    json!({
        "pi": pi,
        "pi_continuation": {
            "source_flat_time": ticket.flat_time,
            "reopen_time": python_iso_timestamp(timestamp),
            "source_entry": ticket.source_entry,
            "source_flat_price": ticket.flat_price,
            "reopen_gap_r": (gap_r * 100_000.0).round_ties_even() / 100_000.0,
            "atr_blend": (width * 1_000_000.0).round_ties_even() / 1_000_000.0,
            "reentries_for_source": 1,
        }
    })
}

fn result(
    case: &PiSignalBuilderCase,
    ticket_consumed: bool,
    mark_consumed: bool,
    signal: Option<PiBuiltSignal>,
) -> PiSignalBuilderResult {
    PiSignalBuilderResult {
        id: case.id.clone(),
        ticket_consumed,
        mark_consumed,
        trades_used_today_after: case.trades_used_today + u32::from(signal.is_some()),
        signal,
    }
}

fn parse_utc(raw: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(raw)
        .map(|value| value.with_timezone(&Utc))
        .map_err(|error| format!("invalid PI builder timestamp {raw}: {error}"))
}

pub fn replay_json(input: &[u8]) -> Result<PiSignalBuilderReport, String> {
    let fixture = serde_json::from_slice::<PiSignalBuilderFixture>(input)
        .map_err(|error| format!("invalid PI signal builder fixture: {error}"))?;
    replay(fixture)
}
