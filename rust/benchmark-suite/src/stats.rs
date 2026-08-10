use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::provenance::sha256_bytes;

pub const STATISTICS_VERSION: &str = "paired-log-median-bootstrap-v1";
pub const BOOTSTRAP_RESAMPLES: u32 = 10_000;
pub const BOOTSTRAP_METHOD: &str = "percentile-block-bootstrap-type7-v1";
pub const BOOTSTRAP_PRNG: &str = "splitmix64-rejection-v1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatisticsError(String);

impl StatisticsError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for StatisticsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for StatisticsError {}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum MetricDirection {
    HigherIsBetter,
    LowerIsBetter,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MetricObservation {
    pub block_id: u32,
    pub allocator_id: String,
    pub value: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AbsoluteSummary {
    pub count: u64,
    pub median: f64,
    pub min: f64,
    pub max: f64,
    pub q1: f64,
    pub q3: f64,
    pub iqr: f64,
    pub relative_iqr: f64,
    pub noisy: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ConfidenceInterval {
    pub lower: f64,
    pub upper: f64,
    pub confidence_level: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct BootstrapMetadata {
    pub seed: u64,
    pub resample_count: u32,
    pub method: String,
    pub prng: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PairedEffectSummary {
    pub candidate_id: String,
    pub reference_id: String,
    pub direction: MetricDirection,
    pub block_count: u64,
    pub effect: f64,
    pub confidence_interval: ConfidenceInterval,
    pub bootstrap: BootstrapMetadata,
    /// Paired intervals describe sampling uncertainty; they are not a winner
    /// or regression classification.
    pub informational: bool,
}

/// R/NumPy Type-7 quantile. The input is retained; only a local copy is sorted.
pub fn type7_quantile(values: &[f64], probability: f64) -> Result<f64, StatisticsError> {
    validate_finite_values(values)?;
    if !probability.is_finite() || !(0.0..=1.0).contains(&probability) {
        return Err(StatisticsError::new(
            "quantile probability must be finite and in [0, 1]",
        ));
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    Ok(type7_quantile_sorted(&sorted, probability))
}

pub fn summarize_absolute(values: &[f64]) -> Result<AbsoluteSummary, StatisticsError> {
    validate_positive_values(values)?;
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let min = sorted[0];
    let max = sorted[sorted.len() - 1];
    let q1 = type7_quantile_sorted(&sorted, 0.25);
    let median = type7_quantile_sorted(&sorted, 0.5);
    let q3 = type7_quantile_sorted(&sorted, 0.75);
    let iqr = q3 - q1;
    let relative_iqr = iqr / median;
    if !relative_iqr.is_finite() {
        return Err(StatisticsError::new(
            "absolute summary produced a non-finite relative IQR",
        ));
    }
    Ok(AbsoluteSummary {
        count: sorted
            .len()
            .try_into()
            .map_err(|_| StatisticsError::new("sample count exceeds u64"))?,
        median,
        min,
        max,
        q1,
        q3,
        iqr,
        relative_iqr,
        noisy: relative_iqr > 0.10,
    })
}

/// Summarize a candidate against a reference by explicit block identity.
///
/// Observations for other allocators may be supplied: the bootstrap still
/// samples the complete block as its indivisible unit. Duplicate allocator
/// values or a block missing either side of the requested pair are rejected.
pub fn summarize_paired(
    run_seed: u64,
    cell_id: &str,
    candidate_id: &str,
    reference_id: &str,
    direction: MetricDirection,
    observations: &[MetricObservation],
) -> Result<PairedEffectSummary, StatisticsError> {
    if cell_id.is_empty() || candidate_id.is_empty() || reference_id.is_empty() {
        return Err(StatisticsError::new(
            "cell, candidate, and reference IDs must be non-empty",
        ));
    }
    if candidate_id == reference_id {
        return Err(StatisticsError::new(
            "candidate and reference IDs must differ",
        ));
    }
    if observations.is_empty() {
        return Err(StatisticsError::new("paired observations are empty"));
    }

    let mut blocks: BTreeMap<u32, BTreeMap<&str, f64>> = BTreeMap::new();
    for observation in observations {
        validate_value(observation.value)?;
        if observation.allocator_id.is_empty() {
            return Err(StatisticsError::new("allocator ID must be non-empty"));
        }
        let values = blocks.entry(observation.block_id).or_default();
        if values
            .insert(&observation.allocator_id, observation.value)
            .is_some()
        {
            return Err(StatisticsError::new(format!(
                "duplicate allocator {} in block {}",
                observation.allocator_id, observation.block_id
            )));
        }
    }

    let mut log_effects = Vec::with_capacity(blocks.len());
    for (block_id, values) in &blocks {
        let candidate = values.get(candidate_id).ok_or_else(|| {
            StatisticsError::new(format!(
                "block {block_id} is missing candidate {candidate_id}"
            ))
        })?;
        let reference = values.get(reference_id).ok_or_else(|| {
            StatisticsError::new(format!(
                "block {block_id} is missing reference {reference_id}"
            ))
        })?;
        let log_effect = match direction {
            MetricDirection::HigherIsBetter => candidate.ln() - reference.ln(),
            MetricDirection::LowerIsBetter => reference.ln() - candidate.ln(),
        };
        if !log_effect.is_finite() {
            return Err(StatisticsError::new(
                "paired observation produced a non-finite log effect",
            ));
        }
        log_effects.push(log_effect);
    }

    let point_log = type7_quantile(&log_effects, 0.5)?;
    let seed = bootstrap_seed(run_seed, cell_id, candidate_id, STATISTICS_VERSION);
    let mut prng = SplitMix64::new(seed);
    let mut bootstrap_logs = Vec::with_capacity(BOOTSTRAP_RESAMPLES as usize);
    let mut resample = Vec::with_capacity(log_effects.len());
    for _ in 0..BOOTSTRAP_RESAMPLES {
        resample.clear();
        for _ in 0..log_effects.len() {
            let index = prng.uniform_below(log_effects.len() as u64) as usize;
            resample.push(log_effects[index]);
        }
        resample.sort_by(f64::total_cmp);
        bootstrap_logs.push(type7_quantile_sorted(&resample, 0.5));
    }
    bootstrap_logs.sort_by(f64::total_cmp);
    let lower = type7_quantile_sorted(&bootstrap_logs, 0.025).exp();
    let upper = type7_quantile_sorted(&bootstrap_logs, 0.975).exp();
    let effect = point_log.exp();
    if !effect.is_finite() || !lower.is_finite() || !upper.is_finite() {
        return Err(StatisticsError::new(
            "paired summary produced a non-finite ratio",
        ));
    }

    Ok(PairedEffectSummary {
        candidate_id: candidate_id.to_owned(),
        reference_id: reference_id.to_owned(),
        direction,
        block_count: log_effects
            .len()
            .try_into()
            .map_err(|_| StatisticsError::new("block count exceeds u64"))?,
        effect,
        confidence_interval: ConfidenceInterval {
            lower,
            upper,
            confidence_level: 0.95,
        },
        bootstrap: BootstrapMetadata {
            seed,
            resample_count: BOOTSTRAP_RESAMPLES,
            method: BOOTSTRAP_METHOD.to_owned(),
            prng: BOOTSTRAP_PRNG.to_owned(),
        },
        informational: true,
    })
}

fn validate_positive_values(values: &[f64]) -> Result<(), StatisticsError> {
    if values.is_empty() {
        return Err(StatisticsError::new("statistical sample is empty"));
    }
    for &value in values {
        validate_value(value)?;
    }
    Ok(())
}

fn validate_finite_values(values: &[f64]) -> Result<(), StatisticsError> {
    if values.is_empty() {
        return Err(StatisticsError::new("statistical sample is empty"));
    }
    if values.iter().any(|value| !value.is_finite()) {
        return Err(StatisticsError::new("statistical values must be finite"));
    }
    Ok(())
}

fn validate_value(value: f64) -> Result<(), StatisticsError> {
    if !value.is_finite() || value <= 0.0 {
        return Err(StatisticsError::new(
            "statistical values must be finite and positive",
        ));
    }
    Ok(())
}

fn type7_quantile_sorted(sorted: &[f64], probability: f64) -> f64 {
    if sorted.len() == 1 {
        return sorted[0];
    }
    let index = (sorted.len() - 1) as f64 * probability;
    let lower = index.floor() as usize;
    let fraction = index - lower as f64;
    if fraction == 0.0 {
        sorted[lower]
    } else {
        sorted[lower] + fraction * (sorted[lower + 1] - sorted[lower])
    }
}

fn bootstrap_seed(
    run_seed: u64,
    cell_id: &str,
    candidate_id: &str,
    statistics_version: &str,
) -> u64 {
    // Domain separation plus length prefixes make this tuple encoding
    // unambiguous and stable across platforms and process invocations.
    let mut material = b"mimalloc-pprof/bootstrap-seed/v1\0".to_vec();
    material.extend_from_slice(&run_seed.to_be_bytes());
    for value in [cell_id, candidate_id, statistics_version] {
        material.extend_from_slice(&(value.len() as u64).to_be_bytes());
        material.extend_from_slice(value.as_bytes());
    }
    let digest = sha256_bytes(&material);
    u64::from_str_radix(&digest[..16], 16).expect("SHA-256 digest is lowercase hexadecimal")
}

/// SplitMix64 with rejection sampling rather than modulo reduction. Both the
/// generator and range mapping are versioned in the serialized metadata.
struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e3779b97f4a7c15);
        let mut value = self.state;
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d049bb133111eb);
        value ^ (value >> 31)
    }

    fn uniform_below(&mut self, upper: u64) -> u64 {
        debug_assert!(upper > 0);
        let limit = u64::MAX - (u64::MAX % upper);
        loop {
            let value = self.next_u64();
            if value < limit {
                return value % upper;
            }
        }
    }
}
