import base64
import hashlib
import re

import pytest

from app.family import tokens
from app.family.tokens import (
    INVITE_PREFIX,
    hash_token,
    is_valid_ref,
    is_valid_token,
    new_invite_token,
    new_ref,
    parse_start_payload,
    start_payload,
)

TELEGRAM_START_PAYLOAD_LIMIT = 64


def test_token_is_128_bits_of_url_safe_randomness():
    token = new_invite_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", token)
    raw = base64.urlsafe_b64decode(token + "==")
    assert len(raw) == 16  # 128 bits


def test_tokens_do_not_repeat():
    assert len({new_invite_token() for _ in range(2000)}) == 2000


def test_token_bytes_look_random():
    # Not a statistical test, just a guard against a constant or counter.
    sample = b"".join(base64.urlsafe_b64decode(new_invite_token() + "==") for _ in range(64))
    assert len(set(sample)) > 200


def test_hash_is_sha256_hex_and_does_not_contain_the_token():
    token = new_invite_token()
    digest = hash_token(token)
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert len(digest) == 64 and re.fullmatch(r"[0-9a-f]{64}", digest)
    assert token not in digest
    assert hash_token(token) == digest  # deterministic, so it can be looked up
    assert hash_token(new_invite_token()) != digest


def test_hashes_fit_the_database_column():
    from app.db.models import FamilyLink

    assert len(hash_token(new_invite_token())) <= FamilyLink.__table__.c.token_hash.type.length


# --- deep link payload --------------------------------------------------------------------------------------


def test_start_payload_round_trip_and_fits_telegram_limit():
    token = new_invite_token()
    payload = start_payload(token)
    assert payload == f"{INVITE_PREFIX}{token}" and payload.startswith("fam_")
    assert len(payload) <= TELEGRAM_START_PAYLOAD_LIMIT
    assert re.fullmatch(r"[A-Za-z0-9_-]+", payload)  # the characters Telegram allows
    assert parse_start_payload(payload) == token


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "fam_",
        "fam_short",
        "fam_" + "a" * 21,
        "fam_" + "a" * 23,
        "FAM_" + "a" * 22,
        "xyz_" + "a" * 22,
        "a" * 22,
        "fam_" + "a" * 21 + "!",
        "fam_" + "a" * 21 + " ",
        " fam_" + "a" * 22,
        "fam_" + "a" * 22 + "\n",
        "fam_" + "a" * 22 + "&x=1",
        "fam_" + "é" * 22,
    ],
)
def test_malformed_payloads_are_rejected(payload):
    assert parse_start_payload(payload) is None


def test_start_payload_refuses_to_wrap_a_bad_token():
    for bad in ("", "short", "a" * 23, "a" * 21 + "?"):
        with pytest.raises(ValueError):
            start_payload(bad)


@pytest.mark.parametrize("value", [None, 123, b"a" * 22, ["a" * 22], "a" * 22 + "\n", "a" * 21])
def test_is_valid_token_rejects_non_tokens(value):
    assert not is_valid_token(value)


def test_is_valid_token_accepts_generated_tokens():
    assert all(is_valid_token(new_invite_token()) for _ in range(100))


# --- refs --------------------------------------------------------------------------------------------------------


def test_refs_are_random_16_char_hex_and_fit_the_column():
    from app.db.models import FamilyLink

    ref = new_ref()
    assert re.fullmatch(r"[0-9a-f]{16}", ref)
    assert len(ref) <= FamilyLink.__table__.c.ref.type.length
    assert len({new_ref() for _ in range(2000)}) == 2000


def test_refs_are_not_sequential():
    refs = [new_ref() for _ in range(20)]
    assert refs != sorted(refs) or len(set(refs)) == 20
    assert not any(int(a, 16) + 1 == int(b, 16) for a, b in zip(refs, refs[1:]))


@pytest.mark.parametrize("value", [None, 5, "", "A" * 16, "g" * 16, "a" * 15, "a" * 17, "a" * 16 + "\n", b"a" * 16])
def test_is_valid_ref_rejects(value):
    assert not is_valid_ref(value)


def test_is_valid_ref_accepts_generated_refs():
    assert all(is_valid_ref(new_ref()) for _ in range(100))


def test_module_exposes_no_way_to_recover_a_token_from_its_hash():
    public = {name for name in dir(tokens) if not name.startswith("_")}
    assert not {name for name in public if "unhash" in name or "decode" in name or "recover" in name}
