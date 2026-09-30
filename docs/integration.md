# Integrating a service with settings-api

How a service in this family reads one person's settings, what it stops configuring for
itself when it does, and what it must keep. One section per service, each with the keys it
reads today and the configuration they replace.

Read [catalogue.md](catalogue.md) for what each setting means, and
[adding-a-service.md](adding-a-service.md) for a service that does not have a namespace
yet.

---

## 1. The shape of the thing

### Two credentials, always

A consuming service needs one **particular person's** settings. If it could authenticate
as itself and then name whoever it liked, anything able to reach settings-api could read
anybody's settings — the confused deputy, moved from inside one process into the gap
between two. So every call to `/v1/internal` carries both:

| Header | Proves | Comes from |
| --- | --- | --- |
| `Authorization: Bearer <service token>` | **which service** is calling | this deployment's configuration |
| `X-Settings-User-Token: <user token>` | **who** it is calling for | the end user's own short-lived keyring token |

The account id comes from the **user token's** `sub` and from nowhere else. There is no
parameter anywhere that names an account.

Two checks then apply, and the second is the one that matters:

1. The service token is compared, in constant time and without an early return, against
   every configured service. That decides which grant is in play.
2. **The user token's audience must belong to that service's own audience family.**
   A service whose prefix is `example-tool` may present `example-tool` and
   `example-tool.jobs`, and nothing else. A token minted for another service, or the
   person's own `settings`-audience token, is refused
   with a 401. Without this check a static service token plus any user token would read
   any account.

**One user token, two services, one name.** A consuming service usually presents the same
user token to keyring's internal surface and to this one. Keyring accepts it only when its
audience is *exactly* the calling service's name in `KEYRING_SERVICE_TOKENS`, so in a
deployment a service's `audience_prefix` here must be that same name: `spotify-api`, not
`spotify`. A mismatch fails closed and looks like a correctly configured service whose every
call is a 401. This service's test suite deliberately uses a prefix that differs from the
service name, to prove the two are independent strings in code; do not copy it.

A service may read and write **only the namespaces it was granted**, plus `common`, which
every service gets. A downstream service asking for `user` is a 403 with a body that
says so.

### Nothing about this needs a change in keyring

This is worth stating plainly because it is the question everybody asks first. settings-api
verifies the user token against **keyring's published JWKS document**, with the calling
service's audience. There is no token exchange, no introspection call, and no new keyring
endpoint. The token a service is already holding to call keyring is the token it presents
here.

### What comes back

One namespace, with `common` merged **underneath** it — the namespace wins on a key
collision. That merge order is not decorative: `common.job_retention_hours` is the general
answer to "how long do you keep the record of my finished jobs", and
`spotify.job_retention_hours` overrides it where the owning service's own ceiling is
different. Adding a key to `common` can therefore never silently
change what an existing namespace resolves to.

The response also carries a **`fallbacks`** block: for each key, the deployment's default
and what to do when settings-api is unreachable. Cache it with the values. It is what lets
a client behave correctly during an outage without shipping its own copy of the catalogue —
a copy that would drift, and would drift silently, because the only time it is read is
during an outage when nobody is looking.

Pass `profile=` when the caller is acting inside a keyring profile. Profile-scoped keys
(Spotify's market, Lucy's model, which shell a workspace starts) resolve for that name;
account-scoped keys (erasure, spend ceilings, `disabled_providers`) always come along.
Omitting it reads catalogue defaults for the profile-scoped keys. A write of a
profile-scoped key without it is a 422.

---

## 2. The client, and the four lines

Do not write an HTTP call. Use `clients/python/settings_client`, which handles the four
things every consuming service would otherwise get slightly wrong: caching, `If-None-Match`
revalidation, single-flight, and outage behaviour.

