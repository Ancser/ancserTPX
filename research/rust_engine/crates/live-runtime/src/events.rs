use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const PROTOCOL_VERSION: u16 = 1;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "message_type", rename_all = "snake_case")]
pub enum BridgeMessage {
    Request {
        protocol_version: u16,
        request_id: u64,
        operation: String,
        payload: Value,
    },
    Response {
        protocol_version: u16,
        request_id: u64,
        ok: bool,
        payload: Value,
        error_code: Option<String>,
    },
    Event {
        protocol_version: u16,
        event_id: String,
        event_type: String,
        observed_at_utc: String,
        payload: Value,
    },
}

impl BridgeMessage {
    pub fn validate_version(&self) -> Result<(), &'static str> {
        let version = match self {
            Self::Request {
                protocol_version, ..
            }
            | Self::Response {
                protocol_version, ..
            }
            | Self::Event {
                protocol_version, ..
            } => *protocol_version,
        };
        if version == PROTOCOL_VERSION {
            Ok(())
        } else {
            Err("unsupported_bridge_protocol_version")
        }
    }
}
