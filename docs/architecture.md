# Architecture

One HTTP service, one SQLite database, and a catalogue that lives in code. This page is
the map: what the layers are, what each one may import, and why the shape is what it is.
[AGENTS.md](../AGENTS.md) is the operating manual; the decisions themselves are in
[docs/adr/](adr/README.md).

## The layers

```
src/settings_api/
  api/          routes, dependencies, error rendering, middleware
  auth/         keyring token verification and service-token comparison
  domain/       the catalogue, namespaces, policy, resolution, value types
  settings/     the service, the store port and its SQL adapter, erasure, the sweeper
  events/       the append-only record of what changed
  storage/      the database, migrations, the checked-in schema snapshot
  core/         config, container, clock, logging, context, version
```

Dependencies point inward: `api` may use everything, `settings` and `auth` use `domain`,
and `domain` imports nothing from the layers above it. `lint-imports` enforces that, so a
shortcut fails `make check` rather than review.

`core/container.py` is the composition root. Every adapter is chosen there, once, and
handed to the app; nothing else constructs its own dependencies, which is what makes the
whole service testable by substitution.

## A request, end to end

1. **Middleware** binds a request id and, after the handler, writes one completion record.
2. **Dependencies** establish who is asking. A person-facing route takes
   `Authorization: Bearer <keyring user token>`; an internal route takes the calling
   service's own token there **and** the person's token in `X-Settings-User-Token`. The
   account id comes from the `sub` of a verified token and from nowhere else — no route
   anywhere takes an account id.
3. **auth/** verifies the token against keyring's published keys, locally, with no call to
   keyring at request time. The rules are `keyring-client`'s, shared with the whole family.
4. **The service** resolves the namespace: the catalogue's defaults, narrowed by operator
   policy, overlaid with whatever this person has actually set, with `common` merged
   *underneath* the namespace so a namespace key always wins.
5. **The store** reads and writes sparsely: only values a person has chosen are stored, so
   a default that changes in a later release changes for everybody who never chose.
6. **events/** appends what changed, which is what makes "what has this system been told
   about how to treat me" answerable after the fact.

## The catalogue is code

Every setting is a `SettingDef` in a module under `domain/catalogue/`, with its type,
bounds, default, `on_unavailable` rule and the prose a person reads. `docs/catalogue.md`
is generated from those modules by `make catalogue`, and CI fails when the page and the
code disagree.

Data in the database would have made the catalogue editable at runtime, which sounds
flexible and means a deployment can drift from its own documentation. See
[ADR-0003](adr/0003-the-catalogue-is-code-not-data-in-the-database.md).

## What a namespace is

A namespace is a scope, not a folder ([ADR-0007](adr/0007-namespaces-are-scopes.md)). A
service is granted the namespaces it owns, plus `common`, which everybody gets. The grant
is `SETTINGS_API_SERVICES`, and the blast radius of one compromised service token is
exactly the namespaces on its entry.

The audience check is what stops a stolen service token being enough: the person's token
must belong to that service's own audience family, so a consuming service may present
tokens minted for itself and nothing else.

## Outages are declared, per setting

Each entry says what a consuming service should do when this service cannot be reached:
`use_default`, or `refuse`. `refuse` is for settings whose default is *permissive* —
falling back would quietly override a restriction somebody asked for. Everything else
falls back, because an outage here must not become an outage in seven other services.
See [ADR-0006](adr/0006-on-unavailable-is-declared-per-setting.md).

## Account-wide vs profile-wide

A setting is either one value for the person or one value per keyring profile, never
both. Overlay of the same key would be a second settings system. The catalogue declares
the level on the entry; the default is account, so a restriction nobody thought about
cannot quietly split across profiles. `common` is forced account-scoped:
`common.default_profile` names a profile and cannot itself be per-profile.

Storage is `(account_id, profile, namespace, key)`. Account rows live under the sentinel
`*`, which keyring's profile-name pattern refuses. See
[ADR-0002](adr/0002-settings-are-per-account-not-per-profile.md) as amended.

## Storage

SQLite, WAL, one file, created private to its owner
([ADR-0012 in keyring](../../Keyring-api/docs/adr/0012-sqlite.md) made the same call for
the same reasons). Migrations are append-only and numbered; `storage/schema.sql` is a
snapshot regenerated with `make schema`, and CI compares the two so a migration that was
never applied cannot pass review.

The sweeper retires values whose setting has left the catalogue, after
`SETTINGS_API_RETIRED_RETENTION_DAYS`, so a removed setting does not leave rows nobody can
explain.

## What this service will never grow

No administrative surface ([ADR-0008](adr/0008-no-administrative-surface.md)): there is no
route that lists accounts, reads somebody else's settings, or lets an operator set a value
on a person's behalf. An operator narrows and pins through policy, which is deployment
configuration, and every narrowing is visible to the person in `describe_settings`.
