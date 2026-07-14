from importlib import resources
from pathlib import Path

import pytest

from wartosc_perp_research.config import ConfigurationError, load_settings

DEFAULT_CONFIG = resources.files("wartosc_perp_research").joinpath("resources", "exchanges.yaml")


def test_load_explicit_settings_resolves_paths_and_disables_adapters(tmp_path: Path) -> None:
    config_path = tmp_path / "exchanges.yaml"
    config_path.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.version == 1
    assert settings.project.timezone == "UTC"
    assert settings.project.data_directory == tmp_path / "data"
    assert settings.database.url.endswith("/data/wartosc.db")
    assert set(settings.exchanges) == {"hyperliquid", "binance", "variational", "lighter"}
    assert not any(exchange.enabled for exchange in settings.exchanges.values())
    assert settings.source_path == config_path


def test_database_environment_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WARTOSC_CONFIG_PATH", raising=False)
    monkeypatch.setenv("WARTOSC_DATABASE_URL", "sqlite:///:memory:")

    settings = load_settings()

    assert settings.database.url == "sqlite:///:memory:"


def test_non_utc_configuration_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "exchanges.yaml"
    path.write_text(
        """
version: 1
project:
  timezone: America/New_York
database:
  url: "sqlite:///:memory:"
exchanges: {}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="must be UTC"):
        load_settings(path)
