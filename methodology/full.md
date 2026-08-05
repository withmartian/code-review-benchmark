# Code Review Bench: Detailed Methodology

## 1. Introduction

Code Review Bench is a living benchmark for measuring how effective base models and code review agents are at code review.

When we say a "living" benchmark, we mean:
* It is versioned and updated as we learn more about what matters.
* The methodology is openly available, reproduction code is open source, and annotation guidelines are published.
* We maintain a public annotated dataset alongside a held-out test set derived from the same guidelines to prevent training contamination.
* We want feedback — while we can't address every critique, we want limitations to be transparent and addressed to the best of our ability.

Most critiques of benchmarks reduce to Goodhart's Law: when a measure becomes a target, it ceases to be a good measure. Companies optimize for benchmarks. If the benchmark doesn't measure what actually matters, optimization makes products worse along the dimensions that matter while appearing to make them better. The gap between measure and intent is the core problem. Nearly everything in our methodology is an attempt to close it: behavioral validation against real-world data, multiple independent signals that are hard to game in isolation, continuous refresh to prevent overfitting, and adversarial validation that expands our ground truth when tools exceed our ability to measure. If we close this gap sufficiently, optimizing for the benchmark means optimizing for the real thing.

Why code review specifically? Code is already the AI application with the clearest product-market fit, and APIs give software the ability to access most of the real world — making it a likely entry point for important applications beyond coding itself. Within code, review is the verification step in the generation process. Generating correct code is hard (effectively in NP); checking whether code is correct may be easier (effectively in P). Training models via RL requires exactly such a verifier as a reward signal, so understanding code review is a high-leverage way of understanding code generation more broadly. Code review is also safety-adjacent — finding bugs and dangerous code is precisely a security application, and understanding what reward gaming looks like in this real-world setting prepares us for reward gaming in higher-stakes domains. This connects to Martian's broader mission of understanding how models behave and why — the same mission behind our $1M prize for mechanistic interpretability.

Building a high-quality, living benchmark is expensive. Generating sufficient data, maintaining independence, keeping the benchmark current, and improving issues over time all require sustained investment. Martian is well-funded and our business doesn't depend on any particular tool winning. This lets us keep the benchmark up to date, independent, and improvable. We also love collaborating with academia — reach out at research@withmartian.com.

**Table of Contents**

1. Introduction
2. What exists today — existing benchmarks and their limitations
3. Our approach: offline + online evaluation
4. Current implementation
5. Defining what counts as a bug
6. Measuring recall
7. Measuring precision
8. Avoiding staleness and contamination
9. Judges and infrastructure
10. Working with tool builders
11. Future directions
12. Roadmap
13. Credibility and independence

---

## 2. What exists today

Code review benchmarking is nascent. There's no established standard, and existing efforts have significant limitations. Through conversations with tool builders, we identified recurring problems: judge variability, data contamination, missing context, stale data, fragile infrastructure, incomparable output formats, no bug definitions, and gold sets that cap at human performance. This section surveys what exists and where it falls short.

**Academic benchmarks.** Academic work on code review evaluation exists, including SWRBench (Zeng et al., 2025) with 1,000 PRs and ContextCRBench (Hu et al., 2025) focusing on fine-grained evaluation with enriched context. However, neither is open source — code, data, and evaluation infrastructure are unavailable or require significant effort to reconstruct. This makes it impossible to run new tools against them or verify their results, limiting their practical utility.

**Industry benchmarks.** Recently, tool vendors have started publishing their own benchmarks. This is a positive development — it moves the field toward measurable evaluation — but introduces bias concerns. When a vendor publishes a benchmark and their tool wins, it's hard to know whether the benchmark was designed, consciously or not, to favor their approach.

Three are worth examining:

*Greptile (July 2025)* backtracks from fix commits to the commits that introduced bugs. It covers 50 PRs across 5 repositories (Sentry, Cal.com, Grafana, Keycloak, Discourse) in 5 languages. It measures bug catch rate — did the tool identify the bug in a line-level comment? The dataset is fully public and reproducible, which is a genuine contribution. But it's limited to a single bug per PR, measures only catch rate (no precision), and is small in scale.

*Augment (December 2025)* built on Greptile's dataset, expanding the golden comments by manually reviewing each PR. This was a meaningful correction — it showed the original dataset was incomplete, with many PRs having multiple issues missing from the gold set. Augment added precision, recall, and F-score. But it's still 50 PRs, still backtracking-based.

*Qodo (2025)* took a different approach: injection-based rather than backtracking. Instead of tracing from fix commits, Qodo injects synthetic bugs into clean, merged PRs. This gives it larger scale (100 PRs, 580 issues) and more realistic review scenarios with multiple issues per PR. It also evaluates code quality, not just correctness. The core question for any injection-based approach is validation: if injected bugs don't match the distribution of bugs humans actually write, the benchmark measures something other than real-world performance.

**Methodological limitations.** Across all existing benchmarks, several conceptual problems recur:

*No clear bug definition.* What counts as a bug varies by user — some want style issues flagged, others want only critical bugs. Existing benchmarks don't condition on user preferences, making "precision" and "recall" ill-defined.

*Judge variability.* Results depend heavily on the LLM used to match tool outputs to golden comments. Different judges give different scores. None of the benchmarks report judge calibration or variance.

*Data contamination.* Most code in these benchmarks was seen by LLMs during training. It's unclear whether we're measuring generalization or memorization.

*Missing context.* No PR body text, no linked issues, no task tracker integration. Tools that use richer context are penalized.

