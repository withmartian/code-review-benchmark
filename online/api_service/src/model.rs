use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

// ---------------------------------------------------------------------------
// Enums — filter dimensions parsed from pr_labels JSON
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Domain {
    Frontend,
    Backend,
    Infra,
    Fullstack,
    Docs,
    Other,
}

impl Domain {
    pub fn from_str_loose(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "frontend" => Self::Frontend,
            "backend" => Self::Backend,
            "infra" | "infrastructure" => Self::Infra,
            "fullstack" | "full-stack" => Self::Fullstack,
            "docs" | "documentation" => Self::Docs,
            _ => Self::Other,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PrType {
    Feature,
    Bugfix,
    Refactor,
    Chore,
    Docs,
    Test,
    Other,
}

impl PrType {
    pub fn from_str_loose(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "feature" => Self::Feature,
            "bugfix" | "bug" | "fix" => Self::Bugfix,
            "refactor" | "refactoring" => Self::Refactor,
            "chore" => Self::Chore,
            "docs" | "documentation" => Self::Docs,
            "test" | "tests" | "testing" => Self::Test,
            _ => Self::Other,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Low,
    Medium,
    High,
    Critical,
}

impl Severity {
    pub fn from_str_loose(s: &str) -> Option<Self> {
        match s.trim().to_lowercase().as_str() {
            "low" => Some(Self::Low),
            "medium" => Some(Self::Medium),
            "high" => Some(Self::High),
            "critical" => Some(Self::Critical),
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// PrRecord — one per analyzed PR
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct PrRecord {
    pub chatbot_idx: u8,
    pub bot_reviewed_at: Option<DateTime<Utc>>,
    pub precision: Option<f32>,
    pub recall: Option<f32>,
    pub diff_lines: Option<u32>,
    pub language: Option<u16>,
    pub domain: Option<Domain>,
    pub pr_type: Option<PrType>,
    pub severity: Option<Severity>,
}

// ---------------------------------------------------------------------------
// VolumeRecord — one per chatbot per date (from pr_volumes table)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct VolumeRecord {
    pub chatbot_idx: u8,
    pub pr_count: u32,
}

// ---------------------------------------------------------------------------
// Snapshot — immutable in-memory dataset
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ChatbotInfo {
    pub github_username: String,
    pub display_name: String,
}

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub by_date: BTreeMap<NaiveDate, Vec<PrRecord>>,
    pub no_date: Vec<PrRecord>,
    pub chatbots: Vec<ChatbotInfo>,
    pub languages: Vec<String>,
    pub volumes: BTreeMap<NaiveDate, Vec<VolumeRecord>>,
}

// ---------------------------------------------------------------------------
// FilterParams — parsed from query string
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct FilterParams {
    pub start_date: Option<NaiveDate>,
    pub end_date: Option<NaiveDate>,
    pub chatbots: Option<Vec<String>>,
    pub languages: Option<Vec<String>>,
    pub domains: Option<Vec<Domain>>,
    pub pr_types: Option<Vec<PrType>>,
    pub severities: Option<Vec<Severity>>,
    pub diff_lines_min: Option<u32>,
    pub diff_lines_max: Option<u32>,
    pub beta: f32,
    pub min_prs_per_day: usize,
    pub min_total_prs: usize,
}

impl Default for FilterParams {
    fn default() -> Self {
        Self {
            start_date: None,
            end_date: None,
            chatbots: None,
            languages: None,
            domains: None,
            pr_types: None,
            severities: None,
            diff_lines_min: None,
            diff_lines_max: None,
            beta: 1.0,
            min_prs_per_day: 0,
            min_total_prs: 0,
        }
    }
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct DailyMetricRow {
    pub date: NaiveDate,
    pub chatbot: String,
    pub avg_precision: f64,
    pub avg_recall: f64,
    pub avg_f_beta: Option<f64>,
    pub pr_count: usize,
}

#[derive(Debug, Serialize)]
pub struct DailyMetricsResponse {
    pub chatbots: Vec<String>,
    pub series: Vec<DailyMetricRow>,
}

#[derive(Debug, Serialize)]
pub struct LeaderboardRow {
    pub chatbot: String,
    pub precision: f64,
    pub recall: f64,
    pub f_score: Option<f64>,
    pub sampled_prs: usize,
    pub scored_prs: usize,
    pub total_prs: u32,
}

#[derive(Debug, Serialize)]
pub struct LeaderboardResponse {
    pub rows: Vec<LeaderboardRow>,
}

#[derive(Debug, Serialize)]
pub struct FilterOptionsResponse {
    pub chatbots: Vec<String>,
    pub languages: Vec<String>,
    pub domains: Vec<String>,
    pub pr_types: Vec<String>,
    pub severities: Vec<String>,
    pub first_date: Option<String>,
    pub last_date: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct VolumeRow {
    pub date: NaiveDate,
    pub chatbot: String,
    pub pr_count: u32,
}

#[derive(Debug, Serialize)]
pub struct VolumesResponse {
    pub chatbots: Vec<String>,
    pub series: Vec<VolumeRow>,
}
