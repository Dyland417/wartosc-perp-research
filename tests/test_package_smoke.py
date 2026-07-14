from importlib import resources
from pathlib import Path

import pytest

from wartosc_perp_research.config import load_settings


def test_installed_package_can_load_default_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the resource path used by a non-editable package installation."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WARTOSC_CONFIG_PATH", raising=False)
    monkeypatch.delenv("WARTOSC_DATABASE_URL", raising=False)

    resource = resources.files("wartosc_perp_research").joinpath("resources", "exchanges.yaml")
    settings = load_settings()

    assert resource.is_file()
    assert settings.source_path is None
    assert settings.project.data_directory == tmp_path / "data"
    assert settings.database.url.endswith("/data/wartosc.db")
    assert set(settings.exchanges) == {"hyperliquid", "binance", "variational", "lighter"}