*The gold set caps at human performance.* If a tool finds a real bug that human annotators missed, it gets scored as a false positive. Existing benchmarks structurally cannot measure superhuman performance and will actively punish it.

*No measurement of real-world value.* These benchmarks measure "did the tool find the bug" — but the real questions are whether the tool makes human programmers more effective and whether it improves code generation when used as a verifier. Neither is measured.

**Operational limitations.** There are also practical problems that make existing benchmarks difficult to use and reproduce:

*Stale data.* Some PRs are from commits merged 15+ years ago. Coding practices and bug patterns evolve — old data may not reflect modern development.

*Infrastructure fragility.* Running benchmarks requires triggering PRs on GitHub, which is manual and unreliable at scale.

*Format differences.* Some tools produce line-by-line comments, others produce single-page summaries, others do both. It's unclear how to compute precision and recall across different output formats.

---

## 3. Our approach: offline + online

Our benchmark has two pieces that check each other.

**The offline benchmark** runs every tool on the same PRs with the same bug definitions and scores them against a curated gold set. This is the controlled comparison — it lets us evaluate any tool or model, including new ones without public installations, on identical inputs. The v0 builds on Augment and Greptile's published dataset and improves from there.

**The online benchmark** measures how developers actually respond to code review tools in open source repos. When a developer fixes a problem flagged by a tool, they're voting that the flag was useful. These behavioral signals aren't controlled by us or by vendors — they're anchored to what actually happens.

The online benchmark is valuable for several reasons beyond just checking the offline benchmark. It shows us revealed preferences — what developers actually care about, rather than proxies or guesses. This covers not just the distribution of human preferences, but also the distribution of generated code, repositories, issues, and fixes that tools encounter in practice. And it requires neither manual annotation (the most expensive part of building a benchmark) nor running models ourselves (the second most expensive part), which lets us gather signal at scale.

**Limitations of the online benchmark.** The online benchmark has real limitations that prevent it from being the sole measure.

You can't get counterfactuals. You can't run two tools on the same repo — if only one is installed you don't have a comparison, and if both are installed they have access to each other's output. Even forking the repo and running a second tool separately means you've left the in-the-wild setting. Tool adoption also correlates with repo characteristics — maybe one language tends toward a particular tool, or repos that are easier to review use a less expensive tool. These distributional differences make direct tool-to-tool comparison unreliable.

You can't separate model quality from harness quality. The online benchmark measures the full product — model plus the harness around it (context gathering, prompt engineering, output formatting, integration). A mediocre model with an excellent harness could outperform a better model with a worse one. The online benchmark can't decompose what's driving performance. This is a fundamental confound: if you want to know which model is better, you need to hold the harness constant, which only the offline benchmark with a standardized evaluation harness can do.

You can't measure new or private tools. There's no in-the-wild data for a tool that hasn't been publicly deployed, or a new model that hasn't been integrated into a product yet.

The total data volume is limited by the rate of new OSS code generation, which may be insufficient for some purposes like training models for code review.

There may be distribution shift between OSS and private repos. We plan to validate this by collecting aggregate statistics with industry partners.

**Why we need both.** The offline benchmark addresses every limitation above — it provides controlled comparison, can isolate model from harness, can evaluate any tool or model, and isn't constrained by OSS data volume. But the offline benchmark might measure the wrong thing. The gold set might be incomplete. The judge might be miscalibrated. The bug definitions might not reflect what developers actually care about.

The online benchmark catches that. If a tool ranks highly offline but developers routinely ignore its comments in practice, something is wrong with our offline methodology. If a tool ranks poorly offline but developers consistently act on its suggestions, we're missing something. By checking one against the other, we keep the measure aligned with intent.

**How online signals improve the offline benchmark.** This is the core loop of our methodology. Online signals validate and calibrate nearly every component of the offline benchmark:

- *Bug definitions.* If developers consistently act on a category of comment that our definition excludes, the definition is too narrow. If they consistently ignore a category we include, it's too broad. We can also test whether agents.md files improve our ability to predict developer actions — if they do, they're capturing real preferences.
- *Gold set completeness.* If tools flag real issues that developers fix but our gold set doesn't contain, we're undercounting recall. Bug lifetime analysis — fitting survival curves to historical bug data — gives us a probabilistic estimate of how many bugs remain undiscovered.
- *Judge accuracy.* If the judge says a tool comment doesn't match a gold set item but developers treat them as the same issue, the judge is wrong. We calibrate judges against both human annotations and behavioral data.
- *Precision calibration.* Comment afterlife — tracking whether flagged-but-ignored code gets changed later, or whether similar flags have high acceptance rates in other contexts — tells us how much we're undercounting or overcounting precision. Cross-tool agreement on ignored flags provides a lower bound on valid comments that weren't acted on. Pattern acceptance rates let us estimate the mix of noise versus valid-but-not-acted-on within categories of similar comments.
- *Distributional representativeness.* Comparing the distribution of repos, languages, diff sizes, and PR types in our offline dataset against what tools encounter in the wild tells us whether we're benchmarking on a representative sample.
- *Overcounting detection.* Comparing action rates across severity levels, tracking whether fixes stick or get reverted, and cross-referencing with repos that don't have tools installed all help us identify cases where developer behavior is compliance-driven rather than signal of genuine agreement.

