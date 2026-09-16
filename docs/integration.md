# Integrating a service with settings-api

How a service in this family reads one person's settings, what it stops configuring for
itself when it does, and what it must keep. One section per service, each with the exact
config keys it replaces and the diff at the call site.

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
   `media-tool` may present `media-tool` and `media-tool.jobs`, and nothing else. A token
   minted for `spotify-api`, or the person's own `settings`-audience token, is refused
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
every service gets. `media-tool` asking for `user` is a 403 with a body that says so.

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
`media.job_retention_hours` and `spotify.job_retention_hours` override it where the owning
service's own ceiling is different. Adding a key to `common` can therefore never silently
change what an existing namespace resolves to.

The response also carries a **`fallbacks`** block: for each key, the deployment's default
and what to do when settings-api is unreachable. Cache it with the values. It is what lets
a client behave correctly during an outage without shipping its own copy of the catalogue —
a copy that would drift, and would drift silently, because the only time it is read is
during an outage when nobody is looking.

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
resolved = await settings.resolve("spotify", user_token=caller.token)
market = resolved["default_market"]
```

### What the client does for you

**Caching, keyed by the token.** Not by account id — and the reason is a vulnerability, so
it is worth knowing. The obvious design reads `sub` out of the user token without
verifying it and caches by that. But this client does not verify tokens (that is
settings-api's job, and putting a JWKS fetch into every consuming service is what this
family avoids), so an unverified `sub` is a string the caller supplied. Anything that could
hand your service a forged token claiming `sub: victim` would be served the victim's cached
settings **without a request to settings-api ever being made** — the server's authorisation
bypassed by the cache in front of it. So nothing is ever served for a token settings-api
has not authorised at least once.

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

Each section lists what the service **stops** configuring for itself, what it **keeps**,
and what needs changing before a `proposed` setting does anything.

### 3.1 user-api — namespace `user`

**The easy one, and the reason the port exists.** `user-api/src/user_api/users/settings.py`
already declares a `SettingsStore` port, and its docstring already says a settings-api
becomes the second adapter and that "nothing above this line changes". Hold it to that.

| Setting | Replaces | Notes |
| --- | --- | --- |
| `user.erasure_mode` | `ErasureMode` behind the port | Already per-account. |
| `user.grace_days` | `grace_days`, `USER_API_DEFAULT_GRACE_DAYS` | |
| `user.log_values` | `log_values` behind the port | |
| `user.max_pinned` | `USER_API_MAX_PINNED` | Person may lower, never raise. |
| `user.search_default_limit` | `USER_API_SEARCH_DEFAULT_LIMIT` | Bounded by its own `search_max_limit`. |
| `common.default_write_scope`¹ | — | ¹ lives in `user.default_write_scope`; proposed. |

**Stays in user-api:** `max_entries_per_account`, `max_fields_per_account`,
`max_value_bytes`, `max_value_depth`, `max_note_chars`, `max_events`, `search_max_limit`,
`purge_interval_seconds`, `allowed_scopes`, `audience_prefix`, every keyring URL, the
database path, host and port. These are capacity and deployment decisions: a person has no
basis on which to choose `max_value_bytes`, and `allowed_scopes` is a security boundary.

**The wiring:**

```python
# src/user_api/remote/http_settings.py  (new package -- see the contract note below)
class HttpSettingsStore:
    """The second adapter behind SettingsStore. Nothing above the port changes."""

    async def get(self, account_id: str, *, default_grace_days: int) -> UserSettings:
        resolved = await self._client.resolve("user", user_token=self._token_for(account_id))
        return UserSettings(
            erasure_mode=ErasureMode(resolved["erasure_mode"]),
            grace_days=int(resolved["grace_days"]),
            log_values=bool(resolved["log_values"]),
        )
