# settings-api

One place a person decides how every service in this family behaves **for them**.

Which country their music searches resolve against. How long an idle workspace of theirs
stays up on a shared box. Which model answers their questions. What "delete" means for
their notes. Which providers must never see their queries.

```
GET /v1/settings/spotify        ->  {"default_market": "PT", "timezone": "Europe/Lisbon", ...}
PUT /v1/settings/spotify/default_market   {"value": "PT"}
```

## Why this exists

Every service in this family has, in its `Settings` class, at least one knob that is not
really a deployment decision. It is a decision belonging to the person whose data is being
handled, frozen into an environment variable that applies to everybody on the box. The
clearest one is in `spotify-api/src/spotify_api/config.py`:

```python
default_market: str | None = Field(
    default=None,
    description="ISO 3166-1 alpha-2 market applied when a request omits one.",
)
```

Which country's catalogue a person's track searches resolve against is a fact about the
person, deployed as if it were a fact about a machine. On a box serving one person that is
invisible. On a box serving two people in different countries it is wrong for one of them,
and without this service the only fix is a second deployment.

It was not the only one. Before this service:

- four services each carried their own "which keyring profile do you mean when I don't
  say", spelled three different ways, with two different defaults;
- persona-api had no per-account settings at all;
- user-api had three, behind a port that was built on day one to take a second adapter;
- and there was nowhere a person could look to answer *"what has this system been told
  about how to treat me"*, and nowhere to change it once.

This is that place. Every setting it holds, with its type, bounds, default and outage
rule, is in [docs/catalogue.md](docs/catalogue.md), which is generated from the code and
states the current count.

## The shape of it

```
                     person's token (aud: settings)
                                  |
                                  v
  keyring  --JWKS-->  [ settings-api ]  <--service token + user's token--  lucy-api (hub)
 (auth root)               |     ^                                         keyring-api
                           |     |                                         user-api
                       SQLite    +---------------------------------------  persona-api
               one row per value a person                                  spotify-api
                 chose, and nothing else                                   web-search-api
                                                                           environments-api
                                                                           memory-api
```

Two surfaces:

**`/v1/settings`** -- the person, or an assistant acting for them, holding one token minted
by keyring with audience `settings` or `settings.<namespace>`. Twelve operations: read,
describe, write, reset, export, import, read the change log, and destroy everything.

**`/v1/internal`** -- a consuming service, holding **two** credentials: its own service
token (which service is calling) and the end user's short-lived token (who it is calling
for). The account comes from the user's token and from nowhere else, and that token's
audience must belong to the calling service's own family. Two operations: resolve a
namespace, and write one setting on the person's behalf.

Nothing about this needs a change in keyring. Tokens are verified locally against
keyring's published JWKS document -- there is no token exchange and no new keyring
endpoint.

## Five properties this service is built around

**A setting is one value per account or one per profile, never both.** Each catalogue
entry declares its scope, and the default is the account, so a restriction nobody thought
about cannot quietly split across keyring profiles. Taste and routing -- a Spotify market,
Lucy's model, a default shell -- may opt in to one value per profile. The primary key is
`(account_id, profile, namespace, key)` with account rows under a sentinel keyring refuses
as a profile name, so a key cannot be overlaid at both levels. The profile is only ever the
`?profile=` query parameter, and every request body forbids extra fields, so
`{"profile": "work"}` is a 422 rather than a field silently ignored.
[ADR-0002](docs/adr/0002-settings-are-per-account-not-per-profile.md).

**Storage is sparse, so defaults can move.** A row exists only where somebody expressed a
preference, so changing a default moves everyone who never chose and nobody who did. This
is the fix for a defect user-api shipped, where a settings update filled in the domain's
default grace window instead of the deployment's and a seven-day deployment silently became
a thirty-day one. [ADR-0005](docs/adr/0005-sparse-storage-so-defaults-can-move.md).

**Every setting declares what happens when this service is down**, and that field has no
default in the code. `use_default` means the default is the most conservative value the
setting has. `refuse` means it is not, and the consuming service must fail the operation
rather than guess -- `search.disabled_providers` defaults to "every provider is allowed",
so falling back to it would send somebody's query to exactly the provider they refused.
[ADR-0006](docs/adr/0006-on-unavailable-is-declared-per-setting.md).

