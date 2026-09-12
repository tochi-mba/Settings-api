# ADR-0007: Namespaces are scopes

**Status:** accepted.

## Context

Forty-two settings need a way to say who may read which. The general answer is a permission
model: roles, grants, an access-control table. This service has forty-two settings and six
readers, and a permission model would be larger than the thing it protected.

## Decision

A namespace is one service's compartment **and** the unit of authorisation. There is no
separate permission model: what a caller may read and write is exactly the set of
namespaces it was granted, and `common` is always among them.

There are two surfaces, and they decide the set two different ways.

**Person-facing.** The audience is `settings` or `settings.<namespace>`. The bare prefix
grants every namespace the deployment allows; `settings.search` grants exactly `search`
plus `common`. This is user-api's scopes-from-the-audience shape applied to a different
question, and the compartment it creates is concrete: an assistant that may set search
preferences and may not touch erasure policy, because the person minted it a token that
says so.

**Service-facing.** A configured `ServiceGrant` names the namespaces. media-tool is granted
`media`, so media-tool cannot read `user.erasure_mode`. The blast radius of one compromised
service token is exactly that service's namespace list.

## The check the service-facing surface rests on

The user token a service presents must have an audience in **that service's own family**:
`aud == prefix` or `aud` beginning `prefix.`. It is `ServiceGrant.accepts_audience`, one
line, and everything on `/v1/internal` depends on it.

Without it, a service token -- a static string in a deployment's configuration, not
something a person mints -- plus any user token would read any account. Anything able to
reach this service with spotify-api's token could pair it with a token minted for
media-tool and read that person's settings. With it, media-tool may present only tokens
minted for media-tool.

The separator is required. `media-toolkit` begins with `media-tool`, and plain
`startswith` would accept it; requiring `media-tool.` is what stops one service's prefix
from being a prefix of another's name.

And two services sharing a token is a startup error, because the constant-time comparison
returns whichever name matched, and whichever matched would decide which audience family
is acceptable -- so the weaker of the two grants would be reachable with the other's token.

## Why `common` is granted to everyone without being listed

`common.timezone` and `common.default_profile` are answers every service needs and none
owns. A deployment that had to remember to add `common` to six grants would eventually
forget it in one, and that service would silently format times in UTC for a person in
Lisbon.

It is merged **underneath** a namespace, with the namespace winning on a key collision, so
that adding a key to `common` can never silently change what an existing namespace resolves
to. That rule is exercised rather than theoretical: `common.job_retention_hours` is
overridden by both `media.job_retention_hours` and `spotify.job_retention_hours`, because
the owning services' ceilings differ.

## The error mapping, and why it is the opposite of user-api's

An unknown namespace is **404** and an ungranted one is **403**. Two different answers,
and both are safe to give.

In user-api a 403 would confirm that somebody else's entry exists, so everything is 404.
Here there is nothing to confirm: the catalogue is identical in every deployment of a
build and is published in full by `describe_settings` to anybody holding a token, so "there
is no such namespace" reveals nothing. Conflating the two would send a caller with a typo
looking for a permissions problem it does not have. A caller told the wrong one goes
looking for the wrong problem.

## What it costs

Granularity below a namespace is not expressible. media-tool gets all six `media` settings
or none of them. Per-setting grants were considered and rejected: they are configuration
nobody would keep correct, and the namespace boundary already matches the service
boundary, which is the boundary a compromised token actually has.

A service that needs two namespaces must be granted two. That is honest rather than
costly: the grant then says what the service can see.

## What would make us change our minds

A service needing exactly one setting from another service's namespace. The answer then is
to move that setting to `common`, where every service may read it -- not to invent
per-setting grants. If a setting is needed by two services it was never really one
service's compartment.