```python
from settings_client import HttpSettingsClient

# In the composition root, once.
settings = HttpSettingsClient(
    base_url=config.settings_api_base_url,
    service_token=config.settings_api_token,
)

# At the call site.
resolved = await settings.resolve("spotify", user_token=caller.token, profile=caller.profile)
market = resolved["default_market"]
```

### What the client does for you

**Caching, keyed by the token and the profile.** Not by account id — and the reason is a
vulnerability, so it is worth knowing. The obvious design reads `sub` out of the user
token without verifying it and caches by that. But this client does not verify tokens (that
is settings-api's job, and putting a JWKS fetch into every consuming service is what this
family avoids), so an unverified `sub` is a string the caller supplied. Anything that could
hand your service a forged token claiming `sub: victim` would be served the victim's cached
settings **without a request to settings-api ever being made** — the server's authorisation
bypassed by the cache in front of it. So nothing is ever served for a token settings-api
has not authorised at least once. The profile is part of the key so two credential sets
of the same person do not share a resolved document.

Tokens are short-lived, which sounds like it defeats the cache and does not: within one
token's life you serve from memory and revalidate with `If-None-Match` (a 304 in the steady
state), and a new token for the same person costs one request rather than one per call.

**Single-flight.** Ten concurrent requests for one person's namespace make one outbound
call. A cold cache under load would otherwise be a thundering herd pointed at a service
that is, by construction, on the critical path of every other service in the family.

**Outage behaviour, per setting.** In order:

1. A cached document for **this token** is served with `stale=True`. The person's own most
   recent values are strictly better than any default.
2. Otherwise, if this client has seen this namespace's fallbacks before, a document is
   assembled from them: `use_default` keys get their default, and `refuse` keys raise
   `SettingsRefused` **when read**, not when the namespace is resolved — an operation that
   never touches `disabled_providers` should not be failed for it.
3. Otherwise `SettingsUnavailable`, because there is nothing honest to return.

```python
from settings_client import SettingsRefused, SettingsUnavailable

try:
    resolved = await settings.resolve("search", user_token=caller.token)
    blocked = resolved["disabled_providers"]  # REFUSE: raises during an outage
except SettingsRefused:
    raise SearchUnavailable("cannot confirm which providers you have disabled")
```

**Writes.** `await settings.set("user", "grace_days", 7, user_token=caller.token)` is the
write-through path: a person changing a setting inside your service's own interface, with
your service passing the change along. It is the person's decision travelling through you,
not your decision — the docstring on every write route in settings-api says so, because a
model reads it.

### Testing against it

```python
from settings_client.testing import FakeSettingsClient, asgi_client

fake = FakeSettingsClient()
fake.seed("spotify", {"default_market": "PT"})
fake.unavailable = True  # the case most services forget to test
```

`asgi_client(app, service_token=...)` runs the **real** client against a **real**
settings-api in-process over ASGI, with no sockets. That is what stops the fake and the
service drifting apart.

### Startup

Construct the client at startup; it makes no network call until the first `resolve`. **Do
not** fetch settings during startup, and do not fail to start when settings-api is
unreachable — that turns one outage into two, at the moment the two services are being
restarted together. Every service in this family already follows that rule for keyring.

---

## 3. Per service

Each section says which keys the service reads today, where it reads them, what still needs
a change in that service before a setting does anything, and what stays in its own
configuration.

A catalogue entry marked `existing` means the owning service has that knob,
deployment-wide. It does not by itself mean the service reads each person's value from
here yet. The table below is the answer to that, and a setting a service does not read is
stored and changes nothing until it does.

