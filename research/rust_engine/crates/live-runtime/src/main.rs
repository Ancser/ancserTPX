mod live;
mod shutdown;

use ancsertpx_live_runtime::adapters::paper::{PaperAdapter, PaperFillPlan};
use ancsertpx_live_runtime::adapters::robinhood_mcp::capability_report;
use ancsertpx_live_runtime::adapters::topstepx::{
    action_to_gateway_payload, bridge_event_to_broker_event,
};
use ancsertpx_live_runtime::config::{load_runtime_config, validate_runtime_config};
use ancsertpx_live_runtime::core::{
    BrokerAction, BrokerEvent, BrokerSnapshot, ChildRole, Direction, EntryIntent, OrderPhase,
    OrderSnapshot, PositionSnapshot, RuntimeConfig, RuntimeCore, RuntimeMode,
};
use ancsertpx_live_runtime::events::BridgeMessage;
use ancsertpx_live_runtime::journal::Journal;
use ancsertpx_live_runtime::runtime::Runtime;
use ancsertpx_live_runtime::strategy::VpBreakoutAdapter;
use ancsertpx_vp_replay::model::{Bar, DataSource, StrategyParams};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::PathBuf;

#[derive(Deserialize)]
struct VpReplayFixture {
    schema: String,
    runtime: RuntimeConfig,
    vp_parameters: VpFixtureParams,
    warmup_bars: Vec<FixtureBar>,
    stream_bars: Vec<FixtureBar>,
}

#[derive(Deserialize)]
struct VpFixtureParams {
    value_area_pct: f64,
    sl_atr: f64,
    tp_atr: f64,
    confirm_bars: u32,
    breakout_buffer_ticks: u32,
    touch_tolerance_ticks: u32,
    reclaim_buffer_ticks: u32,
    max_trades_per_day: u32,
    min_source_candles: usize,
}

impl VpFixtureParams {
    fn strategy_params(&self) -> StrategyParams {
        StrategyParams {
            entry_mode: "breakout".to_owned(),
            target_mode: "atr".to_owned(),
            side_mode: "all".to_owned(),
            value_area_pct: self.value_area_pct,
            sl_atr: self.sl_atr,
            tp_atr: self.tp_atr,
            confirm_bars: self.confirm_bars,
            breakout_buffer_ticks: self.breakout_buffer_ticks,
            touch_tolerance_ticks: self.touch_tolerance_ticks,
            reclaim_buffer_ticks: self.reclaim_buffer_ticks,
            max_trades_per_day: self.max_trades_per_day,
            min_source_candles: self.min_source_candles,
        }
    }
}

#[derive(Deserialize)]
struct FixtureBar {
    timestamp_utc: String,
    source: String,
    rth_date_et: String,
    topstep_trade_date_ct: String,
    is_rth: bool,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: i64,
}

