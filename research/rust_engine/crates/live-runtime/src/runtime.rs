use crate::core::{
    BrokerAction, BrokerEvent, BrokerSnapshot, EntryIntent, RuntimeConfig, RuntimeCore,
    RuntimeStatus,
};
use crate::journal::Journal;
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::Value;
use std::path::Path;

pub struct Runtime {
    core: RuntimeCore,
    journal: Journal,
    exit_state: Value,
}

impl Runtime {
    pub fn open(
        config: RuntimeConfig,
        journal_path: impl AsRef<Path>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let mut journal = Journal::open(journal_path)?;
        let mut core = RuntimeCore::new(config.clone());
        core.set_live_capabilities(false, false, false, false, false, true);
        let mut exit_state = Value::Null;
        for record in journal.records()? {
            if record.kind == "exit_kernel_state" {
                exit_state = record.payload.clone();
            }
            restore_record(&mut core, &record.kind, record.payload)?;
        }
        core.require_startup_refresh(true);
        journal.append(
            "runtime_started",
            &serde_json::json!({
                "mode": config.mode,
                "account_id": config.account_id,
                "contract_id": config.contract_id,
                "at_utc": Utc::now(),
            }),
        )?;
        Ok(Self {
            core,
            journal,
            exit_state,
        })
    }

    pub fn status(&self) -> &RuntimeStatus {
        self.core.status()
    }

    pub fn core(&self) -> &RuntimeCore {
        &self.core
    }

    pub fn exit_state(&self) -> &Value {
        &self.exit_state
    }

    pub fn store_exit_state<T: Serialize>(
        &mut self,
        state: &T,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append("exit_kernel_state", state)?;
        self.exit_state = serde_json::to_value(state)?;
        Ok(())
    }

