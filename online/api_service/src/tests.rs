#[cfg(test)]
mod tests {
    use crate::compute::*;
    use crate::model::*;
    use chrono::{NaiveDate, TimeZone, Utc};
    use std::collections::BTreeMap;

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
            chatbot_idx,
            bot_reviewed_at: reviewed,
            precision: p,
            recall: r,
            diff_lines: None,
            language: None,
            domain: None,
            pr_type: None,
            severity: None,
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
}
