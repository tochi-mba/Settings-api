# Registering a new service's settings

A new service in the family gets a namespace, some catalogue entries, a service grant and
four lines at its call site. This is the whole process, in order, with a worked example at
the end.

Read [catalogue.md](catalogue.md) for what the existing settings look like and
[integration.md](integration.md) for how a service consumes one.

Nothing here requires a migration, a schema change, or a deploy of any other service. That
is the property the whole design is arranged around: **adding a setting is one entry in one
table plus a doc regeneration.** If a step below ever stops being true, that is the thing
to fix rather than to work around.

---

## Step 0 — Decide whether the setting belongs here at all

Before anything else. Apply this test to each knob you are thinking of moving:

> **Could the person whose data it is reasonably choose this, and does choosing it affect
> only them?**

Both halves matter.

| If it is… | It belongs… |
| --- | --- |
| a credential, key or token | in keyring. settings-api refuses a value that even *looks* like one, with no override. |
| a URL, a port, a path, an issuer, an audience | in your service's own configuration. It is identical for everyone on the box. |
| a capacity or resilience number — timeouts, retries, concurrency, byte caps | in your service. The operator pays for the disk and the CPU; a person has no basis on which to choose. |
| a security control that could be *weakened* — rate limits, lockouts, SSRF protection, robots compliance, token lifetimes you issue | in your service, operator-only. The person holding a session at the time is not necessarily the person. |
| a per-account cap the operator sets, that a person might reasonably want **lower** | here, with `maximum` set to the operator's cap and `operator_clampable=True`. A person may narrow it, never widen it. |
| a decision about *their* data, *their* privacy, or how *they* want to be spoken to | **here.** |

If you find yourself wanting a setting that turns a protection off, read the "deliberately
absent" list in the module docstring of `src/settings_api/core/config.py` first. That list
exists because an absent setting is invisible in a diff and a present one is one line from
being set.

---

## Step 1 — Choose the namespace

One namespace per service, named after the service and not after its package: `media` for
media-tool, `search` for web-search-api. Lowercase, `^[a-z][a-z0-9_]*$`.

**Do not create a namespace for a setting that already belongs in `common`.** If the
question you are answering is one that other services also ask — "what time zone are you
in", "which keyring profile do you mean", "how long do you keep the record of a finished
job" — then the answer belongs in `common`, and your namespace overrides it only if your
service's own bounds genuinely differ. `spotify.job_retention_hours` exists only because
spotify-api caps at 24 hours where the common setting allows a week.

`common` is merged **underneath** every namespace, with the namespace winning on a key
collision. So adding a key to `common` can never silently change what an existing namespace
resolves to.

---

## Step 2 — Write the catalogue module

Create `src/settings_api/domain/catalogue/<namespace>.py`. Copy the shape of an existing
one — `spotify.py` is the smallest.

```python
"""``calendar`` -- what a calendar is allowed to say and how far ahead it looks.

One paragraph on why this namespace looks the way it does. This docstring is lifted
verbatim into the generated documentation, so write it for somebody deciding what to set
rather than for somebody reading the code.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "calendar"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_lookahead_days",
        value_type=SettingType.INT,
        default=14,
        minimum=1,
        maximum=90,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(14,),
        origin=Origin.EXISTING,
        origin_note="calendar-api's `CALENDAR_DEFAULT_LOOKAHEAD_DAYS`, default 14.",
        summary="How far ahead to look when nobody says.",
        description=(
            "What choosing each value actually means for the person, in prose, written "
            "for a model that is deciding whether to touch it.\n\n"
            "Say why the fallback is what it is."
        ),
    ),
)
```

### The fields you cannot skip, and why

**`on_unavailable` has no default in the code.** This is deliberate: choosing between
falling back and refusing is the decision you have to make, and getting it wrong is silent
— it only shows up during an outage. Ask yourself: *if settings-api is down and this
service lands on the default, could that override a restriction the person explicitly
asked for?*

- No → `USE_DEFAULT`, and list the values it is safe to land on in `conservative_values`.
  The default must be among them, and a check refuses the entry otherwise. That pair is
  what turns "the default is safe" from a claim in a docstring into something the build
  verifies — and it means a future change of default trips the check unless somebody
  revisits the list.
- Yes → `REFUSE`. Your service must fail the operation rather than guess.
  `search.disabled_providers` is the canonical case: its default is `[]` meaning "every
  provider is allowed", which is the only value that lets the service work out of the box —
  so falling back to it would send the person's query to exactly the provider they refused,
  silently, because a different service was down.

