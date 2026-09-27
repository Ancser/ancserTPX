pub mod adapters;
pub mod clock;
pub mod config;
pub mod core;
pub mod events;
pub mod exit;
pub mod journal;
pub mod pi;
pub mod pi_backtest;
pub mod pi_lifecycle;
pub mod pi_signal_builders;
pub mod risk;
pub mod runtime;
pub mod strategy;

pub use core::{
    BrokerAction, BrokerEvent, BrokerSnapshot, EntryIntent, RuntimeConfig, RuntimeCore,
    RuntimeMode, RuntimeStatus,
};
pub use runtime::Runtime;