| Service | Granted | Reads today | Base URL and token | Where |
| --- | --- | --- | --- | --- |
| lucy-api (the hub) | `lucy`, `search`, `spotify` | the `lucy` namespace on every turn; `search.default_result_count`, `search.search_backend` and `spotify.default_device` as tool defaults | `LUCY_SETTINGS_API_BASE_URL`, `LUCY_SETTINGS_API_TOKEN` | `src/lucy_api/core/container.py` |
| keyring-api | `keyring` | `session_ttl_days`, `session_absolute_ttl_days`, `max_sessions` | `KEYRING_SETTINGS_API_BASE_URL`, `KEYRING_SETTINGS_API_TOKEN` | `src/keyring_api/core/preferences.py` |
| user-api | `user` | `max_pinned`, `search_default_limit` | `USER_API_SETTINGS_API_BASE_URL`, `USER_API_SETTINGS_API_TOKEN` | `src/user_api/core/preferences.py` |
| persona-api | `persona` | `recall_default_limit`, `max_pinned_fields`, `max_pinned_notes` | `PERSONA_SETTINGS_API_BASE_URL`, `PERSONA_SETTINGS_API_TOKEN` | `src/persona_api/core/preferences.py` |
| memory-api | `memory` | nothing yet | — | — |
| spotify-api | `spotify` | `default_market`, `max_batch_size`, `confirm_timeout_seconds`, `common.default_profile` | `SPOTIFY_API_SETTINGS_API_BASE_URL`, `SPOTIFY_API_SETTINGS_API_TOKEN` | `src/spotify_api/preferences.py` |
| web-search-api | `search` | `default_model`, `search_backend`, `disabled_providers`, `max_content_chars`, `common.default_profile`, `common.job_retention_hours` | `WSA_SETTINGS_API_BASE_URL`, `WSA_SETTINGS_API_TOKEN` | `app/services/preferences.py` |
| environments-api | `environments` | `idle_environment_hours`, `idle_shell_minutes`, `max_environments_per_profile`, `common.default_profile` | `ENVAPI_SETTINGS_API_BASE_URL`, `ENVAPI_SETTINGS_API_TOKEN` | `app/preferences.py` |

The six sibling services share one shape, and each says so in the module named above:

- **Nothing is read at startup.** With no base URL configured, every person gets the
  deployment's configuration, exactly as before the service read anybody's settings. That
  is how an integration ships dark.
- **A person may narrow a ceiling and never raise it.** A value from here is clamped to
  the deployment's own cap.
- **An outage degrades per setting.** `use_default` keys fall back to the configuration, or
  to settings-api's own defaults once the client has seen them. `refuse` keys, such as
  `common.default_profile` and `search.disabled_providers`, fail only the operation that
  needs them.
- **A refusal is not an outage.** A 401 or 403 from settings-api means the service is
  misconfigured, so the request fails rather than being served defaults that would hide
  it.

The hub is the exception to the last rule for the two namespaces it borrows: a refused
`search` or `spotify` read is logged and the turn goes on without those defaults.

### 3.1 lucy-api (the hub) — namespace `lucy`

The hub reads the `lucy` namespace once per turn, with the session's profile, and clamps it
into its turn policy: the model knobs, the context window, the turn and plan limits, helper
depth and concurrency, permissions, memory-write policy, session defaults and the
prompt-feed toggles. Every `lucy` entry is `existing`, and its origin note in
[catalogue.md](catalogue.md) says what the hub does with it.

It also reads two namespaces it does not own: `search` for the person's backend and result
count, and `spotify` for their default playback device. That is why its grant lists three
namespaces. A missing grant for either answers 403, which the hub logs and otherwise
ignores, because losing a default result count is not worth losing the answer.

Its audience prefix is `lucy-api`, its name in `KEYRING_SERVICE_TOKENS`. The hub's own
[operations guide](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/operations.md)
has its configuration.

### 3.2 keyring-api — namespace `keyring`

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `keyring.session_ttl_days` | `KEYRING_SESSION_TTL_SECONDS` | Yes. |
| `keyring.session_absolute_ttl_days` | `KEYRING_SESSION_ABSOLUTE_TTL_SECONDS` | Yes. |
| `keyring.max_sessions` | `KEYRING_MAX_SESSIONS_PER_ACCOUNT` | Yes. |
| `keyring.email_notifications` | — | *proposed*: keyring has an email backend and no per-account switch. |
| `keyring.notify_on_new_session` | — | *proposed* |
| `keyring.notify_on_credential_change` | — | *proposed*: keyring audits a credential write and sends nothing. |
| `keyring.require_reauth_for_credential_changes` | — | *proposed* |

