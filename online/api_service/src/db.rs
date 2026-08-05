use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::{DateTime, NaiveDate, Utc};
use sqlx::postgres::PgPoolOptions;
use tracing::info;

use crate::model::*;

/// Load all analyzed PR data from Postgres into an in-memory Snapshot.
pub async fn load_from_postgres(database_url: &str) -> anyhow::Result<Snapshot> {
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(database_url)
        .await?;

    let rows = sqlx::query_as::<_, RawRow>(
        r#"
        SELECT p.id as pr_id,
               la.chatbot_id,
               la.precision,
               la.recall,
               p.bot_reviewed_at,
               p.diff_lines,
               p.pr_author,
               p.repo_name,
               c.github_username,
               c.display_name,
               pl.labels as pr_labels_json,
               (p.reviews IS NOT NULL AND p.reviews != '[]') as has_reviews,
               p.engagement_signals
        FROM llm_analyses la
        JOIN prs p ON la.pr_id = p.id
        JOIN chatbots c ON la.chatbot_id = c.id
        LEFT JOIN pr_labels pl ON pl.pr_id = la.pr_id AND pl.chatbot_id = la.chatbot_id
        WHERE p.pr_merged = TRUE
        ORDER BY p.bot_reviewed_at ASC NULLS FIRST
        "#,
    )
    .fetch_all(&pool)
    .await?;

    // Load ignored tools
    let ignored_rows: Vec<(String,)> = sqlx::query_as(
        "SELECT github_username FROM ignored_tools",
    )
    .fetch_all(&pool)
    .await
    .unwrap_or_default();
    let ignored_usernames: HashSet<String> = ignored_rows.into_iter().map(|(u,)| u).collect();
    info!("Loaded {} ignored tools", ignored_usernames.len());

    // Load PR volumes
    let volume_rows = sqlx::query_as::<_, VolumeRawRow>(
        r#"
        SELECT pv.chatbot_id, pv.date, pv.pr_count,
               c.github_username, c.display_name
        FROM pr_volumes pv
        JOIN chatbots c ON pv.chatbot_id = c.id
        ORDER BY pv.date ASC
        "#,
    )
    .fetch_all(&pool)
    .await?;

    pool.close().await;

    info!("Loaded {} analysis rows, {} volume rows from Postgres", rows.len(), volume_rows.len());

    build_snapshot(rows, volume_rows, &ignored_usernames)
}

#[derive(sqlx::FromRow)]
struct RawRow {
    pr_id: i32,
    chatbot_id: i32,
    precision: Option<f32>,
    recall: Option<f32>,
    bot_reviewed_at: Option<DateTime<Utc>>,
    diff_lines: Option<i32>,
    pr_author: Option<String>,
    repo_name: String,
    github_username: String,
    display_name: Option<String>,
    pr_labels_json: Option<String>,
    has_reviews: Option<bool>,
    engagement_signals: Option<String>,
}

#[derive(sqlx::FromRow)]
struct VolumeRawRow {
    chatbot_id: i32,
    date: String,
    pr_count: i32,
    github_username: String,
    display_name: Option<String>,
}

const GENERAL_BOT_NAMES: &[&str] = &[
    "dependabot", "renovate", "github-actions", "codecov",
    "mergify", "snyk-bot", "greenkeeper", "imgbot",
    "stale", "allcontributors", "semantic-release-bot",
    "github-advanced-security",
    "llamapreview", "ai-coding-guardrails",
    "qodo-free-for-open-source-projects", "amazon-q-developer",
    "sourceryai", "github-code-quality", "copilot-pull-request-review",
    "copilot-pull-request-reviewer", "raycastbot", "clawdbot",
    "cometactions", "kilo-code-bot", "codecov-comment",
];

fn is_bot_username(username: &str, known_bots: &HashSet<String>) -> bool {
    let lower = username.to_lowercase();
    lower.ends_with("[bot]") || known_bots.contains(&lower)
}

