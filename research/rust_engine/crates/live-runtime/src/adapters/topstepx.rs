use crate::core::{
    BrokerAction, BrokerEvent, BrokerSnapshot, ChildRole, Direction, OrderPhase, OrderSnapshot,
    PositionSnapshot,
};
use crate::events::{BridgeMessage, PROTOCOL_VERSION};
use crate::runtime::Runtime;
use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration as StdDuration, Instant};

pub fn bounded_search_window(days: i64, now_utc: DateTime<Utc>) -> (String, String) {
    let bounded_days = days.clamp(1, 60);
    let start = now_utc - Duration::days(bounded_days);
    (
        start.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        now_utc.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
    )
}

pub fn action_to_gateway_payload(action: &BrokerAction) -> Result<Value, String> {
    match action {
        BrokerAction::PlaceEntry {
            custom_tag,
            account_id,
            contract_id,
            internal_side,
            order_type,
            size,
            limit_price,
            stop_loss_ticks,
            take_profit_ticks,
        } => {
            if custom_tag.trim().is_empty() || *size <= 0 || *order_type != 1 {
                return Err("invalid_entry_order_request".to_owned());
            }
            let side = match internal_side {
                1 => 0,
                2 => 1,
                _ => return Err("unsupported_internal_order_side".to_owned()),
            };
            if !limit_price.is_finite() || *limit_price <= 0.0 {
                return Err("invalid_entry_limit_price".to_owned());
            }
            if *stop_loss_ticks == 0 || *take_profit_ticks == 0 {
                return Err("attached_bracket_distance_cannot_be_zero".to_owned());
            }
            Ok(json!({
                "accountId": account_id,
                "contractId": contract_id,
                "type": order_type,
                "side": side,
                "size": size,
                "limitPrice": limit_price,
                "stopPrice": null,
                "customTag": custom_tag,
                "stopLossBracket": { "ticks": stop_loss_ticks, "type": 4 },
                "takeProfitBracket": { "ticks": take_profit_ticks, "type": 1 },
            }))
        }
        _ => Err("action_requires_a_specific_topstepx_bridge_operation".to_owned()),
    }
}

pub fn normalize_order(
    account_id: i64,
    value: &Value,
    child_role: ChildRole,
) -> Result<OrderSnapshot, String> {
    let order_id = int_field(value, "id")?;
    let account_from_payload = value
        .get("accountId")
        .and_then(Value::as_i64)
        .unwrap_or(account_id);
    if account_from_payload != account_id {
        return Err("order_account_id_mismatch".to_owned());
    }
    let raw_side = int_field(value, "side")?;
    if raw_side != 0 && raw_side != 1 {
        return Err("unsupported_gateway_order_side".to_owned());
    }
    let raw_type = int_field(value, "type")?;
    let raw_status = value.get("status").and_then(Value::as_i64).unwrap_or(0);
    let size = i32::try_from(int_field(value, "size")?)
        .map_err(|_| "gateway_order_size_out_of_range".to_owned())?;
    let filled_raw = int_value(value, "fillVolume").unwrap_or(0);
    let filled_size =
        i32::try_from(filled_raw).map_err(|_| "gateway_filled_size_out_of_range".to_owned())?;
    let phase = match raw_status {
        1 => {
            if filled_size > 0 {
                OrderPhase::PartiallyFilled
            } else {
                OrderPhase::Pending
            }
        }
        6 => OrderPhase::Unknown,
        2 if filled_size >= size => OrderPhase::Filled,
        2 => OrderPhase::Unknown,
        3 | 4 => OrderPhase::Cancelled,
        5 => OrderPhase::Rejected,
        _ => OrderPhase::Unknown,
    };
    let contract_id = string_field(value, "contractId")?.to_owned();
    if size <= 0 || filled_size < 0 || filled_size > size {
        return Err("invalid_gateway_order_size".to_owned());
    }
    Ok(OrderSnapshot {
        account_id,
        order_id,
        contract_id,
        custom_tag: value
            .get("customTag")
            .and_then(Value::as_str)
            .filter(|tag| !tag.is_empty())
            .map(str::to_owned),
        side_api: raw_side as i32,
        order_type_api: raw_type as i32,
        phase,
        size,
        filled_size,
        filled_price: optional_number(value, "filledPrice"),
        stop_price: optional_number(value, "stopPrice"),
        limit_price: optional_number(value, "limitPrice"),
        child_role,
    })
}

