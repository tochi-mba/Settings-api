"""End to end over the real app, and the two isolation properties the service is built on.

One flow: a person sets a value, reads it, a service reads it, the person resets it, then
destroys everything -- with the ETag moving at each step. Then the property from ADR-0002,
named in full in the test: two tokens for the same person obtained through different
keyring profiles read and write the identical document, because there is no profile
anywhere. And two different people are completely isolated on every read route.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import (
    ACCOUNT,
    OTHER_ACCOUNT,
    SPOTIFY_TOKEN,
    auth,
    service_auth,
    set_setting,
    token,
)
from tests.fakes.keyring import mint


async def test_set_read_service_reads_reset_forget(client: AsyncClient) -> None:
    owner = token()
    spotify = service_auth(SPOTIFY_TOKEN, mint(audience="spotify"))

    initial = await client.get("/v1/settings", headers=auth(owner))
    assert initial.headers["ETag"] == f'"{ACCOUNT}.0"'

    written = await set_setting(client, owner, "spotify", "default_market", "PT")
    assert written["revision"] == 1

    mine = (await client.get("/v1/settings/spotify/default_market", headers=auth(owner))).json()
    assert (mine["value"], mine["set"], mine["source"]) == ("PT", True, "account")

    theirs = await client.get("/v1/internal/settings/spotify", headers=spotify)
    assert theirs.json()["settings"]["default_market"] == "PT"
    assert theirs.headers["ETag"] == f'"{ACCOUNT}.1"'

    reset = await client.delete("/v1/settings/spotify/default_market", headers=auth(owner))
    assert reset.headers["ETag"] == f'"{ACCOUNT}.2"'
    assert (await client.get("/v1/internal/settings/spotify", headers=spotify)).json()["settings"][
        "default_market"
    ] is None

    forgotten = await client.delete("/v1/settings", headers=auth(owner))
    assert forgotten.status_code == 200
    assert (await client.get("/v1/settings", headers=auth(owner))).headers[
        "ETag"
    ] == f'"{ACCOUNT}.0"'


async def test_two_tokens_for_the_same_sub_via_different_profiles_read_and_write_one_document(
    client: AsyncClient,
) -> None:
    # ADR-0002. keyring mints a token per (account, profile); the profile is not a claim
    # this service reads, so both tokens address one settings set. A second audience in
    # the family stands in for "a different profile" -- it is the only thing about the
    # token that could differ, and it changes nothing.
    personal = token(namespace=None)
    work = token(namespace="spotify")

    await set_setting(client, personal, "spotify", "default_market", "PT")

    seen_by_work = (await client.get("/v1/settings/spotify", headers=auth(work))).json()
    assert seen_by_work["settings"]["spotify"]["default_market"] == "PT"
    assert seen_by_work["revision"] == 1

    await set_setting(client, work, "spotify", "max_batch_size", 10)
    seen_by_personal = (await client.get("/v1/settings/spotify", headers=auth(personal))).json()
    assert seen_by_personal["settings"]["spotify"] == {
        "default_market": "PT",
        "max_batch_size": 10,
        "confirm_timeout_seconds": 15,
        "job_retention_hours": 1,
    }
    assert seen_by_personal["revision"] == 2


async def test_two_accounts_are_completely_isolated_on_every_read_route(
    client: AsyncClient,
) -> None:
    mine = token(ACCOUNT)
    theirs = token(OTHER_ACCOUNT)
    await set_setting(client, mine, "spotify", "default_market", "PT")
    await set_setting(client, mine, "common", "timezone", "Europe/Lisbon")

    whole = (await client.get("/v1/settings", headers=auth(theirs))).json()
    assert whole["revision"] == 0
    assert whole["settings"]["spotify"]["default_market"] is None
    assert whole["settings"]["common"]["timezone"] == "UTC"

    one = (await client.get("/v1/settings/spotify/default_market", headers=auth(theirs))).json()
    assert one["set"] is False

    schema = (await client.get("/v1/settings/schema", headers=auth(theirs))).json()
    assert all(entry["set"] is False for entry in schema["settings"])

    assert (await client.get("/v1/settings/export", headers=auth(theirs))).json()["settings"] == {}
    assert (await client.get("/v1/settings/events", headers=auth(theirs))).json()["events"] == []

    resolved = await client.get(
        "/v1/internal/settings/spotify",
        headers=service_auth(SPOTIFY_TOKEN, mint(account_id=OTHER_ACCOUNT, audience="spotify")),
    )
    assert resolved.json()["settings"]["default_market"] is None
    assert resolved.headers["ETag"] == f'"{OTHER_ACCOUNT}.0"'


async def test_a_change_is_visible_to_a_service_on_its_next_revalidation(
    client: AsyncClient,
) -> None:
    spotify = service_auth(SPOTIFY_TOKEN, mint(audience="spotify"))
    first = await client.get("/v1/internal/settings/spotify", headers=spotify)
    etag = first.headers["ETag"]

    cached = await client.get(
        "/v1/internal/settings/spotify", headers={**spotify, "If-None-Match": etag}
    )
    assert cached.status_code == 304

    await set_setting(client, token(), "spotify", "default_market", "GB")

    revalidated = await client.get(
        "/v1/internal/settings/spotify", headers={**spotify, "If-None-Match": etag}
    )
    assert revalidated.status_code == 200
    assert revalidated.json()["settings"]["default_market"] == "GB"
    assert revalidated.headers["ETag"] != etag