**Stays in keyring:** every credential and secret (`master_key`, `admin_token`,
`service_tokens`, `signing_key_path`, the OAuth client secrets), `lockout_threshold` and
`lockout_seconds`, every `RateLimitSettings` field, the Argon2 cost parameters,
`access_token_ttl_seconds`, `invite_ttl_seconds`, `reset_ttl_seconds`,
`max_profiles_per_account`, `max_connections_per_profile`, and the email transport
configuration. The lockout and rate-limit numbers in particular are anti-guessing controls:
a person lowering their own lockout threshold is harmless and a person *raising* it is a
security downgrade made by whoever is currently holding their session, so they stay with
the operator.

#### The chicken-and-egg at login, and how it is resolved

keyring needs `session_ttl_days` **at the moment it creates a session** — which is before
it has minted any token for that person, and the internal endpoint requires a user token.

The way through: keyring is the issuer. Once the password check has succeeded it knows who
the person is, so it mints a short-lived token with `sub = <account>` and
`aud = keyring`, presents that as the user token alongside its own service token, and
reads the namespace. settings-api verifies it against the JWKS it already holds. Nothing
circular happens: settings-api never calls keyring, it only verifies signatures against a
public document it caches. That is why keyring's grant uses the prefix `keyring`.

The values are stamped on the session when it is created. A later change applies to
sessions created afterwards, rather than silently lengthening or shortening a live one.

**But note what this means: settings-api is on the critical path of every login.** That is
why `keyring.session_ttl_days` and `max_sessions` are `use_default` rather than `refuse`,
and it is the one place in this catalogue where the conservative value is *not* the
default. A person who chose a one-day idle timeout gets fourteen days during an outage.
The alternative — refusing — means nobody in the family can log in while settings-api is
down, which is a far larger failure than a bounded, temporary lengthening of one timeout.
The reasoning is written into the catalogue entry itself so it is read by whoever changes
it next.

`require_reauth_for_credential_changes` is the opposite and is `refuse`: during an outage
keyring must refuse credential *changes* rather than assume no re-authentication is needed.
Refusing a credential change is an inconvenience; assuming the permissive answer is a door
left open.

> **keyring does not read `common.default_profile`.** keyring has no notion of a default
> profile at all — every one of its routes takes the profile as a required path segment
> with no fallback, and there is no `is_default` column anywhere. `common.default_profile`
> is read by the services that *call* keyring, which then pass it as that path segment.

### 3.3 user-api — namespace `user`

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `user.max_pinned` | `USER_API_MAX_PINNED` | Yes. A person may lower it, never raise it. |
| `user.search_default_limit` | `USER_API_SEARCH_DEFAULT_LIMIT` | Yes, bounded by user-api's own `search_max_limit`. |
| `user.erasure_mode` | `ErasureMode` behind user-api's `SettingsStore` port | No — see below. |
| `user.grace_days` | `grace_days`, `USER_API_DEFAULT_GRACE_DAYS` | No — see below. |
| `user.log_values` | `log_values` behind the same port | No — see below. |
| `user.default_write_scope` | — | *proposed*: user-api has no per-account default scope. |

**Why erasure is not read from here yet: the sweeper has no token.** settings-api answers
only when a person's token is presented, and user-api's erasure sweeper reads
`erasure_mode` and `grace_days` for every account with forgotten entries from a background
task, with no request and so no token. A local copy refreshed on each request does not fix
it: a person who switches to `tombstone` directly in settings-api would not be seen by the
sweeper until they next called user-api, and in the meantime the sweeper would destroy
entries they had just asked to keep. `log_values` is written by user-api's own
`PUT /v1/user/settings` and read by its event log from the same row, so it stays with them.

