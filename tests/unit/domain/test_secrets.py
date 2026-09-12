"""The specification for the credential detector: two corpora.

``MUST_ACCEPT`` is what a person must be able to set. ``MUST_REFUSE`` is what must never be
stored here. Change the heuristic and these two lists are what tell you whether you made
it better or just different -- and **``MUST_ACCEPT`` is the one allowed to grow.** A change
that shrinks it is a regression even if it catches more secrets, because there is no
override: a false positive is a value somebody simply cannot set.

**No literal credential is committed in this file.** GitHub push protection blocked a
sibling repository's first push over a realistic Slack token in exactly this kind of
corpus. Every refused shape is assembled from parts by :func:`_shaped`, so no single string
literal here is a plausible real token.
"""

from __future__ import annotations

import pytest

from settings_api.domain import secrets
from settings_api.domain.secrets import (
    MIN_DISTINCT_CHARS,
    MIN_ENTROPY_RUN,
    MIN_PREFIXED_SUFFIX,
    MIN_SHANNON_BITS,
    REQUIRED_CHARACTER_CLASSES,
    looks_like_a_credential,
)


def _shaped(prefix: str, body: str) -> str:
    """A credential-shaped string built from parts, so the literal never exists on disk."""
    return prefix + body


_BODY = "Ab3dEf7hIj9kLm1nOp5qRs8tUv2wXy4z"
"""Thirty-two characters, three classes, high entropy: what follows a real prefix."""

_RANDOM_BLOB = _shaped("Qw9", "zT4mLp2vXk7nRj3bHy6cGd8fSa1eWu5o")
"""Forty base64-legal characters that read as random -- the entropy rule's target."""

MUST_ACCEPT: list[str] = [
    # --- the two real regressions ----------------------------------------------------
    "They are risk-averse with money",  # `sk-` as a bare substring inside "risk-averse"
    "https://en.wikipedia.org/wiki/Lisbon#History_of_the_city_and_surrounding_region",
    # --- prefixes as words, not tokens -----------------------------------------------
    "task-based planning and desk-bound work",
    "disk-encrypted backups every night",
    "Aizawa is a friend from Tokyo",  # `AIza` case-folded would refuse a surname
    "akiaBakia are two made-up words",  # `AKIA` case-folded
    "sk-",  # a bare abbreviation with no suffix
    "sk-short",  # fewer than MIN_PREFIXED_SUFFIX characters follow
    # --- long, alphabet-legal, and not secrets ---------------------------------------
    "3f9a1c7e2b4d6f8a0c1e3b5d7f9a2c4e6b8d0f1a",  # a git SHA
    "#1a2b3c",  # a hex colour
    "#deadbeefcafe",
    "sam.osei-mensah@example-mail.co.uk",  # an email address
    "SW1A 1AA",  # a postcode
    "+44 20 7946 0958",  # a phone number
    "RuaAugusta112Lisboa1100053",  # a Lisbon address with the spaces removed: under 32 characters
    "a-long-lowercase-hyphenated-url-slug-that-goes-on-and-on-and-on",
    "REFERENCE-NUMBER-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0001",
    "aB1aB1aB1aB1aB1aB1aB1aB1aB1aB1aB1aB1aB1aB1",  # mixed case, digits, utterly predictable
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # long and base64-legal and not a secret
    # --- ordinary settings values ------------------------------------------------------
    "Europe/Lisbon",
    "en-GB",
    "anthropic:claude-opus-5",
    "personal",
    "",
    "1.2.3-alpha.4",  # three dot-separated segments that are not a JWT
    "en.wikipedia.org",
    "The quick brown fox jumps over the lazy dog and keeps running for a while.",
]

MUST_REFUSE: list[tuple[str, str]] = [
    (_shaped("sk-", _BODY), "an API key"),
    (_shaped("ghp_", _BODY), "a GitHub personal access token"),
    (_shaped("gho_", _BODY), "a GitHub OAuth token"),
    (_shaped("ghs_", _BODY), "a GitHub server token"),
    (_shaped("github_pat_", _BODY), "a GitHub personal access token"),
    (_shaped("xoxb-", _BODY), "a Slack bot token"),
    (_shaped("xoxa-", _BODY), "a Slack app token"),
    (_shaped("xoxp-", _BODY), "a Slack user token"),
    (_shaped("xoxs-", _BODY), "a Slack workspace token"),
    (_shaped("xoxr-", _BODY), "a Slack refresh token"),
    (_shaped("AKIA", _BODY), "an AWS access key id"),
    (_shaped("ASIA", _BODY), "an AWS temporary access key id"),
    (_shaped("AIza", _BODY), "a Google API key"),
    (_shaped("-----BEGIN ", "RSA PRIVATE KEY-----"), "a private key"),
    (_shaped("-----BEGIN ", "PRIVATE KEY-----\nMIIE"), "a private key"),
    (
        _shaped("eyJ", "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456"),
        "a JSON Web Token",
    ),
    (_RANDOM_BLOB, "a long high-entropy string"),
    (f"my key is {_shaped('sk-', _BODY)} please keep it", "an API key"),  # embedded in prose
    (f"token:{_shaped('ghp_', _BODY)}", "a GitHub personal access token"),  # after punctuation
]


@pytest.mark.parametrize("text", MUST_ACCEPT, ids=lambda text: text[:40])
def test_must_accept(text: str) -> None:
    assert looks_like_a_credential(text) is None, f"{text!r} is not a credential and was refused"