```

Then one line in `core/container.py`:

```python
-        user_settings = SqlSettingsStore(database=database)
+        user_settings = (
+            CachingSettingsStore(
+                remote=HttpSettingsStore(client=settings_client),
+                local=SqlSettingsStore(database=database),
+            )
+            if settings.settings_base_url
+            else SqlSettingsStore(database=database)
+        )
```

`USER_API_SETTINGS_BASE_URL` empty (the default) keeps `SqlSettingsStore` alone, so this
**ships dark** and is turned on per deployment.

**The contract will block you, on purpose.** user-api's import-linter contract "Keyring is
spoken to from one package only" forbids `httpx` to `user_api.users`, so an HTTP adapter
cannot live there. user-api's AGENTS.md invariant 3 says to change the enforcement
deliberately and say why in the commit message rather than work around it. Do that: rename
the contract to "Outbound HTTP lives in one package per remote", add `user_api.remote` as
the one other permitted package, and put the adapter there. The commit body explains that
the service now has two remotes, not one.

**Do not change** `SettingsStore`, `UserSettings`, `ErasureMode`, `users/erasure.py`, or any
route under `/v1/user`. If the port turns out to need changing, that is a finding worth
reporting, not a change to make quietly.

**Why user-api is not wired yet: the sweeper has no token.** The adapter sketch above hides
a problem inside `self._token_for(account_id)`. settings-api answers only when a person's
token is presented, and user-api's erasure sweeper (`Erasure.sweep_once`) reads
`erasure_mode` and `grace_days` for every account with forgotten entries from a background
task, with no request and so no token. A local copy refreshed on each request does not fix
it: a person who switches to `tombstone` directly in settings-api would not be seen by the
sweeper until they next called user-api, and in the meantime the sweeper would destroy
entries they had just asked to keep.

There are two honest ways out, and each changes the port, so choosing is a decision rather
than wiring:

1. **Decide an entry's fate when it is forgotten.** The forgetting request carries a token,
   so the purge deadline can be resolved then and stored on the entry, and the sweeper stops
   reading settings. The cost is that switching to `tombstone` later no longer rescues
   entries already forgotten.
2. **Keep `erasure_mode` and `grace_days` authoritative in user-api**, and move only the
   settings read on the request path (`log_values`, `max_pinned`, `search_default_limit`)
   to settings-api.

Until one is chosen, user-api keeps `SqlSettingsStore`, and nothing here is half-wired.

Also worth having: a one-shot backfill in `scripts/` pushing existing `user_settings` rows
into settings-api, idempotent, **skipping rows equal to the resolved default** — because
storage here is sparse, and a backfill that wrote every row would pin today's defaults for
everybody for ever.

> **`user.erasure_mode` is deliberately not owner-writable.** Marking it so would break
> user-api's existing `PUT /v1/user/settings`, whose `operation_id` is public API and which
> MCP clients have tools bound to. See [ADR-0004](adr/0004-services-may-write-within-their-own-namespace.md).

### 3.2 keyring-api — namespace `keyring`

**The subtle one. Read this section before wiring anything.**

| Setting | Replaces |
| --- | --- |
| `keyring.session_ttl_days` | `KEYRING_SESSION_TTL_SECONDS` |
| `keyring.session_absolute_ttl_days` | `KEYRING_SESSION_ABSOLUTE_TTL_SECONDS` |
| `keyring.max_sessions` | `KEYRING_MAX_SESSIONS_PER_ACCOUNT` |
| `keyring.email_notifications` | *proposed* — keyring has an email backend and no per-account switch |
| `keyring.notify_on_new_session` | *proposed* |
| `keyring.require_reauth_for_credential_changes` | *proposed* |

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
reads `keyring.session_ttl_days`. settings-api verifies it against the JWKS it already
holds. Nothing circular happens: settings-api never calls keyring, it only verifies
signatures against a public document it caches.

**But note what this means: settings-api would be on the critical path of every login.**
That is why `keyring.session_ttl_days` and `max_sessions` are `use_default` rather than
`refuse`, and it is the one place in this catalogue where the conservative value is *not*
the default. A person who chose a one-day idle timeout gets fourteen days during an outage.
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

### 3.3 persona-api — namespace `persona`

**The most work, because persona-api has no per-account settings at all.** Its `Settings`
is one process-wide pydantic-settings model built once at startup and frozen into its
adapters as plain integers; no `account_id` ever reaches a config lookup, there is no
settings table, and there is no request-scoped seam where a per-account override could be
applied.

| Setting | Replaces | Ready? |
| --- | --- | --- |
| `persona.recall_default_limit` | `PERSONA_RECALL_DEFAULT_LIMIT` | See the defect below. |
| `persona.max_pinned_fields` | `PERSONA_MAX_PINNED_FIELDS` | Yes. |
| `persona.max_pinned_notes` | `PERSONA_MAX_PINNED_NOTES` | Yes. |
| `persona.default_persona` | — | *proposed*: no default-selection rule exists. |
| `persona.log_values` | — | *proposed*: the event log records no old values. |
| `persona.erasure_mode` | — | *proposed*: **no erasure mechanism at all**. |
| `persona.grace_days` | — | *proposed*: same. |

**Defect to fix first:** `recall_default_limit` exists in persona-api's config and **nothing
reads it**. The live default is the literal `limit: LimitQuery = 20` on six routes. So the
documented default and the actual default are two different twenties that could drift apart
without anybody noticing. Fix that before wiring the setting up, or wiring it up will
appear to do nothing.

**`erasure_mode` and `grace_days` propose a mechanism rather than configure one.**
persona-api's forgetting is a permanent tombstone with no expiry — its own operations
documentation says so, and `core/container.py` states outright that "rate-limit records
expire; nothing here does". Adopting these means persona-api gains a sweeper and a grace
path. They are spelled exactly as user-api's so a person who has answered "what does delete
mean for my data" once does not answer a differently-shaped version of it.

**Stays in persona-api:** `max_personas_per_account`, `max_fields_per_persona`,
`max_notes_per_persona`, `max_field_value_bytes`, `max_value_depth`,
`max_value_list_items`, `max_value_object_keys`, `max_note_body_chars`, `max_events`,
`recall_max_limit`, `audience`, and every keyring and storage setting.

### 3.4 media-tool — namespace `media`

Verified against media-tool's `core/config.py`: every attribute named below exists under
that name, with the default the catalogue entry records.

| Setting | Replaces |
| --- | --- |
| `media.artifact_retention_hours` | `artifact_ttl_seconds` |
| `media.job_retention_hours` | `job_ttl_seconds` |
| `media.concurrent_jobs` | `max_active_jobs_per_account` |
| `media.max_file_gb` | `max_file_bytes` |
| `media.delete_artifact_after_download` | *proposed* |
| `media.preferred_quality` | *proposed* — **furthest from working** |
| `common.default_profile` | `default_profile` (defaults to `"default"`, not `"personal"`) |

`media.preferred_quality` needs the most work in the owning service: media-tool's
`MediaQuery` has no quality concept at all, so adopting it means that type gains a field
and the downloader learns to pass it on. Until then, setting it stores the value and
changes nothing — which `docs/catalogue.md` says under the entry.

Note the default disagreement this resolves: media-tool's `default_profile` is `"default"`
while spotify-api's and web-search-api's are `"personal"`. `common.default_profile` is
`"personal"`, so **media-tool's effective default changes** when it adopts this. That is
the point of consolidating it, and it is a behaviour change worth calling out in the
migration rather than discovering.

### 3.5 spotify-api — namespace `spotify`

The motivating example. `default_market` is a fact about a person deployed as an
environment variable that applies to everybody on the box.

| Setting | Replaces |
| --- | --- |
| `spotify.default_market` | `DEFAULT_MARKET` |
| `spotify.max_batch_size` | `MAX_BATCH_SIZE` (`ge=1, le=200`) |
| `spotify.confirm_timeout_seconds` | `CONFIRM_TIMEOUT_SECONDS` (`gt=0, le=300`) |
| `spotify.job_retention_hours` | `JOB_TTL_SECONDS` (`gt=0, le=86400`) |
| `common.default_profile` | `KEYRING_DEFAULT_PROFILE` |

**Bounds are the owning service's, deliberately.** The catalogue caps
`confirm_timeout_seconds` at 300 and `job_retention_hours` at 24 because spotify-api does.
A catalogue that allowed more would let a person set a value the service they were
configuring refuses, and the only sign would be the request failing later.

**Stays in spotify-api:** `keyring_base_url`, `keyring_service_token`,
`keyring_timeout_seconds`, `credential_cache_skew_seconds`,
`credential_cache_default_ttl_seconds`, `spotify_api_base_url`,
`request_timeout_seconds`, `max_retries`, `retry_backoff_base_seconds`,
`max_concurrency`, `confirm_poll_interval_seconds`, `environment`, `log_level`,
`log_format`.

**Three mechanical notes:**

- spotify-api has **no env prefix**, so its settings-api configuration is
  `SETTINGS_API_BASE_URL` and `SETTINGS_API_TOKEN` as bare names.
- Its `Settings` is `frozen=True` behind an `lru_cache(maxsize=1)` singleton, and nothing
  holds a reference to it — every collaborator is built from primitives at startup. A
  per-person value therefore has to be threaded through `api/dependencies.py` to the call
  site; it cannot be read out of `get_settings()`.
- **Its README is stale.** The Configuration table lists `SPOTIFY_CLIENT_ID`,
  `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_ACCOUNTS_BASE_URL` and `TOKEN_EXPIRY_SKEW_SECONDS`, none
  of which exist any more — the service moved to keyring-brokered credentials. Build against
  `config.py` and `.env.example`, not the README.

### 3.6 web-search-api — namespace `search`

| Setting | Replaces | Ready? |
| --- | --- | --- |
| `search.default_model` | `WSA_DEFAULT_MODEL` | Yes. |
| `search.search_backend` | `WSA_SEARCH_BACKEND` | Yes — but see below. |
| `search.disabled_providers` | `WSA_DISABLED_PROVIDERS` | Needs per-caller filtering. |
| `search.max_content_chars` | `WSA_MAX_CONTENT_CHARS` | Yes. |
| `search.safe_search` | — | *proposed*: request-only boolean today. |
| `search.store_query_history` | — | *proposed*: nothing persists queries at all. |
| `common.default_profile` | `WSA_KEYRING_DEFAULT_PROFILE` | Yes. |
| `common.job_retention_hours` | `WSA_JOB_RETENTION_SECONDS` | Yes. |
| `common.locale` | — | Can supply the request-only `language`/`region` defaults. |

**The structural obstacle, and it is the real work here.** `disabled_providers` is consumed
exactly once, in `bootstrap.build_llm_providers()` during lifespan startup, and the provider
list is then frozen for the process. Whether a provider is *usable* is already per-caller
(decided by probing keyring), but whether it is *constructed* is global. A per-account "turn
off provider X" needs the registry to filter per caller at catalogue time, not at process
start. Until that lands, `search.disabled_providers` cannot be honoured — and it is the one
`refuse` setting in the namespace, so honouring it half-way would be worse than not at all.

**`search_backend` is a preference, not a restriction.** web-search-api's router tries the
named backend first and fails over through the rest, so a per-account value changes ordering
and not sources. If somebody wants "never scrape Google", that is not expressible in the
owning service today and it should not be faked here: a setting that reads as a guarantee
and behaves as a hint is worse than no setting. The catalogue entry says so.

**Three defects found while surveying, worth fixing while you are in there:**

- `enabled_providers` is **dead**: declared in `app/config.py` and read nowhere. Its comment
  is also stale — providers no longer take API keys from the environment at all.
- `max_concurrency_per_host` is **dead**: declared and never read. Per-host throttling is
  not implemented.
- `SerperSearchBackend.for_caller()` is **never called** anywhere in `app/`. The single
  process-wide instance is built with `caller=None`, so `_auth()` always short-circuits,
  `is_configured()` always returns `False`, and the Serper backend is silently skipped on
  every request. Effectively only `google` and `searxng` can ever serve a query — which is
  why `search.search_backend` offers exactly those two.

**Stays in web-search-api:** `respect_robots`, `allow_private_networks`, `api_keys`,
`user_agent`, `max_response_bytes`, `max_redirects`, `request_timeout_seconds`,
`max_concurrency`, `max_background_jobs`, `max_stored_jobs`, `provider_base_urls`,
`searxng_base_url`, `browser_headless`, `browser_navigation_timeout_ms`,
`model_cache_ttl_seconds`, `provider_probe_timeout_seconds`, and everything keyring. The
first two are not negotiable: **a person cannot turn off SSRF protection or robots
compliance**, and there is no setting in this catalogue that would let them.

**One more mechanical note:** web-search-api's settings model uses `extra="ignore"`, so a
misspelled `WSA_*` variable is silently discarded with no error. Every other service in this
family treats that as a startup error. Worth fixing in the same pass.

### 3.7 environments-api — namespace `environments`

| Setting | Replaces | Ready? |
| --- | --- | --- |
| `environments.idle_environment_hours` | `ENVAPI_ENVIRONMENT_IDLE_TTL_SECONDS` | Yes. |
| `environments.idle_shell_minutes` | `ENVAPI_SHELL_IDLE_TTL_SECONDS` | Yes. |
| `environments.max_environments_per_profile` | `ENVAPI_MAX_ENVIRONMENTS_PER_PROFILE` | Yes, as the lower of the operator cap and the setting. |
| `environments.default_shell` | — | *proposed*: one deployment-wide `ENVAPI_SHELL_BINARY` today. |
| `common.default_profile` | `ENVAPI_DEFAULT_PROFILE` | Yes. |

**Resolve when the environment is created, not when it is reaped.** The reaper runs with no
request in hand, so it has no user token to present to settings-api. Resolve the
`environments` namespace in the request that creates the environment, store the resulting
lifetimes and cap on the environment's record, and let the reaper read the record. A later
change to the setting then applies to environments created afterwards, which is the same
rule media-tool follows for job retention.

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

---

## 4. The audit: what is covered, what is not, and why

Every setting in every service, classified. Read this as the answer to "is there anything
we do not support properly".

### 4.1 Covered

46 settings across 8 namespaces, listed in [catalogue.md](catalogue.md). Of those, 28 are
`existing` — a knob the owning service already has, deployment-wide — and 18 are `proposed`
and say in the generated documentation exactly what change the owning service needs first.

### 4.2 Correctly left in the owning service

Four categories, and the test for each is the same: **could a person reasonably choose
this, and does choosing it affect only them?**

| Category | Examples | Why it stays |
| --- | --- | --- |
| **Credentials and keys** | `master_key`, `admin_token`, `service_tokens`, `keyring_service_token`, `signing_key_path`, OAuth client secrets, `api_keys` | Not settings. keyring is where credentials live, and settings-api refuses a value that even looks like one. |
| **Topology** | every `*_base_url`, `*_jwks_url`, `issuer`, `audience`, `database_path`, `host`, `port`, `environment` | Facts about where things are, identical for everyone on the box. Changing one per person is meaningless. |
| **Capacity and resilience** | `max_entries_per_account`, `max_value_bytes`, `max_concurrency`, `max_retries`, `retry_backoff_base_seconds`, `request_timeout_seconds`, `max_response_bytes`, Argon2 costs, `max_events` | The operator is paying for the disk and the CPU. A person has no basis on which to choose these, and the ones that *cap* a person are already exposed as person-lowerable settings where that makes sense (`max_pinned`, `concurrent_jobs`). |
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
- **A per-profile setting, of any kind.** The whole point of ADR-0002 is that there is one
  settings set per account. `common.default_profile` answers "which profile do you mean
  when I don't say" *once*, at account level, which is the question people actually have.

### 4.4 Gaps closed during this audit

These were missing from the original plan's tables and were added after reading the
services' configuration:

| Added | Why it was a gap |
| --- | --- |
| `keyring.session_absolute_ttl_days` | keyring has **two** session lifetimes and only the idle one was listed. Without the ceiling, a session used daily lives for ever and "re-authenticate occasionally" is something the system never asks for. |
| `persona.max_pinned_fields`, `persona.max_pinned_notes` | `user.max_pinned` was in the catalogue with the "pinned is a token budget" argument; persona-api has the identical knob twice, with the identical docstring, and neither was listed. |
| `user.search_default_limit` | `persona.recall_default_limit` was listed and user-api's exact counterpart was not — an inconsistency nobody could have explained. |
| `common.job_retention_hours` | Three services keep a finished-job record with three spellings of the same TTL, and only media-tool's was listed. |
| `spotify.job_retention_hours` | Needed as a namespace override because spotify-api's own ceiling is 24 hours where the common setting allows a week. |

That last pair is also what makes the `common`-underneath-namespace merge a rule the tests
actually exercise rather than a defensive statement about a collision that could not happen.

### 4.5 Known limitations

- **`search.disabled_providers` cannot be honoured yet** without the per-caller provider
  filtering described in §3.6, and it is a `refuse` setting — so a deployment that turns it
  on before that change would be refusing searches for a restriction it cannot apply.
- **Cross-setting validation is the owning service's job.** A catalogue entry is checked on
  its own, so `keyring.session_absolute_ttl_days` being below `keyring.session_ttl_days` is
  caught by keyring, not here. There is no mechanism for a rule that spans two settings, and
  adding one would mean the catalogue stopped being a table.