impl FixtureBar {
    fn into_bar(self) -> Result<Bar, Box<dyn std::error::Error>> {
        let timestamp = DateTime::parse_from_rfc3339(&self.timestamp_utc)?.with_timezone(&Utc);
        Ok(Bar {
            timestamp_us: timestamp.timestamp_micros(),
            source: DataSource::from_label(&self.source)?,
            rth_date_et: NaiveDate::parse_from_str(&self.rth_date_et, "%Y-%m-%d")?,
            topstep_trade_date_ct: NaiveDate::parse_from_str(
                &self.topstep_trade_date_ct,
                "%Y-%m-%d",
            )?,
            is_rth: self.is_rth,
            open: self.open,
            high: self.high,
            low: self.low,
            close: self.close,
            volume: self.volume,
        })
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("runtime error: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = args.first().map(String::as_str).unwrap_or("help");
    match command {
        "help" | "--help" | "-h" => print_help(),
        "status" => show_status(args.get(1).map(PathBuf::from))?,
        "robinhood-capability" => show_robinhood(args.get(1).map(PathBuf::from))?,
        "topstepx-protocol-demo" => show_topstepx_protocol_demo()?,
        "paper-demo" => run_paper_demo()?,
        "replay" => run_replay(args.get(1).map(PathBuf::from))?,
        "pi-replay" => run_pi_replay(args.get(1).map(PathBuf::from))?,
        "pi-backtest" => run_pi_backtest(args.get(1).map(PathBuf::from))?,
        "pi-lifecycle" => run_pi_lifecycle(args.get(1).map(PathBuf::from))?,
        "pi-signal-builders" => run_pi_signal_builders(args.get(1).map(PathBuf::from))?,
        "live" => live::run(&args[1..])?,
        _ => return Err(format!("unknown command: {command}").into()),
    }
    Ok(())
}

fn print_help() {
    println!(
        "ancsertpx-live-runtime\n\
         Commands:\n\
           status [runtime.json]          Show mode and named readiness blockers\n\
           replay [fixture.json]           Replay a VP fixture through the shared core\n\
           pi-replay <fixture.json>        Replay canonical PI marks through the native strategy\n\
           pi-backtest <fixture.json>      Replay frozen PI fills and trade ledger offline\n\
           pi-lifecycle <fixture.json>     Replay PI continuation and replacement ticket rules\n\
           pi-signal-builders <fixture.json> Build offline PI reopen and replacement signals\n\
           paper-demo                     Run deterministic offline lifecycle scenarios\n\
           topstepx-protocol-demo          Print the documented order mapping\n\
           robinhood-capability [tools.json] Show fail-closed MCP capability status\n\
           live <config.json> --arm --approve-vp-breakout-atr --confirm-auto-oco\n\
         Default mode: paper. Live mode requires every explicit flag and a valid config."
    );
}

fn run_pi_replay(fixture_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let fixture_path = fixture_path.ok_or("pi-replay requires a fixture path")?;
    let fixture = serde_json::from_slice::<ancsertpx_live_runtime::pi::PiReplayFixture>(
        &std::fs::read(fixture_path)?,
    )?;
    let report = ancsertpx_live_runtime::pi::replay(fixture).map_err(std::io::Error::other)?;
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}

fn run_pi_backtest(fixture_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let fixture_path = fixture_path.ok_or("pi-backtest requires a fixture path")?;
    let output = ancsertpx_live_runtime::pi_backtest::replay_json(&std::fs::read(fixture_path)?)
        .map_err(std::io::Error::other)?;
    println!("{}", serde_json::to_string(&output)?);
    Ok(())
}

fn run_pi_lifecycle(fixture_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let fixture_path = fixture_path.ok_or("pi-lifecycle requires a fixture path")?;
    let output = ancsertpx_live_runtime::pi_lifecycle::replay_json(&std::fs::read(fixture_path)?)
        .map_err(std::io::Error::other)?;
    println!("{}", serde_json::to_string(&output)?);
    Ok(())
}

fn run_pi_signal_builders(fixture_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let fixture_path = fixture_path.ok_or("pi-signal-builders requires a fixture path")?;
    let output =
        ancsertpx_live_runtime::pi_signal_builders::replay_json(&std::fs::read(fixture_path)?)
            .map_err(std::io::Error::other)?;
    println!("{}", serde_json::to_string(&output)?);
    Ok(())
}

fn show_status(config_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let config = match config_path {
        Some(path) => load_runtime_config(path)?,
        None => RuntimeConfig::default(),
    };
    validate_runtime_config(&config)?;
    let static_readiness = ancsertpx_live_runtime::core::readiness(&config);
    let status = RuntimeCore::new(config.clone());
    let output = json!({
        "runtime": "ancsertpx-live-runtime",
        "default_mode": "paper",
        "mode": config.mode,
        "live_order_submission_available_from_cli": true,
        "live_order_submission_requires_explicit_flags": [
            "--arm", "--approve-vp-breakout-atr", "--confirm-auto-oco"
        ],
        "readiness": static_readiness,
        "runtime_status": status.status(),
    });
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn show_robinhood(manifest_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let manifest = match manifest_path {
        Some(path) => Some(serde_json::from_slice::<Value>(&std::fs::read(path)?)?),
        None => None,
    };
    let report = capability_report(manifest.as_ref(), false);
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

fn show_topstepx_protocol_demo() -> Result<(), Box<dyn std::error::Error>> {
    let sample = BrokerAction::PlaceEntry {
        custom_tag: "atx-0123456789abcdef".to_owned(),
        account_id: 12345,
        contract_id: "CON.F.US.MES.U26".to_owned(),
        internal_side: 1,
        order_type: 1,
        size: 1,
        limit_price: 6000.0,
        stop_loss_ticks: -8,
        take_profit_ticks: 12,
    };
    println!(
        "{}",
        serde_json::to_string_pretty(&action_to_gateway_payload(&sample)?)?
    );
    Ok(())
}

fn run_replay(fixture_path: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let fixture_path = fixture_path.unwrap_or_else(|| {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/vp_replay_demo.json")
    });
    let fixture: VpReplayFixture = serde_json::from_slice(&std::fs::read(fixture_path)?)?;
    if fixture.schema != "ancsertpx.live-runtime-vp-replay.v1" {
        return Err("unsupported VP replay fixture schema".into());
    }
    if fixture.runtime.mode != RuntimeMode::Replay {
        return Err("replay fixture must select mode=replay".into());
    }
    validate_runtime_config(&fixture.runtime)?;
    let warmup: Vec<Bar> = fixture
        .warmup_bars
        .into_iter()
        .map(FixtureBar::into_bar)
        .collect::<Result<_, _>>()?;
    let stream: Vec<Bar> = fixture
        .stream_bars
        .into_iter()
        .map(FixtureBar::into_bar)
        .collect::<Result<_, _>>()?;
    let mut strategy = VpBreakoutAdapter::new(
        fixture.runtime.tick_size,
        fixture.runtime.contract_size,
        fixture.vp_parameters.strategy_params(),
    )?;
    strategy.warmup(&warmup);
    let mut runtime = Runtime::open(fixture.runtime.clone(), temporary_journal("replay"))?;
    for bar in &warmup {
        runtime.append_record("historical_bar", &bar.timestamp_us)?;
    }
    runtime.mark_warmup_complete()?;
    let snapshot = BrokerSnapshot {
        account_id: fixture
            .runtime
            .account_id
            .ok_or("replay account_id required")?,
        observed_at_utc: Utc::now(),
        orders: Vec::new(),
        positions: Vec::new(),
    };
    runtime.mark_reconciled(snapshot)?;
    let mut ledger = Vec::<Value>::new();
    let mut paper = PaperAdapter::new();
    for bar in stream {
        let timestamp = bar.utc();
        runtime.apply_broker_event(BrokerEvent::MarketTimestamp(timestamp))?;
        match strategy.on_completed_bar(&bar) {
            Some(intent) => match runtime.accept_signal(intent.clone(), timestamp) {
                Ok(action) => {
                    let action_json = serde_json::to_value(&action)?;
                    let events = paper.submit_entry(&action, timestamp, None)?;
                    if let Some(BrokerEvent::Order(order)) = events.first() {
                        runtime.submit_accepted(order.order_id)?;
                    }
                    for event in events {
                        runtime.apply_broker_event(event)?;
                    }
                    ledger.push(json!({
                        "timestamp_utc": timestamp,
                        "decision": "entry_intent_accepted_for_paper_adapter",
                        "intent": intent,
                        "action": action_json,
                    }));
                }
                Err(reason) => ledger.push(json!({
                    "timestamp_utc": timestamp,
                    "decision": "entry_blocked",
                    "reason": reason,
                })),
            },
            None => ledger.push(json!({
                "timestamp_utc": timestamp,
                "decision": "no_signal",
            })),
        }
    }
    let ledger_bytes = serde_json::to_vec(&ledger)?;
    let ledger_sha256 = Sha256::digest(&ledger_bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let output = json!({
        "mode": "replay",
        "strategy": "existing Rust VP breakout/retest ATR",
        "warmup_bar_count": warmup.len(),
        "stream_bar_count": strategy.observed_bars().saturating_sub(warmup.len()),
        "ledger_entry_count": ledger.len(),
        "entry_intents": ledger.iter().filter(|item| item["decision"] == "entry_intent_accepted_for_paper_adapter").count(),
        "ledger_sha256": ledger_sha256,
        "journal_path": runtime.journal_path().to_string_lossy(),
        "runtime_status": runtime.status(),
        "ledger": ledger,
    });
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn run_paper_demo() -> Result<(), Box<dyn std::error::Error>> {
    let account_id = 721_009;
    let now = DateTime::parse_from_rfc3339("2026-09-25T18:30:00Z")?.with_timezone(&Utc);
    let config = RuntimeConfig {
        mode: RuntimeMode::Paper,
        account_id: Some(account_id),
        contract_id: "CON.F.US.MES.U26".to_owned(),
        tick_size: 0.25,
        tick_value: 1.25,
        contract_size: 2,
        max_signal_age_seconds: 90,
        ..RuntimeConfig::default()
    };
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let tag_a = intent.deterministic_tag(account_id, &config.contract_id);
    let tag_b = intent.deterministic_tag(account_id, &config.contract_id);
    if tag_a != tag_b {
        return Err("deterministic intent tag changed for identical input".into());
    }

    let journal_path = temporary_journal("paper-demo");
    let mut runtime = Runtime::open(config.clone(), &journal_path)?;
    let blocked_before_warmup = runtime.accept_signal(intent.clone(), now).is_err();
    runtime.mark_warmup_complete()?;
    let empty_snapshot = empty_snapshot(account_id, now);
    runtime.mark_reconciled(empty_snapshot)?;

    let mut stale_intent = demo_intent(now - chrono::Duration::seconds(120), 6000.0);
    stale_intent.size = config.contract_size;
    let stale_signal_blocked = runtime.accept_signal(stale_intent, now).is_err();
    let action = runtime.accept_signal(intent.clone(), now)?;
    let pending_signal_blocked = runtime.accept_signal(intent.clone(), now).is_err();
    let mut paper = PaperAdapter::new();
    let events = paper.submit_entry(
        &action,
        now,
        Some(PaperFillPlan {
            fill_size: 1,
            fill_price: 6000.0,
        }),
    )?;
    let entry_order_id = match events.first() {
        Some(BrokerEvent::Order(order)) => order.order_id,
        _ => return Err("paper adapter did not create a pending entry".into()),
    };
    runtime.submit_accepted(entry_order_id)?;
    for event in events {
        runtime.apply_broker_event(event)?;
    }
    let (stop, target) = paper.add_attached_children(
        account_id,
        &config.contract_id,
        Direction::Long.closing_api_side(),
        &tag_a,
        1,
        5998.0,
        6003.0,
    );
    runtime.apply_broker_event(BrokerEvent::Order(stop.clone()))?;
    runtime.apply_broker_event(BrokerEvent::Order(target.clone()))?;
    let snapshot = paper.snapshot(account_id, now);
    runtime.mark_reconciled(snapshot)?;
    runtime.apply_broker_event(BrokerEvent::MarketTimestamp(now))?;
    runtime.apply_broker_event(BrokerEvent::MarketReference {
        timestamp_utc: now,
        price: 6001.0,
    })?;
    let risk_lock = runtime.set_risk_lock(true, "demo_daily_loss_gate");
    risk_lock?;
    let managed_exit_while_locked = runtime
        .apply_exit_operation("move_sl", Some(6000.25))?
        .is_some();

    let cancel_race = run_cancel_race(config.clone(), now)?;
    let manual_position_blocked = run_manual_position_gate(config.clone(), now)?;
    let adversarial = run_adversarial_paper_scenarios(config.clone(), now)?;
    let journal_single_writer = run_journal_single_writer_demo()?;

    let post_demo_status = json!({
        "owned_size": runtime.status().owned_size,
        "attached_stop_order_id": runtime.status().attached_stop_order_id,
        "attached_target_order_id": runtime.status().attached_target_order_id,
        "entries_enabled": runtime.status().entries_enabled,
        "blockers": runtime.status().blockers,
    });
    drop(runtime);
    let mut restarted = Runtime::open(config.clone(), &journal_path)?;
    let restart_requires_refresh =
        !restarted.status().warmup_complete && !restarted.status().reconciliation_complete;
    restarted.mark_warmup_complete()?;
    restarted.mark_reconciled(paper.snapshot(account_id, now))?;
    let restart_reconciled = restarted.status().reconciliation_complete;

    let report = json!({
        "mode": "paper",
        "real_orders_submitted": 0,
        "deterministic_custom_tag": tag_a,
        "blocked_before_warmup": blocked_before_warmup,
        "stale_signal_blocked": stale_signal_blocked,
        "pending_entry_blocks_duplicate": pending_signal_blocked,
        "partial_fill_size": post_demo_status["owned_size"],
        "attached_stop_order_id": post_demo_status["attached_stop_order_id"],
        "attached_target_order_id": post_demo_status["attached_target_order_id"],
        "risk_lock_blocks_entries": !post_demo_status["entries_enabled"].as_bool().unwrap_or(true),
        "owned_exit_action_allowed_while_locked": managed_exit_while_locked,
        "cancel_fill_race_keeps_position_gated": cancel_race,
        "manual_position_blocks_entries_without_ownership": manual_position_blocked,
        "restart_requires_fresh_warmup_and_reconciliation": restart_requires_refresh,
        "restart_reconciliation_succeeded": restart_reconciled,
        "adversarial_scenarios": adversarial,
        "journal_rejects_concurrent_writer": journal_single_writer,
        "journal_path": journal_path.to_string_lossy(),
        "status_blockers": post_demo_status["blockers"],
    });
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

fn demo_intent(timestamp: DateTime<Utc>, entry: f64) -> EntryIntent {
    EntryIntent {
        strategy_id: "vp_breakout_retest_atr_vah".to_owned(),
        signal_time_utc: timestamp,
        direction: Direction::Long,
        entry_price: entry,
        stop_price: entry - 2.0,
        target_price: entry + 3.0,
        tick_size: 0.25,
        size: 1,
        reason: "offline lifecycle fixture".to_owned(),
    }
}

fn empty_snapshot(account_id: i64, now: DateTime<Utc>) -> BrokerSnapshot {
    BrokerSnapshot {
        account_id,
        observed_at_utc: now,
        orders: Vec::new(),
        positions: Vec::new(),
    }
}

fn run_cancel_race(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = RuntimeCore::new(config.clone());
    core.mark_warmup_complete();
    core.reconcile(empty_snapshot(account_id, now))
        .map_err(std::io::Error::other)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent.clone(), now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("unexpected paper action".into()),
    };
    core.mark_submit_accepted(77_001);
    let cancel = core.request_cancel();
    if !matches!(
        cancel,
        Some(BrokerAction::CancelEntry {
            order_id: 77_001,
            ..
        })
    ) {
        return Err("cancel race did not request the owned pending order".into());
    }
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: 77_001,
        contract_id: config.contract_id.clone(),
        custom_tag: Some(custom_tag.clone()),
        side_api: 0,
        order_type_api: 1,
        phase: OrderPhase::CancelRequested,
        size: config.contract_size,
        filled_size: 0,
        filled_price: None,
        stop_price: None,
        limit_price: Some(6000.0),
        child_role: ChildRole::Other,
    }));
    core.broker_event(BrokerEvent::TradeFill {
        account_id,
        contract_id: config.contract_id.clone(),
        trade_id: 88_001,
        order_id: 77_001,
        side_api: 0,
        custom_tag: Some(custom_tag.clone()),
        fill_size: 1,
        fill_price: 6000.0,
        timestamp_utc: now,
    });
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: 77_001,
        contract_id: config.contract_id,
        custom_tag: Some(custom_tag),
        side_api: 0,
        order_type_api: 1,
        phase: OrderPhase::Cancelled,
        size: config.contract_size,
        filled_size: 1,
        filled_price: Some(6000.0),
        stop_price: None,
        limit_price: Some(6000.0),
        child_role: ChildRole::Other,
    }));
    Ok(core.status().owned_size == 1 && core.accept_signal(intent, now).is_err())
}

