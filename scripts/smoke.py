"""End-to-end check against a running settings-api and a running keyring.

Not a test. The suite already proves the behaviour against the real app in-process; this
proves the *deployment* -- that the two services agree about the issuer, that the service
tokens configured here match the ones the consuming services hold, that each service's
audience prefix matches the audience keyring mints for it, that the database file is 0600 on
the box it is actually running on, and that a deletion really does remove the bytes from the
file rather than from the table.

Every check is a thing that can only go wrong between two processes. Anything provable
inside one belongs in the suite.

Tokens are minted the way a real caller mints them: log in to keyring with an account that
already exists, then exchange the session for a short-lived token per audience with
``issue_service_token``. Nothing here needs keyring's break-glass token.

Run it with ``make smoke``. It prints what it did and exits non-zero on the first fatal
problem, because a smoke test that carried on would bury the first thing that broke.

Environment:
    SETTINGS_API_URL          default http://127.0.0.1:8003
    KEYRING_URL               default http://127.0.0.1:8001
    SMOKE_EMAIL               an account that already exists in keyring
    SMOKE_PASSWORD            its password
    SETTINGS_SPOTIFY_TOKEN    the service token configured here for spotify-api
    SETTINGS_MEDIA_TOKEN      the service token configured here for media-tool
    SETTINGS_DB_PATH          the database file, for the byte scan and the mode check
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx

SETTINGS_URL = os.environ.get("SETTINGS_API_URL", "http://127.0.0.1:8003").rstrip("/")
KEYRING_URL = os.environ.get("KEYRING_URL", "http://127.0.0.1:8001").rstrip("/")
EMAIL = os.environ.get("SMOKE_EMAIL", "")
PASSWORD = os.environ.get("SMOKE_PASSWORD", "")
SPOTIFY_TOKEN = os.environ.get("SETTINGS_SPOTIFY_TOKEN", "")
MEDIA_TOKEN = os.environ.get("SETTINGS_MEDIA_TOKEN", "")
DB_PATH = Path(os.environ.get("SETTINGS_DB_PATH", "var/settings.db"))

SPOTIFY_AUDIENCE = "spotify-api"
"""spotify-api's name in KEYRING_SERVICE_TOKENS, and therefore its prefix in this grant."""

MEDIA_AUDIENCE = "media-tool"

SENTINEL_MARKET = "GB"
"""The value written and then looked for in the file's bytes.

Two characters, so the byte scan needs a distinctive companion -- which is what the
timezone below is for. A market code alone would match by accident in any file.
"""

SENTINEL_ZONE = "Pacific/Chatham"
"""A real IANA zone nobody sets by accident, so finding it in the bytes means something."""

_failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Report one check and remember whether it failed."""
    global _failures  # noqa: PLW0603 -- a script, and the alternative is threading state
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        _failures += 1


def fatal(message: str) -> None:
    """Stop, because nothing after this could mean anything."""
    print(f"\nfatal: {message}")
    sys.exit(1)


def log_in(client: httpx.Client) -> str:
    """Open a keyring session for the smoke account."""
    response = client.post(
        f"{KEYRING_URL}/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    if response.status_code != httpx.codes.OK:
        fatal(f"keyring would not log the smoke account in: {response.status_code}")
    token: str = response.json()["token"]
    return token


def mint(client: httpx.Client, session: str, audience: str) -> str:
    """Exchange the session for a short-lived token, the way every real caller does."""
    response = client.post(
        f"{KEYRING_URL}/v1/auth/service-token",
        json={"audience": audience},
        headers={"Authorization": f"Bearer {session}"},
    )
    if response.status_code != httpx.codes.OK:
        fatal(f"keyring would not mint an {audience} token: {response.status_code}")
    token: str = response.json()["token"]
    return token


def person(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def service(service_token: str, user_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {service_token}",
        "X-Settings-User-Token": user_token,
    }


# One linear script on purpose: the ORDER of these checks is the documentation. Splitting
# it into helpers would hide that step 11 only proves anything because step 10 confirmed
# the sentinel was in the file first.
def main() -> None:
    if not (EMAIL and PASSWORD):
        fatal("SMOKE_EMAIL and SMOKE_PASSWORD are not set; create an account in keyring first")
    if not (SPOTIFY_TOKEN and MEDIA_TOKEN):
        fatal("SETTINGS_SPOTIFY_TOKEN and SETTINGS_MEDIA_TOKEN must both be set")

    with httpx.Client(timeout=10.0) as client:
        print("\n1. keyring logs the account in and mints a token with audience `settings`")
        session = log_in(client)
        owner = mint(client, session, "settings")
        check("minted", bool(owner))

        print("\n2. GET /v1/settings returns defaults with an ETag, and never 404s")
        response = client.get(f"{SETTINGS_URL}/v1/settings", headers=person(owner))
        check("200", response.status_code == httpx.codes.OK, str(response.status_code))
        check("ETag present", "ETag" in response.headers, response.headers.get("ETag", ""))
        body: dict[str, Any] = response.json()
        check("common present", "common" in body["settings"])

        print("\n3. GET /v1/settings/schema describes every setting")
        response = client.get(f"{SETTINGS_URL}/v1/settings/schema", headers=person(owner))
        described = response.json()["settings"]
        check("200", response.status_code == httpx.codes.OK)
        check(
            "every entry has a default and bounds",
            all("default" in entry and "bounds" in entry for entry in described),
            f"{len(described)} settings",
        )

        print("\n4. PUT a value; a second identical PUT changes nothing")
        first = client.put(
            f"{SETTINGS_URL}/v1/settings/spotify/default_market",
            json={"value": SENTINEL_MARKET},
            headers=person(owner),
        )
        check("200", first.status_code == httpx.codes.OK, first.text[:120])
        etag_after_write = first.headers.get("ETag", "")
        again = client.put(
            f"{SETTINGS_URL}/v1/settings/spotify/default_market",
            json={"value": SENTINEL_MARKET},
            headers=person(owner),
        )
        check("idempotent: ETag unchanged", again.headers.get("ETag") == etag_after_write)
        check("idempotent: reported unchanged", again.json()["unchanged"] is True)

        client.put(
            f"{SETTINGS_URL}/v1/settings/common/timezone",
            json={"value": SENTINEL_ZONE},
            headers=person(owner),
        )

        print("\n5. spotify-api resolves its namespace with the token keyring mints for it")
        spotify_user = mint(client, session, SPOTIFY_AUDIENCE)
        resolved = client.get(
            f"{SETTINGS_URL}/v1/internal/settings/spotify",
            headers=service(SPOTIFY_TOKEN, spotify_user),
        )
        check(
            "200 -- the grant's audience_prefix matches keyring's service name",
            resolved.status_code == httpx.codes.OK,
            resolved.text[:120],
        )
        values = resolved.json().get("settings", {})
        check("its own setting", values.get("default_market") == SENTINEL_MARKET)
        check("common merged underneath", values.get("timezone") == SENTINEL_ZONE)
        check("fallbacks returned", "fallbacks" in resolved.json())
        internal_etag = resolved.headers.get("ETag", "")

        print("\n6. media-tool cannot read the spotify namespace")
        media_user = mint(client, session, MEDIA_AUDIENCE)
        refused = client.get(
            f"{SETTINGS_URL}/v1/internal/settings/spotify",
            headers=service(MEDIA_TOKEN, media_user),
        )
        check("403", refused.status_code == httpx.codes.FORBIDDEN, str(refused.status_code))

        print("\n7. spotify-api presenting the person's own settings token is refused")
        deputy = client.get(
            f"{SETTINGS_URL}/v1/internal/settings/spotify",
            headers=service(SPOTIFY_TOKEN, owner),
        )
        check("401", deputy.status_code == httpx.codes.UNAUTHORIZED, str(deputy.status_code))

        print("\n8. If-None-Match revalidates to 304")
        revalidated = client.get(
            f"{SETTINGS_URL}/v1/internal/settings/spotify",
            headers={**service(SPOTIFY_TOKEN, spotify_user), "If-None-Match": internal_etag},
        )
        check("304", revalidated.status_code == httpx.codes.NOT_MODIFIED)

        print("\n9. a second, separately minted token for the same person sees the same")
        second = mint(client, log_in(client), "settings")
        same = client.get(f"{SETTINGS_URL}/v1/settings/spotify", headers=person(second))
        check(
            "identical document",
            same.json()["settings"]["spotify"]["default_market"] == SENTINEL_MARKET,
            "one settings set per account, however many sessions or profiles",
        )

        print("\n10. the database file is 0600 and holds the sentinel before erasure")
        if DB_PATH.exists():
            mode = stat.S_IMODE(DB_PATH.stat().st_mode)
            check("0600", mode == 0o600, oct(mode))
            wal = DB_PATH.with_name(DB_PATH.name + "-wal")
            if wal.exists():
                check("-wal 0600", stat.S_IMODE(wal.stat().st_mode) == 0o600)
            check(
                "the sentinel is in the file before erasure",
                SENTINEL_ZONE.encode() in _bytes_of(DB_PATH),
                "if this fails the byte scan below proves nothing",
            )
        else:
            check("database readable", False, f"{DB_PATH} not found; set SETTINGS_DB_PATH")

        print("\n11. DELETE /v1/settings destroys the bytes, not just the rows")
        erased = client.delete(f"{SETTINGS_URL}/v1/settings", headers=person(owner))
        check("200", erased.status_code == httpx.codes.OK, erased.text[:120])
        if DB_PATH.exists():
            check(
                "sentinel gone from the database and its -wal",
                SENTINEL_ZONE.encode() not in _bytes_of(DB_PATH),
                "DELETE plus a truncating checkpoint",
            )
        after = client.get(f"{SETTINGS_URL}/v1/settings", headers=person(owner))
        check("reads as never-having-written", after.headers.get("ETag", "").endswith('.0"'))

    print()
    if _failures:
        print(f"{_failures} check(s) failed")
        sys.exit(1)
    print("all checks passed")


def _bytes_of(path: Path) -> bytes:
    """The database and its write-ahead log, together.

    Both, because a DELETE leaves the old pages in the -wal until a checkpoint moves them
    -- which is exactly the failure this check exists to catch.
    """
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


if __name__ == "__main__":
    main()
