# ADR-0005: Sparse storage, so defaults can move

**Status:** accepted.

## Context

A settings table can be dense or sparse. Dense means every account has a row for every
setting, filled in with the default at the moment the row is created. Sparse means a row
exists only where somebody expressed a preference, and everything else is resolved from
the catalogue at read time.

Dense is the obvious design. It makes "what is this account's grace window" one `SELECT`,
and it is what user-api shipped. This service is sparse, and the reason is a defect
user-api actually had to fix.

## The defect

user-api's settings update was a `COALESCE` upsert: change the fields the caller named,
keep the rest. On an account with no row yet, the upsert *created* the row, and the fields
the caller had not named were filled in from the **domain's** defaults rather than the
**deployment's**. An account on a deployment configured for a seven-day grace window whose
first ever settings change was `log_values` silently acquired a thirty-day one. Nobody
would have noticed until somebody's data outlived its window by three weeks.

The fix in user-api was to thread the deployment default through every write. The fix here
is to make the mistake unrepresentable.

## Decision

A row in `settings` exists only where somebody expressed a preference. "Unset" is
distinguishable from "set to a value that happens to equal the default", so changing a
catalogue default -- or a deployment narrowing one in policy -- moves everyone who never
chose and nobody who did.

Resolution is three layers, in `domain/resolution.py`: the catalogue default, then the
deployment's policy, then the account's row, with a pin beating the row. Nothing about
that arithmetic reads a database; the stored values arrive as a mapping and the function
returns values, which is what makes the eight combinations testable as a table.

## The consequence that is easy to get wrong

`set` in every response reports whether a **row exists**, not whether the value differs
from the default. A person who deliberately set `grace_days` to 30 on a deployment whose
default is 30 has expressed a preference, and it survives a change of default. That is what
they asked for.

The alternative -- comparing the stored value to the default and reporting `set: false`
when they match -- would silently discard the person's decision the moment the default
moved, and would make the row's existence depend on a fact about the deployment rather
than a fact about the person. The store deliberately does not know what the default is.

## Export is sparse for the same reason

`export_settings` returns only what was chosen. An export that filled in every default
would freeze this deployment's defaults into whatever deployment it was imported to, so
restoring a backup after a default changed would silently pin the old value -- for every
setting, with no way to tell which ones the person had meant. The export shape and the
storage shape are the same decision made twice.

## What it costs

**A read is a resolution rather than a lookup.** Forty-two entries merged with a policy
and an account's rows, in memory, on every request. There are no joins and the mapping
fits in a page; the cost is arithmetic, not I/O. The storage saving is incidental and is
not the point.

**"What is this account's effective grace_days" is not one `SELECT`.** An operator writing
a query against the database gets the rows that exist and has to know the catalogue to
finish the answer. That is a real cost for operational tooling -- and it is also the
argument of ADR-0008: the answer to "what has this person's grace window resolved to" is
`describe_settings`, asked by the person, not a query run by the operator.

## What would make us change our minds

Nothing about scale. This is a semantic choice and the dense version is wrong at any size
-- it would be wrong with one account. The only thing that would change the decision is
the defaults becoming immutable, and defaults that cannot move would defeat the purpose of
having a catalogue at all.
