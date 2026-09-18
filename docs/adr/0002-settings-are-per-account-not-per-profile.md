# ADR-0002: Settings scopes are exclusive — account or profile, declared per entry

**Status:** accepted (amended 2026-09-17).

The original decision is below. The amendment does not reverse it. Identity still comes
only from the verified `sub`. A body field named `profile` is still a 422. Overlay of the
same key at two levels is still refused. What changed is the claim that *every* setting is
a fact about the person: some are, and some are facts about which credential set is in
use, and the catalogue now says which.

## Amendment

Read the catalogue and ask, entry by entry, whose fact each one is. That test was always
the right test. It does not yield a single answer.

**Account-scoped** (the default): restrictions, spend ceilings, erasure, identity, and
anything whose correct value does not change when a different Spotify login or a different
shell is in use. `search.disabled_providers`, `user.erasure_mode`, `lucy.approval_policy`,
`common.timezone`, `common.default_profile`. A work profile must not silently weaken a
promise made on the account. `common` is forced account-scoped; `default_profile` is
forced even if it moved, because storing it per profile is circular.

**Profile-scoped** (opt in on the entry): taste, routing, and anything whose correct
value depends on which credential set is in use. `spotify.default_market`,
`spotify.default_device`, `lucy.model`, `lucy.permission_mode`, `search.safe_search`,
`environments.default_shell`, prompt-feed toggles. Somebody with a work Spotify in one
country and a personal Spotify in another can have both markets. Somebody who wants the
work assistant on `auto` and the home one on `ask` can have both modes.

The original three objections to "everything per profile" still hold, which is why this
is not that:

1. Multiplying every answer by the number of profiles is still wrong for restrictions.
   Those stay account-scoped.
2. Silent weakening is still the failure. A profile cannot overlay an account
   restriction; exclusive scopes mean there is no second copy of `disabled_providers`
   to forget to set.
3. Identity still does not come from the caller naming an account. The profile is a
   *query parameter* selecting which of this person's profile-scoped rows to read. The
   account is still the verified `sub`. A body `{"profile": "work"}` is still a 422
   (`extra="forbid"`), because a silently ignored body field is the same lie it always
   was.

Storage is `(account_id, profile, namespace, key)`. Account-scoped rows live under the
sentinel `*`, which keyring's profile-name pattern refuses, so the two cannot collide.
A write of an account-scoped key always stores under `*`; an extra `?profile=` is
ignored, so Lucy can always pass the session's profile on a mixed document. A write of
a profile-scoped key without `?profile=` is a 422 that names the keys. A leftover `*`
row for a key that is now profile-scoped is ignored on read — exclusive, not overlay.

Export format 2 splits `settings` (account) from `profiles.{name}` (per profile).
Format 1 is still imported: profile-scoped keys land on `personal`.

The four mechanisms, amended:

| Where | What it makes impossible |
| --- | --- |
| The schema | The primary key includes `profile`. Account rows use `*`; named rows use a keyring profile name. Overlay of the same key at both levels is unrepresentable as a resolved value. |
| The wire | No route accepts an account id. `profile` is a query parameter, never a path segment and never a body field. `extra="forbid"` still makes a body `profile` a 422. |
| The identity | The account comes only from a verified `sub`. |
| The suite | Two tokens for the same `sub` that pass the same `?profile=` share those rows. Account-scoped settings are the same under every profile. A profile-scoped write without a profile is 422. |

The original cost — "somebody who wants a different Spotify market for work cannot have
one" — is paid only for settings that are actually about the person. The catalogue
declares the rest.

---

## Original decision

One settings set per account, regardless of how many keyring profiles that account has.
The primary key is `(account_id, namespace, key)`, and there is no profile column, no
profile parameter, no profile path segment and no profile body field anywhere in the
service.

The rest of this record is the argument that made that the right *default*, and that
still forbids overlay. The amendment above is what happens when the catalogue is read
entry by entry instead of treated as one blob.

## Context

keyring is the auth root for this family. It holds the accounts, and inside each account it
holds **profiles**: named sets of credentials. `personal` and `work` can hold two different
Spotify accounts, two different API keys, two different logins to the same service.
Profiles are not a detail of keyring's storage -- they are in its URLs, and `common.py`
says so:

> keyring itself has no notion of a default profile at all -- every one of its routes takes
> the profile as a required path segment with no fallback.