fn run_manual_position_gate(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = RuntimeCore::new(config.clone());
    core.mark_warmup_complete();
    core.reconcile(BrokerSnapshot {
        account_id,
        observed_at_utc: now,
        orders: Vec::new(),
        positions: vec![PositionSnapshot {
            account_id,
            position_id: 99_001,
            position_id_confirmed: true,
            contract_id: config.contract_id,
            direction: Direction::Long,
            size: 1,
            average_price: 6000.0,
            entry_order_id: None,
            custom_tag: None,
        }],
    })
    .map_err(std::io::Error::other)?;
    Ok(core.status().external_position_present
        && core.apply_exit_operation("close", None).is_err()
        && core.accept_signal(demo_intent(now, 6000.0), now).is_err())
}

fn run_adversarial_paper_scenarios(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<Value, Box<dyn std::error::Error>> {
    let untagged_stop_not_adopted = run_untagged_stop_gate(config.clone(), now)?;
    let filled_order_late_trade_keeps_close_available =
        run_filled_order_late_trade(config.clone(), now)?;
    let flat_partial_entry_keeps_cancel_gate = run_flat_partial_entry_cancel(config.clone(), now)?;
    let partial_exit_keeps_remaining_position_owned =
        run_partial_exit_ownership(config.clone(), now)?;
    let synthetic_position_id_blocks_close =
        run_synthetic_position_close_gate(config.clone(), now)?;
    let fresh_timestamp_without_price_stays_blocked =
        run_live_market_reference_staleness(config.clone(), now)?;
    let foreign_account_update_is_ignored =
        run_foreign_account_event_gate(config.account_id.ok_or("account_id missing")?)?;
    Ok(json!({
        "untagged_manual_stop_not_adopted_or_modified": untagged_stop_not_adopted,
        "filled_order_followed_by_trade_fill_remains_closeable": filled_order_late_trade_keeps_close_available,
        "flat_partial_entry_retains_remainder_cancel_gate": flat_partial_entry_keeps_cancel_gate,
        "partial_exit_retains_bot_ownership_of_residual": partial_exit_keeps_remaining_position_owned,
        "synthetic_order_id_cannot_be_used_as_position_id": synthetic_position_id_blocks_close,
        "fresh_timestamp_without_fresh_price_does_not_open_gate": fresh_timestamp_without_price_stays_blocked,
        "other_account_update_cannot_lock_selected_account": foreign_account_update_is_ignored,
    }))
}

fn ready_demo_core(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<RuntimeCore, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = RuntimeCore::new(config);
    core.mark_warmup_complete();
    core.reconcile(empty_snapshot(account_id, now))
        .map_err(std::io::Error::other)?;
    Ok(core)
}

fn filled_order(
    account_id: i64,
    order_id: i64,
    contract_id: &str,
    custom_tag: &str,
    size: i32,
    phase: OrderPhase,
    filled_size: i32,
) -> OrderSnapshot {
    OrderSnapshot {
        account_id,
        order_id,
        contract_id: contract_id.to_owned(),
        custom_tag: Some(custom_tag.to_owned()),
        side_api: 0,
        order_type_api: 1,
        phase,
        size,
        filled_size,
        filled_price: (filled_size > 0).then_some(6000.0),
        stop_price: None,
        limit_price: Some(6000.0),
        child_role: ChildRole::Other,
    }
}

fn confirm_demo_position(
    account_id: i64,
    position_id: i64,
    config: &RuntimeConfig,
    order_id: i64,
    custom_tag: &str,
    size: i32,
) -> BrokerEvent {
    BrokerEvent::Position(PositionSnapshot {
        account_id,
        position_id,
        position_id_confirmed: true,
        contract_id: config.contract_id.clone(),
        direction: Direction::Long,
        size,
        average_price: 6000.0,
        entry_order_id: Some(order_id),
        custom_tag: Some(custom_tag.to_owned()),
    })
}

fn demo_trade_fill(
    account_id: i64,
    trade_id: i64,
    order_id: i64,
    contract_id: &str,
    side_api: i32,
    size: i32,
    now: DateTime<Utc>,
) -> BrokerEvent {
    BrokerEvent::TradeFill {
        account_id,
        contract_id: contract_id.to_owned(),
        trade_id,
        order_id,
        side_api,
        custom_tag: None,
        fill_size: size,
        fill_price: 6000.0,
        timestamp_utc: now,
    }
}

fn run_filled_order_late_trade(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = ready_demo_core(config.clone(), now)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent, now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("expected demo entry action".into()),
    };
    let order_id = 771_101;
    core.mark_submit_accepted(order_id);
    core.broker_event(BrokerEvent::Order(filled_order(
        account_id,
        order_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::Filled,
        config.contract_size,
    )));
    core.broker_event(demo_trade_fill(
        account_id,
        881_101,
        order_id,
        &config.contract_id,
        0,
        config.contract_size,
        now,
    ));
    core.broker_event(confirm_demo_position(
        account_id,
        991_101,
        &config,
        order_id,
        &custom_tag,
        config.contract_size,
    ));
    let stop_id = 772_101;
    let target_id = 772_102;
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: stop_id,
        contract_id: config.contract_id.clone(),
        custom_tag: Some(custom_tag.clone()),
        side_api: 1,
        order_type_api: 4,
        phase: OrderPhase::Pending,
        size: config.contract_size,
        filled_size: 0,
        filled_price: None,
        stop_price: Some(5998.0),
        limit_price: None,
        child_role: ChildRole::Other,
    }));
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: target_id,
        contract_id: config.contract_id.clone(),
        custom_tag: Some(custom_tag),
        side_api: 1,
        order_type_api: 1,
        phase: OrderPhase::Pending,
        size: config.contract_size,
        filled_size: 0,
        filled_price: None,
        stop_price: None,
        limit_price: Some(6003.0),
        child_role: ChildRole::Other,
    }));
    Ok(matches!(
        core.apply_exit_operation("close", None),
        Ok(Some(BrokerAction::CloseOwnedPosition { .. }))
    ))
}

