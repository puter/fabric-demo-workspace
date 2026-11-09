# ADR 0004: Local Presenter App

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-28 |
| Scope | This repository |
| Owners | Data Platform / Architecture |
| Related | ADR 0002 (*Scenario Source Layout and Real-Time Item Naming*), ADR 0003 (*Data Agent Naming and Placement*) |

## Context

Presenters want one branded screen that shows the live Fabric map and lets
them ask the data agent questions. Real-Time Dashboards allow little
branding: Markdown tiles strip HTML, there are no themes, and only inline
images render. Rebuilding the map outside Fabric would defeat the point of
the demo, which is to show Fabric's live map integration.

Two Fabric features make a thin wrapper possible:

- **Fabric Embed (preview)** embeds a Real-Time Dashboard in a web page with
  delegated user sign-in. It needs an Entra app registration.
- **A published data agent is an MCP server**, with one tool that takes a
  question.

## Decision

1. **A local presenter app, not a hosted site.** It lives in
   `scenarios/<slug>/src/<pkg>/presenter/`. It runs on the presenter's
   machine (`localhost`) only. Nothing is deployed to Azure or the workspace.
2. **The dashboard is embedded, never re-implemented.** The app shows the
   scenario's own Real-Time Dashboard through Fabric Embed. Because the SDK
   embeds a whole dashboard, the dashboard's first page is a `Presenter` page
   that reuses the same tile queries (one definition in `rtd.py`).
3. **The data agent is reached over its MCP endpoint.** The call goes through
   the local server as the `az` user. The browser never calls the agent.
4. **One Entra app per tenant, named `<Scenario> Presenter (local)`.** It is a
   single-tenant SPA. Its only redirect URI is `http://localhost:<port>/`. Its
   only delegated permissions are `Fabric.Embed` and `KQLDashboard.Read.All`
   (Power BI Service), plus `user_impersonation` (Azure Data Explorer). The
   embedded dashboard queries Kusto with the user's token.
   The app is looked up by display name at startup. Its client ID is never
   committed (CLAUDE.md hard rule).

## Consequences

- The presenter must have Fabric access to the dashboard and its database.
  This is acceptable because the presenter is the only viewer.
- Fabric Embed and data agent MCP are both preview. Package names and
  contracts may change, so pin the SDK version and expect to re-test before a
  demo.
- Every chat question consumes Copilot capacity, as in ADR 0003. The app sends
  questions only when a person asks one.
- The app is not suitable for public or attendee-facing use. That would need
  a hosted service with its own identity, which is a separate decision.
