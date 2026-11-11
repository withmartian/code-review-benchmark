# Extraction scope: measurement details

Supporting detail for the step 1 extraction change (`EXTRACT_BOT_SUGGESTIONS`, version `extract-v2`).
The per-PR metrics behind the tables are in `extraction_scope_results.jsonl` next to this file
(public PR URLs, bot, arm, counts, precision, recall and per-type item counts; no review text), and
`extraction_scope_prs.csv` lists the PRs and bots. Judge `gpt-5.2-2025-12-11` at temperature 1.0;
intervals are 95% paired PR bootstrap (2,000 resamples) against the current prompt's first draw.

Arms: the current prompt (upstream main, two draws); an earlier revision `cf4460fb` that also
excluded remarks about the PR itself (two draws); three previous heads of this PR, one draw each:
`cee5e6cc` (coverage tie-break), `093c763a` (priority and impact named as ratings; the located
coverage request named in the aggregate sentence), `a2fcbe51` (a long resolved-findings sentence);
and the head, one draw. The results file maps each arm to its prompt sha256.

## Per bot, every arm

| bot (PRs) | arm | suggestions/PR | matched/PR | P | R | PRs with P and R | dP vs current | dR vs current | d suggestions/PR |
|---|---|---|---|---|---|---|---|---|---|
| CodeRabbit (313) | current | 3.96 | 1.76 | 62.4 | 49.1 | 232 |  |  |  |
|  | current, 2nd draw | 3.93 | 1.78 | 63.7 | 49.2 | 232 | +1.3 [-0.2, +2.6] | +0.1 [-1.5, +1.6] | -0.03 [-0.11, +0.05] |
|  | earlier revision | 3.44 | 1.74 | 71.2 | 47.8 | 232 | +8.8 [+6.2, +11.5] | -1.3 [-3.2, +0.3] | -0.52 [-0.61, -0.42] |
|  | earlier revision, 2nd draw | 3.49 | 1.78 | 71.3 | 49.1 | 232 | +8.9 [+6.4, +11.4] | -0.0 [-1.8, +1.5] | -0.46 [-0.55, -0.38] |
|  | previous revision cee5e6cc | 3.53 | 1.76 | 70.1 | 48.8 | 232 | +7.7 [+5.1, +10.4] | -0.3 [-1.7, +0.9] | -0.43 [-0.52, -0.34] |
|  | previous revision 093c763a | 3.50 | 1.72 | 70.8 | 47.5 | 232 | +8.4 [+5.8, +11.2] | -1.6 [-3.4, +0.0] | -0.46 [-0.55, -0.37] |
|  | previous revision a2fcbe51 | 3.49 | 1.73 | 70.6 | 47.6 | 232 | +8.2 [+5.5, +10.9] | -1.5 [-3.3, +0.2] | -0.46 [-0.55, -0.38] |
|  | **this PR (head)** | 3.50 | 1.76 | 70.0 | 48.5 | 232 | +7.6 [+5.2, +10.1] | -0.6 [-2.3, +0.8] | -0.46 [-0.55, -0.37] |
| Greptile (164) | current | 2.29 | 1.92 | 85.0 | 33.5 | 157 |  |  |  |
|  | current, 2nd draw | 2.28 | 1.90 | 83.7 | 33.8 | 157 | -1.2 [-3.9, +1.1] | +0.3 [-0.7, +1.6] | -0.01 [-0.05, +0.03] |
|  | earlier revision | 2.32 | 1.92 | 83.2 | 33.4 | 157 | -1.8 [-4.0, +0.1] | -0.0 [-0.8, +0.9] | +0.04 [+0.00, +0.07] |
|  | earlier revision, 2nd draw | 2.30 | 1.93 | 84.4 | 34.1 | 157 | -0.6 [-2.5, +1.0] | +0.6 [-0.3, +1.8] | +0.02 [-0.02, +0.05] |
|  | previous revision cee5e6cc | 2.30 | 1.92 | 84.8 | 33.8 | 157 | -0.2 [-2.3, +1.8] | +0.3 [-0.5, +1.2] | +0.01 [-0.02, +0.05] |
|  | previous revision 093c763a | 2.32 | 1.93 | 83.2 | 34.0 | 157 | -1.8 [-3.8, -0.3] | +0.5 [-0.4, +1.7] | +0.04 [-0.01, +0.09] |
|  | previous revision a2fcbe51 | 2.32 | 1.96 | 84.1 | 34.2 | 157 | -0.9 [-3.0, +0.9] | +0.8 [-0.1, +1.9] | +0.04 [-0.01, +0.09] |
|  | **this PR (head)** | 2.34 | 1.96 | 84.0 | 34.2 | 157 | -1.0 [-3.7, +1.4] | +0.8 [-0.1, +1.8] | +0.05 [+0.02, +0.10] |
| cubic (109) | current | 6.92 | 4.07 | 69.1 | 52.7 | 103 |  |  |  |
|  | current, 2nd draw | 6.77 | 3.93 | 69.0 | 52.3 | 103 | -0.1 [-1.8, +1.6] | -0.4 [-1.6, +0.7] | -0.15 [-0.38, +0.01] |
|  | earlier revision | 6.76 | 4.11 | 70.2 | 54.6 | 103 | +1.1 [-0.2, +2.7] | +1.9 [-0.0, +4.7] | -0.16 [-0.42, +0.04] |
|  | earlier revision, 2nd draw | 6.83 | 4.07 | 69.8 | 53.2 | 103 | +0.7 [-0.5, +2.2] | +0.5 [-0.7, +1.8] | -0.08 [-0.29, +0.04] |
|  | previous revision cee5e6cc | 6.83 | 3.95 | 68.9 | 53.3 | 103 | -0.2 [-1.7, +1.3] | +0.6 [-1.2, +3.3] | -0.09 [-0.30, +0.04] |
|  | previous revision 093c763a | 6.85 | 4.01 | 68.6 | 53.3 | 103 | -0.5 [-1.5, +0.3] | +0.6 [-1.3, +3.1] | -0.06 [-0.16, +0.01] |
|  | previous revision a2fcbe51 | 6.93 | 4.12 | 70.1 | 53.2 | 103 | +1.0 [-0.3, +2.4] | +0.4 [-0.7, +1.6] | +0.01 [-0.06, +0.07] |
|  | **this PR (head)** | 6.82 | 4.10 | 69.8 | 54.1 | 103 | +0.7 [-0.3, +1.7] | +1.4 [-0.3, +3.8] | -0.10 [-0.34, +0.05] |
| Claude (66) | current | 5.44 | 2.32 | 53.2 | 41.9 | 58 |  |  |  |
|  | current, 2nd draw | 5.44 | 2.35 | 51.9 | 42.7 | 59 | -1.2 [-5.9, +2.4] | +0.8 [-3.2, +5.9] | +0.00 [-0.21, +0.21] |
|  | earlier revision | 5.29 | 2.38 | 53.9 | 43.6 | 57 | +0.7 [-2.9, +3.9] | +1.7 [-2.1, +5.4] | -0.15 [-0.33, +0.02] |
|  | earlier revision, 2nd draw | 5.35 | 2.30 | 53.5 | 43.9 | 57 | +0.3 [-4.1, +5.5] | +2.0 [-2.5, +7.5] | -0.09 [-0.29, +0.09] |
|  | previous revision cee5e6cc | 5.29 | 2.32 | 54.1 | 41.5 | 58 | +0.9 [-0.4, +2.3] | -0.4 [-2.7, +1.5] | -0.15 [-0.27, -0.05] |
|  | previous revision 093c763a | 5.39 | 2.35 | 54.0 | 43.0 | 58 | +0.8 [-1.4, +3.2] | +1.1 [-2.1, +4.3] | -0.05 [-0.17, +0.06] |
|  | previous revision a2fcbe51 | 5.35 | 2.32 | 55.1 | 42.8 | 58 | +1.9 [-0.4, +4.7] | +0.9 [-0.8, +2.7] | -0.09 [-0.24, +0.06] |
|  | **this PR (head)** | 5.47 | 2.26 | 52.0 | 40.8 | 58 | -1.2 [-5.6, +2.6] | -1.2 [-4.2, +1.6] | +0.03 [-0.09, +0.15] |
| Qodo (13) | current | 2.38 | 1.46 | 68.1 | 48.6 | 8 |  |  |  |
|  | current, 2nd draw | 2.46 | 1.38 | 59.3 | 32.0 | 9 | -8.8 [-26.7, +0.0] | -16.5 [-45.4, +0.0] | +0.08 [+0.00, +0.23] |
|  | earlier revision | 1.85 | 1.31 | 85.7 | 39.6 | 7 | +17.7 [-1.4, +44.1] | -8.9 [-40.0, +16.3] | -0.54 [-1.00, -0.15] |
|  | earlier revision, 2nd draw | 2.08 | 1.38 | 78.1 | 41.2 | 7 | +10.0 [-3.2, +33.9] | -7.3 [-40.0, +17.1] | -0.31 [-0.69, +0.00] |
|  | previous revision cee5e6cc | 2.15 | 1.31 | 72.5 | 34.7 | 8 | +4.4 [-3.5, +14.3] | -13.9 [-42.2, +0.0] | -0.23 [-0.62, +0.00] |
|  | previous revision 093c763a | 1.92 | 1.31 | 82.9 | 39.6 | 7 | +14.8 [-1.9, +40.0] | -8.9 [-40.0, +16.3] | -0.46 [-0.85, -0.08] |
|  | previous revision a2fcbe51 | 2.46 | 1.38 | 78.1 | 41.2 | 7 | +10.0 [-3.2, +33.9] | -7.3 [-40.0, +17.1] | +0.08 [-0.31, +0.69] |
|  | **this PR (head)** | 2.23 | 1.38 | 76.2 | 41.2 | 7 | +8.1 [-4.1, +31.5] | -7.3 [-40.0, +17.1] | -0.15 [-0.38, +0.00] |
| Copilot (18) | current | 2.78 | 1.89 | 81.7 | 50.0 | 15 |  |  |  |
|  | current, 2nd draw | 2.78 | 1.89 | 81.7 | 50.0 | 15 | +0.1 [-1.7, +1.8] | +0.0 [+0.0, +0.0] | +0.00 [+0.00, +0.00] |
|  | earlier revision | 2.83 | 1.94 | 81.4 | 50.0 | 15 | -0.3 [-3.0, +1.9] | +0.0 [+0.0, +0.0] | +0.06 [+0.00, +0.17] |
|  | earlier revision, 2nd draw | 2.78 | 1.72 | 79.8 | 47.7 | 15 | -1.9 [-4.7, +0.0] | -2.2 [-7.1, +0.0] | +0.00 [+0.00, +0.00] |
|  | previous revision cee5e6cc | 2.83 | 1.78 | 79.4 | 47.7 | 15 | -2.3 [-4.8, +0.0] | -2.2 [-7.1, +0.0] | +0.06 [+0.00, +0.17] |
|  | previous revision 093c763a | 2.83 | 2.00 | 82.7 | 50.4 | 15 | +1.0 [+0.0, +2.5] | +0.5 [+0.0, +1.5] | +0.06 [+0.00, +0.17] |
|  | previous revision a2fcbe51 | 2.83 | 1.89 | 80.6 | 48.2 | 15 | -1.1 [-3.8, +1.2] | -1.7 [-6.7, +1.3] | +0.06 [+0.00, +0.17] |
|  | **this PR (head)** | 2.83 | 1.78 | 79.4 | 48.2 | 15 | -2.3 [-4.8, +0.0] | -1.7 [-6.7, +1.3] | +0.06 [+0.00, +0.17] |
| Codex (25) | current | 1.64 | 1.28 | 81.1 | 38.4 | 17 |  |  |  |
|  | current, 2nd draw | 1.68 | 1.24 | 75.2 | 35.5 | 17 | -5.9 [-22.8, +9.9] | -2.9 [-11.4, +4.7] | +0.04 [-0.12, +0.24] |
|  | earlier revision | 1.56 | 1.20 | 79.9 | 36.1 | 16 | -1.2 [-17.6, +13.3] | -2.3 [-10.5, +5.1] | -0.08 [-0.20, +0.00] |
|  | earlier revision, 2nd draw | 1.60 | 1.28 | 85.1 | 40.0 | 16 | +4.0 [-3.3, +15.3] | +1.6 [-1.9, +7.3] | -0.04 [-0.12, +0.00] |
|  | previous revision cee5e6cc | 1.60 | 1.28 | 86.1 | 40.8 | 16 | +5.1 [+0.0, +16.1] | +2.4 [+0.0, +7.8] | -0.04 [-0.12, +0.00] |
|  | previous revision 093c763a | 1.56 | 1.28 | 86.6 | 39.3 | 16 | +5.5 [+0.0, +16.4] | +0.8 [-4.2, +6.7] | -0.08 [-0.20, +0.00] |
|  | previous revision a2fcbe51 | 1.64 | 1.28 | 81.5 | 36.9 | 17 | +0.5 [-13.8, +14.2] | -1.5 [-8.8, +5.1] | +0.00 [-0.16, +0.24] |
|  | **this PR (head)** | 1.56 | 1.24 | 86.1 | 39.3 | 16 | +5.1 [+0.0, +16.1] | +0.8 [-4.2, +6.7] | -0.08 [-0.20, +0.00] |
| six other bots pooled (395) | current | 4.08 | 2.52 | 74.6 | 41.6 | 358 |  |  |  |
|  | current, 2nd draw | 4.04 | 2.48 | 73.2 | 41.3 | 360 | -1.4 [-3.1, +0.2] | -0.4 [-1.6, +0.8] | -0.04 [-0.11, +0.02] |
|  | earlier revision | 4.00 | 2.54 | 74.6 | 42.2 | 355 | +0.0 [-1.4, +1.5] | +0.5 [-0.6, +1.8] | -0.07 [-0.15, -0.01] |
|  | earlier revision, 2nd draw | 4.03 | 2.52 | 74.9 | 42.2 | 355 | +0.3 [-1.0, +1.7] | +0.6 [-0.6, +1.7] | -0.04 [-0.11, +0.01] |
|  | previous revision cee5e6cc | 4.02 | 2.48 | 74.8 | 41.6 | 357 | +0.2 [-0.9, +1.3] | -0.0 [-1.1, +1.0] | -0.05 [-0.11, -0.01] |
|  | previous revision 093c763a | 4.05 | 2.52 | 74.3 | 42.1 | 356 | -0.2 [-1.3, +0.8] | +0.4 [-0.7, +1.6] | -0.03 [-0.07, +0.01] |
|  | previous revision a2fcbe51 | 4.08 | 2.55 | 74.9 | 41.9 | 357 | +0.4 [-0.9, +1.7] | +0.3 [-0.7, +1.2] | +0.01 [-0.03, +0.05] |
|  | **this PR (head)** | 4.07 | 2.53 | 74.4 | 42.0 | 356 | -0.2 [-1.7, +1.2] | +0.4 [-0.7, +1.4] | -0.01 [-0.07, +0.05] |