/// Build set of known bot usernames from chatbot table + general bots.
/// Includes both "name[bot]" and "name" variants since BQ actors may lack the suffix.
fn build_known_bots(chatbot_usernames: &[String]) -> HashSet<String> {
    let mut bots = HashSet::new();
    for name in GENERAL_BOT_NAMES {
        bots.insert(name.to_string());
    }
    for name in chatbot_usernames {
        let lower = name.to_lowercase();
        bots.insert(lower.trim_end_matches("[bot]").to_string());
        bots.insert(lower);
    }
    bots
}

fn build_snapshot(rows: Vec<RawRow>, volume_rows: Vec<VolumeRawRow>, ignored_usernames: &HashSet<String>) -> anyhow::Result<Snapshot> {
    // Collect all chatbot usernames first to build comprehensive bot detection set
    let chatbot_usernames: Vec<String> = rows.iter()
        .map(|r| r.github_username.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let known_bots = build_known_bots(&chatbot_usernames);

    let mut chatbot_map: HashMap<i32, u8> = HashMap::new();
    let mut chatbots: Vec<ChatbotInfo> = Vec::new();
    let mut language_map: HashMap<String, u16> = HashMap::new();
    let mut languages: Vec<String> = Vec::new();
    let mut repo_name_map: HashMap<String, u32> = HashMap::new();
    let mut repo_names: Vec<String> = Vec::new();
    let mut author_map: HashMap<String, u32> = HashMap::new();

    let mut by_date: BTreeMap<chrono::NaiveDate, Vec<PrRecord>> = BTreeMap::new();
    let mut no_date: Vec<PrRecord> = Vec::new();

    // Aggregate accumulators
    let mut repo_contributors: HashMap<u32, HashSet<u32>> = HashMap::new();
    // (repo_name_idx, author_idx, chatbot_idx) -> list of pr_ids
    let mut author_repo_prs: HashMap<(u32, u32, u8), Vec<i64>> = HashMap::new();

    for row in &rows {
        // Chatbot lookup
        let chatbot_idx = *chatbot_map.entry(row.chatbot_id).or_insert_with(|| {
            let idx = chatbots.len() as u8;
            chatbots.push(ChatbotInfo {
                github_username: row.github_username.clone(),
                display_name: row.display_name.clone().unwrap_or_else(|| row.github_username.clone()),
                ignored: ignored_usernames.contains(&row.github_username),
            });
            idx
        });

        // Repo name dedup
        let repo_name_idx = {
            let len = repo_names.len() as u32;
            *repo_name_map.entry(row.repo_name.clone()).or_insert_with(|| {
                repo_names.push(row.repo_name.clone());
                len
            })
        };

        // Parse labels
        let (language, domain, pr_type, severity) = parse_labels(
            row.pr_labels_json.as_deref(),
            &mut language_map,
            &mut languages,
        );

        let self_authored = row.pr_author.as_ref()
            .map(|a| a.eq_ignore_ascii_case(&row.github_username))
            .unwrap_or(false);

        let pr_author_is_bot = row.pr_author.as_ref()
            .map(|a| is_bot_username(a, &known_bots))
            .unwrap_or(false);

        // Author dedup + aggregate accumulators
        let author_idx = match row.pr_author.as_ref() {
            Some(author) => {
                let author_lower = author.to_lowercase();
                let len = author_map.len() as u32;
                let idx = *author_map.entry(author_lower).or_insert(len);
                repo_contributors.entry(repo_name_idx).or_default().insert(idx);
                author_repo_prs.entry((repo_name_idx, idx, chatbot_idx)).or_default().push(row.pr_id as i64);
                idx
            }
            None => u32::MAX,
        };

        let (has_human_engagement, human_reviewer_count, commits_after_review) =
            parse_engagement_signals(row.engagement_signals.as_deref());

        let record = PrRecord {
            pr_id: row.pr_id as i64,
            chatbot_idx,
            bot_reviewed_at: row.bot_reviewed_at,
            precision: row.precision,
            recall: row.recall,
            diff_lines: row.diff_lines.map(|d| d as u32),
            language,
            domain,
            pr_type,
            severity,
            self_authored,
            has_reviews: row.has_reviews.unwrap_or(false),
            pr_author_is_bot,
            repo_name_idx,
            author_idx,
            has_human_engagement,
            human_reviewer_count,
            commits_after_review,
        };

        match row.bot_reviewed_at {
            Some(dt) => {
                let date = dt.date_naive();
                by_date.entry(date).or_default().push(record);
            }
            None => {
                no_date.push(record);
            }
        }
    }

    // Finalize aggregate lookups: repo_name_idx -> unique contributor count
    let repo_contributor_counts: HashMap<u32, u32> = repo_contributors
        .into_iter()
        .map(|(repo_idx, authors)| (repo_idx, authors.len() as u32))
        .collect();

    let total_records: usize = by_date.values().map(|v| v.len()).sum::<usize>() + no_date.len();
    let has_domain: usize = by_date.values().flat_map(|v| v.iter()).chain(no_date.iter())
        .filter(|r| r.domain.is_some()).count();
    let has_severity: usize = by_date.values().flat_map(|v| v.iter()).chain(no_date.iter())
        .filter(|r| r.severity.is_some()).count();

    // Build volumes BTreeMap, reusing chatbot_map when possible
    let mut volumes: BTreeMap<NaiveDate, Vec<VolumeRecord>> = BTreeMap::new();
    for vrow in &volume_rows {
        let date = match NaiveDate::parse_from_str(&vrow.date, "%Y-%m-%d") {
            Ok(d) => d,
            Err(_) => continue,
        };
        let chatbot_idx = *chatbot_map.entry(vrow.chatbot_id).or_insert_with(|| {
            let idx = chatbots.len() as u8;
            chatbots.push(ChatbotInfo {
                github_username: vrow.github_username.clone(),
                display_name: vrow.display_name.clone().unwrap_or_else(|| vrow.github_username.clone()),
                ignored: ignored_usernames.contains(&vrow.github_username),
            });
            idx
        });
        volumes.entry(date).or_default().push(VolumeRecord {
            chatbot_idx,
            pr_count: vrow.pr_count as u32,
        });
    }

    info!(
        "Built snapshot: {} records, {} chatbots, {} languages, {} dates, {} no-date, {} volume dates | labels: {}/{} have domain, {}/{} have severity",
        total_records,
        chatbots.len(),
        languages.len(),
        by_date.len(),
        no_date.len(),
        volumes.len(),
        has_domain, total_records,
        has_severity, total_records,
    );

    Ok(Snapshot {
        by_date,
        no_date,
        chatbots,
        languages,
        volumes,
        repo_contributor_counts,
        author_repo_prs,
    })
}

fn parse_engagement_signals(json_str: Option<&str>) -> (bool, u8, u16) {
    let json_str = match json_str {
        Some(s) if !s.is_empty() => s,
        _ => return (false, 0, 0),
    };
    let obj: serde_json::Value = match serde_json::from_str(json_str) {
        Ok(v) => v,
        Err(_) => return (false, 0, 0),
    };
    let engaged = obj.get("has_human_engagement").and_then(|v| v.as_bool()).unwrap_or(false);
    let reviewers = obj.get("human_reviewer_count").and_then(|v| v.as_u64()).unwrap_or(0) as u8;
    let commits = obj.get("commits_after_review").and_then(|v| v.as_u64()).unwrap_or(0) as u16;
    (engaged, reviewers, commits)
}

fn parse_labels(
    json_str: Option<&str>,
    language_map: &mut HashMap<String, u16>,
    languages: &mut Vec<String>,
) -> (Option<u16>, Option<Domain>, Option<PrType>, Option<Severity>) {
    let json_str = match json_str {
        Some(s) if !s.is_empty() => s,
        _ => return (None, None, None, None),
    };

    let obj: serde_json::Value = match serde_json::from_str(json_str) {
        Ok(v) => v,
        Err(_) => return (None, None, None, None),
    };

    let language = obj
        .get("language")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .map(|s| {
            let len = languages.len() as u16;
            *language_map.entry(s.clone()).or_insert_with(|| {
                languages.push(s);
                len
            })
        });

    let domain = obj
        .get("domain")
        .and_then(|v| v.as_str())
        .map(Domain::from_str_loose);

    let pr_type = obj
        .get("pr_type")
        .and_then(|v| v.as_str())
        .map(PrType::from_str_loose);

    let severity = obj
        .get("severity")
        .and_then(|v| v.as_str())
        .and_then(Severity::from_str_loose);

    (language, domain, pr_type, severity)
}