This is why we're launching with the online benchmark as the headline metric. It's grounded in real behavior and doesn't require us to have solved every methodological challenge upfront. As we validate and improve the offline benchmark against these signals, it will gradually take over as the primary metric — at which point it can do what the online benchmark can't: make controlled, fair comparisons between any tool or model on identical inputs.

---

## 4. Current implementation

This section describes what we've built and deployed today. The methodology sections that follow (§5–8) describe how we plan to improve it.

### Online benchmark

Each day, we collect events from code review tools using the GHArchive dump, searching for events containing the agent ID associated with each tool. We group these events into PRs.

We filter to projects with more than 1,000 PRs, to avoid low-volume projects where behavioral signals are likely to be noisier. We plan to revisit this threshold — smaller projects may still provide useful data, but we need to examine where they do and don't before including them.

For each tool, we randomly sample PRs each day. If the sample size required to compute a statistically significant mean would exceed 10% of the population, we compute the population mean directly by pulling all examples.

We compute the following metrics for each tool:

*Performance metrics:*
- % of comments acted on (a proxy for precision)
- \# of comments acted on (a proxy for recall — though imperfect, since recall requires a denominator of total bugs that exist, which the online benchmark doesn't have)
- $F_{\beta}$

*Activity metrics:*
- \# of comments per PR
- Total comments made
- Total PRs responded to
- Total comments acted on

*Distributional controls:*
- Distribution of diff size, repo size, language, backend vs. frontend, and other repository and PR characteristics across tools, to identify whether tools are operating on comparable datasets

We plot these statistics over time to track trends in tool performance and usage patterns.

### Offline benchmark

We build on the Augment/Greptile dataset. For each PR in the dataset, we:

1. Fork a copy of the repository for each code review tool being evaluated.
2. Open a PR that includes the description from the original human-authored PR.
3. Trigger the code review tool in the forked repo on GitHub.
4. Collect all issues identified by the tool, splitting multi-issue comments into individual issues.
5. Run a judge to evaluate which tool-identified issues correspond to issues in the gold set.

The judge uses the following prompt:

> You are evaluating AI code review tools. Determine if the candidate issue matches the golden (expected) comment.
>
> Golden Comment (the issue we're looking for): {golden_comment}
>
> Candidate Issue (from the tool's review): {candidate}
>
> Instructions:
> - Determine if the candidate identifies the SAME underlying issue as the golden comment
> - Accept semantic matches — different wording is fine if it's the same problem
> - Focus on whether they point to the same bug, concern, or code issue
>
> Respond with ONLY a JSON object: {"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}

Matches against the gold set are counted as true positives. Precision and recall are computed from these matches.

The code to reproduce this is available at: https://github.com/withmartian/code-review-benchmark

### Known limitations of the current implementation

The current implementation is deliberately minimal — a starting point we can measure and improve against. The main limitations, each addressed by a subsequent section of this document:

- Bug definitions are implicit in the gold set rather than explicit or conditioned on user preferences (§5).
- Recall is capped by the gold set, which is itself capped by human performance (§6).
- Precision treats all non-action as false positives (§7).
- The dataset is static and based on older PRs, with contamination risk (§8).
- The judge is a single LLM with a single prompt, with no calibration against human annotations (§9).
- There is no standardized harness — we're measuring product performance, not model performance (§9).

---

## 5. Defining what counts as a bug

In order to measure precision and recall, we need a clear definition of what counts as a bug. This is harder than it sounds, because the answer depends on the user.

Every code review company hears the same competing complaints: "your tool is too noisy" and "your tool missed something important." Both are right. One team wants style issues flagged; another wants only critical bugs. Without conditioning on these preferences, precision and recall are ill-defined. A tool penalized for "false positives" on one team's benchmark might be surfacing exactly what a different team asked for.

The classic approach is to condition on the relevant variables before generating output. We want to condition on features about the user: will a user with properties X, Y, Z consider this to be a bug? In theory, a sufficiently powerful agent could draw on a user's entire GitHub history, social media presence, dev videos — an enormous amount of signal. To keep the problem tractable, we focus on the highest-signal definitions: agents.md and claude.md files.

These files are the most explicit statement a developer makes about what they want from automated tools. We give the same spec to the tool under evaluation and to the grader, so both metrics are measured against the same standard. A comment that matches the spec is a true positive regardless of whether the user acts on it. This means precision and recall aren't just numbers — they're answers to the question "does this tool do what *this user* asked for?"

We can validate this approach in OSS data. Given a flagged issue and an agents.md file, can we better predict whether the developer acts on it? If the spec improves prediction, it's capturing real preferences. This gives us a correlational measure, not a causal one, but that's sufficient for a benchmark — we're measuring "does the tool match the spec," and if the spec correlates with real preferences, that's enough. We could strengthen this to a causal measure by finding repos where agents.md was added or meaningfully changed and comparing developer behavior on reviews before versus after. The deltas from such an experiment would also help capture implicit preferences that aren't mentioned in agents.md files.

**Generating representative specs.** Since most repos don't yet have agents.md files, we need to generate representative ones for the benchmark. We're exploring several approaches, which aren't mutually exclusive:

*Infer from behavior.* Use the action signals from the online benchmark — did the developer fix the issue, thumbs-up the comment — to infer implicit preferences for repos without agents.md. Translate those behavioral patterns into synthetic specs. This is the most principled approach but also the most complex.

*Sample from existing specs.* Analyze existing agents.md files to understand the space of possible preferences, then generate synthetic variations to cover that space. This accepts that we're modeling the preferences of AI-forward teams rather than all teams — but those are likely the most relevant users for this benchmark.

*Taxonomic sampling.* Define a taxonomy of bug categories (security, style, logic, performance) and preference dimensions (severity threshold, verbosity tolerance), then sample from that space. This doesn't require knowing the real distribution — we're covering the space of reasonable preferences.

*Stratify by repo characteristics.* Condition on observable features — language, repo size, team size, domain — and generate appropriate specs for each stratum. A small Python data science repo probably has different preferences than a large enterprise Java codebase.

We'll likely use a combination and validate against the behavioral signals we observe in OSS data. With a sufficiently broad set of definitions running against a sufficiently broad set of repos, this should serve as a good proxy for "does the tool catch real bugs."

**Human definitions versus machine definitions.** In the short term, code review serves human developers. In the long term, it will serve autonomous code generation systems. These audiences have different needs. Humans respond to tone — historically, code review has been political, and "we should fix this" lands differently than "you should fix this." Machines don't care about tone. Conversely, machines might want every marginal issue flagged, while a human would tackle only the highest-value ones. Different coding agents might have different preferences based on their architecture or harness, paralleling the infinity of bug definitions for human developers.

This is a legitimately difficult problem because it can't be validated against OSS data — there's no behavioral signal for how well a code review helps an autonomous agent. The relevant metric would be improvement in code generation when a generation agent has access to a review agent as a tool, but measuring that requires a good measure of code generation quality, which is exactly what code review is for.

For some bug types — security vulnerabilities, logic errors that cause failures — human and machine definitions likely converge. We intend to focus on the human problem first, and only shift focus to the machine problem once human-preference reviews are close to saturated or we see clear divergence in what makes a review useful to a human versus a machine.

When we do turn to the machine case, injection likely becomes the right methodology. For human-written code, we prefer backtracking — we want real bugs that humans actually write. For machine-generated code, machine-injected bugs may be more representative, because we want to test against the kinds of errors machines make. Injection also lets us tune the distribution — selecting for harder bugs, rare categories, or bugs that match specific models' failure modes. For training verifiers via RL, this control over difficulty and distribution is a feature. But we first need to verify that any injection system is meaningful — that it produces bugs representative of real data we care about. Starting with human annotation and backtracking, both connected to clear human signals, lets us develop and validate injection systems from a solid foundation.

---

## 6. Measuring recall

Recall requires knowing what bugs exist in order to measure how many were caught. But if we already knew all the bugs, we wouldn't need the tool. The standard approach is to create a gold set of known bugs and check whether the tool finds them. This has a fundamental problem: the gold set caps measurement at human performance. If a model finds a real bug that human annotators missed, it gets penalized — the bug isn't in the gold set, so the discovery is scored as a false positive rather than evidence of superior recall. Existing benchmarks structurally cannot measure superhuman performance and will actively punish it.

Our goal is to raise the ceiling beyond what humans can find unaided. We do this through four complementary approaches.

**Over-generate and filter.** Rather than relying on humans alone to construct the gold set, we use a human-model hybrid (centaur). Given a code sample and a bug definition (from an agents.md file), we ask LLMs to generate many candidate bugs — varying the model and the definition to cover more of the space. The goal is to cast a wide net, accepting false positives at the generation stage. We then filter through one or more of three approaches: judge-based filtering (give a judge the bug definition and each candidate, check compliance), behavioral filtering (use historical GitHub data to calibrate — find PRs where a tool left a comment, check whether the developer acted on it, fit a classifier to this signal and use it to filter candidates), or human filtering (pay annotators to review each candidate against the bug definition). Human filtering is the most reliable but the most expensive; the other two methods let us scale while reserving human review for calibration and edge cases.

This raises the ceiling from "bugs humans can find" to "bugs humans can recognize when shown." Recognition is easier than generation, so this captures bugs humans wouldn't have spotted on their own but can verify when presented. It's still a ceiling — a sufficiently advanced model might find bugs humans can't recognize even when shown — but it's a meaningful improvement over the current state.

**Held-out production bugs.** Some bugs escape review entirely and are only discovered in production. These are direct evidence of what human reviewers missed — and a direct test of superhuman performance. We collect these by finding GitHub issues labeled "bug" that reference a fix PR, then tracing the fix back to the PR that introduced the bug using git blame. Additional sources include reverts and hotfixes traced to their origin, and CVEs and security advisories that identify vulnerable commits. This is labor-intensive, so we use these as a validation set rather than the full benchmark — but they're the cleanest test of whether a tool can catch what humans can't.

**Adversarial validation.** When a tool flags something the gold set doesn't include, that's either a false positive or a bug we missed. Investigating every case is prohibitively expensive, so we do it strategically. On new model releases, we sample disagreements between the model and the gold set — this is the most likely moment to encounter a capability jump. When rankings would change — if penalizing unrecognized flags determines whether Tool A or Tool B wins — we investigate those cases before publishing. When multiple strong tools agree against the gold set, that's a strong signal to investigate. And periodically, we sample disagreements to estimate the gold set's false negative rate. This process means the gold set improves over time, driven by the tools themselves.

**Bug lifetime analysis.** From GitHub data, we can trace bug fixes back to the PRs that introduced them, giving us the lifespan of each bug — time from introduction to discovery. Fitting a survival curve to this data tells us how long bugs typically live before being caught, and how that varies by bug type, language, and codebase size. Given a PR of a certain age, we can estimate what fraction of its bugs have been discovered versus remain latent. This gives us a probabilistic bound on gold set completeness without claiming to have found everything.

Bug lifetime also serves as a natural severity proxy. A bug that survives decades without causing problems is, by revealed preference, low-priority. Short-lived bugs were caught fast because they hurt. Medium-lived bugs caused enough friction to eventually fix. Bugs never found are either very subtle or genuinely unimportant. We can validate this by having humans rate severity on a sample of bugs without knowing their lifespan and checking the correlation. If lifespan correlates with severity, it's a valid proxy and we can use it to weight the benchmark toward bugs that matter. If it doesn't — if lifespan is driven by discoverability or code churn rather than impact — we learn that too, and can build a better model combining lifespan with code area and bug type.

Bug lifetime also helps us prioritize adversarial validation. If a PR has far fewer bugs in the gold set than the survival model predicts, that's a signal we're missing something. Focusing investigation on these high-gap PRs is more efficient than sampling uniformly — they're more likely to yield gold set expansions.

**Validating recall against the online benchmark.** We compare offline recall rankings against in-the-wild recall signals. If a tool ranks high on benchmark recall but developers frequently encounter bugs it missed, the benchmark is miscalibrated. We also check whether the types of bugs in our benchmark match the types that escape review in practice. If the distribution diverges — if we're overrepresenting one category and underrepresenting another — we adjust accordingly.

---

## 7. Measuring precision

Precision has the opposite problem from recall. Where recall asks "did we find everything that exists," precision asks "was what we flagged actually worth flagging." The naive approach — treat everything the developer acted on as a true positive and everything else as a false positive — conflates "wrong" with "not immediately acted upon."

Consider a tool that flags "this approach works but will be slow at scale." The developer ships it anyway — they know, and they're choosing speed-to-market over performance. Under current benchmarks, that flag counts as a false positive, scored the same as a hallucinated bug that doesn't exist. The tool gets penalized for being useful.

This breaks into two cases. First, comments acted on later but outside the PR. The developer agrees it's worth fixing, but not right now — maybe the fix requires a larger refactor, or they want to keep the PR focused. The comment was valuable but shows up as a false positive because we're only looking at actions within the PR. Second, useful information the developer wouldn't act on regardless. The comment surfaces something worth knowing even if it doesn't change the code. "This will be slow at scale" is valuable context even when the developer intentionally ships it. These aren't noise, but current metrics treat them identically to hallucinations.

We address this through three complementary approaches.

**Conditioning on user preferences.** This is the cleanest solution because it makes precision definitional rather than behavioral. If a user's agents.md file specifies "I want performance warnings even if I won't act on them," then a performance warning is a true positive regardless of whether the developer acts on it. The grader uses the same spec as the tool, so there's no mismatch. We're not inferring what the user wanted — they told us. This aligns directly with our approach to bug definitions in §5: both precision and recall are measured against the same standard.

**Human categorization of non-action.** Rather than treating all non-action as false positives, we have annotators classify ignored comments. Given the comment, the code, and context, annotators judge whether the comment was useful and actionable, useful but not actionable, noise, or actively misleading. This directly measures what we care about rather than inferring from behavior. We use this to calibrate our behavioral proxy — if annotators say 30% of "false positives" were actually useful, we know our precision metric is undercounting by roughly that amount.

**Comment afterlife.** We track what happens to ignored comments over time to estimate how much we're undercounting or overcounting precision. Several signals are useful here:

*Pattern acceptance rate.* Group similar flags — all "unused variable" warnings, all "missing null check" flags — and compute the acceptance rate per pattern. If a pattern is acted on 80% of the time, the other 20% probably aren't all hallucinations. This lets us estimate the mix of noise versus valid-but-not-acted-on within each category.

*Cross-tool agreement on ignored flags.* If multiple independent tools flag the same issue and the developer doesn't act, that's more likely valid than a hallucination. Agreement rate gives a lower bound on undercounted precision.

*Ignored comment to later fix rate.* Track whether code that was flagged but ignored gets changed later in a way that addresses the flag. Did the developer fix the issue in a subsequent commit days or weeks later? Did they open an issue referencing it? This requires semantic matching of later changes to original flags — significant infrastructure investment, but it directly captures the "acted on later" case.

*Ignored comment to future bug rate.* Track whether flagged-but-ignored code is later involved in a bug fix. Very noisy, but it directly measures "the tool was right, the developer was wrong."

Comment afterlife also helps us detect when we're *overcounting* precision — when developers fix things not because they genuinely agree but because clearing bot comments is easier than arguing. Several signals help here:

*Effort proportionality.* If a developer genuinely thinks something is a bug, the fix probably involves real thought — multiple lines changed, tests added, meaningful refactoring. A one-line mechanical change looks more like clearing a bot comment. Classifying fixes by complexity and checking whether acceptance rates differ between trivial and substantive fixes can reveal undiscriminating compliance.

*Action rate across severity levels.* If developers exercise real judgment, they should fix critical bugs at a higher rate than style nits. A roughly flat acceptance rate across severity suggests a "fix everything" policy rather than genuine signal about comment quality.

*Fix durability.* If a developer fixes something they didn't actually think was broken, there's a higher chance it gets reverted, causes a regression, or gets undone in a later refactor. Tracking whether fixes stick is a stronger signal than tracking whether they happen.

*Cross-referencing with repos without the tool.* If a bug pattern gets fixed at roughly the same rate in repos without automated review, it was probably a real bug developers would have caught anyway. If it only gets fixed in repos with the tool, it might be compliance-driven. This is a natural experiment using tool installation as treatment.

*Weighting by confidence indicators.* Rather than treating all fix actions equally, we can weight them by signals that correlate with genuine agreement — fix complexity, whether the developer left a comment acknowledging the issue, whether similar patterns get fixed consistently across the repo. This doesn't eliminate compliance bias but attenuates it.

**Future refinements to precision measurement.** Several additional signals could enrich our precision measurement over time. Action rate by comment position — whether developers act on the first comment on a PR differently from the fifth — could distinguish comment quality decline from developer fatigue. Time-to-action — how quickly after a comment the developer responds — may correlate with comment value, with faster action indicating more obvious or urgent issues. And where platforms distinguish between explicitly dismissing a comment and simply ignoring it, dismissal is a stronger "not useful" signal than silence. These are calibration improvements rather than core methodology, but they'd sharpen our precision estimates as we scale.

---

## 8. Avoiding staleness and contamination

Static benchmarks decay. Coding practices evolve — new languages, frameworks, and patterns emerge. A benchmark built on 2020 code may not reflect 2025 development practices. More critically, most public code has been seen by LLMs during training. If the benchmark uses old, widely-available code, models may have memorized the bugs or the fixes, and we'd be measuring recall from memory rather than generalization. Even without intentional training on the benchmark, if the data is public long enough, it will end up in training sets. This is especially damaging for benchmarks that never refresh — over time, every model has seen the test set.

**Continuous refresh.** We generate a new iteration of the dataset each month from PRs merged in the prior month, using only recent code that post-dates model training cutoffs where possible. The benchmark is a moving target — models can't memorize it because it keeps changing. Each monthly iteration is versioned and published.

**Annotation at the source.** We have PRs annotated by the people who maintain the repos or created the PRs. They have the context to judge whether a flagged issue is real, whether something was intentionally left unfixed, and what the repo's standards are. This is more expensive than third-party annotation but higher signal.

**Held-out test sets.** We may maintain a held-out test set that is never published — derived from the same methodology but kept private to prevent contamination. Public and private sets are generated identically, so performance on the public set should predict performance on the private set.

**Detecting contamination.** We track whether model performance jumps suspiciously on older benchmark versions versus newer ones. If a model does significantly better on v1 (public for a year) than v12 (released this month), that's a contamination signal. We can also check for verbatim memorization — whether the model reproduces exact code snippets or bug descriptions from the benchmark.

**Tradeoffs.** Continuous refresh means continuous annotation, which is expensive. We manage this by focusing human annotation on validation and calibration while using automated pipelines for bulk data collection. There's also a tension between recency and representativeness — very recent code might not yet have revealed its bugs, since short bug lifetimes mean some real issues haven't surfaced yet. We balance this by including both fresh PRs (for contamination avoidance) and traced historical bugs (for known ground truth), and by using the survival analysis from §6 to estimate how many bugs in recent PRs remain undiscovered.

**Maintaining comparability across versions.** If the benchmark changes monthly, raw precision and recall numbers aren't directly comparable across versions — a harder iteration will produce lower scores even for the same model. We need a way to compare performance over time.

*Anchor models.* Run a fixed set of tools on every benchmark version. Their scores provide a calibration point — if the anchor scores 70% on v1 and 60% on v2, v2 is harder, and we normalize other tools' scores accordingly. Simple to implement, but requires committing to running anchors indefinitely.

*ELO / Bradley-Terry ratings.* Treat each benchmark version as a tournament. Compute head-to-head win rates between tools on each version. ELO (or Bradley-Terry, the more statistically principled version) gives each tool a rating that updates over time. Ratings are comparable across versions because they're relative to other tools, not absolute scores. Requires multiple tools participating consistently.

*Item Response Theory.* Borrowed from standardized testing — how the SAT and GRE maintain comparability across test forms. Each bug has a difficulty parameter, each tool has an ability parameter. IRT estimates these jointly, so ability scores are comparable even when bug sets differ. More complex to implement but theoretically the cleanest approach.

*Overlapping items.* Include a small subset of items that appear across multiple versions. Performance on the overlap calibrates across versions. The risk is that overlapping items become stale or contaminated over time, so this is a short-term bridge rather than a long-term solution.

Our approach is to start with anchor models for simplicity, add ELO for relative rankings once we have multiple tools participating consistently, and consider IRT if we need rigorous cross-version ability estimates for research purposes.

---

## 9. Judges and infrastructure

The methodology described in §5–8 depends on two things working well: LLM judges that score accurately, and infrastructure that makes evaluation reproducible. This section covers both.

### Judge quality

LLM-as-a-judge systems have bias and variance. Different judges — or the same judge with different prompts — can produce significantly different results. If benchmark rankings depend heavily on judge choice, the benchmark is measuring the judge as much as the tools.

**Sources of judge error.** Bias: systematic over- or under-counting of certain bug types — a judge might be lenient on security issues but strict on style. Variance: same input, different outputs across runs, meaning results aren't perfectly reproducible. Miscalibration: judge confidence doesn't match accuracy, so the judge is confidently wrong. Ceiling effects: if the judge can't recognize a bug, it can't score tools that find it — the superhuman problem again, now at the judge level.

**Where we need judges.** Judges appear throughout the methodology, not just in the core evaluation. We need them for matching tool comments to gold set items (the core evaluation task), filtering generated bugs in the over-generate-and-filter pipeline, classifying whether user actions were caused by a tool comment or unrelated, categorizing non-action as noise versus useful-but-not-actionable versus wrong, assessing severity, and semantic matching for comment afterlife. Each of these tasks has different error profiles and different tolerance for mistakes.

**How we address this.** We calibrate judges against human labels — for each judge task, we collect a sample of human annotations, measure judge accuracy against them, and adjust for systematic biases. Where possible, we also calibrate against behavioral data, validating judge outputs against OSS signals like user actions and bug lifetimes. We run ensemble judges — multiple models, multiple prompts — and aggregate to reduce variance and surface disagreements. We set confidence thresholds, routing low-confidence cases to human review rather than accepting noisy outputs. We do adversarial validation for judges specifically: when tools and judges disagree, we investigate, because the tool might be right and the judge wrong. And we version and document everything — exactly which judge model, prompt, and parameters were used — publishing judge accuracy metrics alongside benchmark results so readers can assess how much the judge matters.

### Infrastructure for reproducibility

Running code review benchmarks currently requires forking repos, opening PRs, configuring tools, triggering reviews, collecting results, handling failures, and retrying. This is laborious and error-prone.

**Why GitHub is the common interface.** Most code review tools integrate with GitHub, and PRs are the natural unit of review. But GitHub wasn't designed for benchmarking — triggering hundreds of PRs reliably is awkward. We work within this constraint because it's where the tools are, but we wrap it to make it manageable.

**A CLI for replication.** We're building a command-line tool that wraps the benchmark process: forking, PR creation, tool triggering, result collection. Something like `codereview-bench run --tool=X --dataset=v12`. This lowers the barrier to participation — more tools evaluated means more robust rankings. As a side benefit, the CLI could also be used to invoke code review tools during code generation, effectively giving a code generation agent access to a reward estimator at test time. This connects to the e2e pipeline discussion in §11.

**A standardized evaluation harness.** To compare raw model performance rather than model-plus-proprietary-harness, we need a minimal, standardized harness — analogous to mini-swe-agent for SWE-Bench. This separates "how good is the model" from "how good is the product," which matters for both researchers and tool builders. Without this, the offline benchmark has the same confound as the online benchmark: you can't tell whether a tool performs well because the model is good or because the engineering around it is good.

The harness defines a standardized interface. Inputs: the diff, file context, repo context, agents.md file if present, and optional additional signals like PR description and linked issues. Outputs: a list of comments, each with file, line number, description, severity, and an optional suggested fix. Consistent formatting means evaluation can be automated without per-tool parsing.

We want to define this interface collaboratively with tool builders. A standardized harness benefits everyone — fairer comparisons for us, less benchmark-specific engineering for them. This is discussed further in §10.

---

## 10. Working with tool builders

This benchmark is useful only if the companies building code review tools trust it enough to engage with it. We're designing the process to earn that trust through involvement, not just transparency.

**Pre-publication review.** Before publishing results, we want every builder to review our methodology and confirm we're representing their tool fairly. We'd rather catch errors and blind spots before publishing than correct them after. This applies both at launch and for subsequent releases — expanded datasets, improved judges, new metrics. We share methodology and results with builders before publication so they're checking our work at each step, not just reacting to it.

**Collaborative interface design.** The standardized evaluation harness described in §9 defines inputs and outputs for comparing raw model performance. We want to define this interface with builders, not impose it. They know their tools' capabilities and constraints better than we do, and a collaboratively designed interface is one builders will actually adopt. The spec covers inputs (diff, file context, repo context, config files) and outputs (comments with file, line, description, severity). Getting this right benefits everyone: fairer comparisons for us, less benchmark-specific engineering for them.

**Structured relevance.** We'll structure updates so they're relevant to the companies involved. If a release highlights a capability one tool does particularly well — say, a new evaluation of security-focused review — that's something worth talking about. Builders are more likely to engage with a benchmark that occasionally showcases their strengths than one that only produces rankings.

**The long-term goal is adoption as a standard.** When companies release new tools or models, they report scores on Code Review Bench — the way SWE-Bench works for code generation or Terminal Bench for agent performance. We get there by making the benchmark credible enough that good scores are worth advertising and the methodology robust enough that builders trust unfavorable results too.

---

## 11. Future directions

The methodology described so far focuses on measuring whether code review tools find bugs that matter. As the benchmark matures, several natural extensions open up.

**Richer context for evaluation.** The current benchmark focuses on a narrow slice of context: the signals present in the repo. But real code review happens in a richer environment. Issue trackers (Jira, Linear, GitHub Issues) contain intent — why is this PR being made? What problem is it solving? A bug fix for a critical production issue should be reviewed differently than a speculative refactor. Conversation history in Slack threads, PR comments, and design docs explains why code looks the way it does. CI/CD context tells you what tests exist, what the deployment environment is, and what broke last time. Historical accept/reject patterns reveal per-user and per-team preferences over time.


This matters for the benchmark because tools that use richer context may perform better in practice but worse on a context-limited benchmark. If we don't include these signals, we penalize tools for using information that real developers would have. As tools get more sophisticated, the benchmark needs to keep pace. We'll expand the set of available inputs as we learn which context signals actually improve tool performance in practice.

**Richer output evaluation.** Currently we measure whether a tool found the bug. We could also measure whether it pointed to the right line, whether it explained the issue correctly, and whether it suggested a valid fix.

Localization accuracy — requiring both a correct description and a correct file/line reference — is something Qodo's benchmark already evaluates. We don't think this is critical today; a comment that identifies the right bug but points to line 42 instead of line 44 is still valuable. But we're open to learning this matters more than we expect.

Fix quality is the more significant extension. Many tools already suggest patches, but we don't currently measure whether those patches are correct, whether they introduce new bugs, or whether they're stylistically appropriate. Measuring fix quality requires execution-based validation (does the suggested fix pass existing tests?), human preference judgments (given the original bug and two fixes, which do reviewers prefer?), and downstream tracking (do merged fixes hold up or get reverted?). A tool that detects bugs but suggests bad fixes might be worse overall than one that detects fewer bugs but fixes them correctly. Measuring the full loop matters.

**The end-to-end code generation pipeline.** Good code review is equivalent to a good verifier for code generation. Generating correct code is hard; checking whether code is correct may be easier. Training code generation models via RL requires exactly such a verifier as the reward signal. This creates a flywheel: the generation model produces code, the review model evaluates it, the review signal becomes the reward, and the generator improves — which in turn stresses the reviewer to find subtler bugs.

A benchmark that reliably measures review quality is therefore a foundation for training better code generators. As generators improve, the benchmark needs to keep pace, which connects back to adversarial validation and continuous refresh. And as discussed in §5, the transition from human-focused to machine-focused code review will eventually require different evaluation criteria — different bug definitions, different precision/recall tradeoffs, and likely injection-based rather than backtracking-based methodology.

The CLI described in §9 is a first step toward this. By making it easy to invoke code review tools programmatically, it enables code generation agents to use review as a tool at inference time — effectively giving them access to a reward estimator. This is speculative for now, but it's where we think the highest long-term value lies.

---

## 12. Roadmap

We're developing the benchmark in stages, starting with what we can measure most credibly and expanding as the methodology matures.

**Stage 1: Online benchmark.** We launch with the online benchmark — how developers actually respond to code review tools in OSS repos — alongside an initial offline benchmark based on Augment and Greptile's work. The online benchmark is the headline metric at launch. It's grounded in real behavior, doesn't require us to define what a bug is (developers vote with their actions), and gives every installed tool a fair starting point. The offline benchmark exists from day one but plays a supporting role while we validate and improve it. Alongside these, we publish an analysis of the distributional differences between the online and offline data — where they agree, where they diverge, and what that tells us about the offline benchmark's representativeness.

**Stage 2: Validate and improve the offline benchmark.** We use online signals to calibrate the offline benchmark — checking whether offline rankings match what we see in the wild, identifying where they diverge, and fixing the gaps. This is where most of the methodological work described in this document happens:

- Improve the comment equivalence judge — calibrate against human annotations and behavioral data, run ensembles, set confidence thresholds.
- Improve the gold set labels — measure bug lifetimes to estimate completeness, measure comment afterlife to calibrate precision, add back-tracked production bugs.
- Condition on bug definitions — analyze existing agents.md files, develop generation approaches, integrate into both tool evaluation and grading.
- Expand the PR and repo set — analyze distributional differences between the current dataset and in-the-wild data, determine what the dataset should look like to answer key questions with statistical significance, construct the expanded set. Honestly, we need to look at enough data to have a good sense of how to do this well.
- Run a human study with over-generate and filter to measure how much we close the gap identified by bug lifetime and comment afterlife analyses.
- Evaluate bug injection as a complement to backtracking, likely after the agents.md work provides a foundation for validating injected bugs.
- Begin adversarial validation, following the triggers described in §6.
- Segment results by repo characteristics, language, and other dimensions once we have sufficient data.
- Build the standardized evaluation harness and begin benchmarking raw models separately from products — relevant for tool builders choosing which models to build on and researchers studying model capabilities.

At each step, we calibrate any LLM-as-a-judge systems against human annotations or OSS data.

**Stage 3: Foreground the offline benchmark.** Once the offline benchmark reliably reflects real-world tool value — validated against online signals — it becomes the primary metric. At this point it can do what the online benchmark can't: make controlled, fair comparisons between any tool or model, including new ones without OSS installation. Monthly dataset refresh and cross-version comparability via anchor models and eventually ELO/IRT ratings make this sustainable.

**Ongoing: monthly refresh.** Each month, we generate a new iteration of the dataset from PRs merged in the prior month, annotated by repo maintainers and PR authors. Each iteration is versioned and numbered. Anchor models run on every version to maintain comparability. This continues indefinitely — it's not a stage, it's the steady state.

Each stage is documented, versioned, and open for feedback.

---

## 13. Credibility and independence

If we close the Goodhart gap sufficiently, then optimizing for the benchmark is optimizing for the real thing. That's the goal.

But methodology alone isn't enough. There's a prior question: why trust that the methodology is actually designed to close the gap, rather than to favor certain players?

This is the political economy of benchmark construction. Benchmarks from model companies invite suspicion that they favor their own models. Benchmarks from code review vendors invite suspicion that they favor their own tools. Academic benchmarks lack resources for ongoing maintenance against Goodhart pressure—which is why most benchmarks decay into unreliability over time.

Our structure addresses this directly:
* Financial independence. We're well funded and can continue to run the benchmark independent of the companies we're measuring.
* Broad collaboration. We work with all tool builders on interface definitions, not just some. No one gets privileged influence.
* Radical transparency. The methodology is public. Choices that favor one player over another would be fully visible.
* External anchors. Behavioral signals from OSS data aren't controlled by us or by vendors. Reality is the referee.
We're building a structure where distorted intentions would be visible and correctable, not just asking you to trust our intentions.

Our hope is to create a measure for the most economically valuable use case of AI — one that is truly accurate, and remains so.