@pytest.mark.parametrize(("text", "kind"), MUST_REFUSE, ids=lambda value: str(value)[:24])
def test_must_refuse(text: str, kind: str) -> None:
    reason = looks_like_a_credential(text)
    assert reason is not None, f"{kind} was not refused"
    assert kind in reason


class TestTheReasonNeverEchoesTheText:
    @pytest.mark.parametrize(("text", "_kind"), MUST_REFUSE)
    def test_no_refused_string_appears_in_its_own_refusal(self, text: str, _kind: str) -> None:
        reason = looks_like_a_credential(text)
        assert reason is not None
        # The whole point is to keep the value out of the store; echoing it into an error
        # body and a log line would defeat that at the moment of success.
        assert _BODY not in reason
        assert _RANDOM_BLOB not in reason
        assert text.strip() not in reason


class TestThePrefixRules:
    def test_a_prefix_must_begin_a_token(self) -> None:
        # Preceded by an alphanumeric, it is the tail of a word rather than a prefix. The
        # body is kept short so the whole run stays under the entropy rule's length and
        # only the prefix rule is being tested.
        body = _BODY[:20]
        assert looks_like_a_credential(_shaped("risk-", body)) is None
        assert looks_like_a_credential(_shaped("xsk-", body)) is None
        assert looks_like_a_credential(_shaped("sk-", body)) is not None

    def test_a_prefix_may_follow_punctuation_or_whitespace(self) -> None:
        assert looks_like_a_credential(_shaped("(sk-", _BODY)) is not None
        assert looks_like_a_credential(_shaped(" sk-", _BODY)) is not None

    def test_the_suffix_must_be_long_enough(self) -> None:
        short = "a" * (MIN_PREFIXED_SUFFIX - 1)
        enough = "a" * MIN_PREFIXED_SUFFIX
        assert looks_like_a_credential(_shaped("ghp_", short)) is None
        assert looks_like_a_credential(_shaped("ghp_", enough)) is not None

    def test_case_matters(self) -> None:
        # `AIza` is a Google key and `aiza` is the start of a surname.
        body = _BODY[:20]
        assert looks_like_a_credential(_shaped("aiza", body)) is None
        assert looks_like_a_credential(_shaped("GHP_", body)) is None
        assert looks_like_a_credential(_shaped("AIza", body)) is not None


class TestTheEntropyRule:
    def test_a_run_shorter_than_the_minimum_is_never_a_secret(self) -> None:
        # The length gate is the regex that finds runs, not the entropy check itself: a
        # 31-character blob reads as random and is still accepted, because at that length
        # the false-positive rate stops being worth it.
        short = _RANDOM_BLOB[: MIN_ENTROPY_RUN - 1]
        assert secrets._is_high_entropy(short) is True
        assert looks_like_a_credential(short) is None

    def test_a_character_outside_the_alphabet_exempts_the_run(self) -> None:
        # This is also the URL exemption: a colon, a dot or an at-sign is not in the
        # credential alphabet, so a URL never reaches the rule.
        assert secrets._is_high_entropy(_RANDOM_BLOB + ":") is False
        assert secrets._is_high_entropy(_RANDOM_BLOB + "@") is False

    def test_hex_shapes_are_exempt(self) -> None:
        assert secrets._is_high_entropy("3f9a1c7e2b4d6f8a0c1e3b5d7f9a2c4e6b8d0f1a") is False
        assert secrets._is_high_entropy("#" + "a1" * 4) is False

    def test_too_few_distinct_characters_is_not_random(self) -> None:
        run = ("aB1" * 20)[:MIN_ENTROPY_RUN]
        assert len(set(run)) < MIN_DISTINCT_CHARS
        assert secrets._is_high_entropy(run) is False

    def test_fewer_than_three_character_classes_is_not_a_token(self) -> None:
        lower_and_digits = "abcdefghijklmnopqrstuvwxyz012345"
        assert len(set(lower_and_digits)) >= MIN_DISTINCT_CHARS
        assert secrets._is_high_entropy(lower_and_digits) is False
        assert REQUIRED_CHARACTER_CLASSES == 3

    def test_the_shannon_threshold_sits_between_prose_and_random(self) -> None:
        assert secrets._shannon_bits("aaaa") == 0.0
        assert secrets._shannon_bits(_RANDOM_BLOB) >= MIN_SHANNON_BITS
        # Sixteen distinct characters each used once: exactly four bits, comfortably over.
        assert secrets._shannon_bits("abcdefghijklmnop") == 4.0

    def test_the_blob_passes_every_gate(self) -> None:
        assert secrets._is_high_entropy(_RANDOM_BLOB) is True


class TestTheCorporaThemselves:
    def test_no_literal_credential_is_committed(self) -> None:
        # Every refused string is built by `_shaped`, so grep over this file for a real
        # prefix immediately followed by a body finds nothing. Checked here so a future
        # test cannot quietly paste one in.
        from pathlib import Path

        source = Path(__file__).read_text()
        for prefix, _ in secrets._PREFIXES:
            assert f'"{prefix}{_BODY[:8]}' not in source, (
                f"a literal {prefix} token is in the corpus"
            )

    def test_the_accept_corpus_is_not_shrinking(self) -> None:
        # Somebody deleting an entry to make a heuristic change pass is the regression
        # this file's docstring warns about. The number only goes up.
        assert len(MUST_ACCEPT) >= 27