## Two precision figures

The leaderboard averages PRs with both precision and recall; the daily series averages every PR with
a precision. For CodeRabbit they differ (81 of the 313 PRs have no human actions); for the six other
bots every scored PR has both.

| CodeRabbit (313 PRs) | daily-series P (PRs with P) | d vs current | leaderboard P (PRs with P and R) | d vs current |
|---|---|---|---|---|
| current | 46.3 (313) |  | 62.4 (232) |  |
| current, 2nd draw | 47.2 (313) | +0.9 [-0.1, +1.9] | 63.7 (232) | +1.3 [-0.2, +2.6] |
| earlier revision | 52.8 (313) | +6.5 [+4.6, +8.6] | 71.2 (232) | +8.8 [+6.2, +11.5] |
| earlier revision, 2nd draw | 52.8 (313) | +6.6 [+4.7, +8.5] | 71.3 (232) | +8.9 [+6.4, +11.4] |
| previous revision cee5e6cc | 51.9 (313) | +5.7 [+3.7, +7.8] | 70.1 (232) | +7.7 [+5.1, +10.4] |
| previous revision 093c763a | 52.5 (313) | +6.3 [+4.2, +8.3] | 70.8 (232) | +8.4 [+5.8, +11.2] |
| previous revision a2fcbe51 | 52.3 (313) | +6.1 [+4.0, +8.2] | 70.6 (232) | +8.2 [+5.5, +10.9] |
| **this PR (head)** | 51.9 (313) | +5.7 [+3.8, +7.6] | 70.0 (232) | +7.6 [+5.2, +10.1] |

