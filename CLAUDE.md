# CLAUDE.md

See [AGENTS.md](AGENTS.md) -- one source of truth for how work is done in this repository.

Short version: run `make check` before every commit, write the test first, and keep the
architectural contracts in `pyproject.toml` intact. This service holds the decisions a
person has made about their own data; the invariants in AGENTS.md are not negotiable.
