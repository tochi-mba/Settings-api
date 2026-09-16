# Contributing

Start with [AGENTS.md](AGENTS.md) — it is the operating manual for this repository and it
is normative. This file is the short version.

## Setup

```bash
make install             # venv and every dependency, from the lockfile
uv run pre-commit install
make check               # confirm a clean checkout is green before you change anything
```

No credentials and no network are needed. `make smoke` is the exception: it wants a
running settings-api and a running keyring.

## The loop

1. **Write the failing test first.** Run it and confirm it fails for the reason you expect.
2. Write the smallest code that makes it pass.
3. Refactor with it green.
4. `make check` — lint, strict types, the layering contracts, and the tests at 100%
   branch coverage.

All four must pass before you commit.

## Adding or changing a setting

The catalogue is code, not data in the database (ADR-0003), so a setting is a change to a
module under `src/settings_api/domain/catalogue/` and a regenerated page:

```bash
make catalogue           # rewrites docs/catalogue.md from the modules
```

Read [docs/adding-a-service.md](docs/adding-a-service.md) first. Its Step 0 test is the
bar every entry has to pass: a person can reasonably choose it, and their choice affects
only them. Anything an operator owns — quotas, network access, tiers — stays in the owning
service's configuration.

Every entry declares `on_unavailable`. Choose `refuse` only when falling back would
override a restriction somebody set; everything else is `use_default`, because an outage
here must not become an outage everywhere.

## Changing the database

Migrations are append-only and the schema snapshot is checked in:

```bash
make schema              # regenerate the snapshot after adding a migration
```

CI fails if the snapshot and the migrations disagree.

## Commits

Conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`). The
subject says what changed; the body says **why**, and flags anything surprising — a
trade-off taken, a bug found on the way, something deliberately left undone.

Never commit a real token, a real account id, or a `.env`.

## Review

A change is ready when `make check` is green, the tests read as statements about
behaviour rather than about implementation, and the documentation that would otherwise
become wrong has been changed in the same commit.