## CodeRabbit items by type

Over the 313 PRs, matched in parentheses; types by fixed text patterns, then read by hand. The first
two rows can be recomputed from the results file (`items_coverage_check`, `items_other_check` and
their `matched_items_` counterparts); the rest also needs the review bodies.

| item type | current | current, 2nd draw | earlier revision | earlier revision, 2nd draw | cee5e6cc | 093c763a | a2fcbe51 | this PR (head) |
|---|---|---|---|---|---|---|---|---|
| coverage-threshold gate | 124 (1) | 116 (2) | 0 | 0 | 0 | 0 | 0 | 1 (0) |
| title / description / linked-issue check | 18 (1) | 18 (0) | 3 (1) | 4 (1) | 22 (0) | 19 (0) | 19 (2) | 21 (1) |
| quoted tool output | 2 (0) | 3 (0) | 0 | 0 | 0 | 0 | 0 | 0 |
| run status | 0 | 1 (0) | 0 | 0 | 0 | 0 | 0 | 0 |
| other unlocated issue-comment item | 10 (2) | 19 (7) | 14 (4) | 13 (4) | 13 (3) | 10 (4) | 9 (2) | 13 (5) |
| code findings | 1084 (547) | 1072 (549) | 1059 (541) | 1076 (552) | 1069 (549) | 1065 (534) | 1065 (539) | 1059 (546) |
| posted inline comments still extracted | 789/797 | 787/797 | 790/797 | 792/797 | 790/797 | 789/797 | 791/797 | 791/797 |
| collapsed review-body findings still extracted | 154/175 | 156/175 | 155/175 | 155/175 | 158/175 | 156/175 | 154/175 | 157/175 |

## Other tracked bots: step 1 only

For each remaining tracked bot with at least 10 public PRs found (merged 2026-10-05 to 11-06, one PR
per repository, co-reviewed with CodeRabbit), step 1 alone, once per prompt: suggestions per PR over
the PRs with bot review output. Commit diffs are not passed. The head moves suggestions per PR by
-0.18 to +0.33 against the current prompt (one draw per prompt, so small
shifts are expected noise). Of the 19 tracked bots not in the full table, 4 had no public PR
co-reviewed with CodeRabbit in this window (`kiloconnect[bot]`, `propel-code-bot[bot]`,
`mesa-dot-dev[bot]`, `linearb[bot]`) and `bito-code-review[bot]` had 6 with no review output.

