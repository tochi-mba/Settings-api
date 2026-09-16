# Testing

`make check` is the gate: format, lint, strict types, the layering contracts, and the
tests at 100% branch coverage. This page is about how the tests are written, and why they
are written that way.

## The shape of a test

Tests are named for the behaviour they pin, not for the function they call. A reader who
does not know this codebase should be able to read the test names and learn what the
service promises:

```python
async def test_a_reset_lets_the_default_move_again(...)
async def test_a_service_may_not_read_a_namespace_it_was_not_granted(...)
```

Each test does one thing, with the arrangement, the action and the assertion visibly
separated. A comment explains *why* a case matters when that is not obvious from the name
— especially for the cases that exist because something went wrong once.

## 100% branch coverage, and what it is for

The gate is not a target to hit by adding tests for getters. It is a tripwire: a branch
nobody has exercised is a branch nobody has thought about, and this service is one people
trust with what they have decided. `# pragma: no cover` is not used in `src/` — the family
parity check fails a repository that grows one — so a branch that cannot be reached is
removed rather than excused.

## Fakes, not mocks

Fakes live in `tests/fakes/`, are hand-written, and must satisfy the real protocol; if a
port changes, they stop type-checking, which is how you find out.

A fake must also **refuse what the real thing refuses**. `tests/fakes/keyring.py` is a thin
wrapper over `keyring_client.testing`, the one fake the whole family shares: it signs real
RS256 tokens with a real key, serves a real JWKS document, and refuses a token minted for
another audience exactly as keyring would. A fake more permissive than the real service
teaches the wrong contract, and the bug only shows up in production.

## Layers of test

| Layer | Where | What it proves |
| --- | --- | --- |
| Unit | `tests/unit/` | One module's rules: the catalogue's bounds, policy narrowing, value coercion, token verification. |
| Storage | `tests/unit/storage/` | Migrations apply in order, the snapshot matches, the database file is private to its owner. |
| API | `tests/unit/api/` | Status codes, problem bodies, the two-credential rules, and that no route takes an account id. |
| Contract | `tests/contract/` | The **real** `settings_client` against the **real** app in-process, over ASGI. This is what stops the client and the service drifting apart. |

## The tests that must never be deleted

- **Isolation.** One account cannot read or write another's, through any route, and asking
  answers the same as asking for something that does not exist.
- **Grants.** A service reaching for a namespace it was not granted gets a 403, and a user
  token from outside the calling service's audience family gets a 401.
- **Outage behaviour.** Every `refuse` setting refuses rather than falling back, and every
  `use_default` setting falls back rather than failing. That contract is what seven other
  services rely on.
- **Secrets.** No token, and no person's value, reaches a log record.

## Running less than everything

```bash
uv run pytest tests/unit/domain -q            # one area
uv run pytest -k "grant or namespace" -q      # by name
make cov                                      # HTML report in htmlcov/
make matrix                                   # every Python CI runs, because one is one opinion
```

Coverage genuinely differs between interpreter versions, so a green `make check` is one
interpreter's answer and `make matrix` is the honest one.

## Platform notes

The suite runs natively on Windows, macOS and Linux. The file-mode tests — a database
created private to its owner — assert POSIX permission bits where they exist and, on
Windows, the weaker thing NTFS can express; `tests/support/filemode.py` holds that one
helper and says why in its docstring.
