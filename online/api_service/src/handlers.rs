use std::sync::Arc;

use axum::extract::{Query, State};
use axum::response::Html;
use axum::Json;
use chrono::NaiveDate;
use serde::Deserialize;
use tracing::info;

use crate::compute;
use crate::model::*;

pub type AppState = Arc<Snapshot>;

#[derive(Debug, Deserialize)]
pub struct MetricsQuery {
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    pub chatbot: Option<String>,
    pub language: Option<String>,
    pub domain: Option<String>,
    pub pr_type: Option<String>,
    pub severity: Option<String>,
    pub diff_lines_min: Option<u32>,
    pub diff_lines_max: Option<u32>,
    pub beta: Option<f32>,
    pub min_prs_per_day: Option<usize>,
    pub min_total_prs: Option<usize>,
}

fn parse_date(s: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").ok()
}

fn parse_csv(s: &str) -> Vec<String> {
    s.split(',')
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .collect()
}

fn to_filter_params(q: &MetricsQuery) -> FilterParams {
    FilterParams {
        start_date: q.start_date.as_deref().and_then(parse_date),
        end_date: q.end_date.as_deref().and_then(parse_date),
        chatbots: q.chatbot.as_deref().map(parse_csv).filter(|v| !v.is_empty()),
        languages: q.language.as_deref().map(parse_csv).filter(|v| !v.is_empty()),
        domains: q.domain.as_deref().map(|s| {
            parse_csv(s)
                .iter()
                .map(|d| Domain::from_str_loose(d))
                .collect()
        }).filter(|v: &Vec<Domain>| !v.is_empty()),
        pr_types: q.pr_type.as_deref().map(|s| {
            parse_csv(s)
                .iter()
                .map(|t| PrType::from_str_loose(t))
                .collect()
        }).filter(|v: &Vec<PrType>| !v.is_empty()),
        severities: q.severity.as_deref().map(|s| {
            parse_csv(s)
                .iter()
                .filter_map(|sv| Severity::from_str_loose(sv))
                .collect()
        }).filter(|v: &Vec<Severity>| !v.is_empty()),
        diff_lines_min: q.diff_lines_min,
        diff_lines_max: q.diff_lines_max,
        beta: q.beta.unwrap_or(1.0),
        min_prs_per_day: q.min_prs_per_day.unwrap_or(0),
        min_total_prs: q.min_total_prs.unwrap_or(0),
    }
}

pub async fn daily_metrics(
    State(snapshot): State<AppState>,
    Query(query): Query<MetricsQuery>,
) -> Json<DailyMetricsResponse> {
    info!(?query, "daily_metrics request");
    let params = to_filter_params(&query);
    info!(
        domains = ?params.domains,
        severities = ?params.severities,
        languages = ?params.languages,
        start = ?params.start_date,
        end = ?params.end_date,
        "parsed filter params"
    );
    let resp = compute::daily_metrics(&snapshot, &params);
    info!(series_len = resp.series.len(), "daily_metrics response");
    Json(resp)
}

pub async fn leaderboard_handler(
    State(snapshot): State<AppState>,
    Query(query): Query<MetricsQuery>,
) -> Json<LeaderboardResponse> {
    info!(?query, "leaderboard request");
    let params = to_filter_params(&query);
    info!(
        domains = ?params.domains,
        severities = ?params.severities,
        "parsed filter params"
    );
    Json(compute::leaderboard(&snapshot, &params))
}

pub async fn options_handler(
    State(snapshot): State<AppState>,
) -> Json<FilterOptionsResponse> {
    Json(compute::filter_options(&snapshot))
}

pub async fn volumes_handler(
    State(snapshot): State<AppState>,
    Query(query): Query<MetricsQuery>,
) -> Json<VolumesResponse> {
    info!(?query, "volumes request");
    let params = to_filter_params(&query);
    Json(compute::pr_volumes(&snapshot, &params))
}

pub async fn dashboard() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
}