    pub fn require_startup_refresh(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "startup_refresh_required",
            &serde_json::json!({"at_utc": Utc::now()}),
        )?;
        self.core.require_startup_refresh(true);
        Ok(())
    }

    pub fn mark_attached_stop_modified(
        &mut self,
        order_id: i64,
        stop_price: f64,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "attached_stop_modified",
            &serde_json::json!({"order_id": order_id, "stop_price": stop_price}),
        )?;
        self.core
            .mark_attached_stop_modified(order_id, stop_price)
            .map_err(std::io::Error::other)?;
        Ok(())
    }

    pub fn mark_warmup_complete(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "history_warmup_complete",
            &serde_json::json!({
                "at_utc": Utc::now(),
            }),
        )?;
        self.core.mark_warmup_complete();
        Ok(())
    }

    pub fn mark_reconciled(
        &mut self,
        snapshot: BrokerSnapshot,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append("broker_snapshot", &snapshot)?;
        self.core
            .reconcile(snapshot)
            .map_err(std::io::Error::other)?;
        Ok(())
    }

    pub fn set_live_capabilities(
        &mut self,
        account_verified: bool,
        contract_active: bool,
        account_lease_held: bool,
        market_data_connected: bool,
        user_hub_connected: bool,
        journal_ready: bool,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "live_capabilities",
            &serde_json::json!({
                "account_verified": account_verified,
                "contract_active": contract_active,
                "account_lease_held": account_lease_held,
                "market_data_connected": market_data_connected,
                "user_hub_connected": user_hub_connected,
                "journal_ready": journal_ready,
            }),
        )?;
        self.core.set_live_capabilities(
            account_verified,
            contract_active,
            account_lease_held,
            market_data_connected,
            user_hub_connected,
            journal_ready,
        );
        Ok(())
    }

    pub fn accept_signal(
        &mut self,
        intent: EntryIntent,
        now_utc: DateTime<Utc>,
    ) -> Result<BrokerAction, String> {
        let action = self.core.accept_signal(intent.clone(), now_utc)?;
        self.journal
            .append("entry_intent", &intent)
            .map_err(|error| format!("journal_entry_intent_failed:{error}"))?;
        self.journal
            .append("place_entry_action", &action)
            .map_err(|error| format!("journal_place_action_failed:{error}"))?;
        Ok(action)
    }

    pub fn submit_accepted(&mut self, order_id: i64) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "submit_accepted",
            &serde_json::json!({
                "order_id": order_id,
            }),
        )?;
        self.core.mark_submit_accepted(order_id);
        Ok(())
    }

    pub fn submit_ambiguous(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "submit_ambiguous",
            &serde_json::json!({
                "tag": self.status().pending_entry_tag,
            }),
        )?;
        self.core.mark_submit_ambiguous();
        Ok(())
    }

    pub fn submit_rejected(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.journal
            .append("submit_rejected", &serde_json::json!({}))?;
        self.core.mark_submit_rejected();
        Ok(())
    }

    pub fn apply_broker_event(
        &mut self,
        event: BrokerEvent,
    ) -> Result<Vec<BrokerAction>, Box<dyn std::error::Error>> {
        if !matches!(
            &event,
            &BrokerEvent::MarketTimestamp(_) | &BrokerEvent::MarketReference { .. }
        ) {
            self.journal.append("broker_event", &event)?;
        }
        Ok(self.core.broker_event(event))
    }

    pub fn set_risk_lock(
        &mut self,
        locked: bool,
        reason: &str,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(
            "risk_lock",
            &serde_json::json!({
                "locked": locked,
                "reason": reason,
            }),
        )?;
        self.core.set_risk_lock(locked);
        Ok(())
    }

    pub fn request_cancel(&mut self) -> Result<Option<BrokerAction>, Box<dyn std::error::Error>> {
        let action = self.core.request_cancel();
        if let Some(action) = &action {
            self.journal.append("cancel_action", action)?;
        }
        Ok(action)
    }

    pub fn apply_exit_operation(
        &mut self,
        action: &str,
        stop_price: Option<f64>,
    ) -> Result<Option<BrokerAction>, String> {
        let broker_action = self.core.apply_exit_operation(action, stop_price)?;
        if let Some(action) = &broker_action {
            self.journal
                .append("exit_action", action)
                .map_err(|error| format!("journal_exit_action_failed:{error}"))?;
        }
        Ok(broker_action)
    }

    pub fn append_record<T: Serialize>(
        &mut self,
        kind: &str,
        payload: &T,
    ) -> Result<(), Box<dyn std::error::Error>> {
        self.journal.append(kind, payload)?;
        Ok(())
    }

    pub fn journal_path(&self) -> &Path {
        self.journal.path()
    }
}

fn restore_record(
    core: &mut RuntimeCore,
    kind: &str,
    payload: Value,
) -> Result<(), Box<dyn std::error::Error>> {
    match kind {
        "entry_intent" => {
            let intent: EntryIntent = serde_json::from_value(payload)?;
            core.recover_entry_intent(intent)?;
        }
        "submit_accepted" => {
            if let Some(order_id) = payload.get("order_id").and_then(Value::as_i64) {
                core.mark_submit_accepted(order_id);
            }
        }
        "submit_ambiguous" => core.mark_submit_ambiguous(),
        "submit_rejected" => core.mark_submit_rejected(),
        "broker_event" => {
            let event: BrokerEvent = serde_json::from_value(payload)?;
            core.broker_event(event);
        }
        "risk_lock" => {
            if let Some(locked) = payload.get("locked").and_then(Value::as_bool) {
                core.set_risk_lock(locked);
            }
        }
        "history_warmup_complete" | "live_capabilities" | "startup_refresh_required" => {}
        "attached_stop_modified" => {
            if let (Some(order_id), Some(stop_price)) = (
                payload.get("order_id").and_then(Value::as_i64),
                payload.get("stop_price").and_then(Value::as_f64),
            ) {
                let _ = core.mark_attached_stop_modified(order_id, stop_price);
            }
        }
        "broker_snapshot" => {
            let snapshot: BrokerSnapshot = serde_json::from_value(payload)?;
            let _ = core.reconcile(snapshot);
        }
        _ => {}
    }
    Ok(())
}