fn run_untagged_stop_gate(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = ready_demo_core(config.clone(), now)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent, now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("expected demo entry action".into()),
    };
    let entry_id = 771_201;
    core.mark_submit_accepted(entry_id);
    core.broker_event(BrokerEvent::Order(filled_order(
        account_id,
        entry_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::Filled,
        config.contract_size,
    )));
    core.broker_event(demo_trade_fill(
        account_id,
        881_201,
        entry_id,
        &config.contract_id,
        0,
        config.contract_size,
        now,
    ));
    core.broker_event(confirm_demo_position(
        account_id,
        991_201,
        &config,
        entry_id,
        &custom_tag,
        config.contract_size,
    ));
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: 772_201,
        contract_id: config.contract_id.clone(),
        custom_tag: None,
        side_api: 1,
        order_type_api: 4,
        phase: OrderPhase::Pending,
        size: config.contract_size,
        filled_size: 0,
        filled_price: None,
        stop_price: Some(5998.0),
        limit_price: None,
        child_role: ChildRole::Other,
    }));
    core.broker_event(BrokerEvent::Order(OrderSnapshot {
        account_id,
        order_id: 772_202,
        contract_id: config.contract_id,
        custom_tag: Some(custom_tag),
        side_api: 1,
        order_type_api: 1,
        phase: OrderPhase::Pending,
        size: config.contract_size,
        filled_size: 0,
        filled_price: None,
        stop_price: None,
        limit_price: Some(6003.0),
        child_role: ChildRole::Other,
    }));
    Ok(core.status().attached_stop_order_id.is_none()
        && core.apply_exit_operation("move_sl", Some(6000.25)).is_err())
}

