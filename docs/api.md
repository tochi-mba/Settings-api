# The HTTP surface

Two surfaces and a pair of probes. The person-facing one is what somebody's own assistant
calls on their behalf; the internal one is what another service calls when it needs to know
how to behave for a particular person.

Every error is `application/problem+json` with a `request_id`. `operation_id`s are contract:
they become MCP tool names, so they are never renamed.

## Person-facing: `/v1/settings`

`Authorization: Bearer <keyring user token>`, minted for the `settings` audience family.
The account comes from the token's `sub`; no route takes an account id. `?profile=`
selects which keyring profile's profile-scoped rows to read or write. Account-scoped
settings ignore it. A write of a profile-scoped key without `?profile=` is a 422 that
names the keys. A body field named `profile` is also a 422 (`extra="forbid"`).

| Operation | Route | What it does |
| --- | --- | --- |
| `describe_settings` | `GET /v1/settings/schema` | The catalogue as this deployment has it, for every namespace the token grants: each setting's type, scope, bounds, default, current value, whether an operator narrowed or pinned it, and whether an assistant may change it (`agent_writable`). |
| `get_settings` | `GET /v1/settings` | Every namespace the token grants, resolved for this person. |
| `get_namespace` | `GET /v1/settings/{namespace}` | One namespace, with `common` merged underneath it. |
| `get_setting` | `GET /v1/settings/{namespace}/{key}` | One value, with where it came from. |
| `update_settings` | `PUT /v1/settings` | Several values at once, applied atomically. |
| `set_setting` | `PUT /v1/settings/{namespace}/{key}` | One value. |
| `reset_setting` | `DELETE /v1/settings/{namespace}/{key}` | Forget this person's choice, so the default applies again. |
| `reset_namespace` | `DELETE /v1/settings/{namespace}` | The same for a whole namespace. |
| `forget_settings` | `DELETE /v1/settings` | Erase everything this service holds for this person. |
| `export_settings` | `GET /v1/settings/export` | Everything this person has chosen, as a document they can keep. |
| `import_settings` | `POST /v1/settings/import` | Apply such a document back. |
| `read_settings_events` | `GET /v1/settings/events` | What changed, when, and which service asked. Newest first; `?limit=` (1-200, default 50) and `?before=<sequence>` page through it. Never the values. |

Resetting is not the same as setting the default: a reset removes the stored row, so the
person follows the default wherever it goes next. Setting the value pins it to what it is
today ([ADR-0005](adr/0005-sparse-storage-so-defaults-can-move.md)).

### Revisions and ETags

Every account has one revision, bumped by every write that changes something; a write that
changes nothing bumps nothing. `describe_settings`, `get_settings`, `get_namespace`,
`resolve_settings` and every write return it in the body and as the `ETag` header, spelled
`"<account_id>.<revision>"`. Every write under `/v1/settings` except `forget_settings`
accepts `If-Match` with that ETag and answers 412 if the account has moved on since, so
two assistants editing one person's settings cannot overwrite each other unseen. A weak
tag (`W/...`) or one belonging to another account is also a 412.

## Internal: `/v1/internal`

Two credentials, always:

| Header | Proves | Comes from |
| --- | --- | --- |
| `Authorization: Bearer <service token>` | which service is calling | this deployment's `SETTINGS_API_SERVICES` |
| `X-Settings-User-Token: <user token>` | who it is calling for | the person's own keyring token |

| Operation | Route | What it does |
| --- | --- | --- |
| `resolve_settings` | `GET /v1/internal/settings/{namespace}` | This person's effective values for one namespace, with `common` merged underneath, plus a `fallbacks` block saying what each key's default is and what to do during an outage. Supports `If-None-Match` (a 304 when nothing changed) and advertises `Cache-Control: private, max-age=<SETTINGS_API_CACHE_TTL_SECONDS>`. |
| `set_setting_for_user` | `PUT /v1/internal/settings/{namespace}/{key}` | The person's own decision, travelling through a service that offered them the choice. Never the service's decision. |

A service may read and write only the namespaces it was granted, plus `common`. Anything
else is a 403 that says so. The user token's audience must belong to the calling service's
own audience family, which is what stops one service's token plus somebody else's user
token reading a third party's settings.

Do not write an HTTP client for this. Use `clients/python/settings_client`, which handles
caching, revalidation, single-flight and outage behaviour — see
[docs/integration.md](integration.md).

## Probes

| Operation | Route | What it does |
| --- | --- | --- |
| `check_liveness` | `GET /healthy` | That the process is running. No I/O, never fails, needs no token. |
| `check_readiness` | `GET /ready` | The database, keyring's signing keys, the catalogue and the operator policy, reported one line each. 503 when any is unusable. Needs no token. |

Liveness deliberately reports nothing about dependencies: an orchestrator restarts a
container when liveness fails, and a service that failed liveness during keyring's outage
would be restarted repeatedly for somebody else's problem.

## Status codes

| Code | Means |
| --- | --- |
| 304 | `resolve_settings` only: the `If-None-Match` ETag still matches. No body. |
| 400 | A document sent to `update_settings` or `import_settings` names settings that do not exist. The body names every one. |
| 401 | The token was not accepted. One message for every cause. |
| 403 | A namespace the token or the service was not granted, or a setting only the person may change. |
| 404 | No such namespace or key in the catalogue. |
| 409 | The value is pinned by operator policy. |
| 412 | The `If-Match` ETag is not this account's current revision. Re-read and retry. |
| 422 | The value is the wrong type, out of bounds or too large; it looks like a credential; a profile-scoped write omitted `?profile=`; an import names an export format this build does not read; or the request itself is malformed, such as a body `profile` field. The body names the rule, never the value. |
| 503 | Keyring's signing keys could not be fetched, so the token could not be checked. Sent with `Retry-After: 5`. |
