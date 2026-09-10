"""Layered, strict configuration loading for Streamgate."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlparse


_KNOWN_OPTIONS = {
    "runtime": {"environment", "workers", "shutdown_grace_seconds"},
    "delivery": {"url", "request_timeout_seconds", "max_inflight"},
    "security": {"verify_tls", "token"},
    "storage": {"spool_dir"},
}
_ENV_REFERENCE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


class ConfigError(ValueError):
    """Raised with all configuration problems found in one preflight."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        rendered = "invalid configuration:\n" + "\n".join(
            f"- {problem}" for problem in problems
        )
        super().__init__(rendered)


@dataclass(frozen=True)
class RuntimeSettings:
    environment: str
    workers: int
    shutdown_grace_seconds: int


@dataclass(frozen=True)
class DeliverySettings:
    url: str
    request_timeout_seconds: int
    max_inflight: int


@dataclass(frozen=True)
class SecuritySettings:
    verify_tls: bool
    token: str


@dataclass(frozen=True)
class StorageSettings:
    # Deployment mount syntax is POSIX regardless of the machine running the
    # offline preflight (the reference operator is Windows-native).
    spool_dir: PurePosixPath


@dataclass(frozen=True)
class Settings:
    runtime: RuntimeSettings
    delivery: DeliverySettings
    security: SecuritySettings
    storage: StorageSettings


def _integer(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    problems: list[str],
) -> int:
    try:
        return parser.getint(section, option)
    except (configparser.Error, ValueError) as exc:
        problems.append(f"{section}.{option} must be an integer ({exc})")
        return 0


def _boolean(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    problems: list[str],
) -> bool:
    try:
        return parser.getboolean(section, option)
    except (configparser.Error, ValueError) as exc:
        problems.append(f"{section}.{option} must be a boolean ({exc})")
        return False


def _resolve_secret(raw: str, environ: Mapping[str, str], problems: list[str]) -> str:
    match = _ENV_REFERENCE.fullmatch(raw.strip())
    if not match:
        return raw.strip()
    name = match.group(1)
    value = environ.get(name, "")
    if not value:
        problems.append(f"security.token references missing environment variable {name}")
    return value


def load_config(
    base_path: str | Path,
    overlay_path: str | Path,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load a base file and overlay, then validate their effective settings."""

    parser = configparser.ConfigParser(interpolation=None)
    paths = [str(Path(base_path)), str(Path(overlay_path))]
    loaded = parser.read(paths, encoding="utf-8")
    if loaded != paths:
        missing = [path for path in paths if path not in loaded]
        raise ConfigError([f"configuration file not found: {path}" for path in missing])

    problems: list[str] = []
    for section in parser.sections():
        if section not in _KNOWN_OPTIONS:
            problems.append(f"unknown section [{section}]")
            continue
        for option in parser.options(section):
            if option not in _KNOWN_OPTIONS[section]:
                problems.append(f"unknown option {section}.{option}")
    for section in _KNOWN_OPTIONS:
        if not parser.has_section(section):
            problems.append(f"missing section [{section}]")

    if problems:
        raise ConfigError(problems)

    environment = parser.get("runtime", "environment").strip().lower()
    workers = _integer(parser, "runtime", "workers", problems)
    shutdown_grace = _integer(
        parser, "runtime", "shutdown_grace_seconds", problems
    )
    url = parser.get("delivery", "url").strip()
    request_timeout = _integer(
        parser, "delivery", "request_timeout_seconds", problems
    )
    max_inflight = _integer(parser, "delivery", "max_inflight", problems)
    verify_tls = _boolean(parser, "security", "verify_tls", problems)
    token = _resolve_secret(
        parser.get("security", "token"), environ or os.environ, problems
    )
    spool_dir = PurePosixPath(parser.get("storage", "spool_dir").strip())

    if environment not in {"development", "staging", "production"}:
        problems.append("runtime.environment must be development, staging, or production")
    if not 1 <= workers <= 32:
        problems.append("runtime.workers must be between 1 and 32")
    if request_timeout < 1:
        problems.append("delivery.request_timeout_seconds must be positive")
    if shutdown_grace <= request_timeout:
        problems.append(
            "runtime.shutdown_grace_seconds must be greater than "
            "delivery.request_timeout_seconds"
        )
    if max_inflight < workers:
        problems.append("delivery.max_inflight cannot be lower than runtime.workers")
    if max_inflight > workers * 64:
        problems.append(
            f"delivery.max_inflight={max_inflight} exceeds worker capacity {workers * 64}"
        )

    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        problems.append("delivery.url must be an absolute HTTP(S) URL")

    if environment == "production":
        if parsed_url.scheme != "https":
            problems.append("production delivery.url must use https")
        if not verify_tls:
            problems.append("security.verify_tls must be enabled in production")
        if not token:
            problems.append("security.token is required in production")
        if not spool_dir.is_absolute():
            problems.append("storage.spool_dir must be absolute in production")

    if problems:
        raise ConfigError(problems)

    return Settings(
        runtime=RuntimeSettings(environment, workers, shutdown_grace),
        delivery=DeliverySettings(url, request_timeout, max_inflight),
        security=SecuritySettings(verify_tls, token),
        storage=StorageSettings(spool_dir),
    )
