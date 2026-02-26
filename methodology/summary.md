# Code Review Bench

There's no canonical benchmark for code review. Existing benchmarks are small (50–100 PRs), vendor-published (the vendor's tool wins), unrefreshable (stale data, training contamination), and under-defined (no shared notion of what counts as a bug or how to measure precision and recall).

Code review is the verification step for code generation — and verification is the easier side of the problem. This makes it one of the best levers for improving code generation, and one of the most economically valuable uses of AI to measure well.

Code Review Bench is our attempt to do better.

This doc lays out our methodology at a high level (~1.5K words), but links to the relevant sections of our [detailed methodology](./crb_full_methodology.md) (~9k words).

## The core problem

Most critiques of benchmarks reduce to Goodhart's Law: when a measure becomes a target, it ceases to be a good measure. Companies optimize for benchmarks. If the benchmark doesn't measure what actually matters, optimization makes products worse while appearing to make them better.

The gap between measure and intent is the problem. To close it, you need two things: a way to detect when your benchmark is measuring the wrong thing, and a way to fix it when it is. Code Review Bench is built to do both.

## How we're different: offline + online

Our benchmark has two pieces:

**An offline benchmark** runs every tool on the same PRs with the same bug definitions and scores them against a curated gold set. This is the measure — it lets us make controlled comparisons. The v0 builds on Augment and Greptile's published dataset and improves from there.

**An online benchmark** teaches us user intent based on how developers actually use these tools. When a developer fixes a problem flagged by a tool, they're voting that the flag was useful. These behavioral signals aren't controlled by us or by vendors — they're anchored to what actually happens.

