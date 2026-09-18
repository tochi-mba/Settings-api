# ADR-0001: Settings are their own service

**Status:** accepted.

## Context

`spotify-api/src/spotify_api/config.py` declares this, in the block it labels
`# --- Behaviour ---`:

```python
default_market: str | None = Field(
    default=None,
    description="ISO 3166-1 alpha-2 market applied when a request omits one.",
)
```

That field decides which country's catalogue a person's track searches resolve against.
It decides which recordings exist, which are playable, and which of several regional
releases a search comes back with, so the same query genuinely returns different tracks
depending on it. It is a fact about a person, and it is deployed as an environment
variable that applies to everybody on the box.

On a box serving one person that is invisible; the operator and the person are the same
human being and the environment variable is simply where they wrote their answer down. On
a box serving two people in different countries it is wrong for one of them, and the only
fix available is a second deployment of spotify-api with a different value. Nobody would
choose that, which means in practice nobody fixes it: one of the two people quietly gets
the wrong catalogue and has no way to say so.

It is not one field. There are forty-one more in the same shape, and `AGENTS.md` states
the case in one sentence: "The service exists because roughly three dozen knobs across six
services are not really deployment decisions." How long a download sits on a shared box,
which model answers a question, what `DELETE` means for somebody's notes, how long a
session survives being idle, which providers must never see a query. Each of them is
currently an operator's variable answering a person's question.

The second half of the problem is that where these answers do exist per account, they
disagree. "Which profile do you mean when I do not say" is answered three times, in three
places, with two different answers:

| Service | Where | Answer |
| --- | --- | --- |
| spotify-api | `keyring_default_profile` | `"personal"` |
| web-search-api | `WSA_KEYRING_DEFAULT_PROFILE` | `"personal"` |
| a third service | `default_profile` | `"default"` |
| keyring-api | nowhere | the profile is a required path segment |

`domain/catalogue/common.py` puts it plainly: "the question 'which profile do you mean
when I don't say' has, today, three answers and no owner. This gives it one." The
consequence of no owner is not just the disagreement. It is that there is nowhere to look
to answer "what has this system been told about how to treat me", and nowhere to go to
change it once.

user-api is the one service that already holds preferences per account, in a table of
three columns:

```sql
CREATE TABLE user_settings (
    account_id   TEXT    NOT NULL PRIMARY KEY REFERENCES users (account_id) ON DELETE CASCADE,
    erasure_mode TEXT    NOT NULL DEFAULT 'grace',
    grace_days   INTEGER NOT NULL DEFAULT 30,
    log_values   INTEGER NOT NULL DEFAULT 0,
    ...
```

It put a port in front of that table on day one for this decision's sake, and said so:
"a separate settings-api is the next service in this family, and it becomes a second
adapter" (user-api ADR-0007). This ADR is the other end of that bet.

## Decision

Settings are their own HTTP service. One deployment holds, for each account, that
account's choices about how every other service in the family behaves for them, and the
other services read those choices over `/v1/internal` rather than each keeping their own.

It has no accounts of its own. keyring remains the auth root; the only identity this
service ever learns is the `sub` of a token keyring signed, verified locally against
keyring's published JWKS document. `core/config.py` is explicit about what that does not
require: "no change to keyring, no token exchange, and no new keyring endpoint". Nothing
here calls keyring at request time, and nothing here can ask keyring anything about a
person.

The catalogue is a table rather than code. Forty-two entries across seven namespaces, one
module per namespace, restricted by an import contract to `domain/types` alone, so adding
a setting is one `SettingDef` and a documentation regeneration -- no migration, no schema
change, and no deploy of six services. That cheapness is the whole point: it is what makes
"this knob is really a person's" a change somebody will actually make rather than a
change they will put off.

## Why not a table in each service

This is not a hypothetical alternative. It is what exists today, and the shape of what it
costs is already visible in the `default_profile` table above. Three services each hold
the answer, two of them agree by coincidence rather than by contract, and nothing
anywhere would report the disagreement.

The deeper problem is not the disagreement but the absence of a place. A person who wants
to know what this system has been told about them has six services to ask, six different
spellings to know about, and no route that answers the question as a whole. A person who
wants to change one answer -- "keep nothing when I delete" -- has to know which services
have an opinion about it and change each. The per-service table makes the easy case easy
and the actual case, which is a person reasoning about their own posture across a family
of services, impossible.

A catalogue in one place also buys a property the per-service version cannot have: the
`common` namespace, merged underneath every other namespace, so a question six services
share is answered once. "What time zone are you in" is the clearest case. Six tables mean
six answers, and one of them will be wrong.

## Why not a shared library

A library is the version of this that looks like it avoids the operational cost, and it
does not, because a library does not hold state. Every service would still need its own
table, its own migration and its own row per account. What the library would standardise
is the *shape* of the disagreement: six services storing the same three columns under the
same names, and still storing six different values for one person with no mechanism that
could ever reconcile them.