pub fn normalize_position(account_id: i64, value: &Value) -> Result<PositionSnapshot, String> {
    let account_from_payload = value
        .get("accountId")
        .and_then(Value::as_i64)
        .unwrap_or(account_id);
    if account_from_payload != account_id {
        return Err("position_account_id_mismatch".to_owned());
    }
    let raw_type = int_field(value, "type")?;
    let size = i32::try_from(int_field(value, "size")?)
        .map_err(|_| "gateway_position_size_out_of_range".to_owned())?;
    let direction = match raw_type {
        1 => Direction::Long,
        2 => Direction::Short,
        0 if size == 0 => Direction::Long,
        _ => return Err("unsupported_gateway_position_type".to_owned()),
    };
    let average_price = number_field(value, "averagePrice")?;
    if size < 0
        || !average_price.is_finite()
        || (size > 0 && average_price <= 0.0)
        || average_price < 0.0
    {
        return Err("invalid_gateway_position".to_owned());
    }
    Ok(PositionSnapshot {
        account_id,
        position_id: int_field(value, "id")?,
        position_id_confirmed: true,
        contract_id: string_field(value, "contractId")?.to_owned(),
        direction,
        size,
        average_price,
        entry_order_id: value.get("entryOrderId").and_then(Value::as_i64),
        custom_tag: value
            .get("customTag")
            .and_then(Value::as_str)
            .filter(|tag| !tag.is_empty())
            .map(str::to_owned),
    })
}

pub fn normalize_snapshot(
    account_id: i64,
    observed_at_utc: DateTime<Utc>,
    orders: &[Value],
    positions: &[Value],
) -> Result<BrokerSnapshot, String> {
    let normalized_orders = orders
        .iter()
        .map(|order| normalize_order(account_id, order, ChildRole::Other))
        .collect::<Result<Vec<_>, _>>()?;
    let normalized_positions = positions
        .iter()
        .map(|position| normalize_position(account_id, position))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(BrokerSnapshot {
        account_id,
        observed_at_utc,
        orders: normalized_orders,
        positions: normalized_positions,
    })
}

pub fn classify_attached_children(
    snapshot: &mut BrokerSnapshot,
    position: &PositionSnapshot,
    owned_custom_tag: &str,
    _stop_price: f64,
    _target_price: f64,
    tick_size: f64,
) -> Result<(), String> {
    if !tick_size.is_finite() || tick_size <= 0.0 {
        return Err("invalid_tick_size".to_owned());
    }
    let close_side = position.direction.closing_api_side();
    let stop_candidates: Vec<usize> = snapshot
        .orders
        .iter()
        .enumerate()
        .filter_map(|(index, order)| {
            if order.account_id != position.account_id
                || order.contract_id != position.contract_id
                || order.custom_tag.as_deref() != Some(owned_custom_tag)
                || order.side_api != close_side
                || order.size - order.filled_size != position.size.abs()
                || order.order_type_api != 4
                || !matches!(
                    order.phase,
                    OrderPhase::Pending | OrderPhase::PartiallyFilled
                )
            {
                return None;
            }
            order.stop_price?;
            Some(index)
        })
        .collect();
    let target_candidates: Vec<usize> = snapshot
        .orders
        .iter()
        .enumerate()
        .filter_map(|(index, order)| {
            if order.account_id != position.account_id
                || order.contract_id != position.contract_id
                || order.custom_tag.as_deref() != Some(owned_custom_tag)
                || order.side_api != close_side
                || order.size - order.filled_size != position.size.abs()
                || order.order_type_api != 1
                || !matches!(
                    order.phase,
                    OrderPhase::Pending | OrderPhase::PartiallyFilled
                )
            {
                return None;
            }
            order.limit_price?;
            Some(index)
        })
        .collect();
    if stop_candidates.len() == 1 {
        snapshot.orders[stop_candidates[0]].child_role = ChildRole::StopLoss;
    }
    if target_candidates.len() == 1 {
        snapshot.orders[target_candidates[0]].child_role = ChildRole::TakeProfit;
    }
    Ok(())
}

