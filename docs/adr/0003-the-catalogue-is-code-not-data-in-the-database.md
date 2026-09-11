# ADR-0003: The catalogue is code, not data in the database

**Status:** accepted.

## Context

Every setting this build knows about is declared as a frozen dataclass in a Python module
under `src/settings_api/domain/catalogue/`. `media.artifact_retention_hours` is one
`SettingDef(...)` literal in `catalogue/media.py`, and it carries everything anybody needs
to know about that setting: that it is a whole number between 0 and 720, that it defaults
to 1, that an operator may narrow it, that a consuming service should fall back to the
default when this service is unreachable, that 1 is a value it is safe to fall back to,
that it replaces media-tool's existing `artifact_ttl_seconds`, and two paragraphs of prose
saying what choosing each value actually means for the person. There are forty-two entries
like it across seven namespace modules. `catalogue/__init__.py` puts them together, checks
each one, and hands the result to the rest of the service as three read-only structures.

The alternative was there from the first sketch and it is not a silly one. A
`settings_definitions` table: one row per setting, columns for type, default, minimum,
maximum, choices, summary and description, seeded by a migration and editable afterwards.
A settings service is a service that already owns a database, and keeping the definitions
beside the values has an obvious symmetry to it. It also buys something concrete. Adding a
setting becomes an `INSERT`. Changing a default becomes an `UPDATE` an operator can run at
three in the morning without waiting for a release. Correcting a bound that turned out to
be wrong takes a minute rather than a deploy of a service six other services depend on.

This record exists because that trade looks better than it is, and because somebody will
propose it again.

## Decision

The catalogue is code. One module per namespace, each exporting a `NAMESPACE` string and a
`SETTINGS` tuple of `SettingDef`, and `catalogue/__init__.py` assembling them at import
into `CATALOGUE`, `NAMESPACES` and `BY_QUALIFIED`. Nothing at runtime can add an entry,
remove one, or change a field on one: `SettingDef` is `frozen=True, slots=True`, and its
docstring says why in one sentence.

> Frozen and slotted because the catalogue is assembled once at import and read for the
> life of the process. A mutable entry would be a setting whose bounds a request could
> change.

The database holds values and nothing else. A row in `settings` says that one account
chose one value for one `(namespace, key)`; what that key is, what it may be worth and
what it means are not in the database at all. Changing the catalogue is a commit.

## Why a table is the wrong home for a definition

A definition in a table is a definition with no code review, no diff, no type checking and
no test. Those four are not ceremony we would be shedding; they are the entire mechanism by
which anybody knows what this service promises. Move `media.artifact_retention_hours` into
a row and the maximum of 720 stops being a line somebody approved and becomes a number in a
column, reachable by a migration, by an operator with `sqlite3` and a bad afternoon, and by
any bug in this service that can write where it did not mean to. The value a person sends
is checked against bounds that are themselves data. That is a validator you can edit.

The sharper version is the default. Storage here is sparse by design, and ADR-0005 is about
why: a row exists only where somebody expressed a preference, so changing a catalogue
default moves everybody who never chose and nobody who did. That property is worth having
and it is also a loaded weapon. In the code catalogue, firing it requires a commit with a
diff, an author, a reviewer and a message saying why, and the blast radius is legible in
the pull request. In a table, one `UPDATE settings_definitions SET default_value = ...`
silently changes behaviour for every account that never expressed a preference, with
nothing in git to say who did it, when, or what they thought they were fixing. Somebody
noticing six weeks later that their downloads now vanish after an hour has no artefact to
read. There is no diff, because nothing was committed.

The prose is the same argument in a quieter register. The `summary` and `description` are
read by a model deciding whether to touch a setting, which means they are part of this
service's public behaviour rather than documentation about it. `_entry_prose` in
`domain/types.py` refuses an entry whose description merely restates its summary, because
"a description that merely restates the summary is what an author writes when they have not
decided what the setting means". Wire that field to an `UPDATE` and the check still runs on
the write, but nobody reads the sentence before it ships.

## The positive case: forty-two rows somebody can actually check

Rejecting the table is only half of it. The reason the code catalogue is good rather than
merely safer is that it is itself a table in the sense that matters: forty-two rows a
person can read in one sitting and check against what the six consuming services actually
do. That is the property being protected, and `domain/types.py` opens by saying so.

> A catalogue entry is a row in a table, and a table is something a person can read in one
> sitting and check. A catalogue module that could import the store is a catalogue entry
> that could have behaviour, and the moment one of them does, thirty settings stop being
> reviewable and become code.

So the property is enforced rather than hoped for. The import-linter contract named "The
catalogue is data" restricts every module under `domain/catalogue/` to
`domain.types` alone. Not the errors, not the value helpers, not the registry, not the
policy code, and certainly not a store. The contract sets `allow_indirect_imports` nowhere
by accident: it lists each forbidden module explicitly, and `make imports` runs it on every
commit.

A reviewer reading `catalogue/spotify.py` is therefore reading declarations and nothing
else. There is no function to trace, no conditional default, no entry that consults
anything. Whatever the module says is what it is, which is what makes checking forty-two of
them against six services a task somebody can finish. The moment one entry could import a
store, that guarantee is gone for all forty-two: every reader of every entry now has to
check whether *this* one does something. The value of the restriction is that it holds
everywhere, which is why it is a contract and not a convention.