fn run_flat_partial_entry_cancel(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    if config.contract_size < 2 {
        return Ok(false);
    }
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = ready_demo_core(config.clone(), now)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent, now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("expected demo entry action".into()),
    };
    let entry_id = 771_301;
    core.mark_submit_accepted(entry_id);
    core.broker_event(BrokerEvent::Order(filled_order(
        account_id,
        entry_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::PartiallyFilled,
        1,
    )));
    core.broker_event(demo_trade_fill(
        account_id,
        881_301,
        entry_id,
        &config.contract_id,
        0,
        1,
        now,
    ));
    core.broker_event(BrokerEvent::Position(PositionSnapshot {
        account_id,
        position_id: 991_301,
        position_id_confirmed: true,
        contract_id: config.contract_id.clone(),
        direction: Direction::Long,
        size: 0,
        average_price: 6000.0,
        entry_order_id: Some(entry_id),
        custom_tag: Some(custom_tag.clone()),
    }));
    let cancel_requested = matches!(
        core.request_cancel(),
        Some(BrokerAction::CancelEntry { order_id, .. }) if order_id == entry_id
    );
    let mut cancelled = filled_order(
        account_id,
        entry_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::Cancelled,
        1,
    );
    cancelled.side_api = 0;
    core.broker_event(BrokerEvent::Order(cancelled));
    core.reconcile(empty_snapshot(account_id, now))
        .map_err(std::io::Error::other)?;
    Ok(cancel_requested
        && !core.status().pending_entry_remainder_cancel_required
        && core.status().pending_entry_tag.is_none()
        && core.status().owned_size == 0)
}

