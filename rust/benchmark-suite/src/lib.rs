//! Native, isolated four-allocator benchmark producer.
//!
//! Phase 2 writes raw, paired measurements only. Statistical publication is
//! intentionally deferred to Phase 3.

pub mod adapter;
pub mod child;
pub mod comparison_key;
pub mod config;
pub mod execution;
pub mod latency;
pub mod latency_runner;
pub mod memory;
pub mod memory_runner;
pub mod model;
pub mod orchestration;
pub mod provenance;
pub mod report;
pub mod runner;
pub mod scenarios;
pub mod stats;
pub mod validate;

pub const RAW_SCHEMA_VERSION: &str = "benchmark-raw-v1";
pub const CORE_SUITE_VERSION: &str = "core-throughput-v1";