There are two honest ways out, and each changes user-api's `SettingsStore` port, so
choosing is a decision rather than wiring:

1. **Decide an entry's fate when it is forgotten.** The forgetting request carries a token,
   so the purge deadline can be resolved then and stored on the entry, and the sweeper stops
   reading settings. The cost is that switching to `tombstone` later no longer rescues
   entries already forgotten.
2. **Keep `erasure_mode` and `grace_days` authoritative in user-api** for good, and retire
   the two entries here.

Until one is chosen, those three stay in user-api's SQL store, and setting them here does
not change what user-api does.

Also worth having when that lands: a one-shot backfill pushing existing `user_settings`
rows into settings-api, idempotent, **skipping rows equal to the resolved default** —
because storage here is sparse, and a backfill that wrote every row would pin today's
defaults for everybody for ever.

**Stays in user-api:** `max_entries_per_account`, `max_fields_per_account`,
`max_value_bytes`, `max_value_depth`, `max_note_chars`, `max_events`, `search_max_limit`,
`purge_interval_seconds`, `allowed_scopes`, `audience_prefix`, every keyring URL, the
database path, host and port. These are capacity and deployment decisions: a person has no
basis on which to choose `max_value_bytes`, and `allowed_scopes` is a security boundary.

> **`user.erasure_mode` is deliberately not owner-writable.** Marking it so would stop
> user-api's existing `PUT /v1/user/settings`, whose `operation_id` is public API and which
> MCP clients have tools bound to, from ever writing through to here. See
> [ADR-0004](adr/0004-services-may-write-within-their-own-namespace.md).

### 3.4 persona-api — namespace `persona`

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `persona.recall_default_limit` | `PERSONA_RECALL_DEFAULT_LIMIT` | Yes. |
| `persona.max_pinned_fields` | `PERSONA_MAX_PINNED_FIELDS` | Yes. |
| `persona.max_pinned_notes` | `PERSONA_MAX_PINNED_NOTES` | Yes. |
| `persona.default_persona` | — | *proposed*: no default-selection rule exists. |
| `persona.log_values` | — | *proposed*: the event log records no old values. |
| `persona.erasure_mode` | — | *proposed*: **no grace period or sweeper**. |
| `persona.grace_days` | — | *proposed*: same. |

**`erasure_mode` and `grace_days` propose a mechanism rather than configure one.**
persona-api's forgetting is a permanent tombstone with no expiry. Adopting these means
persona-api gains a sweeper and a grace path. They are spelled exactly as user-api's so a
person who has answered "what does delete mean for my data" once does not answer a
differently-shaped version of it.

**Stays in persona-api:** `max_personas_per_account`, `max_fields_per_persona`,
`max_notes_per_persona`, `max_field_value_bytes`, `max_value_depth`,
`max_value_list_items`, `max_value_object_keys`, `max_note_body_chars`, `max_events`,
`recall_max_limit`, `audience`, and every keyring and storage setting.

### 3.5 memory-api — namespace `memory`

memory-api does not read settings-api yet. It has no settings client, and its own
configuration answers for everybody: `MEMORY_FORGET_GRACE_SECONDS` (30 days) for how long a
forgotten memory can be restored, a compiled-in 30-day recency half-life, and a 16,000
character default for the always-in-context block. The `existing` entries name those
knobs; the `proposed` ones need memory-api to take a per-person value first. The grant
exists in the family's generated configuration so that wiring it up is a change in
memory-api alone.

Four entries are `refuse` — `retrieval_trust_floor`, `write_importance_floor`,
`consolidation` and `erasure_grace_days` — and each entry's description in
[catalogue.md](catalogue.md) says why its default is not safe to land on during an outage.

### 3.6 spotify-api — namespace `spotify`

