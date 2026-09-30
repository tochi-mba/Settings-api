# Changelog

All notable changes to settings-api are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `LICENSE`: the MIT text the rest of the family ships. `pyproject.toml` and the README
  already said MIT; the repository carried no licence file to say it with.
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
- CI calls the family's reusable workflow and fetches private family packages through its
  OIDC token broker (`id-token: write`), with no long-lived token in this repository; image
  builds accept a BuildKit `github_token` secret so tagged client packages can be fetched
  from private family repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- Token verification uses `keyring-client`, the verifier shared by the whole family,
  instead of this service's own copy of the rules. The rules themselves are unchanged:
  RS256 only, the issuer pinned, every claim required, expiry on the injected clock, one
  undifferentiated refusal. An unknown key id is now rate-limited, and keys from a good
  fetch keep verifying tokens through a short keyring outage.

### Fixed

- `spotify.default_device` and `search.default_result_count` were marked *proposed* --
  "setting these stores the value and changes no behaviour" -- while the hub reads both as
  its music and research defaults. They are *existing*, with notes naming the reader. Stale
  origin notes are corrected too: `persona.recall_default_limit` is read by persona-api,
  memory-api does run a consolidation pass, and environments-api is one of the services
  `common.default_profile` replaces. A test pins every setting a family service is known
  to read as *existing*.
- Eight namespace module docstrings, which `docs/catalogue.md` prints as each namespace's
  preamble, described a catalogue that has since moved: `persona` said every entry was a
  proposal and persona-api had no per-account settings, `user` counted four entries,
  `keyring` put notifications among the knobs it has and called the re-authentication
  setting the only owner-only one, `spotify` and `environments` said the values were still
  one number per box, `lucy` pointed at `PROPOSED` entries that no longer exist and left
  `decisions` out of its map, `lucy.feeds` miscounted its toggles, and `common` left
  environments-api out of the default-profile story.
- The API description told a model a person could decide "how long their downloads sit on
  the server". No setting in this catalogue governs a download; the example is now the one
  that exists, how long the record of a finished job stays readable
  (`common.job_retention_hours`).
- `scripts/smoke.py` read its target from `SETTINGS_API_URL`, a name under this service's own
  prefix: exported in the shell that starts settings-api, it made the service refuse to start
  with an unknown-variable error. It is now `SMOKE_SETTINGS_API_URL`, matching user-api's
  `SMOKE_USER_API_URL`, and a test checks every variable the script reads against the
  startup check.
- `make matrix` ran 3.11 and 3.12. 3.11 is below `requires-python`, so uv refused it and
  the target failed before a test ran; it now runs 3.12 and 3.13, which is what CI runs.
- `scripts/smoke.py` minted its token with `POST /v1/internal/tokens` and an admin token.
  Keyring has no such route; the script now uses `POST /v1/auth/service-token` with a
  session, which is what keyring actually serves.
- The owner-only database file checks are POSIX rules and now say so, so the suite runs
  green on Windows as well as in CI. `tests/support/filemode.py` holds the one helper.
- The home-relative path tests set `USERPROFILE` as well as `HOME`, and the lower-case
  typo test matches case-insensitively; both were POSIX-only assumptions.
