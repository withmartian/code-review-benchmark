#[cfg(test)]
mod tests {
    use crate::compute::*;
    use crate::model::*;
    use chrono::{NaiveDate, TimeZone, Utc};
    use std::collections::{BTreeMap, HashMap};
    use std::sync::atomic::{AtomicI64, Ordering};

    static NEXT_PR_ID: AtomicI64 = AtomicI64::new(1);

    /// Helper: build a minimal snapshot with given chatbots and languages.
    fn make_snapshot(
        chatbots: Vec<(&str, &str)>,
        languages: Vec<&str>,
        records: Vec<(NaiveDate, PrRecord)>,
    ) -> Snapshot {
        let chatbot_infos: Vec<ChatbotInfo> = chatbots
            .into_iter()
            .map(|(user, display)| ChatbotInfo {
                github_username: user.to_string(),
                display_name: display.to_string(),
                ignored: false,
            })
            .collect();
        let lang_strs: Vec<String> = languages.into_iter().map(|s| s.to_string()).collect();

        let mut by_date: BTreeMap<NaiveDate, Vec<PrRecord>> = BTreeMap::new();
        let mut no_date: Vec<PrRecord> = Vec::new();

        for (date, rec) in records {
            if rec.bot_reviewed_at.is_some() {
                by_date.entry(date).or_default().push(rec);
            } else {
                no_date.push(rec);
            }
        }

        Snapshot {
            by_date,
            no_date,
            chatbots: chatbot_infos,
            languages: lang_strs,
            volumes: BTreeMap::new(),
            repo_contributor_counts: HashMap::new(),
            author_repo_prs: HashMap::new(),
        }
    }

    fn date(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    fn dt(y: i32, m: u32, d: u32) -> Option<chrono::DateTime<Utc>> {
        Some(Utc.with_ymd_and_hms(y, m, d, 12, 0, 0).unwrap())
    }

    fn rec(chatbot_idx: u8, reviewed: Option<chrono::DateTime<Utc>>, p: Option<f32>, r: Option<f32>) -> PrRecord {
        PrRecord {
            pr_id: NEXT_PR_ID.fetch_add(1, Ordering::Relaxed),
            chatbot_idx,
            bot_reviewed_at: reviewed,
            precision: p,
            recall: r,
            diff_lines: None,
            language: None,
            domain: None,
            pr_type: None,
            severity: None,
            self_authored: false,
            has_reviews: true,
            pr_author_is_bot: false,
            repo_name_idx: 0,
            author_idx: 0,
            has_human_engagement: false,
            human_reviewer_count: 0,
            commits_after_review: 0,
        }
    }

    // -----------------------------------------------------------------------
    // f_beta tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_f_beta_standard() {
        // F1 with P=0.6, R=0.8 → 2*0.6*0.8/(0.6+0.8) = 0.96/1.4 ≈ 0.6857
        let result = f_beta(0.6, 0.8, 1.0).unwrap();
        assert!((result - 0.6857).abs() < 0.001, "got {result}");
    }

    #[test]
    fn test_f_beta_zero() {
        assert_eq!(f_beta(0.0, 0.0, 1.0), None);
    }

    #[test]
    fn test_f_beta_beta2() {
        // F2: (1+4)*P*R / (4*P + R) = 5*0.6*0.8/(2.4+0.8) = 2.4/3.2 = 0.75
        let result = f_beta(0.6, 0.8, 2.0).unwrap();
        assert!((result - 0.75).abs() < 0.001, "got {result}");
    }