The motivating example. `default_market` is a fact about a person deployed as an
environment variable that applies to everybody on the box.

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `spotify.default_market` | `SPOTIFY_API_DEFAULT_MARKET` | Yes. |
| `spotify.max_batch_size` | `SPOTIFY_API_MAX_BATCH_SIZE` (`ge=1, le=200`) | Yes, clamped to the deployment's cap. |
| `spotify.confirm_timeout_seconds` | `SPOTIFY_API_CONFIRM_TIMEOUT_SECONDS` (`gt=0, le=300`) | Yes, clamped to the deployment's cap. |
| `spotify.job_retention_hours` | `SPOTIFY_API_JOB_TTL_SECONDS` (`gt=0, le=86400`) | No. |
| `common.default_profile` | `SPOTIFY_API_KEYRING_DEFAULT_PROFILE` | Yes. |
| `spotify.default_device`, `shuffle_on_play`, `repeat_mode`, `allow_explicit` | — | *proposed* in spotify-api. The hub reads `default_device` as its playback default. |

**Bounds are the owning service's, deliberately.** The catalogue caps
`confirm_timeout_seconds` at 300 and `job_retention_hours` at 24 because spotify-api does.
A catalogue that allowed more would let a person set a value the service they were
configuring refuses, and the only sign would be the request failing later. spotify-api
uses a float for the timeout; the catalogue stores an integer, and the conversion happens
once, in spotify-api.

**Stays in spotify-api:** `keyring_base_url`, `keyring_service_token`, `keyring_issuer`,
`keyring_audience`, `keyring_timeout_seconds`, `jwks_cache_seconds`,
`jwks_min_refetch_seconds`, `credential_cache_skew_seconds`,
`credential_cache_default_ttl_seconds`, `spotify_base_url`, `request_timeout_seconds`,
`max_retries`, `retry_backoff_base_seconds`, `max_concurrency`,
`confirm_poll_interval_seconds`, `environment`, `log_level`, `log_format`.

### 3.7 web-search-api — namespace `search`

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `search.default_model` | `WSA_DEFAULT_MODEL` | Yes. |
| `search.search_backend` | `WSA_SEARCH_BACKEND` | Yes — a preference, see below. |
| `search.disabled_providers` | `WSA_DISABLED_PROVIDERS` | Yes, as a union with the deployment's list. |
| `search.max_content_chars` | `WSA_MAX_CONTENT_CHARS` | Yes, clamped to the deployment's cap. |
| `common.default_profile` | `WSA_KEYRING_DEFAULT_PROFILE` | Yes. |
| `common.job_retention_hours` | `WSA_JOB_RETENTION_SECONDS` | Yes. |
| `search.safe_search` | — | *proposed*: a request-only boolean today. |
| `search.store_query_history` | — | *proposed*: nothing persists queries at all. |
| `search.default_result_count`, `search.recency_days` | — | *proposed* in web-search-api. The hub reads `default_result_count` as its research default. |
| `common.locale` | — | Could supply the request-only `language` and `region` defaults. |

**`disabled_providers` is a union.** A person can turn more providers off and can never
re-enable one the operator turned off. It is the one `refuse` setting in the namespace:
during an outage it stays unknown and fails only a search that would need it, because
guessing the empty list would send a query to a provider this person refused.

**`search_backend` is a preference, not a restriction.** web-search-api's router tries the
named backend first and fails over through the rest, so a per-account value changes ordering
and not sources. If somebody wants "never scrape Google", that is not expressible in the
owning service today and it should not be faked here: a setting that reads as a guarantee
and behaves as a hint is worse than no setting. The catalogue entry says so.

**Stays in web-search-api:** `respect_robots`, `allow_private_networks`, `api_keys`,
`user_agent`, `max_response_bytes`, `max_redirects`, `request_timeout_seconds`,
`max_concurrency`, `max_background_jobs`, `max_stored_jobs`, `provider_base_urls`,
`searxng_base_url`, `browser_headless`, `browser_navigation_timeout_ms`,
`model_cache_ttl_seconds`, `provider_probe_timeout_seconds`, and everything keyring. The
first two are not negotiable: **a person cannot turn off SSRF protection or robots
compliance**, and there is no setting in this catalogue that would let them.