fn int_field(value: &Value, field: &str) -> Result<i64, String> {
    int_value(value, field).ok_or_else(|| format!("missing_integer_field:{field}"))
}

fn int_value(value: &Value, field: &str) -> Option<i64> {
    value.get(field).and_then(Value::as_i64)
}

fn string_field<'a>(value: &'a Value, field: &str) -> Result<&'a str, String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing_string_field:{field}"))
}

fn number_field(value: &Value, field: &str) -> Result<f64, String> {
    value
        .get(field)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
        .ok_or_else(|| format!("missing_finite_number_field:{field}"))
}

fn optional_number(value: &Value, field: &str) -> Option<f64> {
    value
        .get(field)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
}

pub struct PythonBridge {
    child: Child,
    stdin: BufWriter<ChildStdin>,
    incoming: Receiver<Result<BridgeMessage, String>>,
    queued: VecDeque<BridgeMessage>,
    next_request_id: u64,
    stopped: bool,
}

impl PythonBridge {
    pub fn spawn(
        python_executable: &str,
        repository_root: impl AsRef<Path>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let mut child = Command::new(python_executable)
            .args(["-m", "backend.rust_live_bridge"])
            .current_dir(repository_root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdin = child.stdin.take().ok_or("bridge stdin unavailable")?;
        let stdout = child.stdout.take().ok_or("bridge stdout unavailable")?;
        let (sender, incoming) = mpsc::channel();
        thread::Builder::new()
            .name("ancsertpx-python-bridge-reader".to_owned())
            .spawn(move || {
                let mut reader = BufReader::new(stdout);
                let mut line = String::new();
                loop {
                    line.clear();
                    match reader.read_line(&mut line) {
                        Ok(0) => {
                            let _ = sender.send(Err("python_bridge_eof".to_owned()));
                            break;
                        }
                        Ok(_) => {
                            let parsed = serde_json::from_str::<BridgeMessage>(&line)
                                .map_err(|_| "python_bridge_emitted_invalid_jsonl".to_owned());
                            if sender.send(parsed).is_err() {
                                break;
                            }
                        }
                        Err(_) => {
                            let _ = sender.send(Err("python_bridge_stdout_read_failed".to_owned()));
                            break;
                        }
                    }
                }
            })?;
        Ok(Self {
            child,
            stdin: BufWriter::new(stdin),
            incoming,
            queued: VecDeque::new(),
            next_request_id: 1,
            stopped: false,
        })
    }

    pub fn repository_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(4)
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf()
    }

    pub fn request(&mut self, operation: &str, payload: Value) -> Result<Value, String> {
        self.request_with_timeout(operation, payload, StdDuration::from_secs(30))
    }