So the family already has two levels a thing can belong to: the account, and the profile
within it. This service holds forty-two settings. Before the first table was written,
somebody had to decide which of those two levels a settings set lives at, because the
answer is the primary key and a primary key is not a thing you revise later over an
afternoon.

## Decision

One settings set per account, regardless of how many keyring profiles that account has.
The primary key is `(account_id, namespace, key)`, and there is no profile column, no
profile parameter, no profile path segment and no profile body field anywhere in the
service.

`0001_initial.sql` opens with it, because the schema is where the decision is actually
made:

> There is NO PROFILE COLUMN (ADR-0002). One settings set per account, regardless of how
> many keyring profiles that account has. The primary key below is
> `(account_id, namespace, key)`, so a per-profile value is not "discouraged" -- it is
> unrepresentable.

The port says the same thing about the interface above it: "A per-profile value is not
discouraged here; it is unspellable."

## Why the level is the person and not the credential set

Read the catalogue and ask, entry by entry, whose fact each one is. `user.erasure_mode` is
what deleting an entry does to this person's data. `search.disabled_providers` is the list
of model providers this person's queries must never be sent to. `environments.idle_environment_hours`
is how long their downloaded files sit on a shared box. `common.timezone` is where they
live. These are answers about a person. None of them becomes a different answer because a
different set of Spotify credentials happens to be in use at the time.

The tempting design says otherwise, and it is tempting for a real reason. `spotify.default_market`
is the setting that made the case for this whole service, and somebody with a `work`
profile in one country and a `personal` profile in another can reasonably want the two to
search different catalogues. Make settings per profile, and they can have it.

Three things are wrong with that.

It multiplies every answer by the number of profiles. A person with three profiles is
asked forty-two questions three times, and for all but a handful of them the three answers
must agree to be correct. Keeping them in agreement is work that never ends, has no
feedback, and is handed to the person rather than the machine.

The failures are silent, which is the second thing. `search.disabled_providers` refuses
rather than falling back, and the entry says why: "'Never send my queries to provider X' is
meaningless if a brief outage turns X back on -- and nobody would find out, because the
query would succeed." A per-profile version has exactly that hole with no outage required.
The person bans a provider on `personal`, never thinks about `work` because `work` is for
Spotify, and one day a search runs under `work` and the ban is not there. The query
succeeds. Nobody finds out. Every argument the catalogue makes about silent failure applies
with more force to a design that creates a second copy of every restriction and does not
say where it is.

The third is about identity. A token does not carry a profile. `Identity.account_id` is
"the token's verified `sub`, and the only identity this service ever learns", and
`StoredSetting.set_by` is provenance "derived, never claimed". A per-profile design has
nowhere to get the profile from except the caller, so it would put a caller-supplied string
into the primary key of a person's own preferences -- the one place in this service where
nothing is taken on the caller's word.

## The setting that settles it: `common.default_profile`

The decisive case is the setting that is itself about profiles. `common.default_profile`
answers "which keyring profile should a service use when the request does not name one".

That question cannot be answered per profile without being circular. To read a per-profile
setting you must already know which profile you are reading for; this is the setting that
tells you. Any design that stores it per profile needs a profile-independent copy to break
the loop, and a profile-independent copy of a setting is an account-level setting reached
by a longer road.

It is not an exotic corner, either. It is the most-duplicated setting in the family:

> Replaces spotify-api's `keyring_default_profile` and web-search-api's
> `WSA_KEYRING_DEFAULT_PROFILE` (both 'personal'), and a third service's `default_profile`
> ('default'). keyring itself has no such notion.

Three services each carry their own copy of the question, with two different defaults, and
the service that owns profiles does not answer it at all. The `common` module docstring
puts it plainly: the question "has, today, three answers and no owner. This gives it one."

One. Not one per profile. A setting whose *value* names a profile is still a single
account-level value, and that is the shape of the whole catalogue.

## Four mechanisms, because a convention is not an invariant

| Where | What it makes impossible |
| --- | --- |
| The schema | The primary key is `(account_id, namespace, key)` with no profile column, so a per-profile value has nowhere to be stored. |
| The wire | No route accepts a profile as a path segment, a query parameter or a body field, and every request body sets `extra="forbid"`, so `{"profile": "work"}` is a 422. |
| The identity | The account comes only from a verified `sub`, and no endpoint accepts an account id at all. |
| The suite | A test asserts that two tokens minted for the same `sub` through different keyring profiles read and write the identical document. |