The offline benchmark can make fair tool-to-tool comparisons; the online benchmark can't (you can't run two tools on the same repo, and tool adoption correlates with repo characteristics). But the offline benchmark might measure the wrong thing. The online benchmark catches that. By checking one against the other, we keep the measure aligned with intent. *(See [Our approach: offline + online](./crb_full_methodology.md#our-approach-offline--online) in the detailed methodology.)*

## How we measure

Both benchmarks measure precision and recall, but measuring each is harder it sounds.

**Recall** requires knowing what bugs exist — but if we already knew, we wouldn't need the tool. Worse, if a tool finds a real bug that human annotators missed, current benchmarks penalize it: the bug isn't in the gold set, so the discovery is scored as a false positive. Existing benchmarks structurally cannot measure superhuman performance and will actively punish it.

We address this by raising the ceiling beyond what humans can find unaided:

- *Over-generate and filter.* Use LLMs to generate candidate bugs, then use humans (or calibrated judges) to filter. Recognition is easier than generation, so this captures bugs humans wouldn't have spotted on their own but can verify when shown.

- *Held-out production bugs.* Trace bugs discovered in production back to the PRs that introduced them. These are bugs humans actually missed — a direct test of superhuman performance.

- *Adversarial validation.* When a tool flags something the gold set doesn't include, we investigate strategically: on new model releases, when rankings would change, when multiple strong tools agree against the gold set.

- *Bug lifetime analysis.* Fit survival curves to historical bug data to estimate how many bugs remain undiscovered in a given PR, giving us a probabilistic bound on gold set completeness.

*(See [Measuring recall](./crb_full_methodology.md#measuring-recall) in the detailed methodology.)*

**Precision** has the opposite problem. Suppose a tool flags "this approach works but will be slow at scale." The developer ships it anyway — they know, and they're choosing speed-to-market over performance. Under current benchmarks, that flag counts as a false positive, scored the same as a hallucinated bug that doesn't exist. The tool gets penalized for being useful.

The core issue is that "not acted on" and "wrong" aren't the same thing. We separate them through:

- *Human categorization of non-action.* Annotators classify ignored comments as useful-but-not-actionable, noise, or harmful — rather than treating all non-action as false positives.

- *Comment afterlife.* Track whether flagged-but-ignored code gets changed later, or whether similar flags have high acceptance rates in other contexts — signals that "not acted on" ≠ "wrong."

- *Conditioning on user preferences.* agents.md / claude.md files let users specify what they want flagged. A comment that matches the spec is a true positive regardless of whether the user acts on it.

*(See [Measuring precision](./crb_full_methodology.md#measuring-precision) in the detailed methodology.)*

## Defining what a bug is

In order to measure precision and recall, we also need a clear definition of what a bug is.

Every code review company hears the same competing complaints: "your tool is too noisy" and "your tool missed something important." Both are right — because what counts as a bug depends on the user. One team wants style issues flagged; another wants only critical bugs. Without conditioning on these preferences, precision and recall are ill-defined. A tool penalized for "false positives" might be surfacing exactly what a different user asked for.

We address this by conditioning on explicit user preferences — primarily agents.md and claude.md files. The same spec is given to the tool under evaluation and to the grader, so both metrics are measured against the same standard. This means precision and recall aren't just numbers — they're answers to the question "does this tool do what *this user* asked for?"

We can validate this approach in OSS data: given a flagged issue and a config file, can we better predict whether the developer acts on it? If the spec improves prediction, it's capturing real preferences.

Since most repos don't yet have these files, we'll need to generate representative ones. We're exploring several approaches — inferring from behavioral data, sampling from a taxonomy of preferences, and stratifying by repo characteristics. *(See [Defining what counts as a bug](./crb_full_methodology.md#defining-what-counts-as-a-bug) in the detailed methodology for details on each approach.)*

## Avoiding staleness and contamination

Static benchmarks decay. Code seen during training gets memorized. We counter this with monthly refresh: each iteration uses PRs from the prior month, versioned and numbered. Anchor models run on every version to maintain comparability across iterations.

We're also evaluating ELO/Bradley-Terry ratings and Item Response Theory (borrowed from standardized testing) for cross-version comparisons as the benchmark matures. *(See [Avoiding staleness and contamination](./crb_full_methodology.md#avoiding-staleness-and-contamination) in the detailed methodology.)*

## How we're building this

We're developing the benchmark in stages, starting with what we can measure most credibly and expanding as the methodology matures.

**Stage 1: Online benchmark.** We launch with the online benchmark — how developers actually respond to code review tools in OSS repos — alongside an initial offline benchmark based on Augment and Greptile's work. The online benchmark is the headline metric at launch. It's grounded in real behavior, doesn't require us to define what a bug is (developers vote with their actions), and gives every installed tool a fair starting point. The offline benchmark exists from day one but plays a supporting role while we validate and improve it.

**Stage 2: Validate and improve the offline benchmark.** We use the online signals to calibrate the offline benchmark — checking whether offline rankings match what we see in the wild, identifying where they diverge, and fixing the gaps. This is where most of the methodological work described above happens: conditioning on user preferences, expanding the gold set, calibrating judges, monthly refresh.

**Stage 3: Foreground the offline benchmark.** Once the offline benchmark reliably reflects real-world tool value — validated against the online signals — it becomes the primary metric. At this point it can do what the online benchmark can't: make controlled, fair comparisons between any tool or model, including new ones without OSS installation.

Each stage is documented, versioned, and open for feedback. *(See [Roadmap](./crb_full_methodology.md#roadmap) in the detailed methodology.)*

## Working with tool builders

This benchmark is useful only if the companies building code review tools trust it enough to engage with it. We're designing the process to earn that trust through involvement, not just transparency.

**At launch,** we'll publish results for each tool on the online and initial offline benchmarks. Before we do, we want every builder to review our methodology and confirm we're representing their tool fairly. We'd rather catch errors and blind spots before publishing than correct them after.

**As we release updates** — expanded datasets, improved judges, new metrics — we'll share methodology and results with builders before publication. We want builders checking our work at each step, not just reacting to it. We'll also structure updates so they're relevant to the companies involved — if a release highlights a capability one tool does particularly well, that's something worth talking about.

**We're building a standardized evaluation harness** — a minimal, shared interface that lets us test raw model performance separately from product engineering. We want to define this interface collaboratively with builders, not impose it. The spec will cover inputs (diff, file context, repo context, config files) and outputs (comments with file, line, description, severity). This benefits everyone: fairer comparisons, less benchmark-specific engineering.

**The long-term goal is adoption as a standard.** When companies release new tools or models, they report scores on the benchmark — the way SWE-Bench works for code generation or Terminal Bench for agent performance. We get there by making the benchmark credible enough that good scores are worth advertising and the methodology is robust enough that builders trust unfavorable results too.

## How the benchmark stays fair

Benchmark credibility has historically had a structural problem: vendor-published benchmarks favor the vendor, and academic benchmarks lack resources to maintain rigor over time. We've designed around both failure modes.

The methodology is public — choices that favor one player would be publicly visible. Behavioral signals from OSS data aren't controlled by us or by any vendor; they anchor the benchmark to what actually happens in practice. We work with all tool builders on interface definitions rather than designing in isolation. And because Martian is funded independently of the code review market, our business doesn't benefit from any particular tool winning.

Our hope is to create a measure for the most economically valuable use case of AI — one that is truly accurate, and remains so.
