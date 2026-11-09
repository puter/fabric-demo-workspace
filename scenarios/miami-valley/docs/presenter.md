# Presenter app (local)

A branded local page with two parts: the live Fabric dashboard (embedded, not
copied) and an **Ask the data** chat panel connected to the `da_mvoi` data
agent. See [ADR 0004](../../../docs/adr/0004-local-presenter-app.md).

```
uv run --extra fabric mvoi-presenter      # then open http://localhost:8000
```

Sign in with your Fabric account the first time. The dashboard opens on its
**Presenter** page (KPIs and the live map). Use the dashboard's page picker to
open Operations or Pipeline health.

## Prerequisites

- `az login` to the demo tenant, as for `scripts/deploy_fabric.py`.
- `rtd_mvoi_operations` and `da_mvoi` deployed (`--steps dashboard,dataagent`).
- Fabric access (Viewer or higher) to the workspace, for whoever signs in.
- The Entra app **MVOI Presenter (local)**, created once per tenant (below).

## One-time Entra app setup

This needs permission to register apps and grant consent (admin):

```
az ad app create --display-name "MVOI Presenter (local)" --sign-in-audience AzureADMyOrg \
  --required-resource-accesses '[{"resourceAppId":"00000009-0000-0000-c000-000000000000",
    "resourceAccess":[{"id":"04994edc-428b-482b-af95-ee1575dde39b","type":"Scope"},
                      {"id":"f19ea7d7-2f31-4c6d-9845-d5480c5d1798","type":"Scope"}]},
   {"resourceAppId":"2746ea77-4702-4b45-80ca-3c97e680e8b7",
    "resourceAccess":[{"id":"00d678f0-da44-4b12-a6d6-c98bcfd1c5fe","type":"Scope"}]}]'
# then: add SPA redirect URI http://localhost:8000/ (Graph PATCH spa.redirectUris),
#       az ad sp create --id <appId>; az ad app permission admin-consent --id <appId>
```

The scopes are `Fabric.Embed` and `KQLDashboard.Read.All` on the Power BI
Service API, plus `user_impersonation` on Azure Data Explorer. The embedded
dashboard runs its tile queries against Kusto directly. Without the third
scope, sign-in fails with `AADSTS650057 ... https://kusto.kusto.windows.net`.
Microsoft's setup doc doesn't list this scope.

The app is found by display name at startup. To point at a different app,
set `MVOI_PRESENTER_CLIENT_ID`.

Using a port other than 8000 means adding `http://localhost:<port>/` as a
redirect URI first, then running `mvoi-presenter --port <port>`.

## Notes

- **Overnight:** while the capacity is suspended (23:00–11:00 UTC), the
  embedded dashboard and the chat both fail. Resume the capacity first.
- **Chat:** each question stands alone, because the agent gets no
  conversation history. Answers take 10–40 s and consume Copilot capacity.
- **Preview:** Fabric Embed (`@microsoft/fabric-embed@0.1.0`) and data agent
  MCP are preview features. Re-test the app before a demo.