All four are needed, and the reason is that each closes a gap the others leave open.
Without the schema rule, a per-profile value is one migration away and a well-meaning patch
can add it. Without the wire rule, the schema's refusal surfaces as a 500, or worse as a
field quietly dropped. Without the identity rule the body hardly matters, because a caller
that can name an account can already read a document that is not its own. And without the
test, the other three describe today's code and nothing obliges them to describe
tomorrow's.

The wire rule is the one that carries the most weight, and `api/schemas/settings.py` says
why in the place somebody will read it:

> The one field a caller might plausibly try to send and must never be able to is
> `profile` [...] a body that carried `{"profile": "work"}` and had it silently ignored
> would be the single most dangerous kind of wrong -- the caller would believe it had
> written a per-profile setting, and every service would read the other one.

Picture the version of this service without `extra="forbid"`. An assistant is told "use PT
for my work Spotify". It sends `{"value": "PT", "profile": "work"}`, gets a 200 with a
bumped revision, and reports that the change is made. The person now believes their two
profiles search two catalogues. They search one. Every request from either profile resolves
against PT, including the personal searches they had deliberately set to `GB`, and there is
nothing in a log, a response or an export that would show them the field they sent went
nowhere. A 422 naming `profile` is an uglier
answer and a far better one: it is wrong immediately, at the caller, with the field named.

## What it costs

Somebody who genuinely wants a different Spotify market for work cannot have one. That is
not softened by anything below; it is the price. `spotify.default_market` is one value for
the account, and if the honest answer is `GB` for one credential set and `PT` for the
other, this service makes them pick one and be wrong about the other.

There is partial relief, and it is worth knowing because it covers the common case. The
setting is nullable, and null is the default: "Null means 'let Spotify decide from the
token', which is what spotify-api does today." A person who wants the market to follow
whichever account is in use can leave it unset and get exactly that. What they cannot have
is the mixture -- one market named explicitly here and a different one named explicitly
there.

The real escape hatch is two keyring accounts rather than two profiles in one. Two accounts
means two `sub`s, two settings documents, and a person who switches context by presenting a
different token. It is heavier, and it is honest: everything is duplicated where you can
see it, rather than most things being shared and a few silently not. Somebody whose work
and personal lives differ in forty-two ways rather than in one is being told something true
by the friction.

The half-measure is the thing to refuse hardest. The obvious compromise is a small
per-profile overlay -- most settings stay per account, and a named handful may be
overridden per profile. It looks cheap and it is not. The moment one setting can be
per-profile, every reader has to know which level each key lives at, `describe_settings` has
to report it, the export format has to carry it, every consuming service's client has to
handle both, and the 422 above becomes a per-key rule a caller cannot predict from the
outside. The reason this invariant is cheap is that it has no exceptions: a reader can be
certain without looking anything up. A per-key overlay would charge all forty-two settings
for the benefit of one.

## What would change our minds

A setting that is genuinely credential-shaped rather than person-shaped. The test to apply
is not whether the person *wants* two answers -- people want two answers to plenty of
things -- but whether the correct value depends on **which credential set is in use**
rather than on **who the person is**. A setting like "this account's API tier allows four
concurrent calls" is a fact about a credential. A setting like "never send my queries to
provider X" is a fact about a person, and would be a lie if a credential could change it.

None of the forty-two is credential-shaped, and the near misses are instructive.
`spotify.default_market` looks like one and is not: which country's catalogue somebody's
searches resolve against "is a fact about the person, deployed as if it were a fact about a
machine", which is the sentence this whole service exists to act on. `keyring.max_sessions`
and `keyring.session_ttl_days` are about the account, which is the level *above* a profile
and not below it. `common.default_profile` names a profile and, as argued above, is the
strongest evidence for the account level rather than against it.

So the arrival of the first genuinely credential-shaped setting is the signal, and it
should be treated as one rather than as an inconvenience. Even then the first move is not a
profile column here. keyring already keys things by account and profile -- its connections
live at `/v1/profiles/{name}/connections/{service}` -- so a value that belongs to a
credential probably belongs beside the credential, in the service that owns it, and this
service would be the wrong home for it at either level.

Two things would specifically **not** change our minds. A caller asking for the field is
not evidence; that is what the 422 is for, and the request it makes is exactly the request
this record refuses. And keyring beginning to mint tokens that name a profile would make
the per-profile design expressible without making it right -- it would only mean the fourth
mechanism, the test with two tokens and one `sub`, had become the one doing the real work.
