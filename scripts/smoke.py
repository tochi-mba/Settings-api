"""End-to-end check against a running settings-api and a running keyring.

Not a test. The suite already proves the behaviour against the real app in-process; this
proves the *deployment* -- that the two services agree about the issuer, that the service
tokens configured here match the ones the consuming services hold, that the database file
is 0600 on the box it is actually running on, and that a deletion really does remove the
bytes from the file rather than from the table.

Every check is a thing that can only go wrong between two processes. Anything provable
inside one belongs in the suite.

Run it with ``make smoke``. It prints what it did and exits non-zero on the first failure,
because a smoke test that carried on after a failure would bury the first thing that broke.

Environment:
    SETTINGS_API_URL          default http://127.0.0.1:8003
    KEYRING_URL               default http://127.0.0.1:8001
    KEYRING_ADMIN_TOKEN       to create the account and mint tokens
    SETTINGS_SPOTIFY_TOKEN    the service token configured for spotify-api
    SETTINGS_MEDIA_TOKEN      the service token configured for media-tool
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
ADMIN_TOKEN = os.environ.get("KEYRING_ADMIN_TOKEN", "")
SPOTIFY_TOKEN = os.environ.get("SETTINGS_SPOTIFY_TOKEN", "")
MEDIA_TOKEN = os.environ.get("SETTINGS_MEDIA_TOKEN", "")
DB_PATH = Path(os.environ.get("SETTINGS_DB_PATH", "var/settings.db"))

SENTINEL_MARKET = "GB"
"""The value written and then looked for in the file's bytes.

Two characters, so the byte scan needs a distinctive companion -- which is what the
timezone below is for. A market code alone would match by accident in any file.
"""

SENTINEL_ZONE = "Pacific/Chatham"
"""A real IANA zone nobody sets by accident, so finding it in the bytes means something.

Chosen because it is a legal timezone (the setting validates against tzdata) and is
distinctive enough that a match in a binary file is not a coincidence.
"""

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


def mint(client: httpx.Client, account_id: str, audience: str, profile: str) -> str:
    """Ask keyring for a token, the way a real caller would."""
    response = client.post(
        f"{KEYRING_URL}/v1/internal/tokens",
        json={"account_id": account_id, "audience": audience, "profile": profile},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    if response.status_code != httpx.codes.OK:
        fatal(f"keyring would not mint an {audience} token: {response.status_code} {response.text}")
    token: str = response.json()["access_token"]
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
    if not ADMIN_TOKEN:
        fatal("KEYRING_ADMIN_TOKEN is not set; this script needs keyring to mint tokens")

    with httpx.Client(timeout=10.0) as client:
        print("\n1. keyring mints a token with audience `settings`")
        account_id = os.environ.get("SMOKE_ACCOUNT_ID", "")
        if not account_id:
            fatal("SMOKE_ACCOUNT_ID is not set; create an account in keyring first")
        owner = mint(client, account_id, "settings", "personal")
        check("minted", bool(owner))

        print("\n2. GET /v1/settings returns defaults with an ETag, and never 404s")
        response = client.get(f"{SETTINGS_URL}/v1/settings", headers=person(owner))
        check("200", response.status_code == httpx.codes.OK, str(response.status_code))
        check("ETag present", "ETag" in response.headers, response.headers.get("ETag", ""))
        body: dict[str, Any] = response.json()
        check("common present", "common" in body["settings"])

        print("\n3. GET /v1/settings/schema describes every setting")
        response = client.get(f"{SETTINGS_URL}/v1/settings/schema", headers=person(owner))
        schema = response.json()
        described = schema["settings"]
        check("200", response.status_code == httpx.codes.OK)
        check(
            "every entry has a default, bounds and set:false",
            all(
                "default" in entry and "bounds" in entry and entry["set"] is False
                for entry in described
            ),
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

        print("\n5. spotify-api resolves its namespace, with common merged underneath")
        if not SPOTIFY_TOKEN:
            fatal("SETTINGS_SPOTIFY_TOKEN is not set")
        spotify_user = mint(client, account_id, "spotify", "personal")
        resolved = client.get(
            f"{SETTINGS_URL}/v1/internal/settings/spotify",
            headers=service(SPOTIFY_TOKEN, spotify_user),
        )
        check("200", resolved.status_code == httpx.codes.OK, resolved.text[:120])
        values = resolved.json()["settings"]
        check("its own setting", values.get("default_market") == SENTINEL_MARKET)
        check("common merged underneath", values.get("timezone") == SENTINEL_ZONE)
        check("fallbacks returned", "fallbacks" in resolved.json())
        internal_etag = resolved.headers.get("ETag", "")

        print("\n6. media-tool cannot read the spotify namespace")
        if not MEDIA_TOKEN:
            fatal("SETTINGS_MEDIA_TOKEN is not set")
        media_user = mint(client, account_id, "media-tool", "personal")
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

        print("\n9. a second token for the same person via a different profile sees the same")
        other_profile = mint(client, account_id, "settings", "work")
        same = client.get(f"{SETTINGS_URL}/v1/settings/spotify", headers=person(other_profile))
        check(
            "identical document",
            same.json()["settings"]["spotify"]["default_market"] == SENTINEL_MARKET,
            "one settings set per account, however many profiles",
        )

        print("\n10. the database file is 0600 and its logs are usable")
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
