"""Configuration resolution: api_key/base_url from arguments vs. environment
variables, and fail-fast validation.
"""

from __future__ import annotations

import pytest

from vigil import Vigil
from vigil.exceptions import VigilConfigurationError


def test_missing_api_key_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIGIL_API_KEY", raising=False)
    with pytest.raises(VigilConfigurationError):
        Vigil()


def test_api_key_is_read_from_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_API_KEY", "vgl_env.secret")
    vigil = Vigil(base_url="http://vigil.test")
    try:
        assert vigil._http.headers["authorization"] == "Bearer vgl_env.secret"
    finally:
        vigil.close()


def test_explicit_api_key_overrides_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_API_KEY", "vgl_env.secret")
    vigil = Vigil(api_key="vgl_explicit.secret", base_url="http://vigil.test")
    try:
        assert vigil._http.headers["authorization"] == "Bearer vgl_explicit.secret"
    finally:
        vigil.close()


def test_base_url_is_read_from_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_BASE_URL", "http://env-configured.test")
    vigil = Vigil(api_key="vgl_x.y")
    try:
        assert str(vigil._http.base_url) == "http://env-configured.test"
    finally:
        vigil.close()


def test_missing_base_url_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # There is deliberately no default base_url -- Vigil has no public
    # hosted ingestion endpoint yet, so a placeholder default could be
    # mistaken for one. base_url is required, exactly like api_key.
    monkeypatch.delenv("VIGIL_BASE_URL", raising=False)
    with pytest.raises(VigilConfigurationError):
        Vigil(api_key="vgl_x.y")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_batch_size": 0},
        {"max_queue_size": 0},
        {"flush_interval": 0},
        {"max_retries": -1},
    ],
)
def test_invalid_batching_config_raises_configuration_error(kwargs) -> None:
    with pytest.raises(VigilConfigurationError):
        Vigil(api_key="vgl_x.y", base_url="http://vigil.test", **kwargs)