    pub fn request_with_timeout(
        &mut self,
        operation: &str,
        payload: Value,
        timeout: StdDuration,
    ) -> Result<Value, String> {
        let request_id = self.next_request_id;
        self.next_request_id = self.next_request_id.saturating_add(1);
        let message = BridgeMessage::Request {
            protocol_version: PROTOCOL_VERSION,
            request_id,
            operation: operation.to_owned(),
            payload,
        };
        serde_json::to_writer(&mut self.stdin, &message)
            .map_err(|_| "python_bridge_request_write_failed".to_owned())?;
        self.stdin
            .write_all(b"\n")
            .and_then(|_| self.stdin.flush())
            .map_err(|_| "python_bridge_request_write_failed".to_owned())?;

        let deadline = Instant::now() + timeout;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("python_bridge_response_timeout".to_owned());
            }
            let message = self.receive_next(remaining)?;
            message.validate_version().map_err(str::to_owned)?;
            match message {
                BridgeMessage::Response {
                    request_id: response_id,
                    ok,
                    payload,
                    error_code,
                    ..
                } if response_id == request_id => {
                    return if ok {
                        Ok(payload)
                    } else {
                        Err(error_code
                            .unwrap_or_else(|| "python_bridge_operation_failed".to_owned()))
                    };
                }
                other => self.queued.push_back(other),
            }
        }
    }

    pub fn send_action(&mut self, action: &BrokerAction) -> Result<Value, String> {
        let (operation, payload) = action_to_bridge_operation(action)?;
        self.request_with_timeout(operation, payload, StdDuration::from_secs(30))
    }

    pub fn send_action_and_record(
        &mut self,
        runtime: &mut Runtime,
        action: &BrokerAction,
    ) -> Result<Value, String> {
        let result = self.send_action(action);
        match action {
            BrokerAction::PlaceEntry { .. } => match &result {
                Ok(payload) if payload["state"] == "accepted" => {
                    let order_id = payload["order_id"]
                        .as_i64()
                        .ok_or_else(|| "broker_order_id_missing".to_owned())?;
                    runtime
                        .submit_accepted(order_id)
                        .map_err(|_| "submit_acceptance_journal_failed".to_owned())?;
                }
                Ok(payload) if payload["state"] == "rejected" => {
                    runtime
                        .submit_rejected()
                        .map_err(|_| "submit_rejection_journal_failed".to_owned())?;
                }
                _ => {
                    runtime
                        .submit_ambiguous()
                        .map_err(|_| "ambiguous_submit_journal_failed".to_owned())?;
                }
            },
            _ => {
                let state = result
                    .as_ref()
                    .ok()
                    .and_then(|payload| payload["state"].as_str());
                if result.is_err() || matches!(state, Some("ambiguous" | "rejected")) {
                    let event = BrokerEvent::ReconciliationRequired {
                        reason: if state == Some("rejected") {
                            "broker_action_rejected_operator_review_required".to_owned()
                        } else {
                            "broker_action_outcome_uncertain".to_owned()
                        },
                    };
                    runtime
                        .apply_broker_event(event)
                        .map_err(|_| "broker_action_reconciliation_journal_failed".to_owned())?;
                }
            }
        }
        result
    }

    pub fn next_event(&mut self, timeout: StdDuration) -> Result<Option<BridgeMessage>, String> {
        if let Some(index) = self
            .queued
            .iter()
            .position(|message| matches!(message, BridgeMessage::Event { .. }))
        {
            return Ok(self.queued.remove(index));
        }
        match self.incoming.recv_timeout(timeout) {
            Ok(Ok(message)) => {
                message.validate_version().map_err(str::to_owned)?;
                if matches!(message, BridgeMessage::Event { .. }) {
                    Ok(Some(message))
                } else {
                    self.queued.push_back(message);
                    Ok(None)
                }
            }
            Ok(Err(error)) => Err(error),
            Err(RecvTimeoutError::Timeout) => Ok(None),
            Err(RecvTimeoutError::Disconnected) => Err("python_bridge_disconnected".to_owned()),
        }
    }

    pub fn drain_events_into(
        &mut self,
        runtime: &mut Runtime,
        account_id: i64,
    ) -> Result<usize, String> {
        let mut count = 0;
        while let Some(message) = self.next_event(StdDuration::ZERO)? {
            if let BridgeMessage::Event {
                event_type,
                payload,
                ..
            } = &message
            {
                if event_type == "connection_state" {
                    let current = runtime.core().config().clone();
                    let connected = payload
                        .get("connected")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    if !connected {
                        runtime
                            .require_startup_refresh()
                            .map_err(|_| "connection_refresh_journal_failed".to_owned())?;
                    }
                    let hub = payload.get("hub").and_then(Value::as_str).unwrap_or("");
                    let market = if hub == "market" {
                        connected
                    } else {
                        current.market_data_connected
                    };
                    let user = if hub == "user" {
                        connected
                    } else {
                        current.user_hub_connected
                    };
                    runtime
                        .set_live_capabilities(
                            current.broker_account_verified,
                            current.contract_active_verified,
                            current.account_lease_held,
                            market,
                            user,
                            current.journal_ready,
                        )
                        .map_err(|_| "connection_state_journal_failed".to_owned())?;
                }
            }
            if let Some(event) = bridge_event_to_broker_event(&message, account_id)? {
                runtime
                    .apply_broker_event(event)
                    .map_err(|_| "broker_event_journal_failed".to_owned())?;
                count += 1;
            }
        }
        Ok(count)
    }

    pub fn stop(&mut self) -> Result<(), String> {
        if self.stopped {
            return Ok(());
        }
        let result = self.request_with_timeout("stop", json!({}), StdDuration::from_secs(10));
        self.stopped = true;
        if result.is_err() {
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
        result.map(|_| ())
    }

    fn receive_next(&mut self, timeout: StdDuration) -> Result<BridgeMessage, String> {
        match self.incoming.recv_timeout(timeout) {
            Ok(Ok(message)) => Ok(message),
            Ok(Err(error)) => Err(error),
            Err(RecvTimeoutError::Timeout) => Err("python_bridge_response_timeout".to_owned()),
            Err(RecvTimeoutError::Disconnected) => Err("python_bridge_disconnected".to_owned()),
        }
    }
}

