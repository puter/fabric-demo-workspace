# ADR 0005: Heartbeat Active-Set Reconciliation

| | |
|---|---|
| Status | Proposed |
| Date | 2026-11-03 |
| Scope | Real-time scenarios in this repository (first: `miami-valley`) |
| Owners | Data Platform / Architecture |
| Related | ADR 0002 (*Scenario Source Layout and Real-Time Item Naming*) |

## Context

The poller is the only component that knows which entities are active. It
keeps that state in SQLite and publishes lifecycle events (`created`,
`updated`, `resolved`) to Eventstream. The Eventhouse rebuilds "what is active
now" from the latest event per entity (`EntityLatest`).

That works only if every event arrives. In `miami-valley`, 107 of 478 OHGO
entities (22%) never reached the Eventhouse between 2026-10-14 and 2026-10-26.
The nightly capacity suspend paused the Eventstream destination, and events
Eventstream had already accepted were dropped. The poller's outbox was empty,
so it could not know. The result:

- **Ghosts.** 24 entities whose `resolved` event was lost stayed active in KQL
  indefinitely: on the live map, in the active counts, and in data agent
  answers (two Oct 13 crashes were still reported near the demo host site on
  Nov 2).
- **Missing entities.** 4 entities the poller tracked as active had no row in
  KQL at all.

These were repaired by hand on 2026-11-02 by diffing the poller's state
database against KQL and re-sending its stored events. The cause has since
been addressed (an Eventstream resume job), but any future delivery loss
would create ghosts again, and nothing would detect them.

## Decision

1. **Each heartbeat carries the poller's active set.** The
   `ingestion_heartbeat` event gains `attributes.active_keys`: a map from
   every entity type the poller's state has tracked to the sorted list of its
   active entity keys. A tracked type with nothing active is listed with an
   empty list, so its stale rows can be hidden too. This is an additive
   contract change; `schema_version` does not change.
2. **KQL treats the latest heartbeat as authoritative for tracked types.**
   `LatestFor(mode)`, which every current-state function uses, hides a row
   when all of these hold:
   - a heartbeat with `active_keys` exists for the row's `environment` (in
     the same run partition);
   - the row's `entity_type` is listed in it;
   - the row's key is not listed;
   - the row's latest event is no newer than that heartbeat.

   The last condition keeps entities created after the snapshot. Rows of
   types the poller doesn't track (`facility_risk`, `operational_alert`,
   heartbeats) are never affected.
3. **Fail open.** With no heartbeat carrying `active_keys` (data from before
   this change, or a partition the poller never reported on), nothing is
   hidden, and behaviour is as before.
4. **Drift is observable.** `ActiveSetDrift(mode)` reports, per environment
   and entity type, how many rows are being hidden (lost `resolved` events)
   and how many active keys have no row (lost `created`/`updated` events).
   The second kind can be detected but not repaired from a heartbeat, which
   carries only keys.
5. **Real-time views only.** Bronze, Silver and Gold still record every event
   as delivered. A ghost stays an open occurrence in Gold until a resolve
   arrives or is backfilled.

## Alternatives considered

- **Reconcile from the poller by reading KQL.** The poller would query the
  Eventhouse and emit `resolved` events for unknown keys. This repairs Gold
  too, but it couples the poller to Eventhouse read access and credentials,
  and it breaks the one-way flow (poller → Eventstream → Eventhouse).
- **Expire rows not seen for N hours.** Simple, but long-lived entities such
  as multi-week construction are re-emitted only when they change, so they
  would disappear while still active.
- **Periodic manual reconciliation**, as on 2026-11-02. It works, but needs
  someone to notice first.

## Consequences

- Ghosts disappear from the map, counts and agent answers on the next
  heartbeat (about a minute), whatever caused the loss.
- Heartbeats grow by roughly 40 bytes per active entity: about 1.5 KB at
  current volumes, about 20 MB a day at a pessimistic 500 active entities.
  This is negligible against the Eventhouse's 30-day hot cache.
- `LatestFor` now joins the latest heartbeat. It reads from `EntityLatest`,
  so the cost is one small extra lookup per query.
- Live pollers in every `environment` share the heartbeat key
  `mvoi:ingestion_heartbeat:poller`, and `EntityLatest` keeps one row per
  key. If pollers in two environments write live data to the same
  Eventhouse, only the one that reported last is reconciled; the other
  fails open. Today only `demo` writes live data to `kqldb_mvoi`.
- A poller that starts with an empty state volume tracks no types yet, so
  nothing is hidden until its first poll. After that, rows it does not
  re-observe are hidden: the previous state's resolves for them can no
  longer arrive.
