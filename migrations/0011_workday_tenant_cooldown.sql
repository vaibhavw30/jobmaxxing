-- Per-tenant cooldown for enrich_workday: a tenant that made zero progress in a run (fully
-- blocked, no rows enriched) is excluded from candidate selection until blocked_until passes,
-- instead of being re-hammered every subsequent batch for no gain.
create table if not exists workday_tenant_cooldown (
    tenant       text primary key,
    blocked_until timestamptz not null
);
