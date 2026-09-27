use chrono::{DateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::America::New_York;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

const DEFAULT_REOPEN_WINDOW_MINUTES: f64 = 5.0;

#[derive(Clone, Debug, Deserialize)]
pub struct PiLifecycleFixture {
    pub schema: String,
    pub config: PiLifecycleConfig,
    #[serde(default)]
    pub source_cases: Vec<SourceCase>,
    #[serde(default)]
    pub arm_cases: Vec<ArmCase>,
    #[serde(default)]
    pub ticket_cases: Vec<TicketCase>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PiLifecycleConfig {
    #[serde(default = "default_strategy_mode")]
    pub strategy_mode: String,
    pub long_only: bool,
    pub long_kinds: Vec<String>,
    pub short_kinds: Vec<String>,
    pub short_levels: Option<Vec<i32>>,
    pub continue_long_kinds: Vec<String>,
    pub continue_short_kinds: Vec<String>,
    pub continue_short_levels: Option<Vec<i32>>,
    pub replacement_enabled: bool,
    #[serde(default, deserialize_with = "deserialize_identity")]
    pub expected_account_id: Option<String>,
    pub expected_contract_id: Option<String>,
    #[serde(default = "default_reopen_window")]
    pub reopen_window_minutes: f64,
}

fn default_reopen_window() -> f64 {
    DEFAULT_REOPEN_WINDOW_MINUTES
}

fn default_strategy_mode() -> String {
    "pi".to_owned()
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PiSource {
    pub direction: String,
    pub kind: String,
    pub level: Option<i32>,
    #[serde(default)]
    pub continuation: bool,
    #[serde(default)]
    pub replacement: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SourceCase {
    pub id: String,
    pub source: PiSource,
}

#[derive(Clone, Debug, Deserialize)]
pub struct LifecycleBar {
    pub timestamp_utc: String,
    pub high: f64,
    pub low: f64,
    pub close: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ArmPosition {
    pub source_entry: f64,
    pub active_sl: f64,
    pub active_tp: f64,
    pub original_sl: f64,
    pub original_tp: f64,
    #[serde(default)]
    pub source_reason: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ArmCase {
    pub id: String,
    #[serde(default = "default_backtest_surface")]
    pub surface: String,
    #[serde(default)]
    pub arm_time_utc: Option<String>,
    pub source: PiSource,
    #[serde(default)]
    pub pi: Option<Value>,
    pub position: ArmPosition,
    pub bar: LifecycleBar,
    #[serde(default)]
    pub flat_time: Option<String>,
}

fn default_backtest_surface() -> String {
    "backtest".to_owned()
}

fn deserialize_identity<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<serde_json::Value>::deserialize(deserializer)?;
    Ok(value.map(|value| match value {
        serde_json::Value::String(value) => value,
        serde_json::Value::Number(value) => value.to_string(),
        serde_json::Value::Bool(value) => value.to_string(),
        other => other.to_string(),
    }))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ReopenTicket {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<u32>,
    pub direction: String,
    pub source_entry: f64,
    pub original_sl: f64,
    pub original_tp: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub risk: Option<f64>,
    pub flat_price: f64,
    pub flat_time: String,
    #[serde(default)]
    pub pi: Value,
    pub source_reason: String,
    #[serde(default, deserialize_with = "deserialize_identity", skip_serializing_if = "Option::is_none")]
    pub account_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contract_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TicketCase {
    pub id: String,
    pub ticket: ReopenTicket,
    pub steps: Vec<TicketStep>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TicketStep {
    pub action: String,
    pub surface: String,
    pub bar: LifecycleBar,
    #[serde(default)]
    pub position_open: bool,
    #[serde(default)]
    pub pending_order: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct SelectorResult {
    pub id: String,
    pub continuation_enabled: bool,
    pub replacement_source_active: bool,
    pub replacement_mark_selected: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct ArmResult {
    pub id: String,
    pub armed: bool,
    pub reason: String,
    pub ticket: Option<ReopenTicket>,
}

#[derive(Clone, Debug, Serialize)]
pub struct TicketStepResult {
    pub action: String,
    pub status: String,
    pub reason: Option<String>,
    pub claimed_ticket: Option<ReopenTicket>,
}

#[derive(Clone, Debug, Serialize)]
pub struct TicketCaseResult {
    pub id: String,
    pub steps: Vec<TicketStepResult>,
    pub ticket_remaining: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiLifecycleReport {
    pub schema: String,
    pub source_results: Vec<SelectorResult>,
    pub arm_results: Vec<ArmResult>,
    pub ticket_results: Vec<TicketCaseResult>,
}

pub fn continuation_enabled(config: &PiLifecycleConfig, source: &PiSource) -> bool {
    let kind = source.kind.trim();
    if source.continuation {
        return false;
    }
    match direction(&source.direction).as_deref() {
        Some("buy") => {
            config.long_kinds.iter().any(|value| value == kind)
                && config
                    .continue_long_kinds
                    .iter()
                    .any(|value| value == kind)
        }
        Some("sell") if !config.long_only => {
            if !config.short_kinds.iter().any(|value| value == kind)
                || !config
                    .continue_short_kinds
                    .iter()
                    .any(|value| value == kind)
            {
                return false;
            }
            if kind == "紫圈"
                && config
                    .continue_short_levels
                    .as_ref()
                    .is_some_and(|levels| source.level.is_none_or(|level| !levels.contains(&level)))
            {
                return false;
            }
            if kind == "紫圈"
                && config
                    .short_levels
                    .as_ref()
                    .is_some_and(|levels| source.level.is_none_or(|level| !levels.contains(&level)))
            {
                return false;
            }
            true
        }
        _ => false,
    }
}

pub fn replacement_source_active(config: &PiLifecycleConfig, source: &PiSource) -> bool {
    config.replacement_enabled
        && direction(&source.direction).as_deref() == Some("buy")
        && source.kind == "青π"
        && source.level == Some(3)
        && !source.continuation
        && !source.replacement
}

pub fn replacement_mark_selected(source: &PiSource) -> bool {
    direction(&source.direction).as_deref() == Some("buy")
        && source.kind == "深蓝圈"
        && source.level == Some(2)
}

pub fn ticket_source(ticket: &ReopenTicket) -> PiSource {
    let kind = ticket
        .pi
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    let level = ticket.pi.get("level").and_then(|value| {
        value
            .as_i64()
            .and_then(|level| i32::try_from(level).ok())
            .or_else(|| value.as_str().and_then(|level| level.parse().ok()))
    });
    PiSource {
        direction: ticket.direction.clone(),
        kind,
        level,
        continuation: false,
        replacement: false,
    }
}

pub fn replay(fixture: PiLifecycleFixture) -> Result<PiLifecycleReport, String> {
    if fixture.schema != "ancsertpx.live-runtime-pi-lifecycle.v1" {
        return Err("unsupported PI lifecycle fixture schema".to_owned());
    }
    let source_results = fixture
        .source_cases
        .iter()
        .map(|case| SelectorResult {
            id: case.id.clone(),
            continuation_enabled: continuation_enabled(&fixture.config, &case.source),
            replacement_source_active: replacement_source_active(&fixture.config, &case.source),
            replacement_mark_selected: replacement_mark_selected(&case.source),
        })
        .collect();
    let arm_results = fixture
        .arm_cases
        .iter()
        .map(|case| arm_ticket(&fixture.config, case))
        .collect::<Result<Vec<_>, _>>()?;
    let ticket_results = fixture
        .ticket_cases
        .into_iter()
        .map(|case| replay_ticket_case(&fixture.config, case))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(PiLifecycleReport {
        schema: "ancsertpx.live-runtime-pi-lifecycle.result.v1".to_owned(),
        source_results,
        arm_results,
        ticket_results,
    })
}

fn arm_ticket(config: &PiLifecycleConfig, case: &ArmCase) -> Result<ArmResult, String> {
    let timestamp = parse_utc(&case.bar.timestamp_utc)?;
    let arm_time = match case.surface.as_str() {
        "backtest" => timestamp,
        "live" => match &case.arm_time_utc {
            Some(value) => parse_utc(value)?,
            None => timestamp,
        },
        _ => return Err(format!("unsupported PI lifecycle surface: {}", case.surface)),
    };
    if config.strategy_mode != "pi" || !is_flatten_window(arm_time) {
        return Ok(ArmResult {
            id: case.id.clone(),
            armed: false,
            reason: "outside_flatten_window".to_owned(),
            ticket: None,
        });
    }
    if !continuation_enabled(config, &case.source) {
        return Ok(ArmResult {
            id: case.id.clone(),
            armed: false,
            reason: "source_not_selected".to_owned(),
            ticket: None,
        });
    }
    let side = direction(&case.source.direction).ok_or("unsupported position direction")?;
    let touched = match side.as_str() {
        "buy" => case.bar.low <= case.position.active_sl || case.bar.high >= case.position.active_tp,
        _ => case.bar.high >= case.position.active_sl || case.bar.low <= case.position.active_tp,
    };
    if touched {
        return Ok(ArmResult {
            id: case.id.clone(),
            armed: false,
            reason: "source_bracket_touched_on_flatten".to_owned(),
            ticket: None,
        });
    }
    let risk = (case.position.source_entry - case.position.original_sl).abs();
    if risk <= 0.0 {
        return Ok(ArmResult {
            id: case.id.clone(),
            armed: false,
            reason: "zero_source_risk".to_owned(),
            ticket: None,
        });
    }
    let ticket = ReopenTicket {
        version: (case.surface == "live").then_some(1),
        direction: side,
        source_entry: case.position.source_entry,
        original_sl: case.position.original_sl,
        original_tp: case.position.original_tp,
        risk: (case.surface == "backtest").then_some(risk),
        flat_price: case.bar.close,
        flat_time: if case.surface == "live" {
            case.flat_time
                .clone()
                .unwrap_or_else(|| case.bar.timestamp_utc.clone())
        } else {
            case.bar.timestamp_utc.clone()
        },
        pi: case.pi.clone().unwrap_or_else(|| {
            serde_json::json!({"kind": case.source.kind, "level": case.source.level})
        }),
        source_reason: if case.position.source_reason.is_empty() {
            "PI signal".to_owned()
        } else {
            case.position.source_reason.clone()
        },
        account_id: (case.surface == "live")
            .then(|| config.expected_account_id.clone())
            .flatten(),
        contract_id: (case.surface == "live")
            .then(|| config.expected_contract_id.clone())
            .flatten(),
    };
    Ok(ArmResult {
        id: case.id.clone(),
        armed: true,
        reason: "armed".to_owned(),
        ticket: Some(ticket),
    })
}

fn replay_ticket_case(
    config: &PiLifecycleConfig,
    case: TicketCase,
) -> Result<TicketCaseResult, String> {
    let mut ticket = Some(case.ticket);
    let mut results = Vec::with_capacity(case.steps.len());
    for step in case.steps {
        if !matches!(step.action.as_str(), "update" | "claim") {
            return Err(format!("unsupported PI lifecycle action: {}", step.action));
        }
        if !matches!(step.surface.as_str(), "backtest" | "live") {
            return Err(format!("unsupported PI lifecycle surface: {}", step.surface));
        }
        let mut claimed_ticket = None;
        let transition = if ticket.is_none() {
            Transition::empty()
        } else if step.action == "claim" && (step.position_open || step.pending_order) {
            Transition {
                status: "pending",
                reason: Some("position_or_order_gate".to_owned()),
                remove_ticket: false,
            }
        } else {
            let current = ticket.as_ref().expect("ticket was checked above");
            update_ticket(config, current, &step)?
        };

        let transition = if transition.status == "pending"
            && step.action == "claim"
            && !step.position_open
            && !step.pending_order
            && ticket.is_some()
        {
            let current = ticket.as_ref().expect("ticket is present");
            if is_claim_window(current, &step.bar, config.reopen_window_minutes)? {
                claimed_ticket = ticket.take();
                Transition {
                    status: "claimed",
                    reason: None,
                    remove_ticket: true,
                }
            } else {
                transition
            }
        } else {
            transition
        };

        if transition.remove_ticket {
            ticket = None;
        }
        results.push(TicketStepResult {
            action: step.action,
            status: transition.status.to_owned(),
            reason: transition.reason,
            claimed_ticket,
        });
    }
    Ok(TicketCaseResult {
        id: case.id,
        steps: results,
        ticket_remaining: ticket.is_some(),
    })
}

#[derive(Clone, Debug)]
struct Transition {
    status: &'static str,
    reason: Option<String>,
    remove_ticket: bool,
}

impl Transition {
    fn empty() -> Self {
        Self {
            status: "empty",
            reason: None,
            remove_ticket: false,
        }
    }
}

fn update_ticket(
    config: &PiLifecycleConfig,
    ticket: &ReopenTicket,
    step: &TicketStep,
) -> Result<Transition, String> {
    let flat_time = parse_utc(&ticket.flat_time)?;
    let now = parse_utc(&step.bar.timestamp_utc)?;
    if step.surface == "live"
        && (config.strategy_mode != "pi"
            || ticket.account_id.as_deref() != config.expected_account_id.as_deref()
            || ticket.contract_id.as_deref() != config.expected_contract_id.as_deref()
            || !continuation_enabled(config, &ticket_source(ticket)))
    {
        return Ok(terminal("invalidated", "ticket_identity_or_selector_changed"));
    }
    let flat_local = flat_time.with_timezone(&New_York);
    let now_local = now.with_timezone(&New_York);
    if now_local.date_naive() > flat_local.date_naive() {
        return Ok(terminal("expired", "same_day_reopen_missed"));
    }
    if now_local.date_naive() != flat_local.date_naive() || now < flat_time {
        return Ok(Transition {
            status: "pending",
            reason: None,
            remove_ticket: false,
        });
    }

    let is_buy = direction(&ticket.direction).as_deref() == Some("buy");
    let stop_hit = if is_buy {
        step.bar.low <= ticket.original_sl
    } else {
        step.bar.high >= ticket.original_sl
    };
    let target_hit = if is_buy {
        step.bar.high >= ticket.original_tp
    } else {
        step.bar.low <= ticket.original_tp
    };
    if stop_hit {
        return Ok(terminal("invalidated", "original_stop_touched_while_flat"));
    }
    if target_hit {
        return Ok(terminal("invalidated", "original_target_reached_while_flat"));
    }

    let Some(elapsed) = reopen_elapsed_minutes(now) else {
        return Ok(Transition {
            status: "pending",
            reason: None,
            remove_ticket: false,
        });
    };
    if step.surface == "live" && step.position_open {
        return Ok(terminal("invalidated", "position_remained_open_at_reopen"));
    }
    if elapsed > config.reopen_window_minutes {
        return Ok(terminal("expired", "reopen_entry_window_elapsed"));
    }
    Ok(Transition {
        status: "pending",
        reason: None,
        remove_ticket: false,
    })
}

fn is_claim_window(
    ticket: &ReopenTicket,
    bar: &LifecycleBar,
    reopen_window_minutes: f64,
) -> Result<bool, String> {
    let flat_time = parse_utc(&ticket.flat_time)?;
    let now = parse_utc(&bar.timestamp_utc)?;
    let flat_date = flat_time.with_timezone(&New_York).date_naive();
    let current_date = now.with_timezone(&New_York).date_naive();
    if current_date != flat_date || now < flat_time {
        return Ok(false);
    }
    Ok(reopen_elapsed_minutes(now).is_some_and(|elapsed| elapsed <= reopen_window_minutes))
}

fn terminal(status: &'static str, reason: &str) -> Transition {
    Transition {
        status,
        reason: Some(reason.to_owned()),
        remove_ticket: true,
    }
}

fn is_flatten_window(timestamp: DateTime<Utc>) -> bool {
    let local = timestamp.with_timezone(&New_York);
    let time = local.time();
    let flatten = NaiveTime::from_hms_opt(15, 45, 0).expect("valid flatten time");
    let reopen = NaiveTime::from_hms_opt(18, 0, 0).expect("valid reopen time");
    time >= flatten && time < reopen
}

fn reopen_elapsed_minutes(timestamp: DateTime<Utc>) -> Option<f64> {
    let local = timestamp.with_timezone(&New_York);
    let time = local.time();
    let reopen_time = NaiveTime::from_hms_opt(18, 0, 0)?;
    if time < reopen_time {
        return None;
    }
    let reopen_local = New_York
        .from_local_datetime(&local.date_naive().and_time(reopen_time))
        .single()?;
    Some((timestamp - reopen_local.with_timezone(&Utc)).num_milliseconds() as f64 / 60_000.0)
}

fn direction(raw: &str) -> Option<String> {
    let text = raw.trim().to_ascii_lowercase();
    match text.as_str() {
        "buy" | "long" | "1" => Some("buy".to_owned()),
        "sell" | "short" | "-1" => Some("sell".to_owned()),
        _ => None,
    }
}

fn parse_utc(raw: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(raw)
        .map(|value| value.with_timezone(&Utc))
        .map_err(|error| format!("invalid lifecycle timestamp {raw}: {error}"))
}

pub fn replay_json(input: &[u8]) -> Result<PiLifecycleReport, String> {
    let fixture = serde_json::from_slice::<PiLifecycleFixture>(input)
        .map_err(|error| format!("invalid PI lifecycle fixture: {error}"))?;
    replay(fixture)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn continuation_and_replacement_selectors_require_structured_source_fields() {
        let config = PiLifecycleConfig {
            strategy_mode: "pi".into(),
            long_only: false,
            long_kinds: vec!["青π".into(), "深蓝圈".into()],
            short_kinds: vec!["粉π".into(), "紫圈".into()],
            short_levels: Some(vec![2]),
            continue_long_kinds: vec!["深蓝圈".into()],
            continue_short_kinds: vec!["紫圈".into()],
            continue_short_levels: Some(vec![2]),
            replacement_enabled: true,
            expected_account_id: None,
            expected_contract_id: None,
            reopen_window_minutes: 5.0,
        };
        let selected = PiSource {
            direction: "buy".into(),
            kind: "深蓝圈".into(),
            level: Some(2),
            continuation: false,
            replacement: false,
        };
        assert!(continuation_enabled(&config, &selected));
        assert!(replacement_source_active(
            &config,
            &PiSource {
                direction: "buy".into(),
                kind: "青π".into(),
                level: Some(3),
                continuation: false,
                replacement: false,
            }
        ));
        assert!(!replacement_mark_selected(&PiSource {
            level: None,
            ..selected.clone()
        }));
    }

    #[test]
    fn new_york_flatten_and_reopen_boundaries_follow_dst() {
        let flatten = parse_utc("2026-03-09T19:45:00Z").unwrap();
        let before = parse_utc("2026-03-09T19:44:00Z").unwrap();
        let reopen = parse_utc("2026-03-09T22:00:00Z").unwrap();
        assert!(!is_flatten_window(before));
        assert!(is_flatten_window(flatten));
        assert_eq!(reopen_elapsed_minutes(reopen), Some(0.0));
        assert_eq!(reopen_elapsed_minutes(reopen + chrono::Duration::minutes(5)), Some(5.0));
    }
}
