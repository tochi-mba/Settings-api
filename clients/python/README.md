# settings-client

The client every service in this family uses to read one person's settings from
settings-api. Four lines at the call site; everything else -- caching, revalidation,
single-flight, and what to do when settings-api is down -- is handled here so that each
consuming service does not get it slightly wrong on its own.

## Install

It is not published to a package index. Take it from a tagged git source with `uv`:

```toml
[project]
dependencies = ["settings-client"]

[tool.uv.sources]
settings-client = { git = "https://github.com/tochi-mba/Settings-api", subdirectory = "clients/python", tag = "settings-client-v0.4.2" }
```

It needs Python 3.12 or later and depends only on `httpx`.

## Use

```python
from settings_client import HttpSettingsClient

settings = HttpSettingsClient(
    base_url=config.settings_api_base_url,
    service_token=config.settings_api_token,
)
resolved = await settings.resolve("spotify", user_token=caller.token, profile=caller.profile)
market = resolved["default_market"]
```

- `service_token` is this service's entry in settings-api's `SETTINGS_API_SERVICES`.
- `user_token` is the end user's keyring token, the one the service already holds. Its
  audience must belong to this service's own audience family.
- `profile` selects profile-scoped values. Account-scoped values always come back.
- `await settings.set(namespace, key, value, user_token=..., profile=...)` writes one
  setting on the person's behalf and returns the new revision. Only ever do this for a
  change the person asked for.
- `settings.forget(user_token, namespace=None)` stops serving that person's cached
  settings, for one namespace or all of them. `set` does this for its own writes; call
  it when a setting changed some other way, so the next `resolve` asks again.
- `await settings.aclose()` releases the connection pool.

Construct the client at startup; it makes no request until the first `resolve`. Do not
fetch settings during startup, and do not fail to start because settings-api is down.

## What it raises

| Exception | Means |
| --- | --- |
| `SettingsRefused` | settings-api is unreachable, and the key you read is one whose default must not be guessed. Raised when the key is **read**, not when the namespace is resolved, so fail only the operation that needs it. |
| `SettingsUnavailable` | settings-api is unreachable and this client has never seen the namespace, so it knows no defaults. On a write: the write could not be sent, or was answered with a body that does not say whether it was saved. |
| `SettingsRejected` | settings-api answered and refused: 401 or 403 for a misconfigured grant or token, 404 for an unknown namespace, and on a write 409 or 422. `.status_code` and `.detail` say which. |

All three derive from `SettingsClientError`. During an outage the client serves this
token's cached document first, with `stale=True`, then a document built from the
namespace's declared fallbacks, and only then raises.

An outage is not only a refused connection. A 5xx is one, and so is a 2xx whose body this
client cannot use: a proxy's HTML page, an empty body, JSON without `settings`, `fallbacks`
or an integer `revision`, or an `on_unavailable` this version does not know. Each degrades
exactly as above, so a consuming service answers 503 rather than a `KeyError` becoming a
500. The client logs a warning on the `settings_client` logger with the status, content
type, length and what was wrong -- never a value from the body. A document that fails part
way through teaches the client nothing: its fallbacks are not kept.

## Testing a consuming service

```python
from settings_client.testing import FakeSettingsClient, asgi_client

fake = FakeSettingsClient()
fake.seed("spotify", {"default_market": "PT"})  # every profile
fake.seed("spotify", {"allow_explicit": False}, profile="family")  # only "family"
fake.unavailable = True  # the case most services forget to test
```

Seed a profile-scoped key **with** its profile, and assert on `fake.asked`, the
`(namespace, profile)` of every resolve. A value seeded without a profile is returned to
every resolve, so a service that forgets to pass the person's profile still passes a test
seeded that way, while in production settings-api would return none of their profile-scoped
choices.

`FakeSettingsClient` satisfies the same `SettingsClient` protocol as the real client.
`asgi_client(app, service_token=...)` is the real client talking to an ASGI app in-process,
for a contract test against a real settings-api.

[docs/integration.md](https://github.com/tochi-mba/Settings-api/blob/main/docs/integration.md)
in the settings-api repository says how each service in the family is wired.
