# ADR 0002: Scenario Source Layout and Real-Time Item Naming

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-09 |
| Scope | This repository |
| Owners | Data Platform / Architecture |
| Related | ADR 0001 (*Demo Workspace Scope and Organization*) |

## Context

ADR 0001 defines `/workspace` as the Fabric Git folder and sets the folder
and naming conventions for Fabric items. The first full demo scenario,
*Miami Valley Operations Intelligence*, also contains assets that are **not**
Fabric items and cannot live in a Fabric Git folder:

- a Python polling and replay service that runs outside Fabric,
- shared Python libraries (event contract, geospatial helpers, risk
  scoring) imported by both that service and Fabric notebooks,
- unit and integration tests, sample payloads, and CI workflows,
- authored KQL scripts and item-definition templates that a deployment
  script pushes to Fabric through the REST API,
- infrastructure-as-code for external hosting.

It also uses Real-Time Intelligence item types that ADR 0001 gives no prefix
for: Eventhouse, KQL database, Eventstream, Real-Time Dashboard, and
Activator.

## Decision

1. **Scenario source lives under `scenarios/<scenario-slug>/`.** Each demo
   scenario gets one self-contained folder holding its code, config, tests,
   docs, Fabric item sources and infrastructure. This sits alongside
   `/workspace`, not inside it.
2. **`/workspace` stays Fabric-managed.** Item definitions are authored in
   `scenarios/<slug>/fabric/` and deployed with the scenario's deployment
   script. The workspace's Git integration then commits the Fabric-native
   serialization to `/workspace/<Scenario>/NN - …/`, following ADR 0001.
   Hand-written `.platform` files are not committed to `/workspace`.
3. **Additional type prefixes**, with environment-neutral names as in
   ADR 0001:

   | Item type | Prefix |
   |---|---|
   | Eventhouse | `eh_` |
   | KQL database | `kqldb_` |
   | Eventstream | `es_` |
   | Real-Time Dashboard | `rtd_` |
   | Activator (Reflex) | `act_` |
   | KQL queryset | `kqlqs_` |
   | Environment | `env_` |
   | Variable library | `vl_` |

4. **Folder placement of RTI items:** Eventstreams go in `10 - Ingestion`.
   The Eventhouse and KQL database go in `00 - Domain Platform`, next to the
   Lakehouse. Real-Time Dashboards and reports go in `30 - Data Products`.
   Activators and schedules go in `40 - Orchestration`.
5. **CI/CD runs in Azure DevOps.** Pipeline definitions live at repo root in
   `.azure-pipelines/`, one YAML per scenario, path-filtered to
   `scenarios/<slug>/`. Shared step templates go in
   `.azure-pipelines/templates/`. Azure access uses ADO service connections
   with workload identity federation (no stored secrets). Container images go
   to Azure Container Registry.

## Consequences

- External code and tests are reviewable alongside the Fabric items they
  feed, without polluting the Fabric Git folder.
- There is one deploy path (script → Fabric), and `/workspace` is a faithful
  mirror of what is deployed.
- RTI prefixes extend the prefix list in ADR 0001.
