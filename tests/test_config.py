from __future__ import annotations

from pathlib import Path

import pytest

from app.main import check_settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_valid_settings_pass(settings):
    check_settings(settings)


def test_missing_settings_are_reported(settings):
    with pytest.raises(RuntimeError, match="missing required configuration.*oidc_issuer"):
        check_settings(settings.model_copy(update={"oidc_issuer": ""}))


@pytest.mark.parametrize("placeholder", ["change-me", "CHANGE-ME", "changeme", " change_me "])
def test_placeholder_session_secret_is_refused(settings, placeholder):
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        check_settings(settings.model_copy(update={"session_secret": placeholder}))


def test_short_session_secret_is_refused(settings):
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        check_settings(settings.model_copy(update={"session_secret": "too-short"}))


def test_placeholder_client_secret_is_refused(settings):
    with pytest.raises(RuntimeError, match="OIDC_CLIENT_SECRET"):
        check_settings(settings.model_copy(update={"oidc_client_secret": "change-me"}))


def test_env_example_is_not_a_working_configuration(settings):
    """Copying .env.example without editing it must fail loudly, not boot."""
    values = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        check_settings(settings.model_copy(update={"session_secret": values["SESSION_SECRET"]}))
