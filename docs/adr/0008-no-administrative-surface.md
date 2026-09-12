# ADR-0008: No administrative surface

**Status:** accepted.

## Context

keyring has an administrative surface, behind an admin token: issuing invites, disabling
accounts. It needs one. Those are actions about an account's *existence*, and only an
operator can take them.

The question for this service is whether it needs one too -- an endpoint to read a
person's settings for support, or to set one for somebody who cannot, or to list who has
changed what.

## Decision

There is no route by which an operator can read or change anybody's settings over HTTP.
Not behind an admin token, not behind an allow-listed address, not read-only. Sixteen
operations, and none of them is administrative.

`core/config.py` keeps a list of the settings this service refused to add, and this is on
it: *no administrative HTTP surface at all. No operator can read somebody's settings over
HTTP, because no route exists that would let them.*

## Why the asymmetry with keyring is right

Everything in this service is a decision a person made about their own data. There is no
operational task that requires reading those decisions on the person's behalf. Support --
"what did you set it to?" -- is answerable by asking the person, who can read their own
settings and their own change log with a token they hold. "Set it for them" is answerable
the same way: they set it. A person who cannot mint a token has a keyring problem, and
keyring has the administrative surface for that.

## What an operator can do, and why it is a different shape

Operator policy, read from a file at startup. It can **narrow** a bound, change the default
within the narrowed bounds, and **pin** a value. Every one of those is a statement about
the deployment -- this box has a small disk, this deployment serves a small context window
-- and none of them is about a person. The policy file cannot name an account; there is
nowhere in its grammar to put one.

A pin is also visible. `describe_settings` reports `pinned: true` and a write to a pinned
setting is a 409 that says so -- never a 200 that did nothing. The operator's decision is
something the person can see and be told about, not something they discover by being
refused with no explanation.

## The file

An operator with shell access on the box can read the SQLite file, and this record should
say so plainly rather than pretend otherwise. The database is plaintext; the file mode is
the only access control there is.

What the absent surface buys is that reading somebody's settings requires *that* access --
being on the box, as the service's user -- and leaves the evidence of having taken it,
instead of being a request anybody holding a token could make from anywhere. And a
compromised admin token is not a route to every person's settings, because there is no
admin token.

## The mechanisms that make the absence structural

Two things stop an administrative route from being added by accident. No route accepts an
account id: both identity dependencies take the account from a verified token's `sub`, so
even a new route could not name a subject without a deliberate change to
`api/dependencies.py`. And the contract test pins the exact set of sixteen operation ids,
so a seventeenth is a failing build until somebody updates the test and explains the
change.

## What it costs

An operator debugging "why did this person's search go to the wrong provider" cannot look.
They ask the person to read `describe_settings`, or they take shell access and read the
file. Both are slower than a query, and both are deliberate. The change log the person can
read -- what changed, when, and through which service -- is what makes that conversation
possible without an operator ever seeing a value.

## What would make us change our minds

A genuine operational need that cannot be met by asking the person. A migration that has
to rewrite everybody's settings is the plausible one. That would be a script with file
access, run on the box, leaving a record -- not an HTTP route. The invariant is that
nothing reachable over the network can read a person's decisions except that person and
the services acting for them with their token.