fn run_partial_exit_ownership(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    if config.contract_size < 2 {
        return Ok(false);
    }
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = ready_demo_core(config.clone(), now)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent, now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("expected demo entry action".into()),
    };
    let entry_id = 771_401;
    core.mark_submit_accepted(entry_id);
    core.broker_event(BrokerEvent::Order(filled_order(
        account_id,
        entry_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::Filled,
        config.contract_size,
    )));
    core.broker_event(demo_trade_fill(
        account_id,
        881_401,
        entry_id,
        &config.contract_id,
        0,
        config.contract_size,
        now,
    ));
    core.broker_event(confirm_demo_position(
        account_id,
        991_401,
        &config,
        entry_id,
        &custom_tag,
        config.contract_size,
    ));
    let stop_id = 772_401;
    for order in [
        OrderSnapshot {
            account_id,
            order_id: stop_id,
            contract_id: config.contract_id.clone(),
            custom_tag: Some(custom_tag.clone()),
            side_api: 1,
            order_type_api: 4,
            phase: OrderPhase::Pending,
            size: config.contract_size,
            filled_size: 0,
            filled_price: None,
            stop_price: Some(5998.0),
            limit_price: None,
            child_role: ChildRole::Other,
        },
        OrderSnapshot {
            account_id,
            order_id: 772_402,
            contract_id: config.contract_id.clone(),
            custom_tag: Some(custom_tag),
            side_api: 1,
            order_type_api: 1,
            phase: OrderPhase::Pending,
            size: config.contract_size,
            filled_size: 0,
            filled_price: None,
            stop_price: None,
            limit_price: Some(6003.0),
            child_role: ChildRole::Other,
        },
    ] {
        core.broker_event(BrokerEvent::Order(order));
    }
    core.broker_event(demo_trade_fill(
        account_id,
        881_402,
        stop_id,
        &config.contract_id,
        1,
        1,
        now,
    ));
    Ok(core.status().owned_size == config.contract_size - 1
        && core.status().owned_position_id == Some(991_401)
        && matches!(
            core.apply_exit_operation("close", None),
            Ok(Some(BrokerAction::CloseOwnedPosition { size, .. }))
                if size == config.contract_size - 1
        ))
}

