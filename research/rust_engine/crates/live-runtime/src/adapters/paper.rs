use crate::core::{BrokerAction, BrokerEvent, BrokerSnapshot, OrderSnapshot, PositionSnapshot};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PaperFillPlan {
    pub fill_size: i32,
    pub fill_price: f64,
}

#[derive(Default)]
pub struct PaperAdapter {
    next_order_id: i64,
    next_trade_id: i64,
    orders: Vec<OrderSnapshot>,
    positions: Vec<PositionSnapshot>,
}

impl PaperAdapter {
    pub fn new() -> Self {
        Self {
            next_order_id: 700_000,
            next_trade_id: 900_000,
            orders: Vec::new(),
            positions: Vec::new(),
        }
    }

    pub fn submit_entry(
        &mut self,
        action: &BrokerAction,
        now_utc: DateTime<Utc>,
        fill: Option<PaperFillPlan>,
    ) -> Result<Vec<BrokerEvent>, String> {
        let BrokerAction::PlaceEntry {
            custom_tag,
            account_id,
            contract_id,
            internal_side,
            size,
            limit_price,
            ..
        } = action
        else {
            return Err("paper_adapter_expected_place_entry".to_owned());
        };
        if *size <= 0 || !limit_price.is_finite() {
            return Err("invalid_paper_order".to_owned());
        }
        let order_id = self.next_order_id;
        self.next_order_id += 1;
        let mut order = OrderSnapshot {
            account_id: *account_id,
            order_id,
            contract_id: contract_id.clone(),
            custom_tag: Some(custom_tag.clone()),
            side_api: if *internal_side == 1 { 0 } else { 1 },
            order_type_api: 1,
            phase: crate::core::OrderPhase::Pending,
            size: *size,
            filled_size: 0,
            filled_price: None,
            stop_price: None,
            limit_price: Some(*limit_price),
            child_role: crate::core::ChildRole::Other,
        };
        let mut events = vec![BrokerEvent::Order(order.clone())];
        self.orders.push(order.clone());
        if let Some(fill) = fill {
            if fill.fill_size <= 0 || fill.fill_size > *size || !fill.fill_price.is_finite() {
                return Err("invalid_paper_fill_plan".to_owned());
            }
            self.next_trade_id += 1;
            order.phase = if fill.fill_size == *size {
                crate::core::OrderPhase::Filled
            } else {
                crate::core::OrderPhase::PartiallyFilled
            };
            order.filled_size = fill.fill_size;
            order.filled_price = Some(fill.fill_price);
            if let Some(saved) = self.orders.last_mut() {
                *saved = order.clone();
            }
            events.push(BrokerEvent::Order(order.clone()));
            events.push(BrokerEvent::TradeFill {
                account_id: *account_id,
                contract_id: contract_id.clone(),
                trade_id: self.next_trade_id,
                order_id,
                side_api: if *internal_side == 1 { 0 } else { 1 },
                custom_tag: Some(custom_tag.clone()),
                fill_size: fill.fill_size,
                fill_price: fill.fill_price,
                timestamp_utc: now_utc,
            });
            self.positions.push(PositionSnapshot {
                account_id: *account_id,
                position_id: order_id,
                position_id_confirmed: true,
                contract_id: contract_id.clone(),
                direction: if *internal_side == 1 {
                    crate::core::Direction::Long
                } else {
                    crate::core::Direction::Short
                },
                size: fill.fill_size,
                average_price: fill.fill_price,
                entry_order_id: Some(order_id),
                custom_tag: Some(custom_tag.clone()),
            });
        }
        Ok(events)
    }

    pub fn add_attached_children(
        &mut self,
        account_id: i64,
        contract_id: &str,
        closing_side: i32,
        custom_tag: &str,
        size: i32,
        stop_price: f64,
        target_price: f64,
    ) -> (OrderSnapshot, OrderSnapshot) {
        let stop = OrderSnapshot {
            account_id,
            order_id: self.next_order_id,
            contract_id: contract_id.to_owned(),
            custom_tag: Some(custom_tag.to_owned()),
            side_api: closing_side,
            order_type_api: 4,
            phase: crate::core::OrderPhase::Pending,
            size,
            filled_size: 0,
            filled_price: None,
            stop_price: Some(stop_price),
            limit_price: None,
            child_role: crate::core::ChildRole::StopLoss,
        };
        self.next_order_id += 1;
        let target = OrderSnapshot {
            account_id,
            order_id: self.next_order_id,
            contract_id: contract_id.to_owned(),
            custom_tag: Some(custom_tag.to_owned()),
            side_api: closing_side,
            order_type_api: 1,
            phase: crate::core::OrderPhase::Pending,
            size,
            filled_size: 0,
            filled_price: None,
            stop_price: None,
            limit_price: Some(target_price),
            child_role: crate::core::ChildRole::TakeProfit,
        };
        self.next_order_id += 1;
        self.orders.extend([stop.clone(), target.clone()]);
        (stop, target)
    }

    pub fn add_manual_position(&mut self, position: PositionSnapshot) {
        self.positions.push(position);
    }

    pub fn snapshot(&self, account_id: i64, now_utc: DateTime<Utc>) -> BrokerSnapshot {
        BrokerSnapshot {
            account_id,
            observed_at_utc: now_utc,
            orders: self.orders.clone(),
            positions: self.positions.clone(),
        }
    }

    pub fn orders(&self) -> &[OrderSnapshot] {
        &self.orders
    }
}
