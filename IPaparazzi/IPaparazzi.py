#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import tomllib


EXIT_OK = 0
EXIT_NO_CONSENSUS = 1
EXIT_CONFIG_ERROR = 2
EXIT_PROVIDER_ERROR = 3
EXIT_NOTIFICATION_ERROR = 4
EXIT_RUNTIME_ERROR = 5
EXIT_ALREADY_RUNNING = 6

USER_AGENT = "IPaparazzi/1.0"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True)
class IpSource:
    name: str
    url: str
    response_format: str = "plain"


IP_SOURCES = (
    IpSource("cloudflare", "https://1.1.1.1/cdn-cgi/trace", "cloudflare-trace"),
    IpSource("aws", "https://checkip.amazonaws.com/"),
    IpSource("ipify", "https://api.ipify.org/"),
)


@dataclass(frozen=True)
class SourceResult:
    source: str
    ip: str | None
    error: str | None = None


@dataclass(frozen=True)
class RecordConfig:
    zone_id: str
    name: str
    proxied: bool
    ttl: int
    enabled: bool = True


@dataclass(frozen=True)
class AccountConfig:
    name: str
    api_token: str
    records: tuple[RecordConfig, ...]
    enabled: bool = True


@dataclass(frozen=True)
class NotificationEventConfig:
    category: str
    title: str
    message: str
    priority: str | None = None


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool
    binary: str
    sender: str
    config_path: str | None
    events: dict[str, NotificationEventConfig]


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    state_file: Path
    log_file: Path
    lock_file: Path
    reconcile_hours: int
    request_timeout_seconds: int
    request_retries: int
    retry_delay_seconds: float
    lock_stale_minutes: int
    log_max_bytes: int
    log_backup_count: int
    accounts: tuple[AccountConfig, ...]
    notifications: NotificationConfig


@dataclass(frozen=True)
class RecordOutcome:
    key: str
    account: str
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class RunSummary:
    public_ip: str | None
    source_results: tuple[SourceResult, ...]
    outcomes: tuple[RecordOutcome, ...]
    global_errors: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[str, ...]:
        record_errors = tuple(
            f"{item.account}/{item.name}: {item.detail}"
            for item in self.outcomes
            if item.status == "error"
        )
        return self.global_errors + record_errors

    @property
    def updated_count(self) -> int:
        return sum(item.status == "updated" for item in self.outcomes)


class ConfigError(RuntimeError):
    pass


class PublicIpError(RuntimeError):
    def __init__(self, message: str, results: tuple[SourceResult, ...]) -> None:
        super().__init__(message)
        self.results = results


class ProviderError(RuntimeError):
    pass


class AlreadyRunningError(RuntimeError):
    pass


HttpFetcher = Callable[[str, int], str]


def as_table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def as_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be an array")
    return value


