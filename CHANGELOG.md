# Changelog

All notable changes to settings-api are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The **`environments`** namespace: `idle_environment_hours`, `idle_shell_minutes`,
  `max_environments_per_profile` and `default_shell`. environments-api was the one
  service in the family with no namespace at all. Operator-only knobs -- network access,
  the sandbox tier, every byte and CPU quota -- deliberately stay in that service's own
  configuration, and the module says why.
- `py.typed` in `clients/python/settings_client`, so a consuming service type-checks
  against the client rather than around it.

### Changed

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