**The catalogue is a table, and it stays reviewable.** One frozen dataclass per setting
and one module per namespace (`lucy` is a package of modules, because it outgrew one file),
restricted by an architectural contract to importing nothing but the shape of a setting. A catalogue module that could import the store is a catalogue entry that
could have behaviour. [ADR-0003](docs/adr/0003-the-catalogue-is-code-not-data-in-the-database.md).

**There is no administrative surface.** No operator can read somebody's settings over HTTP,
because no route exists that would let them. What an operator *can* do is narrow a bound or
pin a value in a policy file -- statements about the deployment rather than about a person
-- and a pinned setting reports itself as pinned, so a write to it is a 409 that says why
rather than a 200 that did nothing.
[ADR-0008](docs/adr/0008-no-administrative-surface.md).

## Using it from another service

Four lines. Do not write the HTTP call.

```python
from settings_client import HttpSettingsClient

settings = HttpSettingsClient(
    base_url=config.settings_api_base_url,
    service_token=config.settings_api_token,
)
resolved = await settings.resolve("spotify", user_token=caller.token, profile=caller.profile)
market = resolved["default_market"]
```

`profile` is optional; pass it whenever the caller is acting inside a keyring profile, so
profile-scoped keys resolve for that profile. The client handles caching (keyed by token,
namespace and profile, never by an unverified `sub`),
`If-None-Match` revalidation, single-flight, and what to do during an outage -- serve the
person's own cached values, or fall back per the setting's declared rule, or raise. See
[docs/integration.md](docs/integration.md) for the per-service wiring and
[docs/adding-a-service.md](docs/adding-a-service.md) for a service that does not have a
namespace yet.

## Running it

```bash
make install     # create the venv and install everything
make check       # the gate: format, lint, strict types, contracts, tests at 100% coverage
make run         # serve on :8003 with reload; docs at /docs
```

The service starts without keyring, and `/healthy` answers at once. Every other route needs
a token keyring signed, checked against the keys at `SETTINGS_API_KEYRING_JWKS_URL`
(`http://127.0.0.1:8001/.well-known/jwks.json` by default), and `/ready` reports whether
they can be fetched. `make smoke` drives a running settings-api and a running keyring end
to end.

Configuration is every `SETTINGS_API_`-prefixed environment variable in
[.env.example](.env.example), described in [docs/operations.md](docs/operations.md). A
prefixed variable matching no setting is a **startup error**, not a warning.

Two generated files are checked in and CI fails on a diff:
`src/settings_api/storage/schema.sql` (`make schema`) and `docs/catalogue.md`
(`make catalogue`).

## Documentation

| | |
| --- | --- |
| [AGENTS.md](AGENTS.md) | How work is done here. Read before your first edit. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, the loop, and what a change needs before review. |
| [docs/architecture.md](docs/architecture.md) | The layers, a request end to end, and why the shape is what it is. |
| [docs/api.md](docs/api.md) | Every route, its headers, and every status it answers. |
| [docs/catalogue.md](docs/catalogue.md) | Every setting, generated from the catalogue. |
| [docs/integration.md](docs/integration.md) | How each service consumes a namespace, and the full audit of what is and is not covered. |
| [docs/adding-a-service.md](docs/adding-a-service.md) | Registering a new service's settings, public or private. |
| [docs/operations.md](docs/operations.md) | Configuration, deployment, and what each failure means. |
| [docs/testing.md](docs/testing.md) | How the tests are written, and which must never be deleted. |
| [docs/mcp.md](docs/mcp.md) | Exposing this as assistant tools. |
| [docs/adr/](docs/adr/README.md) | The decisions, and what would change our minds. |
| [clients/python/](clients/python/README.md) | `settings-client`, the library every consuming service uses. |
| [CHANGELOG.md](CHANGELOG.md) | What changed. |

## What this is not

It is not where credentials go -- keyring is, and a value that even looks like an API key
is refused with a 422 naming it, with no override. It is not a feature-flag service: a flag
is the operator's decision and everything here is the person's. And it is not a key-value
store: every setting is declared in the catalogue with a type, bounds and a description
written for a model to read, because a setting nobody described is a setting nobody can
choose.

## Licence

MIT.
