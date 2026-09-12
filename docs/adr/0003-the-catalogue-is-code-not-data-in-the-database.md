# ADR-0003: The catalogue is code, not data in the database

**Status:** accepted.

## Context

Every setting this service knows about -- its type, default, bounds, summary, description,
what a consuming service does when this service is unreachable, and whether the owning
service has the knob yet -- has to be written down somewhere. There are forty-two of them
across seven namespaces.

The obvious home is a table. A `setting_definitions` table, one row per setting, editable
at runtime through an administrative endpoint or a migration, with the service reading it
on startup or on every request. It is the design most settings services have, and it is the
design this one deliberately does not.

## Decision

The catalogue is Python. Each namespace is a module under
`src/settings_api/domain/catalogue/`, each module is a tuple of frozen `SettingDef`
dataclasses, and `catalogue/__init__.py` assembles them into one mapping and checks every
entry **at import**. A malformed entry -- a default its own bounds reject, an enum with no
choices, a `USE_DEFAULT` entry that never declared what it is safe to fall back to -- is a
process that does not start, rather than a 500 the first time somebody reads that
namespace.

An import-linter contract, "The catalogue is data", restricts every module under
`domain/catalogue/` to importing `domain/types` and nothing else. Not the errors, not the
value helpers, not a store, and not `core`.

## Why not a table

A definition in a table has no code review, no diff, no type check and no test. Changing a
default in a table silently changes behaviour for every account that never expressed a
preference, with nothing in git to say who did it or why. And the bounds a value is checked
against become data that a bug, a migration or an operator with a database client can
change -- which is to say, they stop being bounds.

The argument for a table is that adding a setting should not need a deploy. That argument
is the reason to refuse it. Adding a setting is a change to **what the system promises a
person** -- what it will and will not do with their data on their behalf -- and a change of
that kind should go through the same review as any other change to the service's
behaviour. A deploy is exactly the right cost.

## Why the contract, and what it makes awkward

The catalogue is a *table* in the reviewable sense: forty-two rows a person can read in one
sitting and check against what the services actually do. That property is fragile. A
catalogue module that could import the store is a catalogue entry that could have
behaviour, and the moment one of them does, the table stops being reviewable and becomes
code somebody has to execute in their head.

So the contract is strict, and two consequences follow that look odd until the reason is
known:

* `SettingDef.validate` raises a plain `ValueError` rather than a domain error. Importing
  `domain.errors` from `domain.types` would put the error module one indirect hop from
  every catalogue module, and the contract counts indirect imports. The translation into
  this package's vocabulary happens once, in `domain/values.py`.
* The lookups that raise domain errors -- `definition_for`, `entries_in` -- live next door
  in `domain/registry.py` rather than in the catalogue package. Splitting the table from
  the lookups is what lets the table stay a table.

Both are worth a sentence in the modules concerned, and both have one, so that the next
person who moves `UnknownSettingError` into the catalogue package finds out from the
contract and then from the docstring why it was not there.

## What it costs

**Adding a setting needs a deploy rather than an `INSERT`.** Stated above as the right
trade, and worth stating again as a cost: a deployment that wants a setting the catalogue
does not have waits for a release.

**A deployment cannot add a setting of its own.** The answer to that is operator policy
(see ADR-0006's neighbour, `domain/policy.py`), which can narrow a bound, change a default
within the narrowed bounds, and pin a value -- statements about the deployment rather
than about the catalogue -- and cannot invent an entry. A deployment that genuinely needs a
setting nobody else does is a deployment whose need should be visible in the catalogue's
git history.

**The catalogue is duplicated into six services' behaviour.** A setting here that the
owning service does not read yet does nothing, and the `origin` field exists so that
`docs/catalogue.md` says so per entry rather than letting somebody ship a setting that
silently does nothing.

## What would make us change our minds

A genuine per-tenant extension point: deployments that need settings the shared catalogue
does not have, often enough that waiting for a release is the wrong answer. The fix then
would be a **second, namespaced, deployment-owned catalogue** loaded beside this one, with
the same shape and the same checks -- not making this one editable. The invariant worth
keeping is that every setting a person can be offered went through review somewhere; where
the review happens is negotiable, whether it happens is not.