**`origin`.** `EXISTING` means your service already has the knob, deployment-wide, and
`origin_note` names the attribute. `PROPOSED` means your service needs a change before the
setting does anything, and `origin_note` says what that change is. The generated
documentation repeats it under every entry, so nobody ships a setting that silently does
nothing.

**`summary` and `description` must differ.** A description that only restates the summary
is what somebody writes when they have not decided what the setting means, and there is a
check that refuses it. The summary is one line for deciding whether to touch the setting;
the description says what choosing each value actually means.

**Bounds are the *owning service's*, not aspirational.** If your service validates
`le=300`, the catalogue maximum is 300. A catalogue that allowed more would let a person
set a value the service they were configuring refuses, and the only sign would be a request
failing later.

**`owner_writable_only`** is for the settings your own service must not be able to change
even while holding the person's token. The test: *is the service that would benefit from
changing this the same service that should not be allowed to?* There are two in the
catalogue today — `keyring.require_reauth_for_credential_changes`, because the service most
likely to want it off is the one about to write a credential, and
`search.store_query_history`, because the service that would benefit from keeping a search
history is the one that would be doing the keeping.

### Register the module

In `src/settings_api/domain/catalogue/__init__.py`, add the import and put it in
`_MODULES`. That tuple is also the order the documentation presents namespaces in.

```python
from settings_api.domain.catalogue import calendar, common, keyring, ...

_MODULES = (common, keyring, user, persona, media, spotify, search, calendar)
```

The catalogue is assembled and **checked at import**, so a malformed entry is a process
that does not start rather than a 500 the first time somebody reads that namespace.

### Regenerate the documentation

```
make catalogue
```

and commit `docs/catalogue.md` in the same commit as the entry. A test asserts the two
match, so forgetting is a failing build rather than a stale page.

> **The contract will stop you importing anything else.** Modules under
> `domain/catalogue/` may import `domain/types` and nothing else — not the errors, not the
> value helpers, and certainly not a store. The catalogue is a **table**, and a table is
> something a reviewer can check against what the services actually do. A catalogue module
> that could import the store is a catalogue entry that could have behaviour, and then
> forty-two settings stop being reviewable and become code.

---

## Step 3 — Grant the service access

Add it to `SETTINGS_API_SERVICES`, which is one JSON document:

```bash
SETTINGS_API_SERVICES='{
  "calendar-api": {
    "token": "<32+ random characters, unique to this service>",
    "audience_prefix": "calendar",
    "namespaces": ["calendar"]
  }
}'
```

| Field | What it is | Gotcha |
| --- | --- | --- |
| the key | the service's name, as it appears in logs and in `set_by` provenance | free-form |
| `token` | this service's own bearer token | **≥32 characters**, and **never shared with another service** — both are startup errors |
| `audience_prefix` | the audience family **keyring mints this service's user tokens under** | *not* the service name, and deliberately independent of it. May contain no dot. |
| `namespaces` | which namespaces it may read and write | `common` is added automatically — do not list it. Give it the **narrowest** list that works. |

Three things this configuration is doing, each of which is a security property:

1. **The narrowest list bounds the blast radius.** If this service's token leaks, the
   damage is exactly the namespaces in this list. `media-tool` cannot read
   `user.erasure_mode` because `media` is all it was granted.
2. **`audience_prefix` is the confused-deputy defence.** A user token presented by this
   service must have `aud` equal to the prefix or beginning `prefix.` — so `calendar` and
   `calendar.work` are accepted and `spotify` is refused. Without it, this static token plus
   any user token would read any account.
3. **Two services sharing a token is refused at startup**, because whichever name matched
   first would decide which audience family is acceptable, and the weaker of the two grants
   would be reachable with the other's token.

The service must also be in `SETTINGS_API_ALLOWED_NAMESPACES` if that has been narrowed
from its default — a service granted a namespace the deployment does not allow is refused
at startup rather than at request time.

### And on the keyring side

keyring must be willing to mint tokens with `aud = calendar` for this person. That is
keyring's configuration, not this service's. If it will not, the service will authenticate
fine and every user token it presents will be a 401 — which is the correct behaviour and a
confusing one to debug, so check it early.

---

## Step 4 — Consume it

In the new service. Four lines, and none of them is an HTTP call:

```python
from settings_client import HttpSettingsClient, SettingsRefused

settings = HttpSettingsClient(
    base_url=config.settings_api_base_url,
    service_token=config.settings_api_token,
)

resolved = await settings.resolve("calendar", user_token=caller.token)
lookahead = resolved["default_lookahead_days"]
```

Three rules for the call site:

- **Construct at startup, fetch lazily.** Do not read settings during startup and do not
  fail to start when settings-api is unreachable. That turns one outage into two, at the
  moment the two services are being restarted together.