### 3.8 environments-api — namespace `environments`

| Setting | Replaces | Read today? |
| --- | --- | --- |
| `environments.idle_environment_hours` | `ENVAPI_ENVIRONMENT_IDLE_TTL_SECONDS` | Yes. |
| `environments.idle_shell_minutes` | `ENVAPI_SHELL_IDLE_TTL_SECONDS` | Yes. |
| `environments.max_environments_per_profile` | `ENVAPI_MAX_ENVIRONMENTS_PER_PROFILE` | Yes, as the lower of the operator cap and the setting. |
| `common.default_profile` | `ENVAPI_DEFAULT_PROFILE` | Yes. |
| `environments.default_shell` | — | *proposed*: one deployment-wide `ENVAPI_SHELL_BINARY` today. |
| `environments.persist_history`, `command_timeout_seconds`, `max_output_bytes` | — | *proposed* in environments-api. |

**Resolved when the environment is created, not when it is reaped.** The reaper runs with
no request in hand, so it has no user token to present to settings-api. environments-api
resolves the namespace in the request that creates the environment, stamps the lifetimes
on the environment's record, and the reaper reads the record. A later change to the
setting applies to environments created afterwards, which is the same rule keyring follows
for sessions.

**The grant uses the name environments-api already calls keyring with.** One user token
travels to both hubs, so the audience prefix here must equal the service's name in
`KEYRING_SERVICE_TOKENS`:
`"environments-api": {"audience_prefix": "environments-api", "namespaces": ["environments"]}`.

**Stays in environments-api:** `allow_network`, `min_sandbox_tier`,
`max_environments_per_account`, `max_shells_per_environment`, `max_processes_per_shell`,
every `*_bytes` quota, `max_cpu_seconds`, `reaper_interval_seconds`,
`shell_close_grace_seconds`, `root`, `operator_accounts`, `api_keys`, and everything
keyring. The first two are not negotiable, for the same reason SSRF protection is not in
`search`: **a person cannot choose the machine's exposure.**

### 3.9 A service this repository does not name — its own namespace

Not every member of the family is public, and a public repository never names a private
one: no module, no grant in a sample, no helpful example in a docstring. See
the family's [ADR-0011](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0011-private-services-are-extensions.md).

