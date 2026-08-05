use std::collections::{HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::collections::hash_map::DefaultHasher;

use chrono::NaiveDate;
use rand::seq::SliceRandom;
use rand::rngs::StdRng;
use rand::SeedableRng;

use crate::model::*;

/// Derive a deterministic seed from the filter parameters so the capping
/// random sample is stable across repeated identical queries.
fn params_seed(params: &FilterParams) -> u64 {
    let mut h = DefaultHasher::new();
    params.start_date.hash(&mut h);
    params.end_date.hash(&mut h);
    params.chatbots.hash(&mut h);
    params.languages.hash(&mut h);
    params.domains.hash(&mut h);
    params.pr_types.hash(&mut h);
    params.severities.hash(&mut h);
    params.diff_lines_min.hash(&mut h);
    params.diff_lines_max.hash(&mut h);
    params.beta.to_bits().hash(&mut h);
    params.min_prs_per_day.hash(&mut h);
    params.min_total_prs.hash(&mut h);
    params.include_ignored.hash(&mut h);
    params.exclude_self_authored.hash(&mut h);
    params.require_reviews.hash(&mut h);
    params.exclude_bot_authored.hash(&mut h);
    params.min_repo_contributors.hash(&mut h);
    params.max_author_repo_prs.hash(&mut h);
    params.require_human_engagement.hash(&mut h);
    params.min_human_reviewers.hash(&mut h);
    params.min_commits_after_review.hash(&mut h);
    h.finish()
}

/// F-beta from precision and recall. Returns None if denominator is zero.
pub fn f_beta(precision: f64, recall: f64, beta: f32) -> Option<f64> {
    let b2 = (beta as f64) * (beta as f64);
    let denom = b2 * precision + recall;
    if denom <= 0.0 {
        None
    } else {
        Some((1.0 + b2) * precision * recall / denom)
    }
}

// ---------------------------------------------------------------------------
// Unified filtering
// ---------------------------------------------------------------------------

/// Result of applying all filters once. Every aggregation endpoint consumes this.
pub struct FilteredData<'a> {
    /// Chatbot indices passing chatbot-level filters (name, min_total_prs).
    pub eligible: HashSet<u8>,
    /// PR records passing all filters, paired with their date.
    pub records: Vec<(NaiveDate, &'a PrRecord)>,
    /// Volume totals per chatbot within the date range.
    pub vol_totals: HashMap<u8, u32>,
}

/// Check if a chatbot matches the name filter.
fn chatbot_name_matches(idx: u8, snapshot: &Snapshot, params: &FilterParams) -> bool {
    if let Some(ref names) = params.chatbots {
        let info = &snapshot.chatbots[idx as usize];
        names.iter().any(|n| {
            let n_lower = n.to_lowercase();
            info.github_username.to_lowercase() == n_lower
                || info.display_name.to_lowercase() == n_lower
        })
    } else {
        true
    }
}

/// Check if a record passes all record-level filters (labels, diff lines).
/// Chatbot eligibility is checked separately via the eligible set.
fn record_matches(record: &PrRecord, snapshot: &Snapshot, params: &FilterParams) -> bool {
    // Language filter
    if let Some(ref langs) = params.languages {
        match record.language {
            Some(idx) => {
                let rec_lang = snapshot.languages[idx as usize].to_lowercase();
                if !langs.iter().any(|l| l.to_lowercase() == rec_lang) {
                    return false;
                }
            }
            None => return false, // no label → exclude when filter active
        }
    }

    // Domain filter
    if let Some(ref domains) = params.domains {
        match record.domain {
            Some(d) => {
                if !domains.contains(&d) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // PR type filter
    if let Some(ref pr_types) = params.pr_types {
        match record.pr_type {
            Some(t) => {
                if !pr_types.contains(&t) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // Severity filter
    if let Some(ref severities) = params.severities {
        match record.severity {
            Some(s) => {
                if !severities.contains(&s) {
                    return false;
                }
            }
            None => return false,
        }
    }

    // Diff lines range — records with diff_lines=None pass (matches dashboard default behavior)
    if let Some(dl) = record.diff_lines {
        if let Some(min) = params.diff_lines_min {
            if dl < min {
                return false;
            }
        }
        if let Some(max) = params.diff_lines_max {
            if dl > max {
                return false;
            }
        }
    }

    // Exclude self-authored PRs (bot reviewing its own PR)
    if params.exclude_self_authored && record.self_authored {
        return false;
    }

    // Require non-empty reviews
    if params.require_reviews && !record.has_reviews {
        return false;
    }

    // Exclude PRs where the author is a bot
    if params.exclude_bot_authored && record.pr_author_is_bot {
        return false;
    }

    // Minimum unique contributors in the repo
    if let Some(min_contribs) = params.min_repo_contributors {
        let count = snapshot.repo_contributor_counts
            .get(&record.repo_name_idx)
            .copied()
            .unwrap_or(0);
        if count < min_contribs {
            return false;
        }
    }

    // NOTE: max_author_repo_prs is handled in apply_filters via random sampling,
    // not here, because it requires a pre-computed sampled set across all records.

    // Engagement filters
    if params.require_human_engagement && !record.has_human_engagement {
        return false;
    }
    if let Some(min_rev) = params.min_human_reviewers {
        if (record.human_reviewer_count as u32) < min_rev {
            return false;
        }
    }
    if let Some(min_commits) = params.min_commits_after_review {
        if (record.commits_after_review as u32) < min_commits {
            return false;
        }
    }

    true
}

/// Sum total PR volumes per chatbot from the volumes table (respecting date range).
fn volume_totals(snapshot: &Snapshot, params: &FilterParams) -> HashMap<u8, u32> {
    let mut totals: HashMap<u8, u32> = HashMap::new();

    let iter: Box<dyn Iterator<Item = (&NaiveDate, &Vec<VolumeRecord>)>> =
        match (params.start_date, params.end_date) {
            (Some(start), Some(end)) => Box::new(snapshot.volumes.range(start..=end)),
            (Some(start), None) => Box::new(snapshot.volumes.range(start..)),
            (None, Some(end)) => Box::new(snapshot.volumes.range(..=end)),
            (None, None) => Box::new(snapshot.volumes.iter()),
        };

    for (_date, records) in iter {
        for r in records {
            *totals.entry(r.chatbot_idx).or_default() += r.pr_count;
        }
    }

    totals
}

/// Apply all filters once. Returns eligible chatbots, filtered PR records,
/// and volume totals. All aggregation endpoints consume this.
pub fn apply_filters<'a>(snapshot: &'a Snapshot, params: &FilterParams) -> FilteredData<'a> {
    // 1. Volume totals (needed for min_total_prs check + leaderboard total_prs column)
    let vol = volume_totals(snapshot, params);

    // 2. Eligible chatbots: ignored filter + name filter + min_total_prs threshold
    let eligible: HashSet<u8> = (0..snapshot.chatbots.len() as u8)
        .filter(|&idx| {
            if !params.include_ignored && snapshot.chatbots[idx as usize].ignored {
                return false;
            }
            if !chatbot_name_matches(idx, snapshot, params) {
                return false;
            }
            if params.min_total_prs > 0 {
                let total = vol.get(&idx).copied().unwrap_or(0) as usize;
                if total < params.min_total_prs {
                    return false;
                }
            }
            true
        })
        .collect();

    // 3. Pre-compute random sample for max_author_repo_prs cap.
    //    For triples exceeding the cap, randomly pick `max` pr_ids to keep;
    //    triples at or below the cap pass through entirely.
    let capped_sample: Option<HashSet<i64>> = params.max_author_repo_prs.map(|max_prs| {
        let mut rng = StdRng::seed_from_u64(params_seed(params));
        let mut sampled = HashSet::new();
        for (_, prs) in &snapshot.author_repo_prs {
            if prs.len() as u32 > max_prs {
                let mut shuffled = prs.clone();
                shuffled.shuffle(&mut rng);
                sampled.extend(shuffled.into_iter().take(max_prs as usize));
            } else {
                sampled.extend(prs.iter().copied());
            }
        }
        sampled
    });

    // 4. Collect records passing all filters
    let mut records = Vec::new();

    let iter: Box<dyn Iterator<Item = (&NaiveDate, &Vec<PrRecord>)>> =
        match (params.start_date, params.end_date) {
            (Some(start), Some(end)) => Box::new(snapshot.by_date.range(start..=end)),
            (Some(start), None) => Box::new(snapshot.by_date.range(start..)),
            (None, Some(end)) => Box::new(snapshot.by_date.range(..=end)),
            (None, None) => Box::new(snapshot.by_date.iter()),
        };

    for (date, recs) in iter {
        for r in recs {
            if !eligible.contains(&r.chatbot_idx) {
                continue;
            }
            if !record_matches(r, snapshot, params) {
                continue;
            }
            if let Some(ref sampled) = capped_sample {
                if r.author_idx != u32::MAX && !sampled.contains(&r.pr_id) {
                    continue;
                }
            }
            records.push((*date, r));
        }
    }

    FilteredData {
        eligible,
        records,
        vol_totals: vol,
    }
}

// ---------------------------------------------------------------------------
// Aggregation endpoints — each consumes FilteredData, no independent filtering
// ---------------------------------------------------------------------------

/// Accumulator for computing averages.
/// Tracks precision and recall counts separately — matches pandas mean() which skips NaN per column.
#[derive(Default)]
struct Accum {
    sum_precision: f64,
    precision_count: usize,
    sum_recall: f64,
    recall_count: usize,
}

/// Aggregate filtered records into daily metrics per chatbot.
pub fn daily_metrics(snapshot: &Snapshot, params: &FilterParams) -> DailyMetricsResponse {
    let filtered = apply_filters(snapshot, params);
    let mut buckets: HashMap<(NaiveDate, u8), Accum> = HashMap::new();

    for (date, r) in &filtered.records {
        let p = match r.precision {
            Some(p) => p,
            None => continue,
        };
        let acc = buckets.entry((*date, r.chatbot_idx)).or_default();
        acc.sum_precision += p as f64;
        acc.precision_count += 1;
        if let Some(rc) = r.recall {
            acc.sum_recall += rc as f64;
            acc.recall_count += 1;
        }
    }

    // If min_scored_prs is set, compute total scored per bot and build exclusion set
    let excluded_bots: HashSet<u8> = if params.min_scored_prs > 0 {
        let mut totals: HashMap<u8, usize> = HashMap::new();
        for ((_, idx), acc) in &buckets {
            *totals.entry(*idx).or_default() += acc.precision_count;
        }
        totals.into_iter()
            .filter(|(_, count)| *count < params.min_scored_prs)
            .map(|(idx, _)| idx)
            .collect()
    } else {
        HashSet::new()
    };

    let mut series: Vec<DailyMetricRow> = Vec::new();
    for ((date, chatbot_idx), acc) in &buckets {
        if excluded_bots.contains(chatbot_idx) {
            continue;
        }
        if params.min_prs_per_day > 0 && acc.precision_count < params.min_prs_per_day {
            continue;
        }
        let avg_p = acc.sum_precision / acc.precision_count as f64;
        let (avg_r, avg_fb) = if acc.recall_count > 0 {
            let r = acc.sum_recall / acc.recall_count as f64;
            (r, f_beta(avg_p, r, params.beta))
        } else {
            (0.0, None)
        };
        let info = &snapshot.chatbots[*chatbot_idx as usize];
        series.push(DailyMetricRow {
            date: *date,
            chatbot: info.github_username.clone(),
            avg_precision: avg_p,
            avg_recall: avg_r,
            avg_f_beta: avg_fb,
            pr_count: acc.precision_count,
        });
    }

    series.sort_by(|a, b| a.date.cmp(&b.date).then_with(|| a.chatbot.cmp(&b.chatbot)));

    let chatbots: Vec<String> = {
        let mut v: Vec<_> = filtered
            .eligible
            .iter()
            .map(|&idx| snapshot.chatbots[idx as usize].github_username.clone())
            .collect();
        v.sort();
        v
    };

    DailyMetricsResponse { chatbots, series }
}

/// Aggregate filtered records into one row per chatbot (leaderboard).
/// When min_prs_per_day > 0, only day/bot combos meeting the threshold contribute.
pub fn leaderboard(snapshot: &Snapshot, params: &FilterParams) -> LeaderboardResponse {
    let filtered = apply_filters(snapshot, params);

    // Count all filtered records per chatbot (sampled_prs — regardless of null precision/recall)
    let mut sampled_counts: HashMap<u8, usize> = HashMap::new();
    for (_, r) in &filtered.records {
        *sampled_counts.entry(r.chatbot_idx).or_default() += 1;
    }

    // Accumulate scores only from records with both precision AND recall (scored_prs)
    let mut accums: HashMap<u8, Accum> = HashMap::new();

    if params.min_prs_per_day > 0 {
        // Group by day first, then exclude day/bot combos below threshold
        let mut daily: HashMap<(NaiveDate, u8), Accum> = HashMap::new();

        for (date, r) in &filtered.records {
            let p = match r.precision { Some(p) => p, None => continue };
            let rc = match r.recall { Some(rc) => rc, None => continue };
            let acc = daily.entry((*date, r.chatbot_idx)).or_default();
            acc.sum_precision += p as f64;
            acc.precision_count += 1;
            acc.sum_recall += rc as f64;
            acc.recall_count += 1;
        }

        for ((_, chatbot_idx), day_acc) in &daily {
            if day_acc.precision_count < params.min_prs_per_day { continue; }
            let acc = accums.entry(*chatbot_idx).or_default();
            acc.sum_precision += day_acc.sum_precision;
            acc.precision_count += day_acc.precision_count;
            acc.sum_recall += day_acc.sum_recall;
            acc.recall_count += day_acc.recall_count;
        }
    } else {
        for (_, r) in &filtered.records {
            let p = match r.precision { Some(p) => p, None => continue };
            let rc = match r.recall { Some(rc) => rc, None => continue };
            let acc = accums.entry(r.chatbot_idx).or_default();
            acc.sum_precision += p as f64;
            acc.precision_count += 1;
            acc.sum_recall += rc as f64;
            acc.recall_count += 1;
        }
    }

    // Build rows for all chatbots that have at least one filtered record
    let mut rows: Vec<LeaderboardRow> = sampled_counts
        .iter()
        .filter(|(&idx, _)| {
            if params.min_scored_prs == 0 { return true; }
            accums.get(&idx)
                .map(|a| a.precision_count >= params.min_scored_prs)
                .unwrap_or(false)
        })
        .map(|(&idx, &sampled)| {
            let acc = accums.get(&idx);
            let (avg_p, avg_r, f_score, scored) = match acc {
                Some(acc) if acc.precision_count > 0 => {
                    let avg_p = acc.sum_precision / acc.precision_count as f64;
                    let avg_r = if acc.recall_count > 0 {
                        acc.sum_recall / acc.recall_count as f64
                    } else {
                        0.0
                    };
                    (avg_p, avg_r, f_beta(avg_p, avg_r, params.beta), acc.precision_count)
                }
                _ => (0.0, 0.0, None, 0),
            };
            let info = &snapshot.chatbots[idx as usize];
            LeaderboardRow {
                chatbot: info.github_username.clone(),
                precision: avg_p,
                recall: avg_r,
                f_score,
                sampled_prs: sampled,
                scored_prs: scored,
                total_prs: filtered.vol_totals.get(&idx).copied().unwrap_or(0),
            }
        })
        .collect();

    rows.sort_by(|a, b| {
        b.f_score
            .partial_cmp(&a.f_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    LeaderboardResponse { rows }
}

/// Aggregate PR volumes by date and chatbot.
/// Uses the same eligible set so all charts show the same bots.
/// Missing (date, chatbot) pairs are zero-filled.
pub fn pr_volumes(snapshot: &Snapshot, params: &FilterParams) -> VolumesResponse {
    let filtered = apply_filters(snapshot, params);

    // Collect volume data for eligible chatbots
    let mut data: HashMap<(NaiveDate, u8), u32> = HashMap::new();
    let mut seen_chatbots: HashSet<u8> = HashSet::new();

    let iter: Box<dyn Iterator<Item = (&NaiveDate, &Vec<VolumeRecord>)>> =
        match (params.start_date, params.end_date) {
            (Some(start), Some(end)) => Box::new(snapshot.volumes.range(start..=end)),
            (Some(start), None) => Box::new(snapshot.volumes.range(start..)),
            (None, Some(end)) => Box::new(snapshot.volumes.range(..=end)),
            (None, None) => Box::new(snapshot.volumes.iter()),
        };

    for (date, records) in iter {
        for r in records {
            if !filtered.eligible.contains(&r.chatbot_idx) {
                continue;
            }
            seen_chatbots.insert(r.chatbot_idx);
            data.insert((*date, r.chatbot_idx), r.pr_count);
        }
    }

    if seen_chatbots.is_empty() {
        return VolumesResponse { chatbots: vec![], series: vec![] };
    }

    // Determine date range to fill
    let first_volume_date = snapshot.volumes.keys().next();
    let last_volume_date = snapshot.volumes.keys().last();

    let range_start = params.start_date
        .or_else(|| first_volume_date.copied())
        .unwrap();
    let range_end = params.end_date
        .or_else(|| last_volume_date.copied())
        .unwrap();

    // Build series with zero-fill
    let mut series: Vec<VolumeRow> = Vec::new();
    let mut date = range_start;
    while date <= range_end {
        for &idx in &seen_chatbots {
            let pr_count = data.get(&(date, idx)).copied().unwrap_or(0);
            let info = &snapshot.chatbots[idx as usize];
            series.push(VolumeRow {
                date,
                chatbot: info.github_username.clone(),
                pr_count,
            });
        }
        date = date.succ_opt().unwrap();
    }

    series.sort_by(|a, b| a.date.cmp(&b.date).then_with(|| a.chatbot.cmp(&b.chatbot)));

    let chatbots: Vec<String> = {
        let mut v: Vec<_> = seen_chatbots
            .iter()
            .map(|&idx| snapshot.chatbots[idx as usize].github_username.clone())
            .collect();
        v.sort();
        v
    };

    VolumesResponse { chatbots, series }
}

/// Extract available filter options from the snapshot.
/// Excluded ignored chatbots from the options list.
pub fn filter_options(snapshot: &Snapshot) -> FilterOptionsResponse {
    let chatbots: Vec<String> = snapshot
        .chatbots
        .iter()
        .filter(|c| !c.ignored)
        .map(|c| c.github_username.clone())
        .collect();

    let mut lang_counts: HashMap<u16, usize> = HashMap::new();
    let mut domains = HashSet::new();
    let mut pr_types = HashSet::new();
    let mut severities = HashSet::new();

    let all = snapshot
        .by_date
        .values()
        .flat_map(|v| v.iter())
        .chain(snapshot.no_date.iter());
    for r in all {
        if let Some(idx) = r.language {
            *lang_counts.entry(idx).or_default() += 1;
        }
        if let Some(d) = r.domain {
            domains.insert(format!("{:?}", d).to_lowercase());
        }
        if let Some(t) = r.pr_type {
            pr_types.insert(format!("{:?}", t).to_lowercase());
        }
        if let Some(s) = r.severity {
            severities.insert(format!("{:?}", s).to_lowercase());
        }
    }

    let mut languages: Vec<String> = lang_counts
        .into_iter()
        .filter(|&(_, count)| count >= 5)
        .map(|(idx, _)| snapshot.languages[idx as usize].clone())
        .collect();
    languages.sort();

    let sorted = |s: HashSet<String>| -> Vec<String> {
        let mut v: Vec<_> = s.into_iter().collect();
        v.sort();
        v
    };

    FilterOptionsResponse {
        chatbots,
        languages,
        domains: sorted(domains),
        pr_types: sorted(pr_types),
        severities: sorted(severities),
        first_date: snapshot.by_date.keys().next().map(|d| d.to_string()),
        last_date: snapshot.by_date.keys().last().map(|d| d.to_string()),
    }
}