- **Resolve per request, not per process.** The whole point is that the value is per
  person. The client's cache is what makes that cheap; a value read once into a module-level
  constant is the problem this service exists to fix.
- **Handle `SettingsRefused` where you read the key**, not where you resolve the namespace.
  An operation that never touches the `refuse` setting should not be failed for it.

Configuration to add to the new service, with an empty default so it **ships dark**:

```python
settings_api_base_url: str = ""  # empty disables; the service uses its own defaults
settings_api_token: SecretStr | None = None
```

---

## Step 5 — Tests

In settings-api:

- The parametrised catalogue suite covers every new entry automatically — key shape, the
  default validating against its own bounds, the conservative set, the prose, the anchored
  pattern. **You do not write those.**
- Update the per-namespace counts in `tests/unit/domain/test_catalogue.py`. They exist to
  catch an accidental deletion, so they are meant to need updating.
- Add the service to `tests/conftest.py`'s `SERVICES` if you want it in the internal-surface
  tests, and add: it reads its own namespace merged with `common`; it gets a **403** for
  another service's namespace; a user token from another audience family is a **401**.
- Add a behavioural test only if the setting means something the generic ones cannot check.

In the new service:

- Use `FakeSettingsClient` from `settings_client.testing` for almost everything.
- Write at least one test with `fake.unavailable = True`. It is the case most services
  forget and the one their users notice.
- Use `asgi_client(...)` for one contract test against the real settings-api in-process, so
  the fake and the service cannot drift apart.

---

## Step 6 — The checklist

- [ ] Every setting passes the Step 0 test, and nothing that weakens a protection is in the
      list.
- [ ] `on_unavailable` is a decision you made, not a copy from the entry above.
- [ ] Every `USE_DEFAULT` entry's default is in its `conservative_values`.
- [ ] Bounds match the owning service's own validators exactly.
- [ ] `origin` is honest, and `origin_note` names the attribute or the change needed.
- [ ] The module is in `_MODULES` and `make catalogue` has been run and committed.
- [ ] The service grant has a unique ≥32-character token and the narrowest namespace list.
- [ ] `audience_prefix` matches what keyring actually mints.
- [ ] The new service ships dark: empty base URL keeps its old behaviour.
- [ ] `make check` passes.

---

## Worked example: adding `calendar-api`

Say a new `calendar-api` joins the family with three knobs:
`CALENDAR_DEFAULT_LOOKAHEAD_DAYS=14`, `CALENDAR_WORKING_HOURS_START=9`, and
`CALENDAR_DECLINE_OUTSIDE_WORKING_HOURS=false`.

**Step 0.** All three pass: each is a decision about the person's own time, and choosing one
affects only them. Its `CALENDAR_GOOGLE_CLIENT_SECRET` does not — that goes to keyring. Its
`CALENDAR_SYNC_INTERVAL_SECONDS` does not either — that is a capacity decision.

**Step 1.** Namespace `calendar`. But note: *which time zone are the working hours in?* That
is `common.timezone`, already there, already answered by the person once. Do not add
`calendar.timezone`.

**Step 2.** Three entries in `domain/catalogue/calendar.py`:

- `default_lookahead_days` — `INT`, 14, 1–90, `USE_DEFAULT` with `conservative_values=(14,)`
  (a wider window during an outage shows more of the person's own calendar to the person
  themselves, which is not a privacy downgrade), `EXISTING`.
- `working_hours_start` — `INT`, 9, 0–23, `USE_DEFAULT`, `EXISTING`.
- `decline_outside_working_hours` — `BOOL`, `false`, **`REFUSE`**. Think about this one: the
  default is `false` because the service has to work out of the box, so falling back to it
  during an outage would auto-accept a meeting at midnight for somebody who had explicitly
  said not to. The default is permissive, so the answer is `REFUSE`. This is exactly the
  reasoning `search.disabled_providers` goes through, and it is why the field has no
  default.

**Step 3.** Grant: `{"calendar-api": {"token": "…", "audience_prefix": "calendar",
"namespaces": ["calendar"]}}`. Not `["calendar", "user"]` — the calendar does not need to
know what deletion means for somebody's notes.

**Step 4.** `resolved = await settings.resolve("calendar", user_token=caller.token)`, and
`resolved["decline_outside_working_hours"]` inside a `try`/`except SettingsRefused` that
declines to auto-respond rather than auto-accepting.

**Step 5.** `make catalogue`, update the counts, add the 403/401 tests, and one test in
calendar-api with `fake.unavailable = True` asserting that it does not auto-accept.

Total: one new file, two edited lines, a regenerated doc, and a JSON blob in a deployment.
No migration, no schema change, and no other service redeployed.