impl Drop for PythonBridge {
    fn drop(&mut self) {
        if !self.stopped {
            let _ = self.stop();
        }
    }
}

pub fn action_to_bridge_operation(action: &BrokerAction) -> Result<(&'static str, Value), String> {
    match action {
        BrokerAction::PlaceEntry {
            custom_tag,
            account_id,
            contract_id,
            internal_side,
            size,
            limit_price,
            stop_loss_ticks,
            take_profit_ticks,
            ..
        } => Ok((
            "place_entry",
            json!({
                "custom_tag": custom_tag,
                "account_id": account_id,
                "contract_id": contract_id,
                "internal_side": internal_side,
                "size": size,
                "limit_price": limit_price,
                "stop_loss_ticks": stop_loss_ticks,
                "take_profit_ticks": take_profit_ticks,
            }),
        )),
        BrokerAction::CancelEntry {
            order_id,
            account_id,
            contract_id,
            custom_tag,
        } => Ok((
            "cancel_entry",
            json!({
                "order_id": order_id,
                "account_id": account_id,
                "contract_id": contract_id,
                "custom_tag": custom_tag,
            }),
        )),
        BrokerAction::ModifyAttachedStop {
            order_id,
            entry_order_id,
            custom_tag,
            contract_id,
            side_api,
            size,
            expected_stop_price,
            stop_price,
        } => Ok((
            "modify_attached_stop",
            json!({
                "order_id": order_id,
                "entry_order_id": entry_order_id,
                "custom_tag": custom_tag,
                "contract_id": contract_id,
                "side_api": side_api,
                "size": size,
                "expected_stop_price": expected_stop_price,
                "stop_price": stop_price,
            }),
        )),
        BrokerAction::CloseOwnedPosition {
            contract_id,
            position_id,
            attached_stop_order_id,
            attached_target_order_id,
            entry_order_id,
            custom_tag,
            position_type,
            size,
            average_price,
        } => Ok((
            "close_owned_position",
            json!({
                "contract_id": contract_id,
                "position_id": position_id,
                "attached_stop_order_id": attached_stop_order_id,
                "attached_target_order_id": attached_target_order_id,
                "entry_order_id": entry_order_id,
                "custom_tag": custom_tag,
                "position_type": position_type,
                "size": size,
                "average_price": average_price,
            }),
        )),
    }
}

