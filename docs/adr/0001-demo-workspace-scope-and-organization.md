# ADR 0001: Demo Workspace Scope and Organization

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-09 |
| Scope | This repository (`ws-example` and its Test/Prod counterparts) |
| Owners | Data Platform / Architecture |

## Context

We need a Fabric workspace for demo work that can be shared freely. Repo access
is all-or-nothing, so demo work has to live apart from anything that holds
non-public data.

## Decision

1. **Separate repo, separate workspace.** This repo is the source for
   `ws-example` only. Its Fabric Git folder is `/workspace`.
2. **Demo-safe data only.** Data must be synthetic, public, or explicitly
   cleared for external display.
3. **Folder organization.** A top-level folder per demo scenario, with
   numbered functional folders beneath it: `00 - Domain Platform`,
   `10 - Ingestion`, `20 - Transformation`, `30 - Data Products`,
   `40 - Orchestration`, `90 - Utilities`. A folder is created only when it
   has an item to hold.
4. **Naming.** Use type prefixes (`lh_`, `nb_`, `pl_`, `wh_`, `sm_`, `rpt_`)
   and environment-neutral item names.

## Consequences

- Demo access can be granted without exposing other assets.
- Patterns reused from elsewhere must be copied and re-bound rather than
  referenced.