def as_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def as_bool(value: Any, name: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def as_int(
    value: Any,
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if value is None and default is not None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    else:
        result = value
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return result


def as_float(
    value: Any,
    name: str,
    *,
    default: float,
    minimum: float = 0.0,
) -> float:
    if value is None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    else:
        result = float(value)
    if result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return result


def resolve_local_path(config_path: Path, value: Any, default_name: str) -> Path:
    raw = as_str(value if value is not None else default_name, default_name)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def parse_notification_event(
    notifications: dict[str, Any],
    event: str,
) -> NotificationEventConfig:
    raw = as_table(notifications.get(event, {}), f"global.notifications.{event}")
    defaults = {
        "changed": ("SUCCESS", "IPaparazzi atualizou o DNS", "{report}", None),
        "error": ("FAIL", "IPaparazzi falhou", "{report}", "HIGH"),
        "recovered": ("SUCCESS", "IPaparazzi recuperado", "{report}", None),
    }
    category, title, message, priority = defaults[event]
    configured_priority = raw.get("priority", priority)
    if configured_priority is not None:
        configured_priority = as_str(configured_priority, f"notifications.{event}.priority")
    result = NotificationEventConfig(
        category=as_str(raw.get("category", category), f"notifications.{event}.category"),
        title=as_str(raw.get("title", title), f"notifications.{event}.title"),
        message=as_str(raw.get("message", message), f"notifications.{event}.message"),
        priority=configured_priority,
    )
    validate_notification_template(result.title, f"notifications.{event}.title")
    validate_notification_template(result.message, f"notifications.{event}.message")
    return result


def validate_notification_template(template: str, name: str) -> None:
    try:
        template.format_map({"report": "report", "event": "EVENT"})
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"{name} contains an unsupported placeholder: {exc}") from exc


def parse_notifications(global_cfg: dict[str, Any], config_path: Path) -> NotificationConfig:
    raw = as_table(global_cfg.get("notifications", {}), "global.notifications")
    config_value = raw.get("noticli_config")
    if config_value is not None:
        noticli_path = Path(as_str(config_value, "global.notifications.noticli_config")).expanduser()
        if not noticli_path.is_absolute():
            noticli_path = (config_path.parent / noticli_path).resolve()
        config_value = str(noticli_path)
    return NotificationConfig(
        enabled=as_bool(raw.get("enabled"), "global.notifications.enabled", default=True),
        binary=as_str(raw.get("noticli_bin", "noticli"), "global.notifications.noticli_bin"),
        sender=as_str(raw.get("sender", "IPaparazzi"), "global.notifications.sender"),
        config_path=config_value,
        events={
            event: parse_notification_event(raw, event)
            for event in ("changed", "error", "recovered")
        },
    )


def parse_record(raw_value: Any, account_name: str, index: int) -> RecordConfig:
    raw = as_table(raw_value, f"account {account_name} record {index}")
    name = as_str(raw.get("name"), f"account {account_name} record {index} name")
    normalized_name = name.rstrip(".").lower()
    if "." not in normalized_name:
        raise ConfigError(f"record {name} must be a fully qualified domain name")
    proxied = as_bool(raw.get("proxied"), f"record {name} proxied", default=False)
    ttl = as_int(raw.get("ttl"), f"record {name} ttl", default=1, minimum=1, maximum=86400)
    if 1 < ttl < 60:
        raise ConfigError(f"record {name} ttl must be 1 (Auto) or at least 60 seconds")
    if proxied and ttl != 1:
        raise ConfigError(f"record {name} must use ttl = 1 when proxied = true")
    return RecordConfig(
        zone_id=as_str(raw.get("zone_id"), f"record {name} zone_id"),
        name=normalized_name,
        proxied=proxied,
        ttl=ttl,
        enabled=as_bool(raw.get("enabled"), f"record {name} enabled", default=True),
    )


def parse_accounts(providers: dict[str, Any]) -> tuple[AccountConfig, ...]:
    cloudflare = as_table(providers.get("cloudflare"), "providers.cloudflare")
    raw_accounts = as_list(cloudflare.get("accounts"), "providers.cloudflare.accounts")
    accounts: list[AccountConfig] = []
    account_names: set[str] = set()
    record_keys: set[str] = set()
    for index, raw_value in enumerate(raw_accounts, start=1):
        raw = as_table(raw_value, f"cloudflare account {index}")
        name = as_str(raw.get("name"), f"cloudflare account {index} name")
        if name in account_names:
            raise ConfigError(f"duplicate Cloudflare account name: {name}")
        account_names.add(name)
        enabled = as_bool(raw.get("enabled"), f"Cloudflare account {name} enabled", default=True)
        api_token = as_str(raw.get("api_token"), f"Cloudflare account {name} api_token")
        if enabled and api_token == "CHANGE_ME":
            raise ConfigError(f"Cloudflare account {name} still uses the model api_token")
        records = tuple(
            parse_record(item, name, record_index)
            for record_index, item in enumerate(
                as_list(raw.get("records"), f"Cloudflare account {name} records"),
                start=1,
            )
        )
        if not records:
            raise ConfigError(f"Cloudflare account {name} must contain at least one record")
        for record in records:
            key = record_state_key(name, record)
            if key in record_keys:
                raise ConfigError(f"duplicate record configuration: {name}/{record.name}")
            record_keys.add(key)
        accounts.append(
            AccountConfig(
                name=name,
                api_token=api_token,
                records=records,
                enabled=enabled,
            )
        )
    if not accounts:
        raise ConfigError("providers.cloudflare.accounts must contain at least one account")
    return tuple(accounts)


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc

    root = as_table(data, "config root")
    global_cfg = as_table(root.get("global"), "global")
    providers = as_table(root.get("providers"), "providers")
    return AppConfig(
        config_path=config_path,
        state_file=resolve_local_path(config_path, global_cfg.get("state_file"), "IPaparazzi.state.json"),
        log_file=resolve_local_path(config_path, global_cfg.get("log_file"), "IPaparazzi.log"),
        lock_file=resolve_local_path(config_path, global_cfg.get("lock_file"), "IPaparazzi.lock"),
        reconcile_hours=as_int(
            global_cfg.get("reconcile_hours"),
            "global.reconcile_hours",
            default=24,
            minimum=1,
            maximum=720,
        ),
        request_timeout_seconds=as_int(
            global_cfg.get("request_timeout_seconds"),
            "global.request_timeout_seconds",
            default=10,
            minimum=1,
            maximum=120,
        ),
        request_retries=as_int(
            global_cfg.get("request_retries"),
            "global.request_retries",
            default=2,
            minimum=1,
            maximum=5,
        ),
        retry_delay_seconds=as_float(
            global_cfg.get("retry_delay_seconds"),
            "global.retry_delay_seconds",
            default=1.0,
            minimum=0.0,
        ),
        lock_stale_minutes=as_int(
            global_cfg.get("lock_stale_minutes"),
            "global.lock_stale_minutes",
            default=60,
            minimum=5,
            maximum=1440,
        ),
        log_max_bytes=as_int(
            global_cfg.get("log_max_bytes"),
            "global.log_max_bytes",
            default=5 * 1024 * 1024,
            minimum=1024,
        ),
        log_backup_count=as_int(
            global_cfg.get("log_backup_count"),
            "global.log_backup_count",
            default=5,
            minimum=1,
            maximum=100,
        ),
        accounts=parse_accounts(providers),
        notifications=parse_notifications(global_cfg, config_path),
    )


def setup_logging(config: AppConfig, *, verbose: bool = False) -> logging.Logger:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("IPaparazzi")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def default_http_fetcher(url: str, timeout_seconds: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read(4096).decode("utf-8", errors="strict")


def parse_source_ip(source: IpSource, body: str) -> str:
    if source.response_format == "cloudflare-trace":
        values = {
            key.strip(): value.strip()
            for line in body.splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
        candidate = values.get("ip", "")
    else:
        candidate = body.strip()
    try:
        address = IPv4Address(candidate)
    except ValueError as exc:
        raise PublicIpError(f"{source.name} returned an invalid IPv4", ()) from exc
    if not address.is_global:
        raise PublicIpError(f"{source.name} returned a non-public IPv4", ())
    return str(address)


def fetch_source(
    source: IpSource,
    fetcher: HttpFetcher,
    *,
    timeout_seconds: int,
    retries: int,
    retry_delay_seconds: float,
) -> SourceResult:
    last_error = "unknown failure"
    for attempt in range(1, retries + 1):
        try:
            body = fetcher(source.url, timeout_seconds)
            return SourceResult(source.name, parse_source_ip(source, body))
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, PublicIpError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries and retry_delay_seconds:
                time.sleep(retry_delay_seconds * attempt)
    return SourceResult(source.name, None, last_error)


def discover_public_ipv4(
    *,
    fetcher: HttpFetcher = default_http_fetcher,
    timeout_seconds: int = 10,
    retries: int = 2,
    retry_delay_seconds: float = 1.0,
) -> tuple[str, tuple[SourceResult, ...]]:
    results_by_name: dict[str, SourceResult] = {}
    with ThreadPoolExecutor(max_workers=len(IP_SOURCES)) as executor:
        futures = {
            executor.submit(
                fetch_source,
                source,
                fetcher,
                timeout_seconds=timeout_seconds,
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
            ): source.name
            for source in IP_SOURCES
        }
        for future in as_completed(futures):
            results_by_name[futures[future]] = future.result()
    results = tuple(results_by_name[source.name] for source in IP_SOURCES)
    votes = Counter(item.ip for item in results if item.ip is not None)
    if not votes:
        raise PublicIpError("all public IPv4 sources failed", results)
    selected, count = votes.most_common(1)[0]
    if count < 2:
        raise PublicIpError("public IPv4 sources did not reach two-vote consensus", results)
    return selected, results


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "records": {},
        "meta": {"last_run_had_errors": False},
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RuntimeError(f"unsupported or invalid state file: {path}")
    if not isinstance(data.get("records"), dict) or not isinstance(data.get("meta"), dict):
        raise RuntimeError(f"invalid state structure: {path}")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


@contextmanager
def exclusive_lock(path: Path, stale_minutes: int) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for attempt in range(2):
        try:
            descriptor = os.open(path, flags, 0o600)
            break
        except FileExistsError as exc:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if attempt == 0 and age > stale_minutes * 60:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise AlreadyRunningError(f"another execution holds lock {path}") from exc
    else:
        raise AlreadyRunningError(f"could not acquire lock {path}")

    try:
        payload = json.dumps({"pid": os.getpid(), "created_at": utc_now().isoformat()})
        os.write(descriptor, payload.encode("utf-8"))
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def record_state_key(account_name: str, record: RecordConfig) -> str:
    return f"cloudflare|{account_name}|{record.zone_id}|{record.name}"


def should_reconcile(
    entry: Any,
    public_ip: str,
    *,
    now: datetime,
    reconcile_hours: int,
    force: bool,
    desired_proxied: bool | None = None,
    desired_ttl: int | None = None,
) -> bool:
    if force or not isinstance(entry, dict):
        return True
    if entry.get("last_ip") != public_ip:
        return True
    if desired_proxied is not None and entry.get("proxied") != desired_proxied:
        return True
    if desired_ttl is not None and entry.get("ttl") != desired_ttl:
        return True
    confirmed_at = parse_timestamp(entry.get("confirmed_at"))
    if confirmed_at is None:
        return True
    return now - confirmed_at >= timedelta(hours=reconcile_hours)


class CloudflareClient:
    def __init__(self, api_token: str, timeout_seconds: int) -> None:
        self._api_token = api_token
        self._timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{CLOUDFLARE_API_BASE}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise ProviderError(f"Cloudflare HTTP {exc.code}: {summarize_api_error(detail)}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Cloudflare request failed: {type(exc).__name__}: {exc}") from exc
        try:
            decoded = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderError("Cloudflare returned invalid JSON") from exc
        if not isinstance(decoded, dict) or not decoded.get("success"):
            raise ProviderError(f"Cloudflare API error: {summarize_api_error(response_body)}")
        return decoded.get("result")

    def find_a_record(self, zone_id: str, name: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/zones/{quote(zone_id, safe='')}/dns_records",
            query={"type": "A", "name": name, "per_page": "100"},
        )
        if not isinstance(result, list):
            raise ProviderError("Cloudflare returned an invalid record list")
        exact = [
            item
            for item in result
            if isinstance(item, dict) and item.get("name", "").rstrip(".").lower() == name
        ]
        if not exact:
            raise ProviderError(f"A record does not exist: {name}")
        if len(exact) > 1:
            raise ProviderError(f"multiple A records found for {name}; refusing ambiguous update")
        return exact[0]

    def update_a_record(
        self,
        zone_id: str,
        record_id: str,
        record: RecordConfig,
        public_ip: str,
    ) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            f"/zones/{quote(zone_id, safe='')}/dns_records/{quote(record_id, safe='')}",
            body={
                "type": "A",
                "name": record.name,
                "content": public_ip,
                "ttl": record.ttl,
                "proxied": record.proxied,
            },
        )
        if not isinstance(result, dict):
            raise ProviderError("Cloudflare returned an invalid updated record")
        return result


def summarize_api_error(body: str) -> str:
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return body.replace("\n", " ")[:300] or "empty response"
    errors = decoded.get("errors", []) if isinstance(decoded, dict) else []
    messages: list[str] = []
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                code = item.get("code")
                message = item.get("message")
                messages.append(f"{code}: {message}" if code is not None else str(message))
    return "; ".join(messages)[:300] or "unspecified API error"


def record_needs_update(remote: dict[str, Any], desired: RecordConfig, public_ip: str) -> bool:
    return any(
        (
            remote.get("content") != public_ip,
            remote.get("proxied") != desired.proxied,
            remote.get("ttl") != desired.ttl,
        )
    )


def reconcile_records(
    config: AppConfig,
    state: dict[str, Any],
    public_ip: str,
    *,
    now: datetime,
    force: bool,
    logger: logging.Logger,
    client_factory: Callable[[str, int], CloudflareClient] = CloudflareClient,
) -> tuple[RecordOutcome, ...]:
    outcomes: list[RecordOutcome] = []
    record_state = state["records"]
    for account in config.accounts:
        if not account.enabled:
            logger.info("account=%s status=disabled", account.name)
            continue
        client = client_factory(account.api_token, config.request_timeout_seconds)
        for record in account.records:
            key = record_state_key(account.name, record)
            if not record.enabled:
                logger.info("account=%s record=%s status=disabled", account.name, record.name)
                continue
            if not should_reconcile(
                record_state.get(key),
                public_ip,
                now=now,
                reconcile_hours=config.reconcile_hours,
                force=force,
                desired_proxied=record.proxied,
                desired_ttl=record.ttl,
            ):
                outcome = RecordOutcome(key, account.name, record.name, "skipped", "recent state matches")
                outcomes.append(outcome)
                logger.info(
                    "account=%s record=%s status=skipped reason=recent-state",
                    account.name,
                    record.name,
                )
                continue
            try:
                remote = client.find_a_record(record.zone_id, record.name)
                record_id = as_str(remote.get("id"), f"Cloudflare record id for {record.name}")
                if record_needs_update(remote, record, public_ip):
                    client.update_a_record(record.zone_id, record_id, record, public_ip)
                    status = "updated"
                    detail = "DNS record updated"
                else:
                    status = "confirmed"
                    detail = "provider already matches"
                record_state[key] = {
                    "provider": "cloudflare",
                    "account": account.name,
                    "zone_id": record.zone_id,
                    "name": record.name,
                    "last_ip": public_ip,
                    "confirmed_at": now.isoformat(),
                    "proxied": record.proxied,
                    "ttl": record.ttl,
                }
                outcome = RecordOutcome(key, account.name, record.name, status, detail)
                logger.info(
                    "account=%s record=%s status=%s ip=%s proxied=%s",
                    account.name,
                    record.name,
                    status,
                    public_ip,
                    record.proxied,
                )
            except (ProviderError, ConfigError) as exc:
                previous = record_state.get(key)
                failed_entry = dict(previous) if isinstance(previous, dict) else {}
                failed_entry.update(
                    {
                        "provider": "cloudflare",
                        "account": account.name,
                        "zone_id": record.zone_id,
                        "name": record.name,
                        "confirmed_at": None,
                        "last_error_at": now.isoformat(),
                        "last_error": str(exc),
                    }
                )
                record_state[key] = failed_entry
                outcome = RecordOutcome(key, account.name, record.name, "error", str(exc))
                logger.error(
                    "account=%s record=%s status=error detail=%s",
                    account.name,
                    record.name,
                    exc,
                )
            outcomes.append(outcome)
    return tuple(outcomes)


def format_report(summary: RunSummary) -> str:
    lines = ["IPaparazzi execution", "", f"- Public IPv4: {summary.public_ip or 'unavailable'}"]
    lines.append("- Sources:")
    for source in summary.source_results:
        value = source.ip if source.ip is not None else f"fail ({source.error})"
        lines.append(f"  - {source.source}: {value}")
    if summary.outcomes:
        lines.append("- DNS records:")
        for outcome in summary.outcomes:
            lines.append(
                f"  - {outcome.account}/{outcome.name}: {outcome.status} ({outcome.detail})"
            )
    if summary.global_errors:
        lines.append("- Errors:")
        lines.extend(f"  - {error}" for error in summary.global_errors)
    return "\n".join(lines)


def build_notification_command(
    config: NotificationConfig,
    event: str,
    report: str,
) -> list[str]:
    event_config = config.events[event]
    values = {"report": report, "event": event.upper()}
    command = [
        config.binary,
        "send",
        "--sender",
        config.sender,
        "--category",
        event_config.category,
        "--title",
        event_config.title.format_map(values),
        "--message",
        event_config.message.format_map(values),
    ]
    if event_config.priority:
        command.extend(("--priority", event_config.priority))
    if config.config_path:
        command[2:2] = ("--config", config.config_path)
    return command


def send_notification(
    config: NotificationConfig,
    event: str,
    report: str,
) -> subprocess.CompletedProcess[str] | None:
    if not config.enabled:
        return None
    command = build_notification_command(config, event, report)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command,
            127,
            "",
            f"{type(exc).__name__}: {exc}",
        )


def choose_notification_event(summary: RunSummary, previous_had_errors: bool) -> str | None:
    if summary.errors:
        return "error"
    if previous_had_errors:
        return "recovered"
    if summary.updated_count:
        return "changed"
    return None


def log_source_results(logger: logging.Logger, results: tuple[SourceResult, ...]) -> None:
    for result in results:
        if result.ip:
            logger.info("source=%s status=ok ip=%s", result.source, result.ip)
        else:
            logger.warning("source=%s status=error detail=%s", result.source, result.error)


def run_application(
    config: AppConfig,
    *,
    force_reconcile: bool = False,
    logger: logging.Logger,
    fetcher: HttpFetcher = default_http_fetcher,
    client_factory: Callable[[str, int], CloudflareClient] = CloudflareClient,
    now: datetime | None = None,
) -> int:
    execution_time = (now or utc_now()).astimezone(timezone.utc)
    state = load_state(config.state_file)
    previous_had_errors = bool(state["meta"].get("last_run_had_errors", False))
    logger.info("execution=start force_reconcile=%s", force_reconcile)
    try:
        public_ip, source_results = discover_public_ipv4(
            fetcher=fetcher,
            timeout_seconds=config.request_timeout_seconds,
            retries=config.request_retries,
            retry_delay_seconds=config.retry_delay_seconds,
        )
        log_source_results(logger, source_results)
        logger.info("consensus=ok ip=%s", public_ip)
    except PublicIpError as exc:
        log_source_results(logger, exc.results)
        logger.error("consensus=error detail=%s", exc)
        summary = RunSummary(None, exc.results, (), (str(exc),))
        state["meta"]["last_run_had_errors"] = True
        state["meta"]["last_run_at"] = execution_time.isoformat()
        save_state(config.state_file, state)
        notification = send_notification(config.notifications, "error", format_report(summary))
        if notification is not None and notification.returncode != 0:
            logger.error("notification=error event=error detail=%s", notification.stderr.strip())
        logger.info("execution=end status=no-consensus")
        return EXIT_NO_CONSENSUS

    outcomes = reconcile_records(
        config,
        state,
        public_ip,
        now=execution_time,
        force=force_reconcile,
        logger=logger,
        client_factory=client_factory,
    )
    summary = RunSummary(public_ip, source_results, outcomes)
    current_had_errors = bool(summary.errors)
    state["meta"]["last_run_had_errors"] = current_had_errors
    state["meta"]["last_run_at"] = execution_time.isoformat()
    state["meta"]["last_public_ip"] = public_ip
    save_state(config.state_file, state)

    event = choose_notification_event(summary, previous_had_errors)
    notification_failed = False
    if event:
        notification = send_notification(config.notifications, event, format_report(summary))
        if notification is not None and notification.returncode != 0:
            notification_failed = True
            logger.error(
                "notification=error event=%s detail=%s",
                event,
                notification.stderr.strip() or "NotiCLI returned a non-zero exit code",
            )
        elif notification is not None:
            logger.info("notification=sent event=%s", event)

    if current_had_errors:
        logger.info("execution=end status=provider-error")
        return EXIT_PROVIDER_ERROR
    if notification_failed:
        logger.info("execution=end status=notification-error")
        return EXIT_NOTIFICATION_ERROR
    logger.info(
        "execution=end status=ok updated=%d confirmed=%d skipped=%d",
        summary.updated_count,
        sum(item.status == "confirmed" for item in outcomes),
        sum(item.status == "skipped" for item in outcomes),
    )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="IPaparazzi",
        description="Keep Cloudflare A records synchronized with the public IPv4 address.",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().with_name("IPaparazzi.toml")),
        help="path to IPaparazzi.toml",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration without making network calls",
    )
    parser.add_argument(
        "--force-reconcile",
        action="store_true",
        help="check every configured DNS record regardless of cached state",
    )
    parser.add_argument("--verbose", action="store_true", help="enable verbose logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(Path(args.config))
        if args.check_config:
            print(f"configuration OK: {config.config_path}")
            return EXIT_OK
        logger = setup_logging(config, verbose=args.verbose)
        with exclusive_lock(config.lock_file, config.lock_stale_minutes):
            return run_application(
                config,
                force_reconcile=args.force_reconcile,
                logger=logger,
            )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except AlreadyRunningError as exc:
        print(f"already running: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except (RuntimeError, OSError) as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