pub fn bridge_event_to_broker_event(
    message: &BridgeMessage,
    account_id: i64,
) -> Result<Option<BrokerEvent>, String> {
    let BridgeMessage::Event {
        event_type,
        payload,
        ..
    } = message
    else {
        return Ok(None);
    };
    match event_type.as_str() {
        "user_order" => Ok(Some(BrokerEvent::Order(normalize_order(
            account_id,
            payload,
            ChildRole::Other,
        )?))),
        "user_position" => Ok(Some(BrokerEvent::Position(normalize_position(
            account_id, payload,
        )?))),
        "user_trade" => {
            if payload
                .get("voided")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Ok(Some(BrokerEvent::ReconciliationRequired {
                    reason: "broker_trade_voided_reconcile_required".to_owned(),
                }));
            }
            let timestamp = timestamp_field(payload, "creationTimestamp")?;
            let fill_size = i32::try_from(int_field(payload, "size")?)
                .map_err(|_| "broker_trade_size_out_of_range".to_owned())?;
            let event_account_id = payload
                .get("accountId")
                .and_then(Value::as_i64)
                .ok_or_else(|| "broker_trade_account_id_missing".to_owned())?;
            if event_account_id != account_id {
                return Ok(None);
            }
            Ok(Some(BrokerEvent::TradeFill {
                account_id,
                contract_id: string_field(payload, "contractId")?.to_owned(),
                trade_id: int_field(payload, "id")?,
                order_id: int_field(payload, "orderId")?,
                side_api: int_field(payload, "side")? as i32,
                custom_tag: None,
                fill_size,
                fill_price: number_field(payload, "price")?,
                timestamp_utc: timestamp,
            }))
        }
        "market_trade" => Ok(Some(BrokerEvent::MarketReference {
            timestamp_utc: timestamp_field(payload, "timestamp")?,
            price: number_field(payload, "price")?,
        })),
        "market_quote" => {
            let timestamp = payload
                .get("timestamp")
                .and_then(Value::as_str)
                .or_else(|| payload.get("lastUpdated").and_then(Value::as_str))
                .ok_or_else(|| "missing_market_quote_timestamp".to_owned())?;
            Ok(Some(BrokerEvent::MarketReference {
                timestamp_utc: parse_timestamp(timestamp)?,
                price: number_field(payload, "lastPrice")?,
            }))
        }
        "connection_state" if payload.get("connected").and_then(Value::as_bool) == Some(false) => {
            let hub = payload
                .get("hub")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            Ok(Some(BrokerEvent::ReconciliationRequired {
                reason: format!("{hub}_hub_disconnected"),
            }))
        }
        "market_ordering_error" => Ok(Some(BrokerEvent::ReconciliationRequired {
            reason: payload
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("market_event_ordering_error")
                .to_owned(),
        })),
        "user_event_account_error" => Ok(Some(BrokerEvent::ReconciliationRequired {
            reason: payload
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("user_event_account_identity_invalid")
                .to_owned(),
        })),
        "user_account"
            if payload
                .get("id")
                .or_else(|| payload.get("accountId"))
                .and_then(Value::as_i64)
                .is_some_and(|event_account_id| event_account_id != account_id) =>
        {
            Ok(None)
        }
        "user_account"
            if payload
                .get("id")
                .or_else(|| payload.get("accountId"))
                .and_then(Value::as_i64)
                .is_none() =>
        {
            Ok(Some(BrokerEvent::ReconciliationRequired {
                reason: "user_account_id_missing_or_mismatched".to_owned(),
            }))
        }
        "user_account"
            if payload.get("canTrade").and_then(Value::as_bool) == Some(false)
                || payload.get("isVisible").and_then(Value::as_bool) == Some(false) =>
        {
            Ok(Some(BrokerEvent::RiskLock {
                locked: true,
                reason: "broker_account_no_longer_tradeable_or_visible".to_owned(),
            }))
        }
        _ => Ok(None),
    }
}

fn timestamp_field(value: &Value, field: &str) -> Result<DateTime<Utc>, String> {
    let text = value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing_timestamp_field:{field}"))?;
    parse_timestamp(text)
}

fn parse_timestamp(text: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(text)
        .map(|time| time.with_timezone(&Utc))
        .map_err(|_| "invalid_utc_timestamp".to_owned())
}
