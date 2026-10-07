from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

FAMILY_ENV = (
    "FAMILY_MODE_ENABLED", "TELEGRAM_BOT_USERNAME", "FAMILY_INVITE_TTL_HOURS", "FAMILY_CONSENT_TTL_HOURS",
    "FAMILY_MAX_HELPERS", "FAMILY_MAX_PENDING_INVITES", "FAMILY_ALERT_MAX_PER_HOUR", "FAMILY_ALERT_DEDUPE_MINUTES",
    "FAMILY_ALERT_RETENTION_DAYS", "EMERGENCY_DEDUPE_SECONDS", "EMERGENCY_BUTTON_TTL_HOURS",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in FAMILY_ENV:
        monkeypatch.delenv(name, raising=False)


def example_keys() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


# --- defaults ---------------------------------------------------------------------------------------------


def test_family_mode_is_off_by_default():
    assert Settings(_env_file=None).family_mode_enabled is False


def test_defaults_match_the_approved_plan():
    s = Settings(_env_file=None)
    assert s.telegram_bot_username is None
    assert s.family_invite_ttl_hours == 48
    assert s.family_consent_ttl_hours == 24
    assert s.family_max_helpers == 5
    assert s.family_max_pending_invites == 3
    assert s.family_alert_max_per_hour == 5
    assert s.family_alert_dedupe_minutes == 30
    assert s.family_alert_retention_days == 30
    assert s.emergency_dedupe_seconds == 120
    assert s.emergency_button_ttl_hours == 24


def test_default_database_is_a_local_sqlite_file():
    assert Settings(_env_file=None).database_url == "sqlite:///./ruko.db"


# --- reading from the environment -------------------------------------------------------------------------------------


def test_settings_are_read_from_the_environment(monkeypatch):
    for name, value in {
        "FAMILY_MODE_ENABLED": "true", "TELEGRAM_BOT_USERNAME": "ruko_helper_bot", "FAMILY_INVITE_TTL_HOURS": "12",
        "FAMILY_CONSENT_TTL_HOURS": "6", "FAMILY_MAX_HELPERS": "2", "FAMILY_MAX_PENDING_INVITES": "1",
        "FAMILY_ALERT_MAX_PER_HOUR": "9", "FAMILY_ALERT_DEDUPE_MINUTES": "10", "FAMILY_ALERT_RETENTION_DAYS": "7",
        "EMERGENCY_DEDUPE_SECONDS": "60", "EMERGENCY_BUTTON_TTL_HOURS": "2",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)
    assert s.family_mode_enabled is True and s.telegram_bot_username == "ruko_helper_bot"
    assert (s.family_invite_ttl_hours, s.family_consent_ttl_hours, s.family_max_helpers) == (12, 6, 2)
    assert (s.family_max_pending_invites, s.family_alert_max_per_hour, s.family_alert_dedupe_minutes) == (1, 9, 10)
    assert (s.family_alert_retention_days, s.emergency_dedupe_seconds, s.emergency_button_ttl_hours) == (7, 60, 2)


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_flag_accepts_the_usual_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FAMILY_MODE_ENABLED", raw)
    assert Settings(_env_file=None).family_mode_enabled is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
def test_flag_accepts_the_usual_falsy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FAMILY_MODE_ENABLED", raw)
    if raw == "":  # an empty value is not a valid boolean: refuse rather than guess
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
    else:
        assert Settings(_env_file=None).family_mode_enabled is False


def test_a_typo_in_the_flag_is_an_error_not_silently_off(monkeypatch):
    monkeypatch.setenv("FAMILY_MODE_ENABLED", "maybe")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- bot username ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ruko_dev_bot", "ruko_dev_bot"), ("@ruko_dev_bot", "ruko_dev_bot"), ("  @Ruko_Dev_Bot  ", "Ruko_Dev_Bot"),
     ("", None), ("   ", None), (None, None)],
)  # fmt: skip
def test_bot_username_is_normalised(raw, expected):
    assert Settings(_env_file=None, telegram_bot_username=raw).telegram_bot_username == expected


@pytest.mark.parametrize("bad", ["abc", "a" * 33, "has space", "dash-ed_bot", "bot?start=x", "https://t.me/x_bot", "bot/../x", "ünïcode_bot"])
def test_bad_bot_usernames_are_rejected(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_bot_username=bad)


# --- validation ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["family_invite_ttl_hours", "family_consent_ttl_hours", "family_alert_dedupe_minutes", "family_alert_retention_days",
     "emergency_dedupe_seconds", "emergency_button_ttl_hours"],
)  # fmt: skip
@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_durations_must_be_positive(name, bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: bad})


@pytest.mark.parametrize("name", ["family_max_helpers", "family_max_pending_invites", "family_alert_max_per_hour"])
@pytest.mark.parametrize("bad", [0, -2])
def test_limits_must_be_at_least_one(name, bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: bad})


# --- .env.example stays in sync (a project rule) ----------------------------------------------------------------------------------


def test_env_example_lists_exactly_the_settings_the_code_reads():
    expected = {name.upper() for name in Settings.model_fields}
    assert set(example_keys()) == expected


def test_every_family_setting_is_documented_in_env_example():
    assert set(FAMILY_ENV) <= set(example_keys())


def test_env_example_does_not_switch_family_mode_on():
    assert example_keys()["FAMILY_MODE_ENABLED"].lower() == "false"


def test_loading_env_example_gives_the_defaults():
    """Copying .env.example to .env must not change behaviour from the defaults."""
    from_example = Settings(_env_file=str(ENV_EXAMPLE))
    defaults = Settings(_env_file=None)
    for name in Settings.model_fields:
        a, b = getattr(from_example, name), getattr(defaults, name)
        if name == "llm_api_key":  # Phase 1 quirk: an empty value loads as an empty secret, not None
            a, b = (a.get_secret_value() or None) if a else None, (b.get_secret_value() or None) if b else None
        assert a == b, f"{name}: .env.example gives {a!r} but the default is {b!r}"


def test_env_example_contains_no_secrets():
    for key, value in example_keys().items():
        if key.endswith(("_TOKEN", "_SECRET", "_API_KEY")):  # (LLM_MAX_TOKENS is a number, not a secret)
            assert value == "", f"{key} must be empty in .env.example"
    assert {"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "LLM_API_KEY"} <= set(example_keys())
