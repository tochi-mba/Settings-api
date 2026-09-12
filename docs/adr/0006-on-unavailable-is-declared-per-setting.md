# ADR-0006: `on_unavailable` is declared per setting, and has no default

**Status:** accepted.

## Context

settings-api sits on the request path of six services. It will be unreachable sometimes:
a restart, a network partition, a deploy that went wrong. Each consuming service then has
to decide what to do with a request it cannot resolve a setting for, and the honest answer
is that the right decision is different for different settings.

## Decision

Every catalogue entry declares `on_unavailable`, and the field has **no default in the
code**. The absence is the decision. An author adding a setting cannot copy the entry above
and move on; they have to answer one question:

> If settings-api is down and this service lands on the default, could that override a
> restriction the person explicitly asked for?

If no, `USE_DEFAULT`: the default is the most conservative value the setting has, and
landing on it cannot weaken anything. `user.erasure_mode = grace` destroys nothing;
`user.log_values = false` records nothing.

If yes, `REFUSE`: the default is permissive because the service needs it to be, and falling
back would override the person. The consuming service must fail the operation instead.

## The canonical case

`search.disabled_providers` defaults to `[]`, meaning "every provider is allowed". That is
the only value that lets web-search-api work out of the box, so it has to be the default.
Now suppose a person has written down "never send my queries to provider X", and
settings-api is briefly down. Falling back to `[]` sends their next query to exactly the
provider they refused -- silently, because the query **succeeds**. Nobody finds out.
Refusing the search is the lesser failure, and it is the only one that is not a broken
promise.

This one entry is the whole reason the field exists and has no default. A global policy of
"fall back" gets it wrong; a global policy of "refuse" makes every setting a hard
dependency and turns a brief outage into six outages.

## Two checks turn the claim into something the build verifies

Every entry must declare `on_unavailable`, and every `USE_DEFAULT` entry's default must be
in its own `conservative_values`. Both are checked when the catalogue is assembled at
import, and both are asserted again over the whole catalogue in the test suite.

The second check is the useful one over time. `user.erasure_mode` declares
`conservative_values=("grace", "tombstone")` -- both destroy nothing during an outage --
and not `"immediate"`. If somebody later changes the default to `"immediate"`, the build
fails until they either revisit the conservative set or change their mind. The declaration
is a fence around a future decision.

## The one entry where the conservative value is not the default

`keyring.session_ttl_days` is `USE_DEFAULT` with a fourteen-day default, and one day is the
safer value. A person who chose a one-day idle timeout gets fourteen days during an outage.

The alternative is `REFUSE`, and `REFUSE` on a session lifetime means nobody in the family
can log in while settings-api is down -- it would make this service a hard dependency of
every login. That is a far larger failure than a bounded, temporary lengthening of one
timeout. The reasoning is written into the catalogue entry's own description, and writing
it there **is** the mechanism: the field forces the author to decide, and the description
forces them to justify it where the next reader will look.

## How the client carries it out

The `resolve_settings` response carries a `fallbacks` block: per key, the deployment's
default and the rule. The client caches it **per namespace**, not per token, because
neither fact is about a person and sharing them leaks nothing. That is what lets a cold
client behave correctly during an outage for a person it has never seen, without vendoring
a copy of the catalogue that would drift -- silently, because the only time it is read is
during an outage when nobody is looking.

Degradation is in a fixed order: this token's cached values, served with `stale=True`;
then a document assembled from the fallbacks, with `REFUSE` keys placed in `refused`;
then `SettingsUnavailable`. A `REFUSE` key raises `SettingsRefused` **when it is read**,
not when the namespace is resolved, so an operation that never touches
`disabled_providers` is not failed for it.

## What it costs

An author has to think, and two settings a year will be declared wrong. The
`conservative_values` check catches the incoherent half -- a `USE_DEFAULT` entry whose
default is plainly not safe. The other half is review, and review is what a catalogue that
is a table rather than a database is for.

## What would make us change our minds

A third behaviour turning out to be needed. "Fall back, but log loudly" is the likely
candidate. That is an enum value on `OnUnavailable`, with its own arm in the client, and
not a change to the design.