| bot | PRs with bot review output | current | earlier revision | cee5e6cc | 093c763a | a2fcbe51 | this PR (head) |
|---|---|---|---|---|---|---|---|
| augmentcode[bot] | 15 | 0.60 | 0.53 | 0.67 | 0.67 | 0.67 | 0.67 |
| baz-reviewer[bot] | 7 | 2.71 | 2.57 | 2.71 | 2.71 | 2.71 | 2.71 |
| codeant-ai[bot] | 15 | 4.73 | 4.67 | 4.73 | 4.73 | 4.73 | 4.73 |
| cursor[bot] | 11 | 2.91 | 3.27 | 3.00 | 2.91 | 2.64 | 2.73 |
| devin-ai-integration[bot] | 14 | 1.79 | 1.79 | 1.79 | 1.71 | 1.86 | 1.86 |
| entelligence-ai-pr-reviews[bot] | 12 | 7.42 | 7.50 | 7.50 | 7.50 | 7.50 | 7.50 |
| factory-droid[bot] | 6 | 1.83 | 1.50 | 1.83 | 1.67 | 1.67 | 1.67 |
| gemini-code-assist[bot] | 8 | 1.25 | 1.25 | 1.25 | 1.25 | 1.25 | 1.25 |
| graphite-app[bot] | 12 | 1.83 | 1.83 | 2.00 | 1.92 | 1.83 | 1.92 |
| kody-ai[bot] | 12 | 1.92 | 1.75 | 1.92 | 1.75 | 1.75 | 1.83 |
| macroscopeapp[bot] | 15 | 1.80 | 1.80 | 1.67 | 1.67 | 1.60 | 1.67 |
| qodo-free-for-open-source-projects[bot] | 15 | 2.20 | 2.33 | 2.27 | 2.13 | 2.27 | 2.53 |
| sentry[bot] | 15 | 1.53 | 1.53 | 1.53 | 1.47 | 1.60 | 1.53 |
| sourcery-ai[bot] | 14 | 0.86 | 0.57 | 0.86 | 0.79 | 1.00 | 0.79 |

## Qodo: resolved and dismissed findings

Qodo keeps its earlier findings in its summary comment, struck through and marked as resolved (the
author fixed it) or as dismissed. 7 of the 13 Qodo PRs carry such marks. Items extracted on them
(matched in parentheses): current 30 (19) and 30 (18), earlier revision 24 (17) and 27 (18),
`cee5e6cc` 27 (17), `093c763a` 25 (17), `a2fcbe51` 32 (18), head 29 (18). `093c763a` dropped 5 such
findings that both current-prompt draws extract, on anolilab/neore#451 (two resolved, one of them
matched to the author's fix in both baseline draws), varveio/swath#243 (one resolved, matched in the
first baseline draw; one dismissed) and TW199501/multica#50 (one dismissed). The head extracts 4 of
the 5: both neore findings (neore is back to 2 of 3 matched, as with the current prompt), swath's
resolved finding and multica's dismissed one; swath's dismissed one is not. swath's resolved finding
is extracted but not matched: the judge matched it in 1 of the 5 draws that extracted it (the
current prompt's first), so swath#243's -14.3 recall at the head is the matching draw. No other
bot's comments in the sample carry such marks except one cubic PR (5 matched in every arm) and
CodeRabbit's inline comments, which it marks as addressed once a commit fixes them (posted inline comments
still extracted: see the type table).

## Ruling question 2: remarks about the PR itself

An earlier revision (`cf4460fb`) excluded remarks about the pull request itself. It worked
unevenly: CodeRabbit's title, description and linked-issue items fell from 18 to 3 or 4, while
Copilot's inline remark on fleetdm/fleet#55421 that the PR description omits template sections was
still extracted in 4 of 4 draws. Two neutral wordings that would cover Copilot's case left it
extracted in 3 of 4 and 4 of 4 draws, so the PR now says the opposite.

The first prompt without the exclusion (99d049f, measured at 04e8de3) kept an intro saying all
PR-level content is not a finding, and the intro won (CodeRabbit 18 and 18 such items in two draws
of the current prompt, 6 with 04e8de3). With the scoped intro, one draw each, CodeRabbit's counts
were 19 at 5c683e4, 22 at c35f464 (prompt `cee5e6cc`), 19 at `093c763a`, 19 at `a2fcbe51` and
21 at the head; Claude's were 6, 5, 4, 8 and 5 (current 6 and 4). The 04e8de3 and
5c683e4 draws are not in the results file, so the first values of each list cannot be recomputed
from it; the rest are `items_other_check`, counted by the fixed text patterns of the type table.

## Fixture draws

`gpt-5.2-2025-12-11` at temperature 1.0, every label checked (the probe counts a draw clean only
when no label fails), plus the opt-in live run (`tests/test_extraction_scope_live.py`, one draw per
fixture). These counts come from our harness runs, not from the results file.

### The resolved-findings sentence

The first version of the rule was a 70-word sentence (prompt `a2fcbe51`). It brought back Qodo's
resolved findings but cost other checks: the scylla description-check row was extracted in 1 of 9
draws (4 of 8 on `093c763a` in the same session) and the live run passed 9 of 12. Three short
wordings were then tried, each beside the collapsed-section rule, with the follow-up-comment skip
saying "(this covers the confirmation, not the finding it refers to)". Cells are draws with every label of
that fixture passing; every recorded failure on TajsTokens is its dismissed finding, on zizmor the
quoted tool output, on scylla the description-check row:

| wording | Qodo neore#451 | zizmor (universal-chat-extractor#186) | scylla#3811 | TajsTokens#109 |
|---|---|---|---|---|
| 1. "A finding stays a finding when the bot later marks it resolved, fixed, outdated or dismissed, or strikes it through." | 6/6 | 4/6 | 6/6 | 1/6 |
| 2. wording 1 + ", including in a summary that lists its findings: extract it." (**chosen**) | 6/6 | 6/6 | 5/6 | 1/6 |
| 3. wording 1 + ", wherever it is listed and whatever status or counts the bot shows beside it: extract it." | 6/6 | 4/6 | 5/6 | 0/6 |

