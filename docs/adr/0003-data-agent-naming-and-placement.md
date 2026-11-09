# ADR 0003: Data Agent Naming and Placement

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-27 |
| Scope | This repository |
| Owners | Data Platform / Architecture |
| Related | ADR 0001 (*Demo Workspace Scope and Organization*), ADR 0002 (*Scenario Source Layout and Real-Time Item Naming*) |

## Context

Demo scenarios now add a Fabric **Data Agent**: a natural-language Q&A item
over a scenario's existing semantic model and KQL database. ADR 0001 and
ADR 0002 do not cover this item type. They give it no prefix and no folder,
and they don't say where its authored content (instructions, example queries)
lives.

A data agent adds no data of its own. It only reads items that already exist
in the workspace, so ADR 0001's demo-safe data rule is satisfied by the data
sources it is bound to.

## Decision

1. **Prefix `da_`**, with environment-neutral names as in ADR 0001 (for
   example `da_mvoi`).
2. **Folder placement:** `30 - Data Products`, next to the semantic model and
   Real-Time Dashboard it is built on.
3. **Authored content lives in the scenario.** Agent instructions, per-source
   instructions and example queries are authored as plain files in
   `scenarios/<slug>/fabric/data-agent/`. The scenario's deploy script renders
   them into the item definition, following ADR 0002's single deploy path. The
   agent is not hand-edited in the Fabric UI. UI edits are overwritten on the
   next deploy.
4. **Bindings are resolved at deploy time.** Data source workspace and item
   IDs are looked up by name when the scenario deploys and are never committed,
   per the CLAUDE.md hard rule.

## Consequences

- Data agents are reviewable and repeatable like every other item.
- Tuning the agent means editing files and redeploying. That is a slower loop
  than the UI, but it keeps the instructions under version control.
- Data agents consume Copilot capacity on every question (see the scenario's
  `docs/costs.md`). They should not be wired into schedules or automation.
- The `da_` prefix extends the prefix list in ADR 0001.
