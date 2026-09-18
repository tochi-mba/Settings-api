# AGENTS.md

Working notes for anyone -- human or agent -- changing this codebase. Read this before your
first edit. It is the single source of truth for how work is done here; `CLAUDE.md` just
points at it.

## What this service is

**settings-api** is one HTTP service holding, for each account, that account's choices
about how every other service in the family behaves for them. Which country their music
searches resolve against, how long their downloads sit on a shared box, which model
answers their questions, what "delete" means for their notes, which providers must never
see their queries.

It is the second hub in this family. [keyring](../Keyring-api) holds the accounts and
the credentials and is the auth root; this service has no accounts of its own. The only
identity it ever learns is the `sub` of a token keyring signed, verified locally against
keyring's JWKS document. It never calls keyring at request time, and it cannot ask keyring
anything about a person.

The service exists because roughly four dozen knobs across seven services are not really
deployment decisions. `spotify-api`'s `default_market` is the clearest one: which
country's catalogue somebody's track searches resolve against is a fact about a person,
deployed as an environment variable that applies to everybody on the box.

The HTTP surface is designed to be fronted by an **MCP server**, so an assistant can call
it as tools. That is why route `operation_id`s and descriptions are treated as contract
rather than decoration -- see [Invariants](#invariants) and [docs/mcp.md](docs/mcp.md).

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything. |
| `make check` | **The gate.** Format check, lint, strict types, layering contracts, tests at 100% branch coverage. Run before every commit. |
| `make matrix` | The tests on every Python CI runs. Coverage genuinely differs between versions; a green `check` is one interpreter's opinion. |
| `make test` | Tests only, with coverage enforced. |
| `make cov` | HTML coverage report in `htmlcov/`. |
| `make fmt` | Format and auto-fix. |
| `make lint` / `make type` / `make imports` | The three halves of `check` that are not tests. |
| `make schema` | Regenerate `storage/schema.sql` after changing a migration. Commit the diff with it. |
| `make catalogue` | Regenerate `docs/catalogue.md` after changing a catalogue entry. Commit the diff with it. |
| `make run` | Serve on :8003 with reload. Docs at `/docs`. |
| `make smoke` | End-to-end against a running settings-api **and** a running keyring. See `scripts/smoke.py`. |
| `make docker` | Build the image. |

Always run `make check` rather than a bare `pytest` -- piping any of these to `head`/`tail`
in a shell chain masks the exit code, which is how a broken commit slips through.

## The map

```
src/settings_api/
  core/      config, clock, logging, request context, version, and the composition root
  domain/    pure types and rules: the shape of a setting, value validation, namespaces,
             the credential detector, operator policy, the three-layer resolution chain,
             and catalogue/ -- one module per namespace. Imports nothing internal.
  storage/   the SQLite connection on its one thread, the migrations, the schema
             snapshot, and how a datetime becomes a column.
  auth/      JwksClient, TokenVerifier, ServiceAuthenticator. The only package that
             imports `jwt` or `httpx`, and the only one that decides who a request is for.
  events/    EventLog port + SQL adapter. The person's own history of their own decisions.
  settings/  SettingsStore port + SQL adapter, the service, erasure, and the sweeper.
  api/       FastAPI app, routers, wire schemas, problem+json errors, middleware.
```

Dependencies point inward:
`api → settings → events → auth → storage → domain`. `core` is a shared kernel everything
may use, except `domain`.

`domain/` is the bottom because it is where the rules are written down in a form a person
can read. `domain/catalogue/` is the extreme case: it is a **table**, restricted by
contract to importing `domain/types` alone, so forty-two settings stay something a
reviewer can check against what the services actually do rather than something they have
to execute in their head.

`auth/` sits below `events`/`settings` and above `storage` because it needs nothing from
the database: a token is verified against a cached public key and nothing else. That
position is also what lets one import-linter contract keep `jwt` and `httpx` inside it.

## Invariants

These are enforced mechanically. If you want to break one, change the enforcement
deliberately and say why in the commit message -- do not work around it.

1. **The domain imports nothing from the rest of the package.** The contract "Domain is
   independent", run by `make imports`.
2. **Layers point inward.** The contract "Layers point inward", listing the six packages in
   order. `exhaustive = false`, so `core` is outside it by design.
3. **The catalogue is data, and its namespaces are discovered rather than listed.** The
   contract "The catalogue is data" restricts every module under `domain/catalogue/` to
   `domain/types` -- not the errors, not the value helpers, and certainly not a store.
   `_assemble()` combines the modules this repository ships with anything registered under
   the entry point group `settings_api.namespaces`, so a service that is not public brings
   its own namespace and this repository never names it -- see ADR-0011. A registered
   module supplies `NAMESPACE` and `SETTINGS` exactly as a built-in does and goes through
   the same `entry.check()`, so a malformed extension fails at import too. A catalogue module that could import the store is a
   catalogue entry that could have behaviour. This is why `SettingDef.validate` raises a
   plain `ValueError` and `domain/values.py` is what turns it into a domain error, and why
   the lookups that raise domain errors live in `domain/registry.py` rather than in the
   catalogue package.
4. **Keyring is spoken to from `settings_api.auth` alone, through `keyring_client`.** The
   contract "Outbound HTTP and JWT live in one package" forbids `jwt`, `httpx` and
   `keyring_client` to every other layer. The token rules themselves -- RS256, the pinned
   issuer, the required claims, expiry on the injected clock, the JWKS rate limits -- are the
   family's shared library, tested against keyring's real signer in the keyring repository.
   `auth/` adds only this service's audience rules (namespaces, service grants) and
   translates the library's errors into this service's domain errors, so nothing above it
   knows a library was involved.
5. **SQL stays behind the stores.** The contract "SQL stays behind the stores" forbids
   `api`, `auth` and `domain` from importing `settings_api.storage` or `sqlite3`. A router
   that *could* write a query is a router that will eventually contain one.
6. **One settings set per account. There is no profile, anywhere.** Four mechanisms and
   you need all four: the primary key is `(account_id, namespace, key)` with no profile
   column, so a per-profile value is unrepresentable; no route accepts a profile as a path
   segment, a query parameter or a body field, and every request body sets
   `extra="forbid"` so `{"profile": "work"}` is a 422 rather than a silently ignored
   field; the account comes only from a verified `sub`; and a test asserts that two tokens
   minted for the same `sub` through different keyring profiles read and write the
   identical document. See [ADR-0002](docs/adr/0002-settings-are-per-account-not-per-profile.md).
7. **No account id appears in any path, and no endpoint accepts one.** Every person-facing
   route is under `/v1/settings` and the account comes from `IdentityDep`; every internal
   route takes the account from the **user's** token via `ServiceIdentityDep`. A
   cross-account read is not forbidden -- it is inexpressible.
8. **A service may present only tokens from its own audience family.**
   `ServiceGrant.accepts_audience` in `domain/namespaces.py`, applied in
   `TokenVerifier.verify_for_service`. Without it, a static service token plus any user
   token would read any account: the confused deputy moved into the gap between two
   processes. Two services sharing a token is refused at startup for the same reason.
9. **Nothing reads the wall clock.** Every component that behaves differently over time
   takes a `Clock`, and `SystemClock` in `core/clock.py` is the only caller of
   `datetime.now` or `time.monotonic` in `src/`. This includes JWT expiry: PyJWT's
   `verify_exp` **and** `verify_iat` are switched off and expiry is re-checked against the
   injected clock, because PyJWT refuses a token whose `iat` is in the future by the
   *wall* clock, which would refuse every good token in a test that pinned the clock to
   next Tuesday. What enforces it: ruff's `DTZ` rules, `storage/times.py` raising on a
   naive datetime, and a suite that never sleeps. The one deliberate exception is
   `time.perf_counter()` in `api/middleware.py`, which measures a duration for a log field
   and decides nothing.
10. **No setting value ever reaches a log record.** Two mechanisms, and both are needed: no
    call site passes a value to a logger (log `namespace`, `key`, `revision`, `action`,
    counts), and `redact_secrets` in `core/logging.py` replaces anything whose field name
    is on `_CONTENT_FIELDS` or matches a sensitive substring, at every depth, before
    rendering -- including non-string dict keys, because `{b"value": ...}` renders as
    `"b'value'"` and would otherwise walk straight past. A test drives a request whose body
    carries a sentinel and asserts it appears in no log record, on the success path and on
    every failure path. Note what else this covers: `RequestValidationError` is reshaped by
    hand in `api/errors.py` because FastAPI's own handler echoes the offending **input**,
    and an unhandled exception is rendered by type name only.
11. **No event ever carries a value.** `detail` holds which keys changed and how many.
    user-api makes recording old values an opt-in setting; here there is no such setting,
    because a settings value is short, guessable and repeatedly the same -- so a log of
    them is a log of the person's posture over time, which is exactly what
    `forget_settings` is supposed to destroy.
12. **A credential is refused rather than stored.** `looks_like_a_credential` runs on every
    string value and every string inside a list value, after shape validation and before
    the size check. The refusal names keyring and never echoes the matched text. There is
    no override and no setting that turns it off. The specification is the pair of corpora
    in `tests/unit/domain/test_secrets.py`: `MUST_ACCEPT` is the one allowed to grow, and a
    change that shrinks it is a regression even if it catches more secrets.
13. **Erasure means `DELETE` plus a truncating checkpoint.** `PRAGMA wal_checkpoint(FULL)`
    is not enough and `DELETE` alone is not close: the value sits in the `-wal` file,
    findable with `grep`. `settings/erasure.py` does the deletes and follows with
    `Database.checkpoint_truncate`, because a checkpoint cannot run inside a transaction.
    The sweeper does the same after purging retired rows. A test scans the bytes of the
    database **and** its `-wal` for a sentinel; it is the one test here that a unit test
    cannot replace, because it is about the file rather than about the code.
14. **Storage is sparse, and a write that changes nothing changes nothing.** A row exists
    only where somebody expressed a preference, so changing a catalogue default moves
    everyone who never chose and nobody who did. An identical repeated `PUT` bumps no
    revision and appends no event. Both are tested, and the first is the fix for a defect
    user-api shipped -- see [ADR-0005](docs/adr/0005-sparse-storage-so-defaults-can-move.md).
15. **Every cap and every revision bump is inside the transaction that writes.** The whole
    operation is one submitted callable on the one thread that may touch the connection,
    so "read the revision, then write" cannot go stale and the event trim cannot race an
    append.
16. **A pinned setting is visible as pinned, and a write to it is a 409.** Never a silent
    no-op. `describe_settings` reports `pinned: true`, so a caller can see it coming
    rather than discovering it by being refused. A person whose change vanished with a 200
    and no explanation is the worst outcome this service has.
17. **Every catalogue entry declares `on_unavailable`, and the field has no default.** A
    test asserts every entry declares one, and a second asserts that every `USE_DEFAULT`
    entry's default is in its own `conservative_values`. That pair turns "the default is
    safe to fall back to" from a claim in a docstring into something the build checks.
18. **Route `operation_id`s are public API.** They become MCP tool names, so renaming one
    breaks every client with a tool bound to it. There are sixteen; every route sets one
    explicitly, snake_case `verb_noun`, along with a `summary` and a real `description`
    written for a model rather than for a browser. Keep the OpenAPI contract test that
    pins the exact set in step when you add an endpoint.
19. **Coverage is 100% branch coverage, and the exclusions are only non-executable
    lines** -- `if TYPE_CHECKING:`, bare `...` protocol bodies, `@overload`,
    `raise NotImplementedError`, the `__main__` guard. There is no `# pragma: no cover` in
    `src/`, and `fail_under = 100` is what makes that stick. A line that is hard to cover
    is usually the code saying it is shaped wrong: `domain/types.py` has its per-type
    checker table asserted by a *test* rather than by an import-time assertion nothing can
    reach, and `Database.count` indexes into its result rather than testing for a missing
    row precisely so there is no branch nothing can take.
20. **There is no administrative HTTP surface.** No operator can read somebody's settings
    over HTTP, because no route exists that would let them. See
    [ADR-0008](docs/adr/0008-no-administrative-surface.md).

## How we work: TDD

Every change follows red → green → refactor, and each commit leaves `make check` passing.

1. Write the test first. It should fail for the reason you expect -- check that it does.
2. Write the smallest implementation that passes.
3. Refactor with the test as a safety net.

Notes earned during this build:

- **Name tests after the behaviour.**
  `test_a_service_cannot_read_another_services_namespace` beats `test_internal_403`.
- **Fakes are hand-written and satisfy the real `Protocol`** (`tests/fakes/`). If a port
  changes they fail to type-check, which is how you find out. There is no `unittest.mock`
  in this suite: the fake keyring is a real RSA key, a real JWKS document and a real
  transport, and its forged tokens are assembled by hand because PyJWT refuses to sign
  with a public key -- a guard on the signing side that says nothing about the verifying
  side.
- **Never sleep.** Three rules here are arithmetic on a date -- a token's expiry, the JWKS
  cache's age, and how long a retired key's rows survive -- and the last is measured in
  months. Move the `FakeClock`.
- **Mint test tokens with a decade-long TTL.** Advancing the clock past a retention window
  also expires the token in hand, and the failure reads as an authorisation bug rather
  than as a test that moved time too far. `tests.fakes.clock.DECADE_SECONDS` is the
  default; tests that are *about* expiry pass their own.
- **Choose sentinels the credential detector accepts.** A hyphenated random-looking string
  trips it, and the resulting 422 looks like the behaviour under test failing. Assert that
  the detector accepts your sentinel in the same test that uses it.
- **Two rows written in the same tick share `set_at`.** The clock is injected, so ordering
  falls to whatever SQLite chose. Compare as sets, or page by `sequence`.
- **Assert something that could fail.** `assert await service.get_all(...)` always passes;
  strict mypy's `truthy-bool` catches it mechanically.
- **Test the outcome, not the mechanism.** The erasure test scans the file's bytes rather
  than asserting that a checkpoint was called. The first version would need rewriting for
  any change of mechanism; this one would survive a move to `VACUUM`.
- **Two situations that must look identical need a test that they do.** Every
  authentication refusal answers 401 with a byte-identical body.

## Recipe: add a setting

This is the recipe the whole design exists to make cheap. One catalogue entry and a doc
regeneration -- no migration, no schema change, and no deploy of the services that read it.

1. Add a `SettingDef` to the right module in `src/settings_api/domain/catalogue/`. Every
   field that is not optional is not optional for a reason; `on_unavailable` in particular
   has **no default**, because choosing between falling back and refusing is the decision
   the author has to make and getting it wrong is silent.
2. Write `summary` and `description` for a **model** to read. The summary is one line for
   deciding whether to touch it; the description says what choosing each value actually
   means for the person. They must differ -- a description that restates the summary is
   what somebody writes when they have not decided what the setting means, and there is a
   check that refuses it.
3. If `on_unavailable` is `USE_DEFAULT`, list the values it is safe to land on in
   `conservative_values`, and make sure the default is among them. If the honest answer is
   "there is no safe fallback", the answer is `REFUSE` -- see
   [ADR-0006](docs/adr/0006-on-unavailable-is-declared-per-setting.md).
4. Set `origin`. `EXISTING` means the owning service already has the knob and wiring it up
   is a change at the call site; `PROPOSED` means that service needs a change first, and
   `origin_note` says which. `docs/catalogue.md` repeats it per entry, so nobody ships a
   setting that silently does nothing.
5. `make catalogue`, and commit the regenerated `docs/catalogue.md` with the entry.
6. Tests: the catalogue's parametrised suite covers the entry automatically -- key shape,
   the default validating against its own bounds, the conservative set, the prose. Add a
   behavioural test only if the setting means something the generic ones cannot check.

Do **not** add a setting that turns off a protection. There is a list in the module
docstring of `core/config.py` of the settings this family has refused to add, and it is
worth reading before adding anything: no setting disables token verification, none lets one
account read another's, none turns off the credential refusal, and none lets a person
disable SSRF protection, robots compliance or authentication in a service that owns those.

## Recipe: retire a setting

1. Set `retired_at` on the entry to today's date, in ISO form. **Do not delete the entry.**
2. That is all. Reads ignore it from that moment, so the setting is gone from the service's
   behaviour immediately, and the stored rows survive `retired_retention_days` (90 by
   default) before the sweeper destroys them.
3. The delay is the point. Reverting the catalogue inside the window restores every
   affected person's expressed choice intact; deleting on sight would destroy them all the
   moment somebody merged a typo.
4. If something replaces it, set `deprecated_by` to the new `namespace.key`.
5. `make catalogue` and commit the diff.

## Recipe: add a consuming service

1. Add it to `SETTINGS_API_SERVICES`, as one JSON object: its own token, the
   `audience_prefix` its user tokens carry, and the namespaces it may see.
2. Give it the **narrowest** namespace list that works. `common` is added automatically and
   never needs listing. The blast radius of one compromised service token is exactly the
   namespaces in this list.
3. The token must be at least 32 characters and must not be shared with another service --
   both are refused at startup. Sharing one would make the audience-family check
   meaningless, because whichever name matched first would decide which audience is
   acceptable.
4. `audience_prefix` is independent of the service's key in code. It is the audience family
   keyring mints that service's user tokens under -- and for a service that also presents
   those tokens to keyring's internal surface, keyring requires that audience to be exactly
   the service's `KEYRING_SERVICE_TOKENS` name, so in a deployment the two are the same.
5. Tests: that it reads its own namespace merged with `common`, that it gets a 403 for
   somebody else's, and that a user token from another audience family is a 401.

## Recipe: add an endpoint

1. Add it to `src/settings_api/api/routers/<name>.py`.
2. Add wire models in `src/settings_api/api/schemas/<name>.py`. Set
   `model_config = ConfigDict(extra="forbid")` on every request body -- on this service
   that is load-bearing rather than tidy, because the one field a caller might try to send
   and must never be able to is `profile`. Give every field a `description` and every model
   an `examples` entry: they are the tool documentation, not decoration.
3. On every route set `operation_id` (snake_case `verb_noun`, stable forever), `summary`, a
   real `description`, and `responses` for every failure a caller can provoke.
4. Register it in `ROUTERS` in `api/routers/__init__.py`. **Order matters**: Starlette
   matches in declaration order, so a literal path like `/v1/settings/schema` must be
   declared before `/v1/settings/{namespace}`.
5. Take `IdentityDep` (person) or `ServiceIdentityDep` (service). Never accept an account
   id, and never accept a profile -- both come from the token, or do not exist.
6. Raise domain errors. Map any new one in `_DOMAIN_STATUS` in `api/errors.py`; never build
   an error response in a handler.
7. Tests: one per behaviour, the OpenAPI contract test extended with the new
   `operation_id`, and -- if it writes -- a test that `{"profile": "work"}` in the body is
   a 422.

## Environment gotchas

- `asyncio_mode = "auto"`, so `async def test_*` needs no marker.
- `filterwarnings = ["error"]`: a new deprecation warning fails the suite. Fix it rather
  than filtering it.
- Every setting is an environment variable prefixed `SETTINGS_API_`. A `SETTINGS_API_`-
  prefixed variable that matches **no** setting is a **startup error**, not a warning --
  `check_for_unknown_env_vars` raises before `Settings` is built. pydantic-settings would
  otherwise ignore it, and `SETTINGS_API_ALOWED_NAMESPACES` would leave the namespace list
  on its default with nothing in the logs to say so.
- `SETTINGS_API_SERVICES` is one JSON document rather than a nested tree, because a nested
  spelling cannot be enumerated -- and a configuration this service cannot enumerate is one
  the typo check cannot check.
- Tests must never sleep. `FakeClock` is injected through the container the `app` fixture
  builds, which is the one seam the suite needs: `create_app` deliberately builds its own
  container, and `start()` honours a prebuilt one.
- Test settings are built through the `Settings` constructor rather than
  `model_copy(update=...)`, which skips validators and would accept a namespace list the
  validator rejects, failing somewhere far away instead.
- `database_path` is resolved at load, so a relative path cannot mean two places after a
  `chdir`. The test suite uses a real file rather than `:memory:`, because the erasure
  tests read the file's bytes and an in-memory database has none.
- `make matrix` runs 3.11 and 3.12 because coverage differs between them: until 3.12,
  `isinstance()` against a runtime-checkable Protocol executed property getters, so a
  property with no test of its own looked covered on 3.11 and does not on 3.12.
- Assert a file mode with `assert_mode` from `tests/support/filemode.py`, never with
  `stat.S_IMODE` directly. It is exact on POSIX and compares only the owner's bits on
  Windows, where NTFS has no permission bits and every writable file reads 0666, so a
  direct comparison fails natively there. For the same reason a test that moves `~` sets
  `USERPROFILE` as well as `HOME`: that is what `Path.expanduser()` reads on Windows.

## Commit conventions

Conventional-commit subject (`feat(scope):`, `fix(scope):`, `chore:`, `docs:`), imperative
mood, no trailing period. The body explains **why** -- the tradeoff, the failure mode being
prevented, the thing that surprised you. A reader six months from now has the diff already;
what they lack is your reasoning.

## Definition of done

- [ ] Tests were written first, and failed first.
- [ ] `make check` passes: format, lint, strict types, the five contracts, 100% coverage.
- [ ] New behaviour is covered by a test named after the behaviour.
- [ ] Anything that reads or writes a setting works from the token's account and cannot be
      pointed at another -- and there is no profile anywhere in the change.
- [ ] Anything that accepts a string from a caller passes it through the credential
      refusal, or there is a written reason here why it does not.
- [ ] Nothing new can appear in a log record, an event or a response that should not --
      check `_CONTENT_FIELDS` in `core/logging.py`, and remember that a 422 body is logged
      by the caller.
- [ ] Anything that destroys data leaves nothing in the `-wal`: `DELETE` plus a truncating
      checkpoint, with a byte scan asserting it.
- [ ] Anything touching the schema has a migration, a regenerated `schema.sql`, and the
      snapshot diff in the same commit.
- [ ] Anything touching the catalogue has a regenerated `docs/catalogue.md` in the same
      commit.
- [ ] Public HTTP changes: `operation_id`s stable, descriptions written for a model to
      read, contract test updated.
- [ ] Docs updated -- this file for workflow, `docs/` for design, an ADR for a decision
      that future-you would otherwise re-litigate.