The ruff configuration makes the same admission from the other side. `E501` is switched off
for `domain/catalogue/*`, with the reason written next to it: "The catalogue is a table.
Thirty entries with a paragraph each is a long module by construction, and splitting one
namespace across two files to satisfy a line count would make it harder to read rather than
easier."

## Two consequences that look odd unless you know the contract

Both are cheap, and both would look like mistakes to somebody reading the code cold.

The first is that `SettingDef.validate` raises a plain `ValueError` rather than one of this
service's domain errors. A validator in the domain that does not speak the domain's error
vocabulary is strange on its face. The reason is the contract: `domain/errors.py` is on the
forbidden list, and the contract counts indirect imports, so `types.py` importing the
errors would put every catalogue module one hop from them. `domain/types.py` states it
where a reader will hit it.

> Importing `settings_api.domain.errors` from here would put it one indirect hop from every
> catalogue module, and the contract counts indirect imports. The wrapper that turns a
> `ValueError` into the refusal a caller sees is `settings_api.domain.values`.

The second is `domain/registry.py`, which exists next door to the catalogue package and
does nothing the catalogue package could not have done, for the same reason.

> Thin by design. Everything here could have lived in `settings_api.domain.catalogue`, and
> does not for one reason: an import-linter contract keeps that package restricted to
> `settings_api.domain.types`, so a lookup that raises `UnknownSettingError` cannot live
> there. Splitting the table from the lookups is what lets the table stay a table.

So `entries_in`, `live_entries_in` and `definition_for` sit one module over, re-exporting
`CATALOGUE`, `NAMESPACES`, `BY_QUALIFIED` and `COMMON` so that callers have one place to
import from. One extra module and one extra `__all__` is the whole price of the contract.

## Why the check runs at import

Because the catalogue is code, it can be checked before anything uses it, and it is:
`_assemble()` runs `entry.check()` on every entry while building the dictionary, and
`_entry_default` inside that runs `validate` on the entry's own default. A duplicate key, a
namespace declared in the wrong module, an enum with no choices, a default its own bounds
reject, a `USE_DEFAULT` entry that never said what it is safe to land on: each is a
`ValueError` at import.

> A badly shaped entry [...] is a mistake made while editing a table, and the useful moment
> to hear about it is the moment the process starts, not the first time somebody reads that
> namespace. So every entry is checked here, and a process holding a broken catalogue does
> not start.

That is the reward for the decision, and a table cannot pay it. A malformed row is a 500
the first time somebody reads that namespace, on a deployment that started cleanly, passed
its health check, and served every other namespace fine. Here it is a process that does not
start, which a deployment pipeline notices and a person does not.

## What it costs

Adding a setting needs a deploy. That is the bill, and it is not small: a change that could
have been an `INSERT` is a commit, a review, `make catalogue`, a regenerated
`docs/catalogue.md` in the same commit, and a release of this service before anybody can
set the new value.

We think that is the right trade, and the reason is what adding a setting actually is.
It is not an operational adjustment. It is a change to what this system promises a person:
a new question we are asking them to answer, a new field a model will see in
`describe_settings`, and usually a change at a call site in another service before the
answer means anything. `origin` and `origin_note` exist precisely because a setting can
look like it works and do nothing, and `catalogue/media.py` says what that costs: "a
setting that looks like it works and does not is worse than an absent one". A promise to a
person should go through review. The deploy is the review having somewhere to happen.

The second cost is narrower and worth naming plainly: a deployment cannot add a setting of
its own. An operator who needs a knob this build does not have cannot have it, however
local and however reasonable their need. What they get instead is operator policy in
`domain/policy.py`, which is deliberately one-directional.

> A policy may make a bound **tighter**, change the deployment's default within the bounds
> that result, and **pin** a value outright. It may not loosen a bound, add an enum choice,
> or raise a cap.

A policy that names a key the catalogue does not have is a startup error saying so, in
`_definition`: "policy names {namespace}.{key}, which is not a setting in this build". So
policy can narrow and pin, and cannot invent. That is the whole answer available to an
operator today, and for a deployment whose need is genuinely local it is not an answer at
all.

The third cost is the smallest and shows up most often: a typo in a `description` is a
deploy. We have decided to live with it rather than open a runtime write path into the
catalogue for the sake of prose, because a write path that can fix a typo is a write path
that can change a bound.

## What would make us change our minds

A deployment that genuinely needs settings this catalogue does not have. Not an operator
who wants a different maximum, which policy already covers, but a real extension point: a
deployment running a service this family does not ship, or a tenant whose own tooling needs
per-account choices of its own. Today that deployment has no route at all, and "write your
settings into somebody else's namespace" is not one.

If that arrives, the answer is still not to make this catalogue editable. It is a second
catalogue: namespaced away from the built-in names, owned by the deployment, loaded from
configuration at startup, and checked by the same `SettingDef.check` rules before the
process comes up. The built-in forty-two stay code, reviewable and diffable, and the
deployment's entries are visibly the deployment's. The properties we would be giving up
inside that second catalogue are exactly the ones argued for above, and having them in a
separate, clearly-labelled place is how a reader can tell which entries were reviewed and
which were configured.

The other thing that would move us is discovering that the deploy cost has stopped being
paid: settings that should exist and do not, because somebody judged the ceremony not worth
it. That failure mode is invisible from inside the repository, and the place it would show
up is a consuming service growing a second environment variable for a person's question.
If that starts happening, the argument above is wrong about what review is worth, and the
answer is to make adding a setting cheaper rather than to make the catalogue mutable.
