# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## Architecture Decision Records

Before proposing or making structural changes in this repo — workspace
folder placement, new artifact types, naming, or data sourcing — read the
ADRs in `docs/adr/`. Solutions should conform to the decisions recorded
there, not reinvent them.

- If a change conflicts with an existing ADR, say so explicitly and ask
  before proceeding, rather than silently deviating.
- If a change establishes a new architectural decision, propose a new ADR
  in `docs/adr/` rather than leaving it undocumented.

## Hard rule: public or synthetic data only

This repository is public. Never add workspace IDs, lakehouse IDs, connection
names, Key Vault secret names, credentials or non-public data.
