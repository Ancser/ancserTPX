use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeMode {
    Replay,
    #[default]
    Paper,
    Live,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct RuntimeConfig {
    pub mode: RuntimeMode,
    pub account_id: Option<i64>,
    pub contract_id: String,
    pub tick_size: f64,
    pub tick_value: f64,
    pub contract_size: i32,
    pub max_signal_age_seconds: i64,
    pub max_market_staleness_seconds: i64,
    pub max_entry_reference_distance_points: f64,
    pub live_armed: bool,
    pub strategy_live_approved: bool,
    pub auto_oco_confirmed: bool,
    #[serde(skip)]
    pub broker_account_verified: bool,
    #[serde(skip)]
    pub contract_active_verified: bool,
    #[serde(skip)]
    pub account_lease_held: bool,
    #[serde(skip)]
    pub market_data_connected: bool,
    #[serde(skip)]
    pub user_hub_connected: bool,
    #[serde(skip)]
    pub journal_ready: bool,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            mode: RuntimeMode::Paper,
            account_id: None,
            contract_id: String::new(),
            tick_size: 0.25,
            tick_value: 0.50,
            contract_size: 1,
            max_signal_age_seconds: 90,
            max_market_staleness_seconds: 5,
            max_entry_reference_distance_points: 50.0,
            live_armed: false,
            strategy_live_approved: false,
            auto_oco_confirmed: false,
            broker_account_verified: false,
            contract_active_verified: false,
            account_lease_held: false,
            market_data_connected: false,
            user_hub_connected: false,
            journal_ready: false,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EntryIntent {
    pub strategy_id: String,
    pub signal_time_utc: DateTime<Utc>,
    pub direction: Direction,
    pub entry_price: f64,
    pub stop_price: f64,
    pub target_price: f64,
    pub tick_size: f64,
    pub size: i32,
    pub reason: String,
}

impl EntryIntent {
    pub fn deterministic_tag(&self, account_id: i64, contract_id: &str) -> String {
        let material = format!(
            "{account_id}|{contract_id}|{}|{}|{}|{}|{:.8}|{:.8}|{:.8}",
            self.strategy_id,
            self.signal_time_utc.timestamp_micros(),
            self.direction.as_str(),
            self.size,
            self.entry_price,
            self.stop_price,
            self.target_price,
        );
        let digest = Sha256::digest(material.as_bytes());
        let hex = digest
            .iter()
            .take(16)
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        format!("atx-{hex}")
    }

    pub fn bracket_offsets_ticks(&self) -> Result<(i32, i32), String> {
        if !self.tick_size.is_finite() || self.tick_size <= 0.0 {
            return Err("invalid_tick_size".to_owned());
        }
        let raw_stop = (self.stop_price - self.entry_price) / self.tick_size;
        let raw_target = (self.target_price - self.entry_price) / self.tick_size;
        if !raw_stop.is_finite() || !raw_target.is_finite() {
            return Err("non_finite_bracket_geometry".to_owned());
        }
        let stop = raw_stop.round();
        let target = raw_target.round();
        if (raw_stop - stop).abs() > 1e-6 || (raw_target - target).abs() > 1e-6 {
            return Err("bracket_price_not_aligned_to_tick".to_owned());
        }
        if stop.abs() > i32::MAX as f64 || target.abs() > i32::MAX as f64 {
            return Err("bracket_distance_out_of_range".to_owned());
        }
        let stop = stop as i32;
        let target = target as i32;
        let valid = match self.direction {
            Direction::Long => stop < 0 && target > 0,
            Direction::Short => stop > 0 && target < 0,
        };
        if !valid {
            return Err("bracket_direction_does_not_protect_entry".to_owned());
        }
        Ok((stop, target))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Long,
    Short,
}

impl Direction {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Long => "long",
            Self::Short => "short",
        }
    }

    pub fn internal_order_side(self) -> i32 {
        match self {
            Self::Long => 1,
            Self::Short => 2,
        }
    }

    pub fn closing_api_side(self) -> i32 {
        match self {
            Self::Long => 1,
            Self::Short => 0,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderPhase {
    Pending,
    PartiallyFilled,
    Filled,
    CancelRequested,
    Cancelled,
    Rejected,
    Unknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChildRole {
    StopLoss,
    TakeProfit,
    Other,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OrderSnapshot {
    pub account_id: i64,
    pub order_id: i64,
    pub contract_id: String,
    pub custom_tag: Option<String>,
    pub side_api: i32,
    pub order_type_api: i32,
    pub phase: OrderPhase,
    pub size: i32,
    pub filled_size: i32,
    pub filled_price: Option<f64>,
    pub stop_price: Option<f64>,
    pub limit_price: Option<f64>,
    pub child_role: ChildRole,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PositionSnapshot {
    pub account_id: i64,
    pub position_id: i64,
    #[serde(default)]
    pub position_id_confirmed: bool,
    pub contract_id: String,
    pub direction: Direction,
    pub size: i32,
    pub average_price: f64,
    pub entry_order_id: Option<i64>,
    pub custom_tag: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BrokerSnapshot {
    pub account_id: i64,
    pub observed_at_utc: DateTime<Utc>,
    pub orders: Vec<OrderSnapshot>,
    pub positions: Vec<PositionSnapshot>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum BrokerEvent {
    Order(OrderSnapshot),
    Position(PositionSnapshot),
    TradeFill {
        account_id: i64,
        contract_id: String,
        trade_id: i64,
        order_id: i64,
        #[serde(default)]
        side_api: i32,
        custom_tag: Option<String>,
        fill_size: i32,
        fill_price: f64,
        timestamp_utc: DateTime<Utc>,
    },
    MarketTimestamp(DateTime<Utc>),
    MarketReference {
        timestamp_utc: DateTime<Utc>,
        price: f64,
    },
    RiskLock {
        locked: bool,
        reason: String,
    },
    ReconciliationRequired {
        reason: String,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum BrokerAction {
    PlaceEntry {
        custom_tag: String,
        account_id: i64,
        contract_id: String,
        internal_side: i32,
        order_type: i32,
        size: i32,
        limit_price: f64,
        stop_loss_ticks: i32,
        take_profit_ticks: i32,
    },
    CancelEntry {
        order_id: i64,
        account_id: i64,
        contract_id: String,
        custom_tag: String,
    },
    ModifyAttachedStop {
        order_id: i64,
        entry_order_id: i64,
        custom_tag: String,
        contract_id: String,
        side_api: i32,
        size: i32,
        expected_stop_price: f64,
        stop_price: f64,
    },
    CloseOwnedPosition {
        contract_id: String,
        position_id: i64,
        attached_stop_order_id: Option<i64>,
        attached_target_order_id: Option<i64>,
        entry_order_id: i64,
        custom_tag: String,
        position_type: i32,
        size: i32,
        average_price: f64,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Readiness {
    pub ready: bool,
    pub mode: RuntimeMode,
    pub blockers: Vec<String>,
    pub default_mode: RuntimeMode,
}

pub fn readiness(config: &RuntimeConfig) -> Readiness {
    let mut blockers = Vec::new();
    if config.mode == RuntimeMode::Live {
        if !config.live_armed {
            blockers.push("explicit_live_arm_missing".to_owned());
        }
        if !config.strategy_live_approved {
            blockers.push("strategy_has_no_live_approval".to_owned());
        }
        if !config.auto_oco_confirmed {
            blockers.push("topstepx_auto_oco_mode_unconfirmed".to_owned());
        }
        if config.account_id.is_none() {
            blockers.push("account_id_missing".to_owned());
        }
        if config.contract_id.trim().is_empty() {
            blockers.push("contract_id_missing".to_owned());
        }
        if !config.broker_account_verified {
            blockers.push("broker_account_not_verified_can_trade_and_visible".to_owned());
        }
        if !config.contract_active_verified {
            blockers.push("active_contract_not_verified".to_owned());
        }
        if !config.account_lease_held {
            blockers.push("single_account_lease_not_held".to_owned());
        }
        if !config.market_data_connected {
            blockers.push("topstepx_market_stream_not_connected".to_owned());
        }
        if !config.user_hub_connected {
            blockers.push("topstepx_user_hub_not_connected".to_owned());
        }
        if !config.journal_ready {
            blockers.push("durable_order_journal_not_ready".to_owned());
        }
    }
    Readiness {
        ready: blockers.is_empty(),
        mode: config.mode,
        blockers,
        default_mode: RuntimeMode::Paper,
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct RuntimeStatus {
    pub warmup_complete: bool,
    pub reconciliation_complete: bool,
    pub pending_entry_tag: Option<String>,
    pub pending_entry_order_id: Option<i64>,
    pub owned_position_id: Option<i64>,
    pub position_id_confirmed: bool,
    pub owned_size: i32,
    pub attached_stop_order_id: Option<i64>,
    pub attached_target_order_id: Option<i64>,
    pub external_position_present: bool,
    pub unresolved_submit_tag: Option<String>,
    pub risk_locked: bool,
    pub entries_enabled: bool,
    pub last_market_timestamp_utc: Option<DateTime<Utc>>,
    pub last_market_reference_timestamp_utc: Option<DateTime<Utc>>,
    pub last_market_reference_price: Option<f64>,
    pub pending_entry_remainder_cancel_required: bool,
    pub blockers: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct ManagedPositionContext {
    pub position: PositionSnapshot,
    pub entry_intent: EntryIntent,
    pub entry_order_id: i64,
    pub custom_tag: String,
    pub current_stop_price: Option<f64>,
    pub entry_fill_timestamp_utc: DateTime<Utc>,
}

pub struct RuntimeCore {
    config: RuntimeConfig,
    status: RuntimeStatus,
    pending_intent: Option<EntryIntent>,
    pending_order_phase: Option<OrderPhase>,
    entry_order_id: Option<i64>,
    owned_tag: Option<String>,
    stop_order_id: Option<i64>,
    stop_order_price: Option<f64>,
    target_order_id: Option<i64>,
    active_position: Option<PositionSnapshot>,
    last_market_timestamp: Option<DateTime<Utc>>,
    last_market_reference_timestamp: Option<DateTime<Utc>>,
    hard_blockers: Vec<String>,
    seen_trade_ids: HashSet<i64>,
    attempted_entry_tags: HashSet<String>,
    last_market_price: Option<f64>,
    order_cumulative_fill_size: i32,
    trade_fill_size: i32,
    trade_fill_notional: f64,
    trade_exit_fill_size: i32,
    entry_order_size: i32,
    entry_fill_timestamp_utc: Option<DateTime<Utc>>,
    verified_exit_order_ids: HashSet<i64>,
    flat_confirmed_entry_fill_size: Option<i32>,
}

impl RuntimeCore {
    pub fn new(config: RuntimeConfig) -> Self {
        let mut core = Self {
            config,
            status: RuntimeStatus::default(),
            pending_intent: None,
            pending_order_phase: None,
            entry_order_id: None,
            owned_tag: None,
            stop_order_id: None,
            stop_order_price: None,
            target_order_id: None,
            active_position: None,
            last_market_timestamp: None,
            last_market_reference_timestamp: None,
            hard_blockers: Vec::new(),
            seen_trade_ids: HashSet::new(),
            attempted_entry_tags: HashSet::new(),
            last_market_price: None,
            order_cumulative_fill_size: 0,
            trade_fill_size: 0,
            trade_fill_notional: 0.0,
            trade_exit_fill_size: 0,
            entry_order_size: 0,
            entry_fill_timestamp_utc: None,
            verified_exit_order_ids: HashSet::new(),
            flat_confirmed_entry_fill_size: None,
        };
        core.refresh_entry_gate();
        core
    }

    pub fn config(&self) -> &RuntimeConfig {
        &self.config
    }

    pub fn status(&self) -> &RuntimeStatus {
        &self.status
    }

    pub fn managed_position_context(&self) -> Option<ManagedPositionContext> {
        Some(ManagedPositionContext {
            position: self.active_position.as_ref()?.clone(),
            entry_intent: self.pending_intent.as_ref()?.clone(),
            entry_order_id: self.entry_order_id?,
            custom_tag: self.owned_tag.clone()?,
            current_stop_price: self.stop_order_price,
            entry_fill_timestamp_utc: self.entry_fill_timestamp_utc.or_else(|| {
                self.pending_intent
                    .as_ref()
                    .map(|intent| intent.signal_time_utc)
            })?,
        })
    }

    pub fn last_market_reference(&self) -> Option<(DateTime<Utc>, f64)> {
        Some((
            self.last_market_reference_timestamp?,
            self.last_market_price?,
        ))
    }

    pub fn mark_warmup_complete(&mut self) {
        self.status.warmup_complete = true;
        self.refresh_entry_gate();
    }

    pub fn recover_entry_intent(&mut self, intent: EntryIntent) -> Result<String, String> {
        let account_id = self.config.account_id.ok_or("account_id_missing")?;
        let tag = intent.deterministic_tag(account_id, &self.config.contract_id);
        self.entry_order_size = intent.size;
        self.attempted_entry_tags.insert(tag.clone());
        self.pending_intent = Some(intent);
        self.owned_tag = Some(tag.clone());
        self.pending_order_phase = Some(OrderPhase::Unknown);
        self.status.pending_entry_tag = Some(tag.clone());
        self.status.unresolved_submit_tag = Some(tag.clone());
        self.set_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
        self.refresh_entry_gate();
        Ok(tag)
    }

    pub fn mark_reconciliation_complete(&mut self) {
        self.status.reconciliation_complete = true;
        self.refresh_entry_gate();
    }

    pub fn set_live_capabilities(
        &mut self,
        account_verified: bool,
        contract_active: bool,
        account_lease_held: bool,
        market_data_connected: bool,
        user_hub_connected: bool,
        journal_ready: bool,
    ) {
        self.config.broker_account_verified = account_verified;
        self.config.contract_active_verified = contract_active;
        self.config.account_lease_held = account_lease_held;
        self.config.market_data_connected = market_data_connected;
        self.config.user_hub_connected = user_hub_connected;
        self.config.journal_ready = journal_ready;
        self.refresh_entry_gate();
    }

    pub fn require_startup_refresh(&mut self, journal_ready: bool) {
        self.status.warmup_complete = false;
        self.status.reconciliation_complete = false;
        self.last_market_timestamp = None;
        self.last_market_reference_timestamp = None;
        self.last_market_price = None;
        self.status.last_market_timestamp_utc = None;
        self.status.last_market_reference_timestamp_utc = None;
        self.status.last_market_reference_price = None;
        self.config.broker_account_verified = false;
        self.config.contract_active_verified = false;
        self.config.account_lease_held = false;
        self.config.market_data_connected = false;
        self.config.user_hub_connected = false;
        self.config.journal_ready = journal_ready;
        self.refresh_entry_gate();
    }

    pub fn observe_historical_bar(&mut self, timestamp: DateTime<Utc>) {
        if self
            .last_market_timestamp
            .map(|last| timestamp > last)
            .unwrap_or(true)
        {
            self.last_market_timestamp = Some(timestamp);
            self.status.last_market_timestamp_utc = Some(timestamp);
        }
    }

    pub fn observe_live_timestamp(&mut self, timestamp: DateTime<Utc>) -> Result<(), String> {
        if self
            .last_market_timestamp
            .map(|last| timestamp <= last)
            .unwrap_or(false)
        {
            self.set_hard_blocker("out_of_order_market_event_operator_resync_required");
            self.refresh_entry_gate();
            return Err("out_of_order_market_event_operator_resync_required".to_owned());
        }
        self.last_market_timestamp = Some(timestamp);
        self.status.last_market_timestamp_utc = Some(timestamp);
        self.refresh_entry_gate();
        Ok(())
    }

    pub fn set_risk_lock(&mut self, locked: bool) {
        self.status.risk_locked = locked;
        self.refresh_entry_gate();
    }

    pub fn broker_event(&mut self, event: BrokerEvent) -> Vec<BrokerAction> {
        match event {
            BrokerEvent::Order(order) if self.account_matches(order.account_id) => {
                self.apply_order(order);
            }
            BrokerEvent::Position(position) if self.account_matches(position.account_id) => {
                self.apply_position(position);
            }
            BrokerEvent::TradeFill {
                account_id,
                contract_id,
                trade_id,
                order_id,
                side_api,
                custom_tag: _custom_tag,
                fill_size,
                fill_price,
                timestamp_utc,
            } if self.account_matches(account_id) => {
                if contract_id == self.config.contract_id
                    && self.seen_trade_ids.insert(trade_id)
                    && fill_size > 0
                    && fill_price.is_finite()
                {
                    if Some(order_id) == self.entry_order_id {
                        self.apply_entry_fill(order_id, fill_size, fill_price, timestamp_utc);
                    } else if Some(order_id) == self.stop_order_id
                        || Some(order_id) == self.target_order_id
                        || self.verified_exit_order_ids.contains(&order_id)
                    {
                        self.apply_exit_fill(order_id, side_api, fill_size);
                    }
                }
            }
            BrokerEvent::MarketTimestamp(timestamp) => {
                let _ = self.observe_live_timestamp(timestamp);
            }
            BrokerEvent::MarketReference {
                timestamp_utc,
                price,
            } => {
                if !price.is_finite() || price <= 0.0 {
                    self.set_hard_blocker("invalid_market_reference_price");
                } else if self
                    .last_market_reference_timestamp
                    .is_none_or(|last| timestamp_utc >= last)
                {
                    self.last_market_reference_timestamp = Some(timestamp_utc);
                    self.last_market_price = Some(price);
                    self.status.last_market_reference_timestamp_utc = Some(timestamp_utc);
                    self.status.last_market_reference_price = Some(price);
                    self.clear_hard_blocker("invalid_market_reference_price");
                }
            }
            BrokerEvent::RiskLock { locked, .. } => self.set_risk_lock(locked),
            BrokerEvent::ReconciliationRequired { reason } => {
                self.set_hard_blocker(&format!("reconciliation_required:{reason}"));
            }
            BrokerEvent::Order(_) | BrokerEvent::Position(_) | BrokerEvent::TradeFill { .. } => {
                self.set_hard_blocker("broker_event_account_mismatch");
            }
        }
        self.refresh_entry_gate();
        Vec::new()
    }

    pub fn reconcile(&mut self, snapshot: BrokerSnapshot) -> Result<(), String> {
        let Some(expected_account) = self.config.account_id else {
            self.set_hard_blocker("account_id_missing");
            self.refresh_entry_gate();
            return Err("account_id_missing".to_owned());
        };
        if snapshot.account_id != expected_account
            || snapshot
                .orders
                .iter()
                .any(|order| order.account_id != expected_account)
            || snapshot
                .positions
                .iter()
                .any(|position| position.account_id != expected_account)
        {
            self.set_hard_blocker("reconciliation_account_mismatch");
            self.status.reconciliation_complete = false;
            self.refresh_entry_gate();
            return Err("reconciliation_account_mismatch".to_owned());
        }

        self.status.reconciliation_complete = false;
        let previously_verified_stop_id = self.stop_order_id;
        let previously_verified_target_id = self.target_order_id;
        self.stop_order_id = None;
        self.stop_order_price = None;
        self.target_order_id = None;
        self.status.external_position_present = false;
        self.status.attached_stop_order_id = None;
        self.status.attached_target_order_id = None;

        if let Some(tag) = self.status.pending_entry_tag.clone() {
            if let Some(order) = snapshot
                .orders
                .iter()
                .find(|order| self.entry_order_candidate(order, Some(tag.as_str())))
            {
                self.apply_order(order.clone());
                self.status.unresolved_submit_tag = None;
                self.clear_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
                self.clear_hard_blocker("submit_outcome_unresolved_operator_review_required");
            } else if self.status.unresolved_submit_tag.as_deref() == Some(tag.as_str()) {
                self.set_hard_blocker("submit_outcome_unresolved_operator_review_required");
            } else if self.entry_order_id.is_some() {
                self.set_hard_blocker(
                    "entry_order_missing_from_reconciliation_operator_review_required",
                );
            } else {
                self.pending_intent = None;
                self.pending_order_phase = None;
                self.status.pending_entry_tag = None;
                self.status.pending_entry_order_id = None;
            }
        }

        let open_contract_positions: Vec<_> = snapshot
            .positions
            .iter()
            .filter(|position| {
                position.contract_id == self.config.contract_id && position.size != 0
            })
            .collect();
        let tagged_parent_confirmed = self.entry_order_id.is_some()
            && self.owned_tag.is_some()
            && snapshot.orders.iter().any(|order| {
                Some(order.order_id) == self.entry_order_id
                    && tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
                    && order.contract_id == self.config.contract_id
                    && order.filled_size > 0
            });
        let inferred_position_id = if tagged_parent_confirmed && open_contract_positions.len() == 1
        {
            let candidate = open_contract_positions[0];
            self.position_matches_owned_fill(candidate)
                .then_some(candidate.position_id)
        } else {
            None
        };
        let owned_positions: Vec<_> = snapshot
            .positions
            .iter()
            .filter(|position| {
                position.contract_id == self.config.contract_id
                    && position.size != 0
                    && (tags_match(position.custom_tag.as_deref(), self.owned_tag.as_deref())
                        || (position.entry_order_id.is_some()
                            && position.entry_order_id == self.entry_order_id)
                        || inferred_position_id == Some(position.position_id))
            })
            .collect();
        if owned_positions.len() > 1 {
            self.set_hard_blocker("multiple_bot_owned_positions_operator_review_required");
        } else if let Some(position) = owned_positions.first() {
            self.clear_hard_blocker("multiple_bot_owned_positions_operator_review_required");
            self.apply_position((*position).clone());
            self.status.unresolved_submit_tag = None;
            self.clear_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
            self.clear_hard_blocker("submit_outcome_unresolved_operator_review_required");
            let closing_side = position.direction.closing_api_side();
            let stop_candidates: Vec<_> = snapshot
                .orders
                .iter()
                .filter(|order| {
                    order.contract_id == position.contract_id
                        && order.side_api == closing_side
                        && order.size >= position.size.abs()
                        && order.order_type_api == 4
                        && (tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
                            || Some(order.order_id) == previously_verified_stop_id)
                        && order.phase != OrderPhase::Cancelled
                        && order.phase != OrderPhase::Rejected
                        && order.phase != OrderPhase::Filled
                        && (Some(order.order_id) == previously_verified_stop_id
                            || tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref()))
                })
                .collect();
            let target_candidates: Vec<_> = snapshot
                .orders
                .iter()
                .filter(|order| {
                    order.contract_id == position.contract_id
                        && order.side_api == closing_side
                        && order.size >= position.size.abs()
                        && order.order_type_api == 1
                        && (tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
                            || Some(order.order_id) == previously_verified_target_id)
                        && order.phase != OrderPhase::Cancelled
                        && order.phase != OrderPhase::Rejected
                        && order.phase != OrderPhase::Filled
                        && (Some(order.order_id) == previously_verified_target_id
                            || tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref()))
                })
                .collect();
            if stop_candidates.len() == 1 {
                self.stop_order_id = Some(stop_candidates[0].order_id);
                self.stop_order_price = stop_candidates[0].stop_price;
                self.verified_exit_order_ids
                    .insert(stop_candidates[0].order_id);
            }
            if target_candidates.len() == 1 {
                self.target_order_id = Some(target_candidates[0].order_id);
                self.verified_exit_order_ids
                    .insert(target_candidates[0].order_id);
            }
            self.status.attached_stop_order_id = self.stop_order_id;
            self.status.attached_target_order_id = self.target_order_id;
            if self.stop_order_id.is_none() || self.target_order_id.is_none() {
                self.set_hard_blocker("attached_auto_oco_children_not_confirmed");
            } else {
                self.clear_hard_blocker("attached_auto_oco_children_not_confirmed");
            }
            let has_unowned_position = snapshot.positions.iter().any(|candidate| {
                candidate.contract_id == self.config.contract_id
                    && candidate.size != 0
                    && candidate.position_id != position.position_id
            });
            if has_unowned_position {
                self.status.external_position_present = true;
                self.set_hard_blocker("external_or_untracked_position_open");
            } else {
                self.clear_hard_blocker("external_or_untracked_position_open");
            }
        } else {
            self.active_position = None;
            self.status.owned_position_id = None;
            self.status.position_id_confirmed = false;
            self.status.owned_size = 0;
            self.clear_hard_blocker("broker_position_id_unconfirmed_operator_review_required");
            self.clear_hard_blocker("multiple_bot_owned_positions_operator_review_required");
            self.clear_hard_blocker("attached_auto_oco_children_not_confirmed");
            if snapshot.positions.iter().any(|position| {
                position.contract_id == self.config.contract_id && position.size != 0
            }) {
                self.status.external_position_present = true;
                self.set_hard_blocker("external_or_untracked_position_open");
            } else {
                self.clear_hard_blocker("external_or_untracked_position_open");
                self.clear_hard_blocker("multiple_bot_owned_positions_operator_review_required");
            }
        }

        let has_open_contract_position = snapshot
            .positions
            .iter()
            .any(|position| position.contract_id == self.config.contract_id && position.size != 0);
        if !has_open_contract_position && self.active_position.is_none() {
            self.resolve_flat_entry_from_snapshot(&snapshot.orders);
        }

        let has_unowned_working_order = snapshot.orders.iter().any(|order| {
            order.contract_id == self.config.contract_id
                && matches!(
                    order.phase,
                    OrderPhase::Pending | OrderPhase::PartiallyFilled | OrderPhase::CancelRequested
                )
                && Some(order.order_id) != self.entry_order_id
                && Some(order.order_id) != self.stop_order_id
                && Some(order.order_id) != self.target_order_id
        });
        if has_unowned_working_order {
            self.set_hard_blocker("external_or_untracked_order_open");
        } else {
            self.clear_hard_blocker("external_or_untracked_order_open");
        }

        self.clear_hard_blocker("reconciliation_account_mismatch");
        self.clear_hard_blocker("broker_event_account_mismatch");
        self.hard_blockers
            .retain(|blocker| !blocker.starts_with("reconciliation_required:"));
        self.status.reconciliation_complete = true;
        self.refresh_entry_gate();
        Ok(())
    }

    pub fn accept_signal(
        &mut self,
        intent: EntryIntent,
        now_utc: DateTime<Utc>,
    ) -> Result<BrokerAction, String> {
        self.refresh_entry_gate();
        if !self.status.warmup_complete {
            return Err("historical_warmup_incomplete".to_owned());
        }
        if !self.status.reconciliation_complete {
            return Err("broker_reconciliation_incomplete".to_owned());
        }
        if !self.status.entries_enabled {
            return Err(self
                .status
                .blockers
                .first()
                .cloned()
                .unwrap_or_else(|| "entry_gate_closed".to_owned()));
        }
        if intent.signal_time_utc > now_utc {
            return Err("signal_timestamp_in_future".to_owned());
        }
        let age = now_utc
            .signed_duration_since(intent.signal_time_utc)
            .num_seconds();
        if age > self.config.max_signal_age_seconds {
            return Err("stale_signal".to_owned());
        }
        if self.pending_intent.is_some()
            || self.active_position.is_some()
            || self.status.external_position_present
        {
            return Err("one_position_gate_occupied".to_owned());
        }
        if self.status.unresolved_submit_tag.is_some() {
            return Err("submit_outcome_unresolved".to_owned());
        }
        if intent.size <= 0 || intent.size != self.config.contract_size {
            return Err("contract_size_mismatch".to_owned());
        }
        if intent.entry_price <= 0.0
            || !intent.entry_price.is_finite()
            || !intent.stop_price.is_finite()
            || !intent.target_price.is_finite()
        {
            return Err("missing_or_invalid_price_reference".to_owned());
        }
        if (intent.tick_size - self.config.tick_size).abs() > 1e-9 {
            return Err("strategy_tick_size_mismatch".to_owned());
        }
        if self.config.mode == RuntimeMode::Live {
            let reference_price = self
                .last_market_price
                .ok_or("live_market_reference_missing")?;
            let reference_timestamp = self
                .last_market_reference_timestamp
                .ok_or("live_market_timestamp_missing")?;
            if reference_timestamp > now_utc + chrono::Duration::seconds(1)
                || now_utc
                    .signed_duration_since(reference_timestamp)
                    .num_seconds()
                    > self.config.max_market_staleness_seconds
            {
                return Err("live_market_reference_stale_or_future_dated".to_owned());
            }
            let unsafe_price = (intent.entry_price - reference_price).abs()
                > self.config.max_entry_reference_distance_points;
            if unsafe_price {
                return Err("entry_price_outside_market_reference_safety_band".to_owned());
            }
        }
        let (stop_loss_ticks, take_profit_ticks) = intent.bracket_offsets_ticks()?;
        let account_id = self.config.account_id.ok_or("account_id_missing")?;
        let custom_tag = intent.deterministic_tag(account_id, &self.config.contract_id);
        if self.attempted_entry_tags.contains(&custom_tag) {
            return Err("intent_tag_already_attempted".to_owned());
        }
        self.attempted_entry_tags.insert(custom_tag.clone());
        self.pending_intent = Some(intent.clone());
        self.pending_order_phase = Some(OrderPhase::Pending);
        self.entry_order_id = None;
        self.order_cumulative_fill_size = 0;
        self.trade_fill_size = 0;
        self.trade_fill_notional = 0.0;
        self.trade_exit_fill_size = 0;
        self.entry_fill_timestamp_utc = None;
        self.verified_exit_order_ids.clear();
        self.flat_confirmed_entry_fill_size = None;
        self.entry_order_size = intent.size;
        self.stop_order_id = None;
        self.stop_order_price = None;
        self.target_order_id = None;
        self.owned_tag = Some(custom_tag.clone());
        self.status.pending_entry_tag = Some(custom_tag.clone());
        self.status.pending_entry_order_id = None;
        self.refresh_entry_gate();
        Ok(BrokerAction::PlaceEntry {
            custom_tag,
            account_id,
            contract_id: self.config.contract_id.clone(),
            internal_side: intent.direction.internal_order_side(),
            order_type: 1,
            size: intent.size,
            limit_price: intent.entry_price,
            stop_loss_ticks,
            take_profit_ticks,
        })
    }

    pub fn mark_submit_accepted(&mut self, order_id: i64) {
        self.entry_order_id = Some(order_id);
        self.status.pending_entry_order_id = Some(order_id);
        self.pending_order_phase = Some(OrderPhase::Pending);
        self.status.unresolved_submit_tag = None;
        self.order_cumulative_fill_size = 0;
        self.trade_fill_size = 0;
        self.trade_fill_notional = 0.0;
        self.trade_exit_fill_size = 0;
        self.entry_fill_timestamp_utc = None;
        self.verified_exit_order_ids.clear();
        self.flat_confirmed_entry_fill_size = None;
        self.clear_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
        self.clear_hard_blocker("submit_outcome_unresolved_operator_review_required");
        self.refresh_entry_gate();
    }

    pub fn mark_submit_ambiguous(&mut self) {
        self.status.unresolved_submit_tag = self.status.pending_entry_tag.clone();
        self.set_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
        self.refresh_entry_gate();
    }

    pub fn mark_submit_rejected(&mut self) {
        self.pending_intent = None;
        self.pending_order_phase = None;
        self.entry_order_id = None;
        self.owned_tag = None;
        self.status.pending_entry_tag = None;
        self.status.pending_entry_order_id = None;
        self.status.unresolved_submit_tag = None;
        self.status.pending_entry_remainder_cancel_required = false;
        self.entry_order_size = 0;
        self.clear_hard_blocker("submit_outcome_unresolved_reconcile_before_retry");
        self.clear_hard_blocker("submit_outcome_unresolved_operator_review_required");
        self.refresh_entry_gate();
    }

    pub fn request_cancel(&mut self) -> Option<BrokerAction> {
        let order_id = self.entry_order_id?;
        if matches!(
            self.pending_order_phase,
            Some(OrderPhase::Pending | OrderPhase::PartiallyFilled)
        ) {
            let account_id = self.config.account_id?;
            let custom_tag = self
                .status
                .pending_entry_tag
                .clone()
                .or_else(|| self.owned_tag.clone())?;
            self.pending_order_phase = Some(OrderPhase::CancelRequested);
            return Some(BrokerAction::CancelEntry {
                order_id,
                account_id,
                contract_id: self.config.contract_id.clone(),
                custom_tag,
            });
        }
        None
    }

    pub fn apply_exit_operation(
        &mut self,
        action: &str,
        stop_price: Option<f64>,
    ) -> Result<Option<BrokerAction>, String> {
        let Some(position) = self.active_position.as_ref() else {
            return Err("no_bot_owned_position".to_owned());
        };
        if action == "move_sl" {
            let order_id = self.stop_order_id.ok_or("attached_stop_order_id_missing")?;
            let entry_order_id = self.entry_order_id.ok_or("owned_entry_order_id_missing")?;
            let custom_tag = self.owned_tag.clone().ok_or("owned_custom_tag_missing")?;
            let expected_stop_price = self
                .stop_order_price
                .ok_or("current_attached_stop_price_missing")?;
            let price = stop_price.ok_or("exit_kernel_stop_price_missing")?;
            if !price.is_finite() || price <= 0.0 {
                return Err("invalid_exit_kernel_stop_price".to_owned());
            }
            let market_price = self
                .last_market_price
                .ok_or("live_market_reference_missing")?;
            let improves_protection = match position.direction {
                Direction::Long => price > expected_stop_price && price < market_price,
                Direction::Short => price < expected_stop_price && price > market_price,
            };
            let tick_aligned =
                ((price / self.config.tick_size) - (price / self.config.tick_size).round()).abs()
                    <= 1e-6;
            if !improves_protection || !tick_aligned {
                return Err("exit_kernel_stop_does_not_tighten_safely".to_owned());
            }
            return Ok(Some(BrokerAction::ModifyAttachedStop {
                order_id,
                entry_order_id,
                custom_tag,
                contract_id: position.contract_id.clone(),
                side_api: position.direction.closing_api_side(),
                size: position.size.abs(),
                expected_stop_price,
                stop_price: price,
            }));
        }
        if action == "close" {
            if matches!(
                self.pending_order_phase,
                Some(
                    OrderPhase::Pending | OrderPhase::PartiallyFilled | OrderPhase::CancelRequested
                )
            ) {
                return Err(
                    "entry_remainder_must_be_cancelled_and_reconciled_before_close".to_owned(),
                );
            }
            if !position.position_id_confirmed {
                return Err("broker_position_id_not_confirmed".to_owned());
            }
            let entry_order_id = self.entry_order_id.ok_or("owned_entry_order_id_missing")?;
            let custom_tag = self.owned_tag.clone().ok_or("owned_custom_tag_missing")?;
            return Ok(Some(BrokerAction::CloseOwnedPosition {
                contract_id: position.contract_id.clone(),
                position_id: position.position_id,
                attached_stop_order_id: self.stop_order_id,
                attached_target_order_id: self.target_order_id,
                entry_order_id,
                custom_tag,
                position_type: match position.direction {
                    Direction::Long => 1,
                    Direction::Short => 2,
                },
                size: position.size.abs(),
                average_price: position.average_price,
            }));
        }
        if action == "none" {
            return Ok(None);
        }
        Err(format!("unsupported_exit_operation:{action}"))
    }

    pub fn mark_attached_stop_modified(
        &mut self,
        order_id: i64,
        stop_price: f64,
    ) -> Result<(), String> {
        if self.stop_order_id != Some(order_id) {
            return Err("modified_stop_order_id_mismatch".to_owned());
        }
        if !stop_price.is_finite() || stop_price <= 0.0 {
            return Err("modified_stop_price_invalid".to_owned());
        }
        self.stop_order_price = Some(stop_price);
        Ok(())
    }

    fn account_matches(&self, account_id: i64) -> bool {
        self.config.account_id == Some(account_id)
    }

    fn apply_order(&mut self, order: OrderSnapshot) {
        if self.entry_order_candidate(&order, self.owned_tag.as_deref()) {
            self.entry_order_id = Some(order.order_id);
            self.entry_order_size = order.size;
            self.status.pending_entry_order_id = Some(order.order_id);
            self.order_cumulative_fill_size =
                self.order_cumulative_fill_size.max(order.filled_size);
            if self
                .flat_confirmed_entry_fill_size
                .is_some_and(|flat_size| self.order_cumulative_fill_size > flat_size)
            {
                self.flat_confirmed_entry_fill_size = None;
                self.set_hard_blocker("late_entry_fill_after_flat_operator_review_required");
            }
            self.pending_order_phase = Some(merge_order_phase(
                self.pending_order_phase,
                order.phase,
                self.order_cumulative_fill_size,
                order.size,
            ));
            if matches!(
                self.pending_order_phase,
                Some(OrderPhase::Filled | OrderPhase::Cancelled | OrderPhase::Rejected)
            ) {
                self.status.pending_entry_remainder_cancel_required = false;
                self.clear_hard_blocker("entry_remainder_cancel_and_reconcile_required");
            }
            if self.flat_confirmed_entry_fill_size.is_none() {
                self.sync_fill_position(order.order_id);
            }
            if matches!(order.phase, OrderPhase::Cancelled | OrderPhase::Rejected)
                && order.filled_size == 0
            {
                self.mark_submit_rejected();
                return;
            }
            if self.pending_order_phase == Some(OrderPhase::Filled) {
                self.status.pending_entry_tag = None;
                self.status.pending_entry_order_id = None;
            }
        }
        self.classify_live_child(&order);
        if self.active_position.is_some() {
            if self.stop_order_id.is_none() || self.target_order_id.is_none() {
                self.set_hard_blocker("attached_auto_oco_children_not_confirmed");
            } else {
                self.clear_hard_blocker("attached_auto_oco_children_not_confirmed");
            }
        }
        self.status.attached_stop_order_id = self.stop_order_id;
        self.status.attached_target_order_id = self.target_order_id;
        if self.active_position.is_none()
            && self.flat_confirmed_entry_fill_size.is_some()
            && matches!(
                self.pending_order_phase,
                Some(OrderPhase::Filled | OrderPhase::Cancelled | OrderPhase::Rejected)
            )
        {
            self.resolve_flat_entry_from_snapshot(std::slice::from_ref(&order));
        }
    }

    fn classify_live_child(&mut self, order: &OrderSnapshot) {
        let Some((contract_id, direction, size)) = self.active_position.as_ref().map(|position| {
            (
                position.contract_id.clone(),
                position.direction,
                position.size.abs(),
            )
        }) else {
            return;
        };
        let stop_id_matches = self.stop_order_id == Some(order.order_id);
        let target_id_matches = self.target_order_id == Some(order.order_id);
        let identity_confirmed = tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
            || stop_id_matches
            || target_id_matches;
        if !identity_confirmed {
            return;
        }
        let is_stop = order.order_type_api == 4
            && (tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
                || stop_id_matches);
        let is_target = order.order_type_api == 1
            && (tags_match(order.custom_tag.as_deref(), self.owned_tag.as_deref())
                || target_id_matches);
        if !is_stop && !is_target {
            return;
        }
        self.verified_exit_order_ids.insert(order.order_id);
        let safe_working_order = order.contract_id == contract_id
            && order.side_api == direction.closing_api_side()
            && order.size.checked_sub(order.filled_size) == Some(size)
            && matches!(
                order.phase,
                OrderPhase::Pending | OrderPhase::PartiallyFilled
            );
        if is_stop {
            if safe_working_order {
                self.stop_order_id = Some(order.order_id);
                self.stop_order_price = order.stop_price;
            } else {
                self.stop_order_id = None;
                self.stop_order_price = None;
            }
        }
        if is_target {
            if safe_working_order {
                self.target_order_id = Some(order.order_id);
            } else {
                self.target_order_id = None;
            }
        }
    }

    fn apply_entry_fill(
        &mut self,
        order_id: i64,
        fill_size: i32,
        fill_price: f64,
        timestamp_utc: DateTime<Utc>,
    ) {
        if self.pending_intent.is_none() {
            return;
        }
        if self.flat_confirmed_entry_fill_size.take().is_some() {
            self.set_hard_blocker("late_entry_fill_after_flat_operator_review_required");
        }
        if matches!(
            self.pending_order_phase,
            Some(OrderPhase::Cancelled | OrderPhase::Rejected)
        ) {
            self.set_hard_blocker("late_entry_fill_after_terminal_order_operator_review_required");
        }
        self.trade_fill_size = self.trade_fill_size.saturating_add(fill_size.abs());
        self.trade_fill_notional += fill_price * fill_size.abs() as f64;
        self.entry_fill_timestamp_utc = Some(
            self.entry_fill_timestamp_utc
                .map(|previous| previous.min(timestamp_utc))
                .unwrap_or(timestamp_utc),
        );
        self.entry_order_id = Some(order_id);
        let cumulative = self.order_cumulative_fill_size.max(self.trade_fill_size);
        let observed_phase = if self.entry_order_size > 0 && cumulative >= self.entry_order_size {
            OrderPhase::Filled
        } else {
            OrderPhase::PartiallyFilled
        };
        self.pending_order_phase = Some(merge_order_phase(
            self.pending_order_phase,
            observed_phase,
            cumulative,
            self.entry_order_size,
        ));
        self.sync_fill_position(order_id);
    }

    fn apply_exit_fill(&mut self, _order_id: i64, side_api: i32, fill_size: i32) {
        let Some(position) = self.active_position.as_mut() else {
            self.set_hard_blocker("owned_exit_fill_without_open_position_operator_review_required");
            return;
        };
        if side_api != position.direction.closing_api_side() {
            self.set_hard_blocker("owned_exit_fill_side_mismatch_operator_review_required");
            return;
        }
        if fill_size.abs() > position.size.abs() {
            self.set_hard_blocker("owned_exit_fill_exceeds_position_operator_review_required");
            return;
        }
        self.trade_exit_fill_size = self.trade_exit_fill_size.saturating_add(fill_size.abs());
        position.size = (position.size - fill_size.abs()).max(0);
        self.status.owned_size = position.size;
        let (position_id, flat) = (position.position_id, position.size == 0);
        if flat {
            self.resolve_position_flat(position_id);
        }
    }

    fn sync_fill_position(&mut self, order_id: i64) {
        let Some(intent) = self.pending_intent.as_ref() else {
            return;
        };
        let gross = self.order_cumulative_fill_size.max(self.trade_fill_size);
        let total = self
            .active_position
            .as_ref()
            .filter(|position| position.position_id_confirmed)
            .map(|position| position.size.abs())
            .unwrap_or_else(|| gross.saturating_sub(self.trade_exit_fill_size).max(0));
        if total <= 0 {
            return;
        }
        let average = if self.trade_fill_size >= self.order_cumulative_fill_size
            && self.trade_fill_size > 0
        {
            self.trade_fill_notional / self.trade_fill_size as f64
        } else {
            self.active_position
                .as_ref()
                .map(|position| position.average_price)
                .or_else(|| Some(intent.entry_price))
                .unwrap_or(intent.entry_price)
        };
        let confirmed_position_id = self
            .active_position
            .as_ref()
            .filter(|position| position.position_id_confirmed)
            .map(|position| position.position_id);
        let position = PositionSnapshot {
            account_id: self.config.account_id.unwrap_or_default(),
            position_id: confirmed_position_id.unwrap_or(order_id),
            position_id_confirmed: confirmed_position_id.is_some(),
            contract_id: self.config.contract_id.clone(),
            direction: intent.direction,
            size: total,
            average_price: average,
            entry_order_id: Some(order_id),
            custom_tag: self.owned_tag.clone(),
        };
        self.active_position = Some(position);
        self.status.owned_position_id = confirmed_position_id;
        self.status.position_id_confirmed = confirmed_position_id.is_some();
        self.status.owned_size = total;
    }

    fn apply_position(&mut self, position: PositionSnapshot) {
        let identity_matches =
            tags_match(position.custom_tag.as_deref(), self.owned_tag.as_deref())
                || (position.entry_order_id.is_some()
                    && position.entry_order_id == self.entry_order_id)
                || self.position_matches_owned_fill(&position);
        let owned = position.contract_id == self.config.contract_id && identity_matches;
        if owned && position.size != 0 {
            self.status.owned_position_id = position
                .position_id_confirmed
                .then_some(position.position_id);
            self.status.position_id_confirmed = position.position_id_confirmed;
            self.status.owned_size = position.size.abs();
            let mut position = position;
            if position.entry_order_id.is_none() {
                position.entry_order_id = self.entry_order_id;
            }
            if position.custom_tag.is_none() {
                position.custom_tag = self.owned_tag.clone();
            }
            self.active_position = Some(position);
            self.status.external_position_present = false;
            self.clear_hard_blocker("external_or_untracked_position_open");
            if self.status.position_id_confirmed {
                self.clear_hard_blocker("broker_position_id_unconfirmed_operator_review_required");
            } else {
                self.set_hard_blocker("broker_position_id_unconfirmed_operator_review_required");
            }
        } else if position.contract_id == self.config.contract_id && position.size != 0 {
            self.status.external_position_present = true;
            self.set_hard_blocker("external_or_untracked_position_open");
        } else if position.size == 0
            && position.contract_id == self.config.contract_id
            && identity_matches
        {
            self.resolve_position_flat(position.position_id);
        }
    }

    fn resolve_position_flat(&mut self, position_id: i64) {
        if self
            .active_position
            .as_ref()
            .is_some_and(|active| active.position_id_confirmed && active.position_id != position_id)
        {
            self.set_hard_blocker("flat_position_id_mismatch_operator_review_required");
            return;
        }
        self.active_position = None;
        self.status.owned_position_id = None;
        self.status.position_id_confirmed = false;
        self.status.owned_size = 0;
        self.clear_hard_blocker("broker_position_id_unconfirmed_operator_review_required");
        self.stop_order_id = None;
        self.stop_order_price = None;
        self.target_order_id = None;
        self.status.attached_stop_order_id = None;
        self.status.attached_target_order_id = None;

        let entry_filled = self.order_cumulative_fill_size.max(self.trade_fill_size);
        let entry_remainder_working = self.entry_order_size > entry_filled
            && matches!(
                self.pending_order_phase,
                Some(
                    OrderPhase::Pending | OrderPhase::PartiallyFilled | OrderPhase::CancelRequested
                )
            );
        if entry_remainder_working {
            self.flat_confirmed_entry_fill_size = Some(entry_filled);
            self.status.pending_entry_remainder_cancel_required = true;
            self.set_hard_blocker("entry_remainder_cancel_and_reconcile_required");
            return;
        }
        if matches!(
            self.pending_order_phase,
            Some(OrderPhase::Filled | OrderPhase::Cancelled | OrderPhase::Rejected)
        ) {
            self.pending_intent = None;
            self.pending_order_phase = None;
            self.entry_order_id = None;
            self.owned_tag = None;
            self.status.pending_entry_tag = None;
            self.status.pending_entry_order_id = None;
            self.status.unresolved_submit_tag = None;
            self.order_cumulative_fill_size = 0;
            self.trade_fill_size = 0;
            self.trade_fill_notional = 0.0;
            self.trade_exit_fill_size = 0;
            self.entry_order_size = 0;
            self.entry_fill_timestamp_utc = None;
            self.verified_exit_order_ids.clear();
            self.flat_confirmed_entry_fill_size = Some(entry_filled);
            self.status.pending_entry_remainder_cancel_required = false;
            self.clear_hard_blocker("entry_remainder_cancel_and_reconcile_required");
        }
    }

    fn resolve_flat_entry_from_snapshot(&mut self, orders: &[OrderSnapshot]) {
        if self.pending_intent.is_none() {
            return;
        }
        let related = orders.iter().find(|order| {
            order.contract_id == self.config.contract_id
                && (Some(order.order_id) == self.entry_order_id
                    || self.entry_order_candidate(order, self.owned_tag.as_deref()))
        });
        if let Some(order) = related {
            self.pending_order_phase = Some(merge_order_phase(
                self.pending_order_phase,
                order.phase,
                self.order_cumulative_fill_size.max(order.filled_size),
                order.size,
            ));
            self.order_cumulative_fill_size =
                self.order_cumulative_fill_size.max(order.filled_size);
        }
        let terminal = matches!(
            self.pending_order_phase,
            Some(OrderPhase::Filled | OrderPhase::Cancelled | OrderPhase::Rejected)
        );
        let unresolved = self.status.unresolved_submit_tag.is_some();
        if terminal && !unresolved {
            self.pending_intent = None;
            self.pending_order_phase = None;
            self.entry_order_id = None;
            self.owned_tag = None;
            self.status.pending_entry_tag = None;
            self.status.pending_entry_order_id = None;
            self.order_cumulative_fill_size = 0;
            self.trade_fill_size = 0;
            self.trade_fill_notional = 0.0;
            self.trade_exit_fill_size = 0;
            self.entry_order_size = 0;
            self.entry_fill_timestamp_utc = None;
            self.verified_exit_order_ids.clear();
            self.flat_confirmed_entry_fill_size = None;
            self.status.pending_entry_remainder_cancel_required = false;
            self.clear_hard_blocker("entry_remainder_cancel_and_reconcile_required");
        } else if related.is_none() && self.entry_order_id.is_some() && !unresolved {
            self.set_hard_blocker(
                "entry_order_missing_from_reconciliation_operator_review_required",
            );
        }
    }

    fn set_hard_blocker(&mut self, blocker: &str) {
        if !self
            .hard_blockers
            .iter()
            .any(|existing| existing == blocker)
        {
            self.hard_blockers.push(blocker.to_owned());
        }
    }

    fn position_matches_owned_fill(&self, position: &PositionSnapshot) -> bool {
        let Some(active) = self.active_position.as_ref() else {
            return false;
        };
        self.entry_order_id.is_some()
            && self.owned_tag.is_some()
            && (active.position_id_confirmed && position.position_id == active.position_id
                || active.entry_order_id == self.entry_order_id
                    && position.size.abs() == active.size.abs())
            && position.contract_id == active.contract_id
            && position.direction == active.direction
            && position.size.abs() <= active.size.abs()
            && position.average_price.is_finite()
            && (position.average_price - active.average_price).abs() <= self.config.tick_size
    }

    fn entry_order_candidate(&self, order: &OrderSnapshot, tag: Option<&str>) -> bool {
        let expected_side = self
            .pending_intent
            .as_ref()
            .map(|intent| intent.direction.internal_order_side())
            .or_else(|| {
                self.active_position
                    .as_ref()
                    .map(|position| position.direction.internal_order_side())
            })
            .map(|side| if side == 1 { 0 } else { 1 });
        order.contract_id == self.config.contract_id
            && order.order_type_api == 1
            && Some(order.side_api) == expected_side
            && self.entry_order_id.is_none_or(|id| id == order.order_id)
            && tags_match(order.custom_tag.as_deref(), tag)
    }

    fn clear_hard_blocker(&mut self, blocker: &str) {
        self.hard_blockers.retain(|existing| existing != blocker);
    }

    fn refresh_entry_gate(&mut self) {
        let mut blockers = self.hard_blockers.clone();
        if !self.status.warmup_complete {
            blockers.push("historical_warmup_incomplete".to_owned());
        }
        if !self.status.reconciliation_complete {
            blockers.push("broker_reconciliation_incomplete".to_owned());
        }
        if self.status.risk_locked {
            blockers.push("risk_lock_new_entries_only".to_owned());
        }
        if self.pending_intent.is_some() {
            blockers.push("pending_entry_counts_toward_position_gate".to_owned());
        }
        if self.status.pending_entry_remainder_cancel_required {
            blockers.push("entry_remainder_cancel_and_reconcile_required".to_owned());
        }
        if self.active_position.is_some() {
            blockers.push("bot_owned_position_open".to_owned());
        }
        if self.status.external_position_present {
            blockers.push("external_or_untracked_position_open".to_owned());
        }
        if self
            .hard_blockers
            .iter()
            .any(|blocker| blocker == "external_or_untracked_order_open")
        {
            blockers.push("external_or_untracked_order_open".to_owned());
        }
        if self.status.unresolved_submit_tag.is_some() {
            blockers.push("submit_outcome_unresolved_reconcile_before_retry".to_owned());
        }
        if self.config.mode == RuntimeMode::Live {
            blockers.extend(readiness(&self.config).blockers);
        }
        blockers.sort();
        blockers.dedup();
        self.status.entries_enabled = blockers.is_empty();
        self.status.blockers = blockers;
    }
}

fn merge_order_phase(
    current: Option<OrderPhase>,
    observed: OrderPhase,
    cumulative_filled: i32,
    order_size: i32,
) -> OrderPhase {
    if order_size > 0 && cumulative_filled >= order_size {
        return OrderPhase::Filled;
    }
    match current {
        Some(OrderPhase::Filled) => OrderPhase::Filled,
        Some(OrderPhase::Cancelled) => OrderPhase::Cancelled,
        Some(OrderPhase::Rejected) => OrderPhase::Rejected,
        Some(OrderPhase::CancelRequested)
            if !matches!(observed, OrderPhase::Cancelled | OrderPhase::Rejected) =>
        {
            OrderPhase::CancelRequested
        }
        Some(OrderPhase::PartiallyFilled) if observed == OrderPhase::Pending => {
            OrderPhase::PartiallyFilled
        }
        _ => observed,
    }
}

fn tags_match(left: Option<&str>, right: Option<&str>) -> bool {
    matches!((left, right), (Some(left), Some(right)) if left == right)
}