It would also make the catalogue worse rather than better. A setting added to the library
does nothing until every consumer upgrades and redeploys, so the moment a setting exists
and the moment it takes effect are six separate events spread over however long the
slowest service takes to release. Here, adding a setting is one commit in one repository
and the next `resolve` call sees it.

The one thing a library genuinely does buy -- a client that gets caching, revalidation,
single-flight and outage behaviour right once -- we take. That is the client under
`clients/python/`, and it is a library *in front of* the service, not instead of it.

## Why not a column family on keyring's accounts

keyring already holds the accounts, so hanging settings off them is the change that looks
smallest. It is the wrong place for three reasons.

keyring is the auth root, and it is built around holding secrets. Its secret store's
module docstring describes itself as "the only layer that sees plaintext credential
material at rest", and the encryption exists because what it holds justifies it. Settings
are the opposite kind of data: plaintext, mostly not security-relevant, and read on every
request by six services. Putting them in the same store means either encrypting data that
does not need it, on a read path that cannot afford it, or maintaining two storage
disciplines in one service and relying on everybody who touches it to know which is which.

Second, availability. Every login in this family depends on keyring. Making every settings
read a read against keyring points six services' steady-state request volume at the one
service whose outage is already total. Settings reads are frequent and cheap; logins are
infrequent and precious. They should not share a failure domain in that direction.

Third, credentials. Six services would need a reason to hold a keyring credential capable
of reading account rows, and today most of them do not have one. The blast radius of a
compromised service token should be the namespaces that service was granted, which is what
`ServiceConfig.namespaces` is for. Making it "can talk to keyring" instead is a strictly
larger one for no gain.

## What it costs

One more service to run. A sixth process, a sixth database file, a sixth thing to deploy,
back up and watch. For a family that already runs five that is a real increment and not a
rounding error.

One more hop on other services' request paths. A call that previously read a frozen
integer out of a process-wide settings object now reads a resolved namespace over HTTP.
The client keeps this off the hot path most of the time -- a cache keyed by the user's
token, revalidated with `If-None-Match`, and `cache_ttl_seconds` says what that trades:
"Revalidation is cheap -- an ETag and a 304 -- so this trades a minute of staleness for
most of the request volume." It is still a hop, and it is still a place a request can fail
that it could not fail before.

And settings-api becomes a dependency of six services. Three things keep that from being
the single point of failure it sounds like. Every catalogue entry declares
`on_unavailable`, and the field deliberately has no default -- `domain/types.py` says
"There is no default for the field that carries this, and that is the point: an author
adding a setting has to decide which of these two it is, because getting it wrong is
silent and the failure only shows up during an outage." Clients cache, and a cached
document for the same token is served during an outage in preference to any default,
because a person's own stale values beat a deployment's guess. And nothing fetches at
startup: `docs/integration.md` is direct about it -- "Do not fetch settings during
startup, and do not fail to start when settings-api is unreachable -- that turns one
outage into two, at the moment the two services are being restarted together."

The sharpest cost is worth stating without softening it. keyring reading
`keyring.session_ttl_days` from here puts settings-api on the critical path of every
login. That is why that entry is `USE_DEFAULT` rather than `REFUSE`, and it is the one
place in the catalogue where the conservative value is not the default: a person who chose
a one-day idle timeout gets fourteen days while this service is down. The entry's own
description carries the reasoning, so whoever changes it next reads it first: refusing
"would make this service a hard dependency of every login, which is a far larger failure
than a bounded, temporary lengthening of one timeout." We took the lengthened timeout. It
is a real downgrade of one person's expressed choice, accepted deliberately, and it is the
price of settings-api existing at all.

## What would change our minds

If the catalogue stopped growing. The argument above is an argument about forty-two
entries across seven namespaces and the fact that adding the forty-third is cheap. At ten
entries, stable, the per-service table is cheaper by every measure: no extra process, no
extra hop, no dependency to reason about during an outage, and the disagreements are few
enough to reconcile by hand. If a year from now the catalogue is still forty-two entries
and nobody has wanted a forty-third, this service is carrying operational cost for a
problem that stopped growing, and folding each namespace back into its owning service
would be the honest response.

If a deployment only ever serves one person. Most of the argument here is about two people
on one box wanting different answers. Where that cannot happen, the environment variable
is not wrong -- it is a perfectly good place for one person to write their answer down,
and `default_market` in `spotify-api`'s config is exactly as correct as it needs to be.
A single-tenant deployment should be allowed to not run this service, which is why nothing
in the family fails to start without it and why every integration ships dark behind an
empty base URL.

What would *not* change our minds is the count of services being small today. Six is
already enough for three spellings of one question, and the cost of consolidating grows
with every service that ships its own table in the meantime.