fn run_synthetic_position_close_gate(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut core = ready_demo_core(config.clone(), now)?;
    let mut intent = demo_intent(now, 6000.0);
    intent.size = config.contract_size;
    let action = core.accept_signal(intent, now)?;
    let custom_tag = match action {
        BrokerAction::PlaceEntry { custom_tag, .. } => custom_tag,
        _ => return Err("expected demo entry action".into()),
    };
    let entry_id = 771_501;
    core.mark_submit_accepted(entry_id);
    core.broker_event(BrokerEvent::Order(filled_order(
        account_id,
        entry_id,
        &config.contract_id,
        &custom_tag,
        config.contract_size,
        OrderPhase::Filled,
        config.contract_size,
    )));
    core.broker_event(demo_trade_fill(
        account_id,
        881_501,
        entry_id,
        &config.contract_id,
        0,
        config.contract_size,
        now,
    ));
    Ok(!core.status().position_id_confirmed
        && core
            .apply_exit_operation("close", None)
            .is_err_and(|reason| reason == "broker_position_id_not_confirmed"))
}

fn run_live_market_reference_staleness(
    config: RuntimeConfig,
    now: DateTime<Utc>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let account_id = config.account_id.ok_or("account_id missing")?;
    let mut live = config.clone();
    live.mode = RuntimeMode::Live;
    live.live_armed = true;
    live.strategy_live_approved = true;
    live.auto_oco_confirmed = true;
    let mut without_price = RuntimeCore::new(live.clone());
    without_price.set_live_capabilities(true, true, true, true, true, true);
    without_price.mark_warmup_complete();
    without_price
        .reconcile(empty_snapshot(account_id, now))
        .map_err(std::io::Error::other)?;
    without_price.broker_event(BrokerEvent::MarketTimestamp(now));
    let mut intent = demo_intent(now, 6000.0);
    intent.size = live.contract_size;
    let no_price_blocked = without_price.accept_signal(intent.clone(), now).is_err();

    let mut stale_price = RuntimeCore::new(live.clone());
    stale_price.set_live_capabilities(true, true, true, true, true, true);
    stale_price.mark_warmup_complete();
    stale_price
        .reconcile(empty_snapshot(account_id, now))
        .map_err(std::io::Error::other)?;
    stale_price.broker_event(BrokerEvent::MarketTimestamp(now));
    stale_price.broker_event(BrokerEvent::MarketReference {
        timestamp_utc: now - chrono::Duration::minutes(2),
        price: 6000.0,
    });
    let stale_price_blocked = stale_price.accept_signal(intent, now).is_err();
    Ok(no_price_blocked && stale_price_blocked)
}

fn run_foreign_account_event_gate(account_id: i64) -> Result<bool, Box<dyn std::error::Error>> {
    let message = BridgeMessage::Event {
        protocol_version: 1,
        event_id: "user_account:other".to_owned(),
        event_type: "user_account".to_owned(),
        observed_at_utc: "2026-09-25T18:30:00Z".to_owned(),
        payload: json!({"id": account_id + 1, "canTrade": false, "isVisible": false}),
    };
    Ok(bridge_event_to_broker_event(&message, account_id)?.is_none())
}

fn run_journal_single_writer_demo() -> Result<bool, Box<dyn std::error::Error>> {
    let path = temporary_journal("single-writer");
    let first = Journal::open(&path)?;
    let second_rejected = Journal::open(&path).is_err();
    drop(first);
    let reopened = Journal::open(&path).is_ok();
    let _ = std::fs::remove_file(&path);
    Ok(second_rejected && reopened)
}

fn temporary_journal(label: &str) -> PathBuf {
    let nonce = Utc::now().timestamp_nanos_opt().unwrap_or_default();
    std::env::temp_dir().join(format!(
        "ancsertpx-{label}-{}-{nonce}.jsonl",
        std::process::id()
    ))
}