Wording 2 was chosen as the one with the most clean draws over the four fixtures: 18 of 24, against
17 and 15. Against wording 1 the whole margin is the two CodeRabbit fixtures, both in CodeRabbit's
favour: zizmor kept out in 2 more draws (6 vs 4 of 6) and the scylla description-check row extracted
in 1 fewer (5 vs 6 of 6). Against wording 3 it is zizmor (+2, in CodeRabbit's favour) and TajsTokens
(+1, in Qodo's favour); scylla is equal (5 of 6). All of these are within 6-draw noise.

The first live run on wording 2 passed 10 of 12 fixtures (the pcsx-redux and local-search-mcp
findings were not yet asserted). It failed the scylla row in its one draw and the TajsTokens
dismissed finding. That finding is also missed by the current upstream prompt (`e616e849`: 0 of 6
draws) and by `093c763a` (0 of 8), so it is not a regression of this PR; the rule raises it a
little (counts below). Both rows stay asserted. The new Qodo fixture (anolilab/neore#451:
two resolved findings, one dismissed) passes, but also passes on `093c763a` (8 of 8): trimmed to the
summary comment, it does not reproduce the drop seen on the full input, so it guards the rule rather
than measuring it.

### Earlier counts

- scylladb/scylla-operator#3811 description-check row: 6 of 6 draws with the scoped intro, 5 of 6
  before the two `093c763a` rewordings, 13 of 18 on `093c763a` (4 of 8 in a later run), 1 of 9 on
  `a2fcbe51`, 6 of 8 on the head.
- hyperpolymath/universal-chat-extractor#186 zizmor lines: extracted in 0 of 14 targeted draws with
  the "configured review instructions" wording, 1 of 13 after the quoted-tool-output rule was
  narrowed, 1 of 9 on `a2fcbe51`, 0 of 6 on the head (and kept out in its live draw).
- Copilot's description remark on fleetdm/fleet#55421: extracted in every draw of the scoped-intro
  prompts.

### Asserted finding labels the model misses

Rule: a finding label stays asserted whatever its pass rate, and its failures are disclosed here.
Only the 10 overview sentences carry no pattern (no text pattern separates them from findings on
the same code), plus one neutral linked-issue row that names no problem
(Fooftilly/local-search-mcp#27), so the live run does not check overviews. With every finding asserted, the live run
on the head (one draw per fixture, after the wording draws) passes 9 of 12 and fails exactly these
three, each counted over the targeted draws plus the live draws:

- Qodo's dismissed finding on tajemniktv/TajsTokens#109: 1 of 8 on the head, 2 of 9 on
  `a2fcbe51`, 0 of 8 on `093c763a`, 0 of 6 with the current upstream prompt (`e616e849`).
- CodeRabbit's rating-explanation finding on pcsx-redux/pcsx-redux#2544 (a concrete problem named
  only in the merge-risk explanation, which the rule says to extract): 0 of 7 on the head, 1 of 6
  with the current upstream prompt (earlier: 1 of 5 and 0 of 5).
- cubic's linked-issue table row on Fooftilly/local-search-mcp#27 that names a concrete problem:
  0 of 7 on the head, 0 of 6 with the current upstream prompt (earlier: 0 of 3 and 0 of 4). The
  prompt's intro calls a remark or check result that says what "the pull request's title,
  description, or linked issue lacks" a finding; a linked-issue row reports what the change lacks
  against the issue, which that wording does not literally cover, so it may contribute to this miss.
  A reword ("what the pull request's title or description lacks, or what the change lacks against
  its linked issue") left the row at 0 of 6 and did worse elsewhere (scylla row 3 of 6, zizmor kept
  out 2 of 6, TajsTokens 2 of 6, the Qodo fixture 6 of 6, 6 draws each), so it was not adopted.
  Narrowing the gate bullet's carve-out to title and description checks only (so a linked-issue
  check that names a concrete problem would fall under the concrete-problem rule) also left the row
  at 0 of 6 and lowered the scylla row to 1 of 6 (zizmor kept out 5 of 6, TajsTokens 3 of 6, the
  Qodo fixture 6 of 6), so it was not adopted either. The fixture test maps this label to the
  sentence that judges other PR-level content like any other comment. Both rejected tries extracted
  the scylla CodeRabbit description-check row less often (3 of 6 and 1 of 6, against 6 of 8 on the
  head), so adopting either would have favoured CodeRabbit's score and rejecting them went against
  our interest (unlike the resolved-findings wording choice above, whose small differences favour
  CodeRabbit).

The scylla description-check row passed this live draw (6 of 8 on the head in all). The same
pcsx-redux draws missed one asserted collapsed follow-up finding once on the head (5 of 6).

## Status notes and the coverage-only templates

The type patterns behind `items_*` were written on CodeRabbit's text, so the six other bots'
unlocated items were read by hand and their run-status notes typed `items_run_status`: Codex
review-incomplete and usage-limit notes, Qodo paused-review notes, Claude notes that the bot could
not verify CI or run the checks, and a Greptile note that no user flows were identified. There are 5
in the current prompt's first draw and 6 in its second; the head extracts 3. So the dropout lands
unevenly: the head drops the Codex and Qodo notes (harshanandak/forge#98 and
Heretek-AI/claude-4-opencode#4 leave the means), but still extracts the two Claude notes
(RiGuJeCaAb/agente-editais#18, shakacode/react-on-rails-demo-flagship#91) and the Greptile one
(ever-co/ever-gauzy#11009), as the current prompt does, so those PRs stay in; on agente-editais#18
the note is the PR's only item.
`coverage_only.py`'s whole-text templates catch 2 and 0 of them, and 76 of 119 and 61 of 112 of
CodeRabbit's unlocated docstring-coverage items in the two current-prompt draws, so its f is a
lower bound. It is not comparable across bots: the templates follow CodeRabbit's lines, so f is
understated more for the other bots (`--bot all` prints this caveat). It finds two excluded-only
PRs in our rows, harshanandak/forge#98 (Codex) and
Heretek-AI/claude-4-opencode#4 (Qodo).

**Offline dataset (`offline/results/benchmark_data.json`), 20 PRs x 8 tools.** Each tool's
published comments; golden comments stand in for the extracted actions (commit diffs are not
published, so the diff block is a placeholder). "This PR" is an earlier revision (5c683e4), one
draw; not re-run since.

| tool | suggestions/PR: current / 2nd draw / this PR | P: current / 2nd draw / this PR | R: current / 2nd draw / this PR |
|---|---|---|---|
| augment | 3.15 / 3.15 / 3.15 | 66.3 / 66.3 / 66.3 | 61.6 / 61.6 / 61.6 |
| bugbot | 2.55 / 2.55 / 2.50 | 65.2 / 63.5 / 65.8 | 52.8 / 50.3 / 52.8 |
| coderabbit | 7.70 / 7.60 / 7.35 | 36.5 / 35.7 / 39.2 | 66.4 / 66.4 / 63.8 |
| copilot | 5.35 / 5.25 / 5.35 | 44.5 / 44.9 / 43.5 | 59.7 / 59.1 / 59.6 |
| cubic | 3.10 / 3.10 / 3.20 | 65.9 / 65.9 / 67.5 | 63.2 / 63.2 / 64.5 |
| gemini | 4.20 / 4.05 / 4.05 | 53.2 / 56.2 / 56.2 | 61.6 / 62.2 / 62.2 |
| greptile | 3.80 / 3.80 / 3.80 | 47.3 / 46.0 / 42.2 | 44.2 / 41.7 / 39.2 |
| qodo | 2.45 / 2.35 / 2.40 | 58.7 / 52.9 / 58.2 | 45.2 / 40.5 / 44.8 |

At 20 PRs the matching step at temperature 1.0 moves P and R by up to 6 points between draws of
the same prompt. Greptile's P and R drop here with the same suggestion count on every PR: four PRs
changed only in match verdicts on the same issues.

## What moved, per bot

Generated by `cd online/etl && uv run python -m prompt_eval.what_moved` (defaults: `--base current
--arm "this PR at head, one draw" --top 3`). Each change splits exactly into PRs leaving (PR i adds
(current mean over the PRs in both - its value) / PRs before), PRs entering (adds (its value - head
mean over the PRs in both) / PRs after) and PRs in both (each adds its change / PRs in both). The
listed PRs in both are the largest contributions to the last part.

**chatgpt-codex-connector[bot]**: PRs with both 17 -> 16 (16 in both arms)
- precision 81.1 -> 86.1 (+5.1) = PRs leaving +5.1 + PRs entering +0.0 + PRs in both +0.0
- recall 38.4 -> 39.3 (+0.8) = PRs leaving +2.4 + PRs entering +0.0 + PRs in both -1.6
- harshanandak/forge#98 leaves: precision 0.0 -> undefined, recall 0.0 -> 0.0; effect on precision +5.1, on recall +2.4
- jfox85/devx#98: recall 50.0 -> 25.0, contributes -1.6
  - harshanandak/forge#98 matched/extracted by arm: current: 0/1; current, 2nd draw: 0/0; earlier revision cf4460fb: 0/0; earlier revision cf4460fb, 2nd draw: 0/0; previous revision 093c763a, one draw: 0/0; previous revision a2fcbe51, one draw: 0/0; previous revision cee5e6cc, one draw: 0/0; this PR at head, one draw: 0/0
  - jfox85/devx#98 matched/extracted by arm: current: 2/2; current, 2nd draw: 2/2; earlier revision cf4460fb: 1/1; earlier revision cf4460fb, 2nd draw: 2/2; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 2/2; this PR at head, one draw: 1/1

**claude[bot]**: PRs with both 58 -> 58 (58 in both arms)
- precision 53.2 -> 52.0 (-1.2) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -1.2
- recall 41.9 -> 40.8 (-1.2) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -1.2
- RiGuJeCaAb/agente-editais#18: precision 100.0 -> 0.0, contributes -1.7
- GluuFederation/flex#3143: precision 50.0 -> 100.0, contributes +0.9
- escoffier-labs/brigade#2330: precision 75.0 -> 100.0, contributes +0.4
- ckeller42/xrdp-nvidia-ppa#6: recall 100.0 -> 50.0, contributes -0.9
- breathless-youth/frontend#373: recall 66.7 -> 33.3, contributes -0.6
- GluuFederation/flex#3143: recall 25.0 -> 50.0, contributes +0.4
  - GluuFederation/flex#3143 matched/extracted by arm: current: 1/2; current, 2nd draw: 1/2; earlier revision cf4460fb: 1/2; earlier revision cf4460fb, 2nd draw: 1/2; previous revision 093c763a, one draw: 1/2; previous revision a2fcbe51, one draw: 2/2; previous revision cee5e6cc, one draw: 1/2; this PR at head, one draw: 2/2
  - RiGuJeCaAb/agente-editais#18 matched/extracted by arm: current: 1/1; current, 2nd draw: 0/1; earlier revision cf4460fb: 0/0; earlier revision cf4460fb, 2nd draw: 0/0; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 0/1
  - breathless-youth/frontend#373 matched/extracted by arm: current: 2/9; current, 2nd draw: 2/10; earlier revision cf4460fb: 2/10; earlier revision cf4460fb, 2nd draw: 2/10; previous revision 093c763a, one draw: 2/9; previous revision a2fcbe51, one draw: 2/8; previous revision cee5e6cc, one draw: 2/9; this PR at head, one draw: 1/9
  - ckeller42/xrdp-nvidia-ppa#6 matched/extracted by arm: current: 2/15; current, 2nd draw: 1/17; earlier revision cf4460fb: 1/15; earlier revision cf4460fb, 2nd draw: 1/14; previous revision 093c763a, one draw: 1/15; previous revision a2fcbe51, one draw: 2/13; previous revision cee5e6cc, one draw: 1/14; this PR at head, one draw: 1/16
  - escoffier-labs/brigade#2330 matched/extracted by arm: current: 3/4; current, 2nd draw: 4/5; earlier revision cf4460fb: 2/3; earlier revision cf4460fb, 2nd draw: 3/5; previous revision 093c763a, one draw: 3/4; previous revision a2fcbe51, one draw: 3/4; previous revision cee5e6cc, one draw: 3/4; this PR at head, one draw: 4/4

**coderabbitai[bot]**: PRs with both 232 -> 232 (232 in both arms)
- precision 62.4 -> 70.0 (+7.6) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both +7.6
- recall 49.1 -> 48.5 (-0.6) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -0.6
- EntryDSM/entrydsm-platform#418: precision 33.3 -> 100.0, contributes +0.3
- coleam00/Archon#5303: precision 33.3 -> 100.0, contributes +0.3
- hyperpolymath/universal-chat-extractor#186: precision 33.3 -> 100.0, contributes +0.3
- Altinn/altinn-studio#20952: recall 100.0 -> 0.0, contributes -0.4
- valkey-io/valkey#5129: recall 100.0 -> 0.0, contributes -0.4
- knowall-ai/zapdesk#594: recall 50.0 -> 0.0, contributes -0.2
  - Altinn/altinn-studio#20952 matched/extracted by arm: current: 1/3; current, 2nd draw: 0/3; earlier revision cf4460fb: 0/3; earlier revision cf4460fb, 2nd draw: 0/3; previous revision 093c763a, one draw: 0/3; previous revision a2fcbe51, one draw: 0/3; previous revision cee5e6cc, one draw: 1/3; this PR at head, one draw: 0/3
  - EntryDSM/entrydsm-platform#418 matched/extracted by arm: current: 1/3; current, 2nd draw: 1/2; earlier revision cf4460fb: 1/1; earlier revision cf4460fb, 2nd draw: 1/1; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 1/1
  - coleam00/Archon#5303 matched/extracted by arm: current: 1/3; current, 2nd draw: 1/2; earlier revision cf4460fb: 1/2; earlier revision cf4460fb, 2nd draw: 1/2; previous revision 093c763a, one draw: 1/2; previous revision a2fcbe51, one draw: 1/2; previous revision cee5e6cc, one draw: 1/2; this PR at head, one draw: 2/2
  - hyperpolymath/universal-chat-extractor#186 matched/extracted by arm: current: 1/3; current, 2nd draw: 1/3; earlier revision cf4460fb: 1/1; earlier revision cf4460fb, 2nd draw: 1/1; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 1/1
  - knowall-ai/zapdesk#594 matched/extracted by arm: current: 1/3; current, 2nd draw: 0/1; earlier revision cf4460fb: 1/2; earlier revision cf4460fb, 2nd draw: 1/3; previous revision 093c763a, one draw: 0/1; previous revision a2fcbe51, one draw: 0/1; previous revision cee5e6cc, one draw: 0/1; this PR at head, one draw: 0/1
  - valkey-io/valkey#5129 matched/extracted by arm: current: 1/2; current, 2nd draw: 1/2; earlier revision cf4460fb: 0/1; earlier revision cf4460fb, 2nd draw: 0/1; previous revision 093c763a, one draw: 0/1; previous revision a2fcbe51, one draw: 0/1; previous revision cee5e6cc, one draw: 0/1; this PR at head, one draw: 0/1

**copilot-pull-request-reviewer[bot]**: PRs with both 15 -> 15 (15 in both arms)
- precision 81.7 -> 79.4 (-2.3) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -2.3
- recall 50.0 -> 48.2 (-1.7) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -1.7
- inclunet/assistente#1029: precision 75.0 -> 60.0, contributes -1.0
- saulpatinojr/HCW-HybridCloudWorks#1042: precision 20.0 -> 10.0, contributes -0.7
- sensslen/Cgf.CameraControl.Net#61: precision 63.6 -> 54.5, contributes -0.6
- saulpatinojr/HCW-HybridCloudWorks#1042: recall 66.7 -> 33.3, contributes -2.2
- sensslen/Cgf.CameraControl.Net#61: recall 35.7 -> 42.9, contributes +0.5
  - inclunet/assistente#1029 matched/extracted by arm: current: 3/4; current, 2nd draw: 3/4; earlier revision cf4460fb: 3/5; earlier revision cf4460fb, 2nd draw: 3/4; previous revision 093c763a, one draw: 4/5; previous revision a2fcbe51, one draw: 3/5; previous revision cee5e6cc, one draw: 3/5; this PR at head, one draw: 3/5
  - saulpatinojr/HCW-HybridCloudWorks#1042 matched/extracted by arm: current: 2/10; current, 2nd draw: 3/10; earlier revision cf4460fb: 3/10; earlier revision cf4460fb, 2nd draw: 1/10; previous revision 093c763a, one draw: 3/10; previous revision a2fcbe51, one draw: 1/10; previous revision cee5e6cc, one draw: 1/10; this PR at head, one draw: 1/10
  - sensslen/Cgf.CameraControl.Net#61 matched/extracted by arm: current: 7/11; current, 2nd draw: 6/11; earlier revision cf4460fb: 7/11; earlier revision cf4460fb, 2nd draw: 5/11; previous revision 093c763a, one draw: 7/11; previous revision a2fcbe51, one draw: 8/11; previous revision cee5e6cc, one draw: 6/11; this PR at head, one draw: 6/11

**cubic-dev-ai[bot]**: PRs with both 103 -> 103 (103 in both arms)
- precision 69.1 -> 69.8 (+0.7) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both +0.7
- recall 52.7 -> 54.1 (+1.4) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both +1.4
- marionettejs/marionette#3761: precision 33.3 -> 66.7, contributes +0.3
- Fooftilly/local-search-mcp#27: precision 50.0 -> 75.0, contributes +0.2
- praveenjuge/vadivam#51: precision 66.7 -> 50.0, contributes -0.2
- bompus/codegraph#510: recall 0.0 -> 100.0, contributes +1.0
- zerostrat/awesome-coderabbit#1: recall 0.0 -> 20.0, contributes +0.2
- recoupable/skills#202: recall 50.0 -> 66.7, contributes +0.2
  - Fooftilly/local-search-mcp#27 matched/extracted by arm: current: 2/4; current, 2nd draw: 2/4; earlier revision cf4460fb: 2/4; earlier revision cf4460fb, 2nd draw: 2/4; previous revision 093c763a, one draw: 2/4; previous revision a2fcbe51, one draw: 2/4; previous revision cee5e6cc, one draw: 2/4; this PR at head, one draw: 3/4
  - bompus/codegraph#510 matched/extracted by arm: current: 0/17; current, 2nd draw: 0/16; earlier revision cf4460fb: 1/18; earlier revision cf4460fb, 2nd draw: 0/17; previous revision 093c763a, one draw: 1/18; previous revision a2fcbe51, one draw: 0/17; previous revision cee5e6cc, one draw: 1/18; this PR at head, one draw: 1/18
  - marionettejs/marionette#3761 matched/extracted by arm: current: 1/3; current, 2nd draw: 2/3; earlier revision cf4460fb: 2/3; earlier revision cf4460fb, 2nd draw: 2/3; previous revision 093c763a, one draw: 1/3; previous revision a2fcbe51, one draw: 2/3; previous revision cee5e6cc, one draw: 1/3; this PR at head, one draw: 2/3
  - praveenjuge/vadivam#51 matched/extracted by arm: current: 4/6; current, 2nd draw: 4/6; earlier revision cf4460fb: 4/6; earlier revision cf4460fb, 2nd draw: 4/6; previous revision 093c763a, one draw: 4/6; previous revision a2fcbe51, one draw: 3/6; previous revision cee5e6cc, one draw: 4/6; this PR at head, one draw: 3/6
  - recoupable/skills#202 matched/extracted by arm: current: 3/15; current, 2nd draw: 2/15; earlier revision cf4460fb: 2/15; earlier revision cf4460fb, 2nd draw: 3/15; previous revision 093c763a, one draw: 2/15; previous revision a2fcbe51, one draw: 1/15; previous revision cee5e6cc, one draw: 3/15; this PR at head, one draw: 4/15
  - zerostrat/awesome-coderabbit#1 matched/extracted by arm: current: 0/16; current, 2nd draw: 0/16; earlier revision cf4460fb: 1/16; earlier revision cf4460fb, 2nd draw: 2/16; previous revision 093c763a, one draw: 0/16; previous revision a2fcbe51, one draw: 1/16; previous revision cee5e6cc, one draw: 0/16; this PR at head, one draw: 1/16

**greptile-apps[bot]**: PRs with both 157 -> 157 (157 in both arms)
- precision 85.0 -> 84.0 (-1.0) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both -1.0
- recall 33.5 -> 34.2 (+0.8) = PRs leaving +0.0 + PRs entering +0.0 + PRs in both +0.8
- ParadevAI/Paradev#9074: precision 0.0 -> 100.0, contributes +0.6
- float-ritual-stack/ep0ch#861: precision 100.0 -> 0.0, contributes -0.6
- lusky3/SportsPress-Admin-Tools#242: precision 100.0 -> 0.0, contributes -0.6
- classroomio/classroomio#1425: recall 50.0 -> 100.0, contributes +0.3
- FranciscoMoretti/chat-js#925: recall 33.3 -> 66.7, contributes +0.2
- getstoop/stoop#386: recall 25.0 -> 50.0, contributes +0.2
  - FranciscoMoretti/chat-js#925 matched/extracted by arm: current: 1/2; current, 2nd draw: 1/2; earlier revision cf4460fb: 1/2; earlier revision cf4460fb, 2nd draw: 1/2; previous revision 093c763a, one draw: 1/2; previous revision a2fcbe51, one draw: 2/2; previous revision cee5e6cc, one draw: 1/2; this PR at head, one draw: 2/2
  - ParadevAI/Paradev#9074 matched/extracted by arm: current: 0/1; current, 2nd draw: 1/1; earlier revision cf4460fb: 0/1; earlier revision cf4460fb, 2nd draw: 0/1; previous revision 093c763a, one draw: 0/1; previous revision a2fcbe51, one draw: 0/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 1/1
  - classroomio/classroomio#1425 matched/extracted by arm: current: 1/1; current, 2nd draw: 2/2; earlier revision cf4460fb: 2/2; earlier revision cf4460fb, 2nd draw: 2/2; previous revision 093c763a, one draw: 2/2; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 2/2
  - float-ritual-stack/ep0ch#861 matched/extracted by arm: current: 1/1; current, 2nd draw: 0/1; earlier revision cf4460fb: 0/1; earlier revision cf4460fb, 2nd draw: 0/1; previous revision 093c763a, one draw: 0/1; previous revision a2fcbe51, one draw: 0/1; previous revision cee5e6cc, one draw: 0/1; this PR at head, one draw: 0/1
  - getstoop/stoop#386 matched/extracted by arm: current: 1/1; current, 2nd draw: 1/1; earlier revision cf4460fb: 1/1; earlier revision cf4460fb, 2nd draw: 1/1; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 2/2
  - lusky3/SportsPress-Admin-Tools#242 matched/extracted by arm: current: 1/1; current, 2nd draw: 0/1; earlier revision cf4460fb: 2/2; earlier revision cf4460fb, 2nd draw: 1/1; previous revision 093c763a, one draw: 1/1; previous revision a2fcbe51, one draw: 1/1; previous revision cee5e6cc, one draw: 1/1; this PR at head, one draw: 0/1

**qodo-code-review[bot]**: PRs with both 8 -> 7 (7 in both arms)
- precision 68.1 -> 76.2 (+8.1) = PRs leaving +9.7 + PRs entering +0.0 + PRs in both -1.6
- recall 48.6 -> 41.2 (-7.3) = PRs leaving +6.9 + PRs entering +0.0 + PRs in both -14.3
- Heretek-AI/claude-4-opencode#4 leaves: precision 0.0 -> undefined, recall 0.0 -> 0.0; effect on precision +9.7, on recall +6.9
- varveio/swath#243: precision 11.1 -> 0.0, contributes -1.6
- varveio/swath#243: recall 100.0 -> 0.0, contributes -14.3
  - Heretek-AI/claude-4-opencode#4 matched/extracted by arm: current: 0/1; current, 2nd draw: 0/1; earlier revision cf4460fb: 0/0; earlier revision cf4460fb, 2nd draw: 0/0; previous revision 093c763a, one draw: 0/0; previous revision a2fcbe51, one draw: 0/0; previous revision cee5e6cc, one draw: 0/1; this PR at head, one draw: 0/0
  - varveio/swath#243 matched/extracted by arm: current: 1/9; current, 2nd draw: 0/9; earlier revision cf4460fb: 0/7; earlier revision cf4460fb, 2nd draw: 0/7; previous revision 093c763a, one draw: 0/7; previous revision a2fcbe51, one draw: 0/12; previous revision cee5e6cc, one draw: 0/9; this PR at head, one draw: 0/8
