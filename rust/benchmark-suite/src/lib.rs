//! Native, isolated four-allocator benchmark producer.
//!
//! Phase 2 writes raw, paired measurements only. Statistical publication is
//! intentionally deferred to Phase 3.

pub mod adapter;
pub mod child;
pub mod config;
pub mod execution;
pub mod model;
pub mod orchestration;
pub mod provenance;
pub mod runner;
pub mod scenarios;

pub const RAW_SCHEMA_VERSION: &str = "benchmark-raw-v1";
pub const CORE_SUITE_VERSION: &str = "core-throughput-v1";