Such a service brings its own namespace with it, as an installed package that registers a
module under the entry-point group `settings_api.namespaces`.
[adding-a-service.md](adding-a-service.md#if-the-service-is-not-public) has the steps.
The module supplies `NAMESPACE` and `SETTINGS` exactly as a built-in namespace module
does, and `_assemble()` puts it through the identical check — so a malformed extension
fails at import, in the process that has it, rather than at the first request for that
namespace. Two modules claiming one namespace is refused, which is what stops an extension
quietly replacing a public table.

Everything else is unchanged: the deployment grants the namespace in
`SETTINGS_API_SERVICES`, and lists it in `SETTINGS_API_ALLOWED_NAMESPACES` if that has been
narrowed. Both are a deployment's own configuration rather than anything checked in here.

An entry point rather than a configuration file because a namespace is code — defaults,
bounds, validation and the prose a person reads to decide. A file would mean either
shipping a schema language for settings definitions or handing this process arbitrary
Python at request time.

---

## 4. The audit: what is covered, what is not, and why

Every setting in every service, classified. Read this as the answer to "is there anything
we do not support properly".

### 4.1 Covered

Every setting in [catalogue.md](catalogue.md), which is generated from the code and
states the current count. Existing entries are knobs the owning service already has,
deployment-wide; proposed entries say in the generated documentation exactly what change
the owning service needs first.

### 4.2 Correctly left in the owning service

Four categories, and the test for each is the same: **could a person reasonably choose
this, and does choosing it affect only them?**

| Category | Examples | Why it stays |
| --- | --- | --- |
| **Credentials and keys** | `master_key`, `admin_token`, `service_tokens`, `keyring_service_token`, `signing_key_path`, OAuth client secrets, `api_keys` | Not settings. keyring is where credentials live, and settings-api refuses a value that even looks like one. |
| **Topology** | every `*_base_url`, `*_jwks_url`, `issuer`, `audience`, `database_path`, `host`, `port`, `environment` | Facts about where things are, identical for everyone on the box. Changing one per person is meaningless. |
| **Capacity and resilience** | `max_entries_per_account`, `max_value_bytes`, `max_concurrency`, `max_retries`, `retry_backoff_base_seconds`, `request_timeout_seconds`, `max_response_bytes`, Argon2 costs, `max_events` | The operator is paying for the disk and the CPU. A person has no basis on which to choose these, and the ones that *cap* a person are already exposed as person-lowerable settings where that makes sense (`user.max_pinned`, `environments.max_environments_per_profile`). |
| **Security controls** | `lockout_threshold`, `lockout_seconds`, every rate limit, `respect_robots`, `allow_private_networks`, `allowed_scopes`, `access_token_ttl_seconds` | Would be a downgrade in one direction, and the person holding the session at the time is not necessarily the person. These are in `core/config.py`'s "deliberately absent" list, and that list is the point. |

### 4.3 Considered and deliberately not added

- **`max_events` as a person-level setting** ("how much of my own history do you keep").
  There is a real privacy argument for letting somebody keep *less*, and it was left out:
  each service caps its own log independently, so a person could not reason about the
  effect, and the setting would read as a privacy control while being a storage one. If it
  is ever added it should be one `common` setting, not five.
- **`web-search-api`'s `language` and `region`.** Request-only fields today, and
  `common.locale` already carries the same information in a standard form. A service should
  derive them rather than this catalogue growing two more strings that can disagree with
  the locale.
- **A setting at both levels.** ADR-0002, as amended, lets an entry be profile-scoped
  where its correct value depends on which credential set is in use, but never account
  *and* profile: an overlay would put a second copy of every restriction where a person
  would forget to set it. `common.default_profile` answers "which profile do you mean when
  I don't say" *once*, at account level, which is the question people actually have.

### 4.4 Gaps closed during this audit

These were missing from the original plan's tables and were added after reading the
services' configuration:

| Added | Why it was a gap |
| --- | --- |
| `keyring.session_absolute_ttl_days` | keyring has **two** session lifetimes and only the idle one was listed. Without the ceiling, a session used daily lives for ever and "re-authenticate occasionally" is something the system never asks for. |
| `persona.max_pinned_fields`, `persona.max_pinned_notes` | `user.max_pinned` was in the catalogue with the "pinned is a token budget" argument; persona-api has the identical knob twice, with the identical docstring, and neither was listed. |
| `user.search_default_limit` | `persona.recall_default_limit` was listed and user-api's exact counterpart was not — an inconsistency nobody could have explained. |
| `common.job_retention_hours` | Three services keep a finished-job record with three spellings of the same TTL, and only one of them was listed. |
| `spotify.job_retention_hours` | Needed as a namespace override because spotify-api's own ceiling is 24 hours where the common setting allows a week. |

That last pair is also what makes the `common`-underneath-namespace merge a rule the tests
actually exercise rather than a defensive statement about a collision that could not happen.

### 4.5 Known limitations

- **Cross-setting validation is the owning service's job.** A catalogue entry is checked on
  its own, so `keyring.session_absolute_ttl_days` being below `keyring.session_ttl_days` is
  caught by keyring, not here. There is no mechanism for a rule that spans two settings, and
  adding one would mean the catalogue stopped being a table.
