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

## When to update a map

Only when a PR changes: subsystem boundaries, execution flow, major data
flow, persistence semantics, risk-control ordering, lifecycle semantics, or
an externally visible integration. Most PRs don't need this. Suggested PR
template fragment:

```text
Architecture impact: yes/no
Affected architecture questions: <e.g. order-risk-path>
Architecture graph updated: yes/no/not applicable
```

## Diffing an architecture change

```bash
git show origin/main:architecture/<name>.json > /tmp/base.json
node ~/.agents/skills/archify/bin/archify.mjs compare architecture \
  /tmp/base.json architecture/<name>.json /tmp/diff.html \
  --repo-root . --json
```

Read the `topology` vs `geometry` classification in the JSON summary --
`topology` changes (edges/nodes added or removed) are the ones worth
calling out in a PR description; `geometry` changes are usually just
re-layout noise.

## Current maps

- `order-risk-path.json` -- How does a strategy-generated BUY proposal
  become an accepted/rejected order and reach the broker? (calc_buy_qty ->
  trade_proposals -> evaluate_proposal() binding clamp -> order submission
  -> Alpaca)
