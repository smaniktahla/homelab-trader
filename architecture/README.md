# Architecture maps

Evidence-backed architecture diagrams built with [Archify](https://github.com/tt-a1i/archify)
(installed as a global agent skill; not vendored in this repo). Each `*.json`
file here is a validated, typed JSON IR -- the **source artifact**. HTML/PNG
render output goes to `architecture/generated/` and is gitignored; it is a
**compiled presentation**, regenerated on demand with:

```bash
node ~/.agents/skills/archify/bin/archify.mjs deliver architecture \
  architecture/<name>.json architecture/generated/<name>.html \
  --quality showcase --repo-root .
```

## Rules for every map in this directory

1. **One question per file.** The filename and `meta.title`/`meta.subtitle`
   must state the single architectural question the diagram answers. A map
   that can't state its question doesn't belong here.
2. **6-15 nodes, target 8-12.** More requires justification in the PR that
   adds it. The goal is comprehension of one path, not a repo map.
3. **Repository evidence required for every implementation node.** Use
   `components[].sources` (repo-relative path + line range) and pin
   `meta.repository` to a commit SHA that is actually pushed to `origin`
   (evidence links resolve against public GitHub, and Archify's own
   validator checks the SHA/paths/lines against the local git history --
   run `validate`/`deliver` with `--repo-root .` from the repo root).
   Never invent a service, queue, module, or dependency that doesn't
   appear in the code. If a node is legitimately conceptual (e.g. an
   external actor), omit evidence rather than fabricate it.
4. **Prefer behavior over directory structure.** Nodes and edges should
   describe execution/data/control flow and persistence boundaries, not a
   converted file tree.
5. **Validation is necessary, not sufficient.** A schema-valid, `showcase`-
   passing graph can still be architecturally wrong. Treat every graph as
   requiring a human/agent accuracy pass against the cited source lines
   before it's trusted for review.
6. **Freeze after `deliver`.** A passing `deliver` run freezes that JSON;
   don't hand-edit a delivered file afterward -- change it, then
   re-validate and re-deliver.

## Two kinds of map

- **`system-overview.json`** -- the one overarching map. Answers "what are
  the major subsystems and how do they depend on each other?" at the
  granularity of whole components (Signal Generation, Risk Engine, API,
  Postgres, Alpaca, ...), not individual functions. Update it only when a
  PR/epic adds, removes, or rewires a subsystem-level dependency -- this
  should be rare.
- **Question-specific maps** (e.g. `order-risk-path.json`) -- one bounded
  execution/data-flow path each, at function-and-line granularity. Create
  a new one only after the "First empirical test" bar has been cleared for
  it once (see the original evaluation write-up): validate clean, render,
  and manually check the graph against the cited source before trusting it
  for review. Don't create all plausible questions speculatively -- add
  one when an epic actually needs it.

## When to update a map

Only when a PR changes: subsystem boundaries, execution flow, major data
flow, persistence semantics, risk-control ordering, lifecycle semantics, or
an externally visible integration. Most PRs don't need this. PR description
fragment to include when it does apply:

```text
Architecture impact: yes/no
Affected architecture questions: <e.g. order-risk-path, system-overview>
Architecture graph updated: yes/no/not applicable
Architecture diff: <paste the topology-change summary from diff-map.sh, or "none">
```

## PR-level workflow

For a PR with architecture impact:

1. Edit the affected `architecture/<name>.json`, then
   `validate`/`deliver` it clean (see command above).
2. Run the diff helper against the PR's merge-base:
   ```bash
   architecture/scripts/diff-map.sh <name>
   ```
   This diffs your working copy against `origin/main`'s version (or prints
   "nothing to diff" if the map is new) and writes
   `architecture/generated/<name>.diff.html`.
3. Paste the `summary` block's `topology` counts (added/removed
   components/connections) into the PR description. `geometry`-only
   changes (moved boxes, rerouted labels) aren't worth mentioning --
   they're re-layout noise, not architecture.
4. If the diff shows a `topology` change you didn't intend (e.g. an edge
   into the risk engine silently disappeared), that's a signal to look
   again before merging -- this is exactly the "did sizing accidentally
   bypass risk clamps" check this workflow exists for.

## Epic-level workflow

An epic's PRs each produce their own small, honest per-PR diff -- but the
cumulative epic-level change is usually the one worth seeing in one place
(e.g. "across the whole VR epic, did volatility sizing end up bypassing
the existing risk clamps, yes or no"). At epic kickoff, note the commit the
epic branches from; at epic completion, diff against that commit instead
of `origin/main`:

```bash
architecture/scripts/diff-map.sh <name> <epic-start-commit-or-tag>
```

Do this once per affected map when the epic lands, not after every PR --
per-PR diffs already cover incremental review; the epic-level diff is for
seeing the epic's net effect on architecture in one pass.

## Current maps

- `system-overview.json` -- What are homelab-trader's major subsystems and
  how do they depend on each other? (ingest -> signal generation -> risk
  engine -> API -> Alpaca, plus market regime/structure, trade thesis
  lifecycle, backtest engine, Postgres, and the digest/ATQ handoff)
- `order-risk-path.json` -- How does a strategy-generated BUY proposal
  become an accepted/rejected order and reach the broker? (calc_buy_qty ->
  trade_proposals -> evaluate_proposal() binding clamp -> order submission
  -> Alpaca)
