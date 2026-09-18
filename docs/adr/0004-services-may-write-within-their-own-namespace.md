# ADR-0004: Services may write within their own namespace

**Status:** accepted.

## Context

Reads on `/v1/internal` are scoped to the calling service's granted namespaces. That part
is uncontroversial: a downstream service needs its own settings and has no business seeing
`user.erasure_mode`.

Writes are the subtler question. The safest answer is that a service may not write at all
-- every change comes from the person, through `/v1/settings`, holding a token minted for
settings itself. It is clean and it is wrong, for a concrete reason.

user-api already has `PUT /v1/user/settings`. Its `operation_id` is public API, and MCP
clients have tools bound to it. A person changing their erasure mode inside user-api's own
interface is a thing that works today, and breaking it on the day this service is turned
on is not on the table.

## Decision

A service **may** write, through `set_setting_for_user`, but only within its own granted
namespaces and only while holding that person's own token. It is the person's decision
travelling through a service, not the service's decision.

Two properties are untouched by this. A consuming service still cannot write `user.*`,
because the namespace grant applies to writes exactly as it applies to reads. And it
cannot
write anything at all for somebody whose token it does not hold, because the account comes
from the user token's `sub` and from nowhere else.

For the cases where "within its own namespace" is still too much, a catalogue entry may set
`owner_writable_only = True`: writable only with a settings-audience token, direct from the
person. A service holding that person's token is refused with a 403.

## When to use `owner_writable_only`

The test is one question: **is the service that would benefit from changing this the same
service that should not be allowed to?**

Two entries pass it, and they are the only two.

`keyring.require_reauth_for_credential_changes`. The service most likely to want it off is
keyring itself, at the moment it is about to write a credential and would rather not ask
for a password again. A service that could turn off the check standing between it and the
credentials it is about to ask for has no check.

`search.store_query_history`. The service that would benefit from keeping a search history
is the one that would be doing the keeping. Turning it on has to be the person's own act,
with a token they minted for settings, so that no code path inside web-search-api could
ever be the thing that decided to start recording what somebody searched for.

## Why `user.erasure_mode` is not owner-writable, out loud

It is the most consequential setting in the catalogue and it is not the most protected one,
and the next reader will wonder why. The answer is the context section: marking it
`owner_writable_only` would break user-api's existing route on the day this service is
turned on, because that route writes through `/v1/internal` holding the person's token.

The protection that matters is unaffected. user-api can change `user.erasure_mode` only for
somebody whose token it is holding, which is to say only when that person is using
user-api, which is the case in which they could have changed it through user-api anyway. A
service cannot reach in and change what deletion means for a person it is not currently
acting for. That is the property; the write path is not.

## What it costs

A compromised service can change the settings in its own namespace, for any person whose
token passes through it, for as long as that token is valid. Bounded honestly: one
namespace, one person at a time, one token lifetime, and never the two owner-only entries.
It is a real cost.

The alternative costs more. No service writes at all means breaking a shipped public API,
and it means every settings change goes through a separate interface the person may not
have open -- somebody adjusting their playlist market inside spotify-api's own UI is told
to go somewhere else and do it there. Settings that are inconvenient to change are settings
that stay wrong.

## What would make us change our minds

A service turning out to write settings its users did not ask for -- an assistant "tidying
up" through a service's write path, say. The fix would be per-setting rather than global:
more `owner_writable_only` entries, chosen by the question above. The mechanism is already
per-setting so that the answer to that day is a catalogue change and not a redesign.
