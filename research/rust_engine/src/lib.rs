//! Reusable, read-only entry points for the isolated Rust research workspace.
//!
//! The CLI and future in-process adapters share these operations so that input
//! auditing and study validation keep one implementation.

mod audit;
mod study;

pub use audit::{AuditReport, FileAudit, audit_inputs};
pub use study::{StudyValidationReport, validate_study};

use std::error::Error;

/// Errors from synchronous research I/O; the bounds allow worker-thread callers.
pub type ResearchError = Box<dyn Error + Send + Sync + 'static>;

pub type ResearchResult<T> = Result<T, ResearchError>;
