"""Typed, validated project configuration loaded from YAML."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml


class ConfigurationError(ValueError):
    """Raised when a configuration file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    timezone: str
    data_directory: Path


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str
    echo: bool = False


@dataclass(frozen=True, slots=True)
class ExchangeSettings:
    name: str
    adapter: str
    enabled: bool
    rate_limit_per_second: float
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Settings:
    version: int
    project: ProjectSettings
    database: DatabaseSettings
    exchanges: Mapping[str, ExchangeSettings]
    source_path: Path | None


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"'{field_name}' must be a mapping")
    return value


def _resolve_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _resolve_database_url(value: str, project_root: Path) -> str:
    # Keep configuration independent of the ORM. SQLAlchemy validates non-SQLite
    # URLs when Database creates an engine; only portable local paths need work here.
    match = re.match(r"^(sqlite(?:\+[A-Za-z0-9_]+)?:///)(.+)$", value)
    if not match:
        return value
    prefix, database_path = match.groups()
    if database_path == ":memory:" or Path(database_path).is_absolute():
        return value
    absolute_path = (project_root / database_path).resolve().as_posix()
    return f"{prefix}{absolute_path}"


def _read_configuration(
    path: str | Path | None,
) -> tuple[str, Path | None, Path]:
    """Return YAML text, its optional filesystem source, and its path base."""

    requested_path: str | Path | None = path
    if requested_path is None:
        environment_path = os.getenv("WARTOSC_CONFIG_PATH")
        if environment_path:
            requested_path = environment_path

    if requested_path is not None:
        source_path = Path(requested_path).expanduser().resolve()
        if not source_path.is_file():
            raise ConfigurationError(f"Configuration file does not exist: {source_path}")
        try:
            return source_path.read_text(encoding="utf-8"), source_path, source_path.parent
        except OSError as exc:
            raise ConfigurationError(f"Cannot read configuration file: {source_path}") from exc

    resource = resources.files("wartosc_perp_research").joinpath("resources", "exchanges.yaml")
    if not resource.is_file():
        raise ConfigurationError("Packaged default configuration is missing")
    try:
        return resource.read_text(encoding="utf-8"), None, Path.cwd().resolve()
    except OSError as exc:
        raise ConfigurationError("Cannot read packaged default configuration") from exc


def load_settings(path: str | Path | None = None) -> Settings:
    """Load configuration and reject ambiguous or unsafe defaults early."""

    source_text, source_path, project_root = _read_configuration(path)
    source_description = str(source_path) if source_path else "packaged default configuration"

    try:
        document = yaml.safe_load(source_text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {source_description}") from exc

    root = _mapping(document, "root")
    version = root.get("version")
    if version != 1:
        raise ConfigurationError("Only configuration version 1 is supported")

    project_data = _mapping(root.get("project"), "project")
    timezone_name = project_data.get("timezone", "UTC")
    if timezone_name != "UTC":
        raise ConfigurationError("'project.timezone' must be UTC")
    data_directory_value = project_data.get("data_directory", "data")
    if not isinstance(data_directory_value, str) or not data_directory_value.strip():
        raise ConfigurationError("'project.data_directory' must be a non-empty path")

    database_data = _mapping(root.get("database"), "database")
    database_url = os.getenv("WARTOSC_DATABASE_URL", database_data.get("url"))
    if not isinstance(database_url, str) or not database_url.strip():
        raise ConfigurationError("'database.url' must be a non-empty string")
    database_echo = database_data.get("echo", False)
    if not isinstance(database_echo, bool):
        raise ConfigurationError("'database.echo' must be true or false")

    exchange_data = _mapping(root.get("exchanges"), "exchanges")
    exchanges: dict[str, ExchangeSettings] = {}
    for raw_name, raw_settings in exchange_data.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigurationError("Exchange names must be non-empty strings")
        exchange_name = raw_name.strip().lower()
        settings_data = _mapping(raw_settings, f"exchanges.{exchange_name}")
        adapter = settings_data.get("adapter")
        enabled = settings_data.get("enabled", False)
        rate_limit = settings_data.get("rate_limit_per_second", 1)
        options = settings_data.get("options", {})
        if not isinstance(adapter, str) or not adapter.strip():
            raise ConfigurationError(f"'exchanges.{exchange_name}.adapter' is required")
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"'exchanges.{exchange_name}.enabled' must be boolean")
        if (
            isinstance(rate_limit, bool)
            or not isinstance(rate_limit, (int, float))
            or rate_limit <= 0
        ):
            raise ConfigurationError(
                f"'exchanges.{exchange_name}.rate_limit_per_second' must be positive"
            )
        exchanges[exchange_name] = ExchangeSettings(
            name=exchange_name,
            adapter=adapter.strip(),
            enabled=enabled,
            rate_limit_per_second=float(rate_limit),
            options=MappingProxyType(dict(_mapping(options, f"exchanges.{exchange_name}.options"))),
        )

    return Settings(
        version=version,
        project=ProjectSettings(
            timezone=timezone_name,
            data_directory=_resolve_path(data_directory_value, project_root),
        ),
        database=DatabaseSettings(
            url=_resolve_database_url(database_url, project_root),
            echo=database_echo,
        ),
        exchanges=MappingProxyType(exchanges),
        source_path=source_path,
    )
