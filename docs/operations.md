# Operations

Running settings-api: what to configure, what to watch, and what each failure means.

## Configuration

Every variable is prefixed `SETTINGS_API_`. A prefixed variable that matches no setting is
a **startup error**, so a typo fails loudly instead of leaving a default in place.

### Core

| Variable | Default | Notes |
| --- | --- | --- |
| `SETTINGS_API_ENVIRONMENT` | `local` | Reported by the probes. |
| `SETTINGS_API_LOG_LEVEL` | `INFO` | |
| `SETTINGS_API_LOG_FORMAT` | `json` | `console` for readable local output. |
| `SETTINGS_API_HOST` / `_PORT` | `127.0.0.1` / `8003` | |
| `SETTINGS_API_DATABASE_PATH` | `var/settings.db` | SQLite file. Created private to its owner. |

### Identity

This service verifies keyring's tokens locally and never calls keyring at request time.

| Variable | Default | Notes |
| --- | --- | --- |
| `SETTINGS_API_KEYRING_JWKS_URL` | `http://127.0.0.1:8001/.well-known/jwks.json` | Where keyring publishes its public keys. |
| `SETTINGS_API_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Must equal keyring's own `KEYRING_ISSUER`, or every token is refused. |
| `SETTINGS_API_AUDIENCE_PREFIX` | `settings` | The audience family this service answers to. |
| `SETTINGS_API_JWKS_CACHE_SECONDS` | `3600` | How long keys are held before being read again. |
| `SETTINGS_API_JWKS_MIN_REFETCH_SECONDS` | `60` | Floor between the refetches an unknown key id may provoke. Not a tuning knob. |
| `SETTINGS_API_KEYRING_HTTP_TIMEOUT_SECONDS` | `5` | Per fetch of the key document. |

### Consuming services

`SETTINGS_API_SERVICES` is one JSON document, because a nested spelling cannot be
enumerated and a configuration this service cannot enumerate is one its own typo check
cannot check. Per service: `token` (at least 32 characters, never shared with another
service — both are startup errors), `audience_prefix`, and `namespaces`.

`common` is added to every grant automatically; do not list it. Give the narrowest list
that works: the blast radius of one compromised service token is exactly that list.

One rule decides every `audience_prefix`: a service that also calls keyring's internal
surface presents the *same* user token there, and keyring accepts it only when its
audience is exactly that service's name in `KEYRING_SERVICE_TOKENS`. So those services use
their own name — `media-tool`, `spotify-api`, `web-search-api`, `environments-api`. A
prefix that differs fails closed, and looks like a working service whose every call is a
401.

### Operator policy

| Variable | Default | Notes |
| --- | --- | --- |
| `SETTINGS_API_POLICY_PATH` | unset | A JSON file narrowing or pinning catalogue entries. A configured path that does not exist is a startup error, so a deployment that meant to narrow something and mistyped does not run wide open. |
| `SETTINGS_API_ALLOWED_NAMESPACES` | every namespace | Narrow to the namespaces this deployment actually serves. |

A policy may make a bound tighter, move a default inside the bounds that result, and pin a
value. It may never loosen a bound, add an enum choice or raise a cap; a policy that tries
is a startup error. Every narrowing is visible to the person in `describe_settings`.

### Limits and housekeeping

| Variable | Default | Notes |
| --- | --- | --- |
| `SETTINGS_API_MAX_VALUE_BYTES` | `4096` | Ceiling on one stored value. |
| `SETTINGS_API_MAX_EVENTS` | `2000` | Events kept per account. |
| `SETTINGS_API_CACHE_TTL_SECONDS` | `60` | How long a resolved namespace is served before revalidating. |
| `SETTINGS_API_RETIRED_RETENTION_DAYS` | `90` | How long values whose setting left the catalogue are kept before the sweeper drops them. |
| `SETTINGS_API_SWEEP_INTERVAL_SECONDS` | `3600` | How often that sweep runs. |

## Deploying

```bash
make docker                                   # build the image
docker run -p 8003:8003 --env-file .env settings-api:local
```

The image runs as a non-root user and its `HEALTHCHECK` calls `/healthy`, which does no
I/O. Point your load balancer at `/ready` instead: that one reports the database, keyring's
keys, the catalogue and the policy, and answers 503 when any of them is unusable.

## Migrations

Migrations are append-only and numbered, and `storage/schema.sql` is a checked-in snapshot:

```bash
make schema      # regenerate the snapshot after adding a migration
```

CI fails when the snapshot and the migrations disagree, which is what stops a migration
that was never applied from passing review.

## What each failure means

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every request 401 | `KEYRING_ISSUER` or `AUDIENCE_PREFIX` disagrees with keyring | Make both match keyring's configuration exactly. |
| One service's calls 401, everything else fine | That service's `audience_prefix` is not the name keyring mints its tokens under | Set it to that service's name in `KEYRING_SERVICE_TOKENS`. |
| One service's calls 403 | The namespace is not on its grant | Add it to that entry's `namespaces`. |
| `/ready` reports keyring unusable | The key document cannot be fetched | Check `KEYRING_JWKS_URL` is reachable from this host. Tokens keep verifying against held keys for a bounded grace. |
| Startup error naming a variable | A typo, or a setting that was renamed | The message names the variable and, where it was renamed, its replacement. |
| Startup error about `SERVICES` | A token under 32 characters, or two services sharing one | Generate one per service: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

## Backups

The database is one SQLite file. Back it up with `sqlite3 var/settings.db ".backup out.db"`
rather than copying the file while the service is running, and keep the backups as private
as the original: they hold what every person has chosen, which is not a secret but is
personal.