    // -----------------------------------------------------------------------
    // apply_filters tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_filter_by_date_range() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.6), Some(0.6))),
                (date(2026, 2, 3), rec(0, dt(2026, 2, 3), Some(0.7), Some(0.7))),
            ],
        );
        let params = FilterParams {
            start_date: Some(date(2026, 2, 2)),
            end_date: Some(date(2026, 2, 2)),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert!((result.records[0].1.precision.unwrap() - 0.6).abs() < 0.001);
    }

    #[test]
    fn test_filter_by_chatbot() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7))),
            ],
        );
        let params = FilterParams {
            chatbots: Some(vec!["bot2".to_string()]),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert_eq!(result.records[0].1.chatbot_idx, 1);
    }

    #[test]
    fn test_filter_by_domain() {
        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r1.domain = Some(Domain::Backend);
        let mut r2 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2.domain = Some(Domain::Frontend);
        let mut r3 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3.domain = Some(Domain::Backend);

        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r2),
                (date(2026, 2, 1), r3),
            ],
        );
        let params = FilterParams {
            domains: Some(vec![Domain::Backend]),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 2);
    }

    #[test]
    fn test_filter_by_severity_multi() {
        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r1.severity = Some(Severity::Low);
        let mut r2 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2.severity = Some(Severity::High);
        let mut r3 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3.severity = Some(Severity::Critical);

        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r2),
                (date(2026, 2, 1), r3),
            ],
        );
        let params = FilterParams {
            severities: Some(vec![Severity::High, Severity::Critical]),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 2);
    }

    #[test]
    fn test_filter_by_diff_lines_range() {
        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r1.diff_lines = Some(50);
        let mut r2 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2.diff_lines = Some(500);
        let mut r3 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3.diff_lines = Some(3000);
        // r4 has no diff_lines — should pass (matches dashboard: None included)
        let r4 = rec(0, dt(2026, 2, 1), Some(0.8), Some(0.8));

        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r2),
                (date(2026, 2, 1), r3),
                (date(2026, 2, 1), r4),
            ],
        );
        let params = FilterParams {
            diff_lines_min: Some(100),
            diff_lines_max: Some(2000),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        // r1 (50) excluded by min, r2 (500) passes, r3 (3000) excluded by max, r4 (None) passes
        assert_eq!(result.records.len(), 2);
    }

    // -----------------------------------------------------------------------
    // daily_metrics tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_filter_excludes_none_precision() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), None, None)),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7))),
            ],
        );
        let params = FilterParams::default();
        let resp = daily_metrics(&snap, &params);
        // Only 2 records with precision, avg = (0.5+0.7)/2 = 0.6
        assert_eq!(resp.series.len(), 1);
        assert_eq!(resp.series[0].pr_count, 2);
        assert!((resp.series[0].avg_precision - 0.6).abs() < 0.001);
    }

    #[test]
    fn test_precision_present_recall_absent_counted() {
        // Records with precision but no recall should still be counted (matches pandas behavior)
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.8))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.7), None)),   // precision only
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.9), None)),   // precision only
            ],
        );
        let params = FilterParams::default();
        let resp = daily_metrics(&snap, &params);
        assert_eq!(resp.series.len(), 1);
        // pr_count should be 3 (all have precision)
        assert_eq!(resp.series[0].pr_count, 3);
        // avg_precision = (0.5+0.7+0.9)/3 = 0.7
        assert!((resp.series[0].avg_precision - 0.7).abs() < 0.001);
        // avg_recall = 0.8/1 = 0.8 (only 1 record has recall)
        assert!((resp.series[0].avg_recall - 0.8).abs() < 0.001);
    }

    #[test]
    fn test_daily_metrics_groups_by_date_and_chatbot() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.7), Some(0.7))),
                (date(2026, 2, 2), rec(1, dt(2026, 2, 2), Some(0.8), Some(0.8))),
            ],
        );
        let params = FilterParams::default();
        let resp = daily_metrics(&snap, &params);
        assert_eq!(resp.series.len(), 4); // 2 chatbots × 2 days
        assert_eq!(resp.chatbots.len(), 2);
    }

    #[test]
    fn test_daily_metrics_min_prs_per_day() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                // day 2 has only 1 PR
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.7), Some(0.7))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6))),
            ],
        );
        let params = FilterParams {
            min_prs_per_day: 2,
            ..Default::default()
        };
        let resp = daily_metrics(&snap, &params);
        // Only 2026-02-01 has 2 PRs, 2026-02-02 has 1 → dropped
        assert_eq!(resp.series.len(), 1);
        assert_eq!(resp.series[0].date, date(2026, 2, 1));
    }

    // -----------------------------------------------------------------------
    // leaderboard tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_leaderboard_aggregates_across_days() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.4), Some(0.6))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.6), Some(0.8))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.7), Some(0.9))),
            ],
        );
        let params = FilterParams::default();
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 2);

        // bot1: avg_p = (0.4+0.6)/2 = 0.5, avg_r = (0.6+0.8)/2 = 0.7
        let bot1 = resp.rows.iter().find(|r| r.chatbot == "bot1").unwrap();
        assert!((bot1.precision - 0.5).abs() < 0.001);
        assert!((bot1.recall - 0.7).abs() < 0.001);
        assert_eq!(bot1.sampled_prs, 2);
        assert_eq!(bot1.scored_prs, 2);

        // bot2: p=0.7, r=0.9
        let bot2 = resp.rows.iter().find(|r| r.chatbot == "bot2").unwrap();
        assert!((bot2.precision - 0.7).abs() < 0.001);
        assert_eq!(bot2.sampled_prs, 1);
        assert_eq!(bot2.scored_prs, 1);
    }

    #[test]
    fn test_no_date_range_returns_all() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 1, 1), rec(0, dt(2026, 1, 1), Some(0.5), Some(0.5))),
                (date(2026, 6, 1), rec(0, dt(2026, 6, 1), Some(0.5), Some(0.5))),
                (date(2026, 12, 1), rec(0, dt(2026, 12, 1), Some(0.5), Some(0.5))),
            ],
        );
        let params = FilterParams::default();
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 3);
    }

    #[test]
    fn test_filter_by_language() {
        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r1.language = Some(0); // rust
        let mut r2 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2.language = Some(1); // python
        let mut r3 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3.language = Some(0); // rust

        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec!["rust", "python"],
            vec![
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r2),
                (date(2026, 2, 1), r3),
            ],
        );
        let params = FilterParams {
            languages: Some(vec!["rust".to_string()]),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 2);
    }

    #[test]
    fn test_daily_metrics_with_label_filters() {
        // Simulate: 10 records for bot1, only 3 have severity=High AND domain=Backend
        let mut records = Vec::new();
        for i in 0..10u8 {
            let mut r = rec(0, dt(2026, 2, 16), Some(0.5 + i as f32 * 0.01), Some(0.5));
            if i < 3 {
                r.domain = Some(Domain::Backend);
                r.severity = Some(Severity::High);
            } else if i < 6 {
                r.domain = Some(Domain::Frontend);
                r.severity = Some(Severity::Low);
            } else {
                // No labels
            }
            records.push((date(2026, 2, 16), r));
        }

        let snap = make_snapshot(
            vec![("gemini-code-assist[bot]", "Gemini")],
            vec![],
            records,
        );

        // No filters → all 10
        let params_all = FilterParams::default();
        let resp_all = daily_metrics(&snap, &params_all);
        assert_eq!(resp_all.series.len(), 1);
        assert_eq!(resp_all.series[0].pr_count, 10);

        // With severity=High AND domain=Backend → only 3
        let params_filtered = FilterParams {
            domains: Some(vec![Domain::Backend]),
            severities: Some(vec![Severity::High]),
            ..Default::default()
        };
        let resp_filtered = daily_metrics(&snap, &params_filtered);
        assert_eq!(resp_filtered.series.len(), 1);
        assert_eq!(resp_filtered.series[0].pr_count, 3, "should only count records with domain=Backend AND severity=High");
    }

    // -----------------------------------------------------------------------
    // Quality filter tests: exclude_bot_authored, min_repo_contributors,
    //                       max_author_repo_prs
    // -----------------------------------------------------------------------

    /// Build a snapshot that populates the aggregate maps needed for quality filters.
    fn make_quality_snapshot(
        chatbots: Vec<(&str, &str)>,
        records: Vec<(NaiveDate, PrRecord)>,
        repo_contributor_counts: HashMap<u32, u32>,
        author_repo_prs: HashMap<(u32, u32, u8), Vec<i64>>,
    ) -> Snapshot {
        let chatbot_infos: Vec<ChatbotInfo> = chatbots
            .into_iter()
            .map(|(user, display)| ChatbotInfo {
                github_username: user.to_string(),
                display_name: display.to_string(),
                ignored: false,
            })
            .collect();

        let mut by_date: BTreeMap<NaiveDate, Vec<PrRecord>> = BTreeMap::new();
        for (date, rec) in records {
            by_date.entry(date).or_default().push(rec);
        }

        Snapshot {
            by_date,
            no_date: Vec::new(),
            chatbots: chatbot_infos,
            languages: Vec::new(),
            volumes: BTreeMap::new(),
            repo_contributor_counts,
            author_repo_prs,
        }
    }

    #[test]
    fn test_exclude_bot_authored() {
        let mut r_human = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r_human.pr_author_is_bot = false;

        let mut r_bot = rec(0, dt(2026, 2, 1), Some(0.8), Some(0.8));
        r_bot.pr_author_is_bot = true;

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r_human),
                (date(2026, 2, 1), r_bot),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        // Without filter: both included
        let params_off = FilterParams::default();
        assert_eq!(apply_filters(&snap, &params_off).records.len(), 2);

        // With filter: bot-authored excluded
        let params_on = FilterParams {
            exclude_bot_authored: true,
            ..Default::default()
        };
        let result = apply_filters(&snap, &params_on);
        assert_eq!(result.records.len(), 1);
        assert!(!result.records[0].1.pr_author_is_bot);
    }

    #[test]
    fn test_min_repo_contributors() {
        // repo 0 = solo contributor, repo 1 = 3 contributors
        let mut r_solo = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r_solo.repo_name_idx = 0;

        let mut r_diverse = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r_diverse.repo_name_idx = 1;

        let mut repo_counts = HashMap::new();
        repo_counts.insert(0u32, 1u32); // solo
        repo_counts.insert(1u32, 3u32); // 3 contributors

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r_solo),
                (date(2026, 2, 1), r_diverse),
            ],
            repo_counts,
            HashMap::new(),
        );

        // No filter: both included
        assert_eq!(apply_filters(&snap, &FilterParams::default()).records.len(), 2);

        // min_repo_contributors=2: solo repo excluded
        let params = FilterParams {
            min_repo_contributors: Some(2),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert_eq!(result.records[0].1.repo_name_idx, 1);

        // min_repo_contributors=4: both excluded
        let params_high = FilterParams {
            min_repo_contributors: Some(4),
            ..Default::default()
        };
        assert_eq!(apply_filters(&snap, &params_high).records.len(), 0);
    }

    #[test]
    fn test_max_author_repo_prs_samples_n() {
        // Create 10 records for author 0 in repo 0, cap at 5 → exactly 5 sampled
        let mut records = Vec::new();
        let mut pr_ids = Vec::new();
        for _ in 0..10 {
            let mut r = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
            r.repo_name_idx = 0;
            r.author_idx = 0;
            pr_ids.push(r.pr_id);
            records.push((date(2026, 2, 1), r));
        }

        let mut author_repo_prs = HashMap::new();
        author_repo_prs.insert((0u32, 0u32, 0u8), pr_ids);

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            records,
            HashMap::new(),
            author_repo_prs,
        );

        // No filter: all 10 included
        assert_eq!(apply_filters(&snap, &FilterParams::default()).records.len(), 10);

        // max_author_repo_prs=5: exactly 5 randomly sampled
        let params = FilterParams {
            max_author_repo_prs: Some(5),
            ..Default::default()
        };
        assert_eq!(apply_filters(&snap, &params).records.len(), 5);

        // max_author_repo_prs=20: all 10 pass (under cap)
        let params_high = FilterParams {
            max_author_repo_prs: Some(20),
            ..Default::default()
        };
        assert_eq!(apply_filters(&snap, &params_high).records.len(), 10);
    }

    #[test]
    fn test_max_author_repo_prs_under_cap_passes_all() {
        // 3 records, cap at 5 → all pass
        let mut records = Vec::new();
        let mut pr_ids = Vec::new();
        for _ in 0..3 {
            let mut r = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
            r.repo_name_idx = 0;
            r.author_idx = 1;
            pr_ids.push(r.pr_id);
            records.push((date(2026, 2, 1), r));
        }

        let mut author_repo_prs = HashMap::new();
        author_repo_prs.insert((0u32, 1u32, 0u8), pr_ids);

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            records,
            HashMap::new(),
            author_repo_prs,
        );

        let params = FilterParams {
            max_author_repo_prs: Some(5),
            ..Default::default()
        };
        assert_eq!(apply_filters(&snap, &params).records.len(), 3);
    }

    #[test]
    fn test_max_author_repo_prs_unknown_author() {
        // author_idx = u32::MAX means unknown author — should not be filtered
        let mut r_unknown = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r_unknown.author_idx = u32::MAX;

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![(date(2026, 2, 1), r_unknown)],
            HashMap::new(),
            HashMap::new(),
        );

        let params = FilterParams {
            max_author_repo_prs: Some(10),
            ..Default::default()
        };
        // Unknown author should pass (not in sampled set check is skipped)
        assert_eq!(apply_filters(&snap, &params).records.len(), 1);
    }

    #[test]
    fn test_combined_quality_filters() {
        // Test all three quality filters together
        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r1.pr_author_is_bot = false;
        r1.repo_name_idx = 0;
        r1.author_idx = 0;

        let mut r2_bot = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2_bot.pr_author_is_bot = true;
        r2_bot.repo_name_idx = 0;
        r2_bot.author_idx = 1;

        let mut r3_solo = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3_solo.pr_author_is_bot = false;
        r3_solo.repo_name_idx = 1; // solo repo
        r3_solo.author_idx = 2;

        let mut r4_concentrated = rec(0, dt(2026, 2, 1), Some(0.8), Some(0.8));
        r4_concentrated.pr_author_is_bot = false;
        r4_concentrated.repo_name_idx = 0;
        r4_concentrated.author_idx = 3;

        // Build author_repo_prs: author 3 has 200 pr_ids (only 1 record in snapshot, but
        // 200 entries in the map so the triple exceeds cap=50 and this record's pr_id
        // has only a 50/200 = 25% chance of being sampled — we include 200 dummy IDs
        // plus the real record's ID is NOT guaranteed to be sampled, so we create a
        // separate set where the record's pr_id is absent to ensure deterministic test).
        let r4_id = r4_concentrated.pr_id;
        let mut author_repo_prs = HashMap::new();
        // r1's triple: 10 PRs (under cap of 50)
        let mut r1_prs = vec![r1.pr_id];
        r1_prs.extend(10_000..10_009); // 9 dummy IDs
        author_repo_prs.insert((0u32, 0u32, 0u8), r1_prs);
        // r2's triple: 5 PRs
        author_repo_prs.insert((0u32, 1u32, 0u8), vec![r2_bot.pr_id, 20_000, 20_001, 20_002, 20_003]);
        // r3's triple: 3 PRs
        author_repo_prs.insert((1u32, 2u32, 0u8), vec![r3_solo.pr_id, 30_000, 30_001]);
        // r4's triple: 200 dummy IDs that do NOT include r4_id → r4 will never be sampled
        let concentrated_prs: Vec<i64> = (40_000..40_200).collect();
        author_repo_prs.insert((0u32, 3u32, 0u8), concentrated_prs);
        // Verify the real pr_id is excluded from the map
        assert!(!author_repo_prs[&(0u32, 3u32, 0u8)].contains(&r4_id));

        let mut repo_counts = HashMap::new();
        repo_counts.insert(0u32, 5u32);
        repo_counts.insert(1u32, 1u32); // solo

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r2_bot),
                (date(2026, 2, 1), r3_solo),
                (date(2026, 2, 1), r4_concentrated),
            ],
            repo_counts,
            author_repo_prs,
        );

        let params = FilterParams {
            exclude_bot_authored: true,
            min_repo_contributors: Some(2),
            max_author_repo_prs: Some(50),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        // r1: human, diverse repo, 10 PRs (under cap) → passes
        // r2: bot-authored → excluded by exclude_bot_authored
        // r3: solo repo → excluded by min_repo_contributors
        // r4: 200 PRs and r4's pr_id not in the sampled set → excluded
        assert_eq!(result.records.len(), 1);
        assert_eq!(result.records[0].1.author_idx, 0);
    }

    #[test]
    fn test_quality_filters_affect_leaderboard() {
        // Bot-authored PRs inflate bot1's score; filtering them out changes the average
        let mut r_human = rec(0, dt(2026, 2, 1), Some(0.4), Some(0.4));
        r_human.pr_author_is_bot = false;

        let mut r_bot_high = rec(0, dt(2026, 2, 1), Some(0.9), Some(0.9));
        r_bot_high.pr_author_is_bot = true;

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r_human),
                (date(2026, 2, 1), r_bot_high),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        // Without filter: avg precision = (0.4 + 0.9) / 2 = 0.65
        let resp_all = leaderboard(&snap, &FilterParams::default());
        assert!((resp_all.rows[0].precision - 0.65).abs() < 0.001);

        // With filter: avg precision = 0.4 only
        let params = FilterParams {
            exclude_bot_authored: true,
            ..Default::default()
        };
        let resp_filtered = leaderboard(&snap, &params);
        assert!((resp_filtered.rows[0].precision - 0.4).abs() < 0.001);
        assert_eq!(resp_filtered.rows[0].sampled_prs, 1);
    }

    // -----------------------------------------------------------------------
    // Engagement filter tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_require_human_engagement() {
        let mut r_engaged = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r_engaged.has_human_engagement = true;
        r_engaged.human_reviewer_count = 1;

        let r_no_engage = rec(0, dt(2026, 2, 1), Some(0.8), Some(0.8));
        // defaults: has_human_engagement = false

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r_engaged),
                (date(2026, 2, 1), r_no_engage),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        // Without filter: both included
        assert_eq!(apply_filters(&snap, &FilterParams::default()).records.len(), 2);

        // With filter: only engaged PR passes
        let params = FilterParams {
            require_human_engagement: true,
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert!(result.records[0].1.has_human_engagement);
    }

    #[test]
    fn test_min_human_reviewers() {
        let mut r0 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r0.human_reviewer_count = 0;

        let mut r1 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r1.human_reviewer_count = 1;

        let mut r3 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r3.human_reviewer_count = 3;

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r0),
                (date(2026, 2, 1), r1),
                (date(2026, 2, 1), r3),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        let params = FilterParams {
            min_human_reviewers: Some(2),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert_eq!(result.records[0].1.human_reviewer_count, 3);
    }

    #[test]
    fn test_min_commits_after_review() {
        let mut r0 = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));
        r0.commits_after_review = 0;

        let mut r2 = rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6));
        r2.commits_after_review = 2;

        let mut r5 = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r5.commits_after_review = 5;

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r0),
                (date(2026, 2, 1), r2),
                (date(2026, 2, 1), r5),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        let params = FilterParams {
            min_commits_after_review: Some(3),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        assert_eq!(result.records.len(), 1);
        assert_eq!(result.records[0].1.commits_after_review, 5);
    }

    // -----------------------------------------------------------------------
    // min_scored_prs tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_min_scored_prs_hides_low_count_bot_from_leaderboard() {
        // bot1: 5 scored PRs, bot2: 2 scored PRs
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.5), Some(0.5))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.6), Some(0.6))),
                (date(2026, 2, 3), rec(0, dt(2026, 2, 3), Some(0.7), Some(0.7))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.8), Some(0.8))),
                (date(2026, 2, 2), rec(1, dt(2026, 2, 2), Some(0.9), Some(0.9))),
            ],
        );

        // No filter: both bots appear
        let resp = leaderboard(&snap, &FilterParams::default());
        assert_eq!(resp.rows.len(), 2);

        // min_scored_prs=3: bot2 (2 scored) hidden, bot1 (5 scored) remains
        let params = FilterParams {
            min_scored_prs: 3,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 1);
        assert_eq!(resp.rows[0].chatbot, "bot1");
        assert_eq!(resp.rows[0].scored_prs, 5);
    }

    #[test]
    fn test_min_scored_prs_zero_shows_all() {
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.8), Some(0.8))),
            ],
        );

        let params = FilterParams {
            min_scored_prs: 0,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 2);
    }

    #[test]
    fn test_min_scored_prs_counts_only_fully_scored() {
        // bot1 has 3 records: 2 with both P+R, 1 with only P (no recall)
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.7), None)),
            ],
        );

        // min_scored_prs=3: bot1 has only 2 scored (the one without recall doesn't count)
        let params = FilterParams {
            min_scored_prs: 3,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 0);

        // min_scored_prs=2: bot1 has exactly 2 scored → passes
        let params = FilterParams {
            min_scored_prs: 2,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 1);
    }

    #[test]
    fn test_min_scored_prs_hides_bot_from_daily_metrics() {
        // bot1: 4 scored PRs across 2 days, bot2: 1 scored PR
        let snap = make_snapshot(
            vec![("bot1", "Bot One"), ("bot2", "Bot Two")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.7), Some(0.7))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.8), Some(0.8))),
                (date(2026, 2, 1), rec(1, dt(2026, 2, 1), Some(0.9), Some(0.9))),
            ],
        );

        // No filter: bot1 has 2 day entries, bot2 has 1 → 3 total
        let resp = daily_metrics(&snap, &FilterParams::default());
        assert_eq!(resp.series.len(), 3);

        // min_scored_prs=2: bot2 (1 scored) excluded from chart
        let params = FilterParams {
            min_scored_prs: 2,
            ..Default::default()
        };
        let resp = daily_metrics(&snap, &params);
        assert_eq!(resp.series.len(), 2);
        assert!(resp.series.iter().all(|r| r.chatbot == "bot1"));
    }

    #[test]
    fn test_min_scored_prs_with_min_prs_per_day() {
        // bot1: day1 has 3 scored, day2 has 1 scored
        // min_prs_per_day=2 drops day2, so only day1's 3 count as scored
        // min_scored_prs=4 should then hide bot1 since effective scored = 3
        let snap = make_snapshot(
            vec![("bot1", "Bot One")],
            vec![],
            vec![
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.6), Some(0.6))),
                (date(2026, 2, 1), rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7))),
                (date(2026, 2, 2), rec(0, dt(2026, 2, 2), Some(0.8), Some(0.8))),
            ],
        );

        // min_prs_per_day=2, min_scored_prs=4: day2 dropped (1 pr), remaining scored=3 < 4
        let params = FilterParams {
            min_prs_per_day: 2,
            min_scored_prs: 4,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 0);

        // min_prs_per_day=2, min_scored_prs=3: day2 dropped, remaining scored=3 >= 3
        let params = FilterParams {
            min_prs_per_day: 2,
            min_scored_prs: 3,
            ..Default::default()
        };
        let resp = leaderboard(&snap, &params);
        assert_eq!(resp.rows.len(), 1);
        assert_eq!(resp.rows[0].scored_prs, 3);
    }

    #[test]
    fn test_engagement_filters_combined() {
        let mut r_full = rec(0, dt(2026, 2, 1), Some(0.9), Some(0.9));
        r_full.has_human_engagement = true;
        r_full.human_reviewer_count = 2;
        r_full.commits_after_review = 3;

        let mut r_partial = rec(0, dt(2026, 2, 1), Some(0.7), Some(0.7));
        r_partial.has_human_engagement = true;
        r_partial.human_reviewer_count = 0;
        r_partial.commits_after_review = 1;

        let r_none = rec(0, dt(2026, 2, 1), Some(0.5), Some(0.5));

        let snap = make_quality_snapshot(
            vec![("bot1", "Bot One")],
            vec![
                (date(2026, 2, 1), r_full),
                (date(2026, 2, 1), r_partial),
                (date(2026, 2, 1), r_none),
            ],
            HashMap::new(),
            HashMap::new(),
        );

        let params = FilterParams {
            require_human_engagement: true,
            min_human_reviewers: Some(1),
            min_commits_after_review: Some(2),
            ..Default::default()
        };
        let result = apply_filters(&snap, &params);
        // Only r_full passes all three
        assert_eq!(result.records.len(), 1);
        assert!((result.records[0].1.precision.unwrap() - 0.9).abs() < 0.001);
    }
}
