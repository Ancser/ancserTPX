use crate::{FixtureBar, VpFixtureParams};
use ancsertpx_live_runtime::adapters::topstepx::{
    PythonBridge, bridge_event_to_broker_event, normalize_snapshot,
};
use ancsertpx_live_runtime::core::{
    BrokerAction, BrokerEvent, Direction, RuntimeConfig, RuntimeMode,
};
use ancsertpx_live_runtime::runtime::Runtime;
use ancsertpx_live_runtime::strategy::VpBreakoutAdapter;
use ancsertpx_vp_replay::model::{Bar, DataSource};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[derive(Deserialize)]
struct LiveConfigFile {
    runtime: RuntimeConfig,
    vp_parameters: VpFixtureParams,
    #[serde(default = "default_exit_strategy")]
    exit_strategy_mode: String,
    #[serde(default)]
    exit_params: Value,
    #[serde(default = "default_history_days")]
    history_days: u32,
    #[serde(default = "default_minimum_warmup_bars")]
    minimum_warmup_bars: usize,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
struct ExitKernelState {
    custom_tag: String,
    trail_triggered: bool,
    ladder_max_r: f64,
    ladder_lock_r: Option<f64>,
}

struct MinuteBar {
    start: DateTime<Utc>,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: i64,
}

#[derive(Default)]
struct TradeBarBuilder {
    current: Option<MinuteBar>,
}

impl TradeBarBuilder {
    fn push(
        &mut self,
        timestamp: DateTime<Utc>,
        price: f64,
        volume: i64,
    ) -> Result<Option<MinuteBar>, String> {
        if !price.is_finite() || price <= 0.0 || volume <= 0 {
            return Err("invalid_topstepx_trade_tick".to_owned());
        }
        let minute = timestamp.timestamp().div_euclid(60) * 60;
        let bucket = DateTime::from_timestamp(minute, 0)
            .ok_or_else(|| "topstepx_trade_timestamp_out_of_range".to_owned())?;
        match self.current.as_mut() {
            None => {
                self.current = Some(MinuteBar {
                    start: bucket,
                    open: price,
                    high: price,
                    low: price,
                    close: price,
                    volume,
                });
                Ok(None)
            }
            Some(current) if bucket < current.start => {
                Err("topstepx_trade_minute_regressed".to_owned())
            }
            Some(current) if bucket == current.start => {
                current.high = current.high.max(price);
                current.low = current.low.min(price);
                current.close = price;
                current.volume = current
                    .volume
                    .checked_add(volume)
                    .ok_or_else(|| "topstepx_trade_bar_volume_overflow".to_owned())?;
                Ok(None)
            }
            Some(_) => {
                let completed = self.current.replace(MinuteBar {
                    start: bucket,
                    open: price,
                    high: price,
                    low: price,
                    close: price,
                    volume,
                });
                Ok(completed)
            }
        }
    }
}

fn default_exit_strategy() -> String {
    "factor".to_owned()
}

fn default_history_days() -> u32 {
    15
}

fn default_minimum_warmup_bars() -> usize {
    250
}

pub(super) fn run(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    crate::shutdown::install().map_err(std::io::Error::other)?;
    let Some(config_path) = args.first().filter(|arg| !arg.starts_with("--")) else {
        return Err("live requires a config path and all three explicit arming flags".into());
    };
    let supplied: Vec<&str> = args.iter().skip(1).map(String::as_str).collect();
    for required in ["--arm", "--approve-vp-breakout-atr", "--confirm-auto-oco"] {
        if supplied.iter().filter(|flag| **flag == required).count() != 1 {
            return Err(format!("live command requires exactly one {required}").into());
        }
    }
    if supplied
        .iter()
        .any(|flag| !["--arm", "--approve-vp-breakout-atr", "--confirm-auto-oco"].contains(flag))
    {
        return Err("live command received an unsupported flag".into());
    }

    let text = std::fs::read_to_string(config_path)?;
    let mut file: LiveConfigFile = serde_json::from_str(&text)?;
    if file.runtime.mode != RuntimeMode::Live {
        return Err("live config must set runtime.mode=live".into());
    }
    file.runtime.live_armed = true;
    file.runtime.strategy_live_approved = true;
    file.runtime.auto_oco_confirmed = true;
    file.runtime.broker_account_verified = false;
    file.runtime.contract_active_verified = false;
    file.runtime.account_lease_held = false;
    file.runtime.market_data_connected = false;
    file.runtime.user_hub_connected = false;
    file.runtime.journal_ready = false;
    ancsertpx_live_runtime::config::validate_runtime_config(&file.runtime)?;
    if file
        .runtime
        .contract_id
        .split('.')
        .nth(3)
        .is_none_or(|root| !matches!(root, "MES" | "MNQ" | "ES" | "NQ"))
    {
        return Err("live config contract root is unsupported".into());
    }
    if file.history_days == 0 || file.minimum_warmup_bars < 250 {
        return Err(
            "history_days must be positive and warmup must request at least 250 bars".into(),
        );
    }
    if file.exit_strategy_mode.trim().is_empty() {
        file.exit_strategy_mode = default_exit_strategy();
    }

    let account_id = file
        .runtime
        .account_id
        .ok_or("live account_id is required")?;
    let contract_id = file.runtime.contract_id.clone();
    let strategy = VpBreakoutAdapter::new(
        file.runtime.tick_size,
        file.runtime.contract_size,
        file.vp_parameters.strategy_params(),
    )?;
    let mut strategy = strategy;
    let repository_root = PythonBridge::repository_root();
    let journal_path = default_journal_path(&repository_root, account_id, &contract_id);
    let mut runtime = Runtime::open(file.runtime.clone(), &journal_path)?;
    let python = std::env::var("ANCERSTPX_PYTHON").unwrap_or_else(|_| "python".to_owned());
    let mut bridge = PythonBridge::spawn(&python, &repository_root)?;

    let start = bridge.request(
        "start_topstepx",
        json!({
            "mode": "live",
            "live_armed": true,
            "strategy_live_approved": true,
            "auto_oco_confirmed": true,
            "account_id": account_id,
            "contract_id": contract_id,
            "contract_size": file.runtime.contract_size,
            "tick_size": file.runtime.tick_size,
            "tick_value": file.runtime.tick_value,
            "history_days": file.history_days,
            "minimum_warmup_bars": file.minimum_warmup_bars,
        }),
    )?;
    verify_started_session(&start, account_id, &contract_id)?;

    let history_value = start
        .get("history_bars")
        .cloned()
        .ok_or("live_start_history_bars_missing")?;
    let history: Vec<FixtureBar> = serde_json::from_value(history_value)?;
    let bars: Vec<Bar> = history
        .into_iter()
        .map(FixtureBar::into_bar)
        .collect::<Result<_, _>>()?;
    if bars.len() < file.minimum_warmup_bars {
        return Err("live_start_history_below_requested_warmup".into());
    }
    for pair in bars.windows(2) {
        if pair[0].timestamp_us >= pair[1].timestamp_us {
            return Err("live_start_history_not_strictly_monotonic".into());
        }
    }
    strategy.warmup(&bars);
    runtime.append_record(
        "live_history_warmup",
        &json!({
            "bar_count": bars.len(),
            "first_bar_utc": bars.first().map(Bar::utc),
            "last_bar_utc": bars.last().map(Bar::utc),
            "source": "topstepx",
        }),
    )?;
    runtime.mark_warmup_complete()?;

    let snapshot = snapshot_from_payload(&start, account_id)?;
    runtime.set_live_capabilities(true, true, true, true, true, true)?;
    runtime.mark_reconciled(snapshot)?;
    if runtime.status().blockers.iter().any(|reason| {
        reason.starts_with("external_or_untracked_")
            || reason.starts_with("submit_outcome_unresolved")
            || reason.starts_with("entry_order_missing")
            || reason == "attached_auto_oco_children_not_confirmed"
    }) {
        return Err(format!(
            "live startup reconciliation requires operator review: {}",
            runtime.status().blockers.join(",")
        )
        .into());
    }

    println!(
        "{}",
        serde_json::to_string(&json!({
            "event": "live_coordinator_ready",
            "mode": "live",
            "strategy": "vp_breakout_retest_atr",
            "contract_id": contract_id,
            "history_bar_count": bars.len(),
            "initial_order_submissions": 0,
            "journal_path": journal_path,
            "runtime_status": runtime.status(),
        }))?
    );

    run_event_loop(
        &mut bridge,
        &mut runtime,
        &mut strategy,
        &file,
        account_id,
        &contract_id,
    )?;
    let shutdown = bridge.stop();
    println!(
        "{}",
        serde_json::to_string(&json!({
            "event": "live_coordinator_stopped",
            "broker_position_size_at_stop": runtime.status().owned_size,
            "runtime_blockers": runtime.status().blockers,
            "bridge_stopped": shutdown.is_ok(),
            "shutdown_error": shutdown.err(),
        }))?
    );
    Ok(())
}

fn verify_started_session(
    payload: &Value,
    account_id: i64,
    contract_id: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let matched = payload.get("account_id").and_then(Value::as_i64) == Some(account_id)
        && payload.get("contract_id").and_then(Value::as_str) == Some(contract_id)
        && payload.get("can_trade").and_then(Value::as_bool) == Some(true)
        && payload.get("account_visible").and_then(Value::as_bool) == Some(true)
        && payload
            .get("active_contract_verified")
            .and_then(Value::as_bool)
            == Some(true)
        && payload.get("account_lease_held").and_then(Value::as_bool) == Some(true)
        && payload
            .get("market_data_connected")
            .and_then(Value::as_bool)
            == Some(true)
        && payload.get("user_hub_connected").and_then(Value::as_bool) == Some(true);
    if !matched {
        return Err("topstepx_startup_capability_verification_failed".into());
    }
    Ok(())
}

fn snapshot_from_payload(
    payload: &Value,
    account_id: i64,
) -> Result<ancsertpx_live_runtime::core::BrokerSnapshot, Box<dyn std::error::Error>> {
    let orders = payload
        .get("orders")
        .and_then(Value::as_array)
        .ok_or("topstepx_start_orders_missing")?;
    let positions = payload
        .get("positions")
        .and_then(Value::as_array)
        .ok_or("topstepx_start_positions_missing")?;
    Ok(normalize_snapshot(
        account_id,
        Utc::now(),
        orders,
        positions,
    )?)
}

fn run_event_loop(
    bridge: &mut PythonBridge,
    runtime: &mut Runtime,
    strategy: &mut VpBreakoutAdapter,
    config: &LiveConfigFile,
    account_id: i64,
    contract_id: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut bars = TradeBarBuilder::default();
    let mut latest_trade_timestamp: Option<DateTime<Utc>> = None;
    let mut next_reconciliation = Instant::now() + Duration::from_secs(15);
    let mut cancel_requested = false;
    let mut close_in_flight = false;
    let mut unprotected_since: Option<Instant> = None;

    loop {
        if crate::shutdown::requested() {
            break;
        }
        if let Some(message) = bridge.next_event(Duration::from_millis(250))? {
            let (event_type, payload) = match &message {
                ancsertpx_live_runtime::events::BridgeMessage::Event {
                    event_type,
                    payload,
                    ..
                } => (event_type.as_str(), payload),
                _ => continue,
            };
            if event_type == "connection_state"
                && payload.get("connected").and_then(Value::as_bool) == Some(false)
            {
                runtime.require_startup_refresh()?;
                if let Some(event) = bridge_event_to_broker_event(&message, account_id)? {
                    runtime.apply_broker_event(event)?;
                }
                break;
            }

            let mut closed_bar = None;
            if event_type == "market_trade" {
                let timestamp_text = payload
                    .get("timestamp")
                    .and_then(Value::as_str)
                    .ok_or("market_trade_timestamp_missing")?;
                let timestamp = DateTime::parse_from_rfc3339(timestamp_text)?.with_timezone(&Utc);
                if latest_trade_timestamp.is_some_and(|previous| timestamp < previous) {
                    return Err("market_trade_timestamp_regressed_after_bridge_filter".into());
                }
                if latest_trade_timestamp.is_none_or(|previous| timestamp > previous) {
                    runtime.apply_broker_event(BrokerEvent::MarketTimestamp(timestamp))?;
                    latest_trade_timestamp = Some(timestamp);
                }
                let price = payload
                    .get("price")
                    .and_then(Value::as_f64)
                    .ok_or("market_trade_price_missing")?;
                let volume = payload
                    .get("volume")
                    .and_then(Value::as_i64)
                    .ok_or("market_trade_volume_missing")?;
                closed_bar = bars.push(timestamp, price, volume)?;
            }
            if let Some(event) = bridge_event_to_broker_event(&message, account_id)? {
                if matches!(event, BrokerEvent::ReconciliationRequired { .. }) {
                    runtime.apply_broker_event(event)?;
                    return Err("broker_event_requires_operator_reconciliation".into());
                }
                runtime.apply_broker_event(event)?;
            }
            if let Some(minute) = closed_bar {
                process_completed_bar(
                    bridge,
                    runtime,
                    strategy,
                    config,
                    contract_id,
                    minute,
                    &mut close_in_flight,
                )?;
            }
        }

        if runtime.status().owned_size == 0 {
            close_in_flight = false;
            unprotected_since = None;
        } else if runtime.status().attached_stop_order_id.is_none()
            || runtime.status().attached_target_order_id.is_none()
        {
            let since = unprotected_since.get_or_insert_with(Instant::now);
            if since.elapsed() >= Duration::from_secs(10) {
                return Err("live_position_auto_oco_children_unconfirmed".into());
            }
        } else {
            unprotected_since = None;
        }

        if runtime.status().pending_entry_remainder_cancel_required && !cancel_requested {
            if let Some(action) = runtime.request_cancel()? {
                let response = bridge.send_action_and_record(runtime, &action)?;
                let state = response.get("state").and_then(Value::as_str).unwrap_or("");
                if !matches!(state, "cancel_requested" | "not_working") {
                    return Err("entry_remainder_cancel_outcome_not_confirmed".into());
                }
            }
            cancel_requested = true;
        } else if !runtime.status().pending_entry_remainder_cancel_required {
            cancel_requested = false;
        }

        if Instant::now() >= next_reconciliation {
            let response = bridge.request("reconcile", json!({}))?;
            if response.get("account_id").and_then(Value::as_i64) != Some(account_id) {
                return Err("topstepx_reconciliation_account_mismatch".into());
            }
            let snapshot = snapshot_from_payload(&response, account_id)?;
            runtime.mark_reconciled(snapshot)?;
            next_reconciliation = Instant::now() + Duration::from_secs(15);
        }
    }
    Ok(())
}

fn process_completed_bar(
    bridge: &mut PythonBridge,
    runtime: &mut Runtime,
    strategy: &mut VpBreakoutAdapter,
    config: &LiveConfigFile,
    contract_id: &str,
    minute: MinuteBar,
    close_in_flight: &mut bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let context = bridge.request("market_context", json!({"timestamp_utc": minute.start}))?;
    let bar = Bar {
        timestamp_us: minute.start.timestamp_micros(),
        source: DataSource::TopstepX,
        rth_date_et: chrono::NaiveDate::parse_from_str(
            context
                .get("rth_date_et")
                .and_then(Value::as_str)
                .ok_or("market_context_rth_date_missing")?,
            "%Y-%m-%d",
        )?,
        topstep_trade_date_ct: chrono::NaiveDate::parse_from_str(
            context
                .get("topstep_trade_date_ct")
                .and_then(Value::as_str)
                .ok_or("market_context_topstep_date_missing")?,
            "%Y-%m-%d",
        )?,
        is_rth: context
            .get("is_rth")
            .and_then(Value::as_bool)
            .ok_or("market_context_rth_flag_missing")?,
        open: minute.open,
        high: minute.high,
        low: minute.low,
        close: minute.close,
        volume: minute.volume,
    };
    runtime.append_record(
        "completed_live_bar",
        &json!({
            "timestamp_utc": bar.utc(),
            "source": "topstepx",
            "rth_date_et": bar.rth_date_et,
            "topstep_trade_date_ct": bar.topstep_trade_date_ct,
            "is_rth": bar.is_rth,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }),
    )?;

    if let Some(intent) = strategy.on_completed_bar(&bar) {
        match runtime.accept_signal(intent, Utc::now()) {
            Ok(action) => {
                let response = bridge.send_action_and_record(runtime, &action)?;
                let state = response.get("state").and_then(Value::as_str).unwrap_or("");
                match state {
                    "accepted" => {}
                    "rejected" => println!(
                        "{}",
                        json!({"event":"entry_rejected","contract_id":contract_id})
                    ),
                    "ambiguous" => {
                        return Err("entry_submit_outcome_ambiguous_reconcile_required".into());
                    }
                    _ => return Err("entry_submit_response_state_invalid".into()),
                }
            }
            Err(reason) => println!(
                "{}",
                json!({"event":"entry_signal_blocked","reason":reason,"bar_utc":bar.utc()})
            ),
        }
    }

    let Some(context) = runtime.core().managed_position_context() else {
        if runtime.exit_state() != &Value::Null {
            runtime.store_exit_state(&ExitKernelState::default())?;
        }
        return Ok(());
    };
    if *close_in_flight {
        return Ok(());
    }
    let Some((market_timestamp, market_price)) = runtime.core().last_market_reference() else {
        return Ok(());
    };
    if market_timestamp > Utc::now() + chrono::Duration::seconds(1)
        || Utc::now()
            .signed_duration_since(market_timestamp)
            .num_seconds()
            > runtime.core().config().max_market_staleness_seconds
    {
        return Ok(());
    }

    let current_stop = context
        .current_stop_price
        .unwrap_or(context.entry_intent.stop_price);
    let previous_state = restored_exit_state(runtime.exit_state(), &context, config);
    let mut params = config.exit_params.clone();
    if !params.is_object() {
        params = json!({});
    }
    let params_object = params.as_object_mut().expect("object initialized above");
    params_object.insert(
        "tp_ticks".to_owned(),
        json!(
            ((context.entry_intent.target_price - context.entry_intent.entry_price).abs()
                / context.entry_intent.tick_size)
                .round() as i64
        ),
    );
    params_object.insert(
        "sl_ticks".to_owned(),
        json!(
            ((context.entry_intent.entry_price - context.entry_intent.stop_price).abs()
                / context.entry_intent.tick_size)
                .round() as i64
        ),
    );
    let target_ticks = params_object
        .get("tp_ticks")
        .cloned()
        .unwrap_or_else(|| json!(0));
    let risk_ticks = params_object
        .get("sl_ticks")
        .cloned()
        .unwrap_or_else(|| json!(0));
    params_object.insert("tr_tp_ticks".to_owned(), target_ticks);
    params_object.insert("tr_sl_ticks".to_owned(), risk_ticks);
    for suffix in ["trail_sl_ticks", "trail_trigger_pct", "trail_enabled"] {
        if let Some(value) = params_object.get(suffix).cloned() {
            params_object.insert(format!("tr_{suffix}"), value);
        }
    }
    let exit_response = bridge.request(
        "exit_decision",
        json!({
            "strategy_mode": config.exit_strategy_mode,
            "params": params,
            "direction": if context.position.direction == Direction::Long { "buy" } else { "sell" },
            "state": {
                "trail_triggered": previous_state.trail_triggered,
                "ladder_max_r": previous_state.ladder_max_r,
                "ladder_lock_r": previous_state.ladder_lock_r,
            },
            "entry_price": context.position.average_price,
            "current_sl": current_stop,
            "original_sl": context.entry_intent.stop_price,
            "tp_price": context.entry_intent.target_price,
            "market_price": market_price,
            "held_minutes": market_timestamp
                .signed_duration_since(context.entry_fill_timestamp_utc)
                .num_seconds()
                .max(0) as f64 / 60.0,
            "tick_size": context.entry_intent.tick_size,
        }),
    )?;
    let proposed_state = serde_json::from_value::<ExitKernelState>(json!({
        "custom_tag": context.custom_tag,
        "trail_triggered": exit_response["state"]["trail_triggered"],
        "ladder_max_r": exit_response["state"]["ladder_max_r"],
        "ladder_lock_r": exit_response["state"]["ladder_lock_r"],
    }))?;
    runtime.append_record(
        "exit_kernel_decision",
        &json!({
            "custom_tag": context.custom_tag,
            "bar_timestamp_utc": bar.utc(),
            "market_timestamp_utc": market_timestamp,
            "market_price": market_price,
            "decision": exit_response,
        }),
    )?;

    let action = exit_response
        .get("action")
        .and_then(Value::as_str)
        .ok_or("exit_kernel_action_missing")?;
    let stop_price = exit_response.get("stop_price").and_then(Value::as_f64);
    match runtime.apply_exit_operation(action, stop_price) {
        Ok(Some(action @ BrokerAction::ModifyAttachedStop { .. })) => {
            let response = bridge.send_action_and_record(runtime, &action)?;
            if response.get("state").and_then(Value::as_str) != Some("accepted") {
                return Err("attached_stop_modify_outcome_not_confirmed".into());
            }
            if let BrokerAction::ModifyAttachedStop {
                order_id,
                stop_price,
                ..
            } = action
            {
                runtime.mark_attached_stop_modified(order_id, stop_price)?;
            }
            runtime.store_exit_state(&proposed_state)?;
        }
        Ok(Some(action @ BrokerAction::CloseOwnedPosition { .. })) => {
            let response = bridge.send_action_and_record(runtime, &action)?;
            if response.get("state").and_then(Value::as_str) != Some("accepted") {
                return Err("owned_position_close_outcome_not_confirmed".into());
            }
            runtime.store_exit_state(&proposed_state)?;
            *close_in_flight = true;
        }
        Ok(None) => runtime.store_exit_state(&proposed_state)?,
        Ok(Some(_)) => return Err("unsupported_live_exit_action".into()),
        Err(reason) if reason == "attached_stop_order_id_missing" => {}
        Err(reason) if reason == "exit_kernel_stop_does_not_tighten_safely" => {
            runtime.apply_broker_event(BrokerEvent::ReconciliationRequired {
                reason: "exit_kernel_stop_failed_live_protection_guard".to_owned(),
            })?;
            return Err(reason.into());
        }
        Err(reason) => return Err(reason.into()),
    }
    Ok(())
}

fn restored_exit_state(
    saved: &Value,
    position: &ancsertpx_live_runtime::core::ManagedPositionContext,
    config: &LiveConfigFile,
) -> ExitKernelState {
    if let Ok(state) = serde_json::from_value::<ExitKernelState>(saved.clone()) {
        if state.custom_tag == position.custom_tag {
            return state;
        }
    }
    let initial_stop = position.entry_intent.stop_price;
    let current_stop = position.current_stop_price.unwrap_or(initial_stop);
    let risk = (position.position.average_price - initial_stop).abs();
    let lock_r = if risk > 0.0 {
        match position.position.direction {
            Direction::Long => (current_stop - position.position.average_price) / risk,
            Direction::Short => (position.position.average_price - current_stop) / risk,
        }
    } else {
        0.0
    };
    let trailed = (current_stop - initial_stop).abs() > position.entry_intent.tick_size / 2.0;
    let ladder_gap = config
        .exit_params
        .get("ladder_gap_r")
        .and_then(Value::as_f64)
        .unwrap_or(2.0);
    ExitKernelState {
        custom_tag: position.custom_tag.clone(),
        trail_triggered: trailed,
        ladder_max_r: if trailed {
            (lock_r + ladder_gap).max(0.0)
        } else {
            0.0
        },
        ladder_lock_r: trailed.then_some(lock_r),
    }
}

fn default_journal_path(root: &Path, account_id: i64, contract_id: &str) -> PathBuf {
    let safe_contract: String = contract_id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '_'
            }
        })
        .collect();
    root.join("data")
        .join("live_runtime")
        .join(format!("topstepx_{account_id}_{safe_contract}.jsonl"))
}
