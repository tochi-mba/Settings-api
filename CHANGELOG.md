# Changelog

All notable changes to settings-api are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **settings-client 0.2.0.** `resolve()` takes `profile`, and the cache is keyed by token, namespace *and* profile, so two profiles of one person never share a resolved document. A consumer pins the tag `settings-client-v0.2.0`; the 0.1.0 signature is not kept, because a client that accepts a call it cannot honour answers with the wrong profile's values, and that is worse than a `TypeError`.
- The **`environments`** namespace: `idle_environment_hours`, `idle_shell_minutes`,
  `max_environments_per_profile` and `default_shell`. environments-api was the one
  service in the family with no namespace at all. Operator-only knobs -- network access,
  the sandbox tier, every byte and CPU quota -- deliberately stay in that service's own
  configuration, and the module says why.
- `py.typed` in `clients/python/settings_client`, so a consuming service type-checks
  against the client rather than around it.

### Changed

- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- CI inherits `FAMILY_GITHUB_TOKEN`; image builds accept a BuildKit `github_token`
  secret so tagged client packages can be fetched from private family repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- Token verification uses `keyring-client`, the verifier shared by the whole family,
  instead of this service's own copy of the rules. The rules themselves are unchanged:
  RS256 only, the issuer pinned, every claim required, expiry on the injected clock, one
  undifferentiated refusal. An unknown key id is now rate-limited, and keys from a good
  fetch keep verifying tokens through a short keyring outage.

### Fixed

- `scripts/smoke.py` minted its token with `POST /v1/internal/tokens` and an admin token.
  Keyring has no such route; the script now uses `POST /v1/auth/service-token` with a
  session, which is what keyring actually serves.
- The owner-only database file checks are POSIX rules and now say so, so the suite runs
  green on Windows as well as in CI. `tests/support/filemode.py` holds the one helper.
- The home-relative path tests set `USERPROFILE` as well as `HOME`, and the lower-case
  typo test matches case-insensitively; both were POSIX-only assumptions.
