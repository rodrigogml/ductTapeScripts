#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import subprocess
import sys
from dataclasses import dataclass
import calendar
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import tomllib


__author__ = "Alan Turing"

EXIT_OK = 0
EXIT_CHECK_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_NOTIFICATION_ERROR = 3
EXIT_RUNTIME_ERROR = 4

DEFAULT_CATEGORIES = (
    "Problemas na SEFAZ",
    "Cupons em Fila",
    "SEFAZ Offline (Direto)",
    "SEFAZ Offline (Corrigidos)",
)

CONSOLIDATED_QUERY = """
SELECT
    categoria.titulo,
    COUNT(*) AS quantidade
FROM (
    SELECT
        df.id,
        CASE
            WHEN df.status IN ('SEFAZPROBLEM', 'SEFAZERROR')
                THEN 'Problemas na SEFAZ'

            WHEN df.status = 'ERROR_SYNC'
                 AND df.deviceId IS NULL
                 AND (
                     dfg.status IS NULL
                     OR dfg.status <> 'ERROR_SYNC'
                 )
                 AND df.emission <= %(fila_limite)s
                THEN 'Cupons em Fila'

            WHEN df.status = 'SEFAZOFFLINE'
                 AND df.subDeviceId IS NULL
                THEN 'SEFAZ Offline (Direto)'

            WHEN df.status = 'SEFAZOFFLINE'
                 AND df.subDeviceId IS NOT NULL
                THEN 'SEFAZ Offline (Corrigidos)'

            ELSE NULL
        END AS titulo
    FROM fiscal_docfiscal df
    LEFT JOIN fiscal_docfiscal dfg
        ON dfg.id = df.subDeviceId
    WHERE df.type = 'NFCe'
      AND df.emission >= %(data_inicio)s
      AND df.emission < %(data_fim)s
) categoria
WHERE categoria.titulo IS NOT NULL
GROUP BY categoria.titulo
ORDER BY categoria.titulo
"""


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    queue_limit: datetime


@dataclass(frozen=True)
class CheckResult:
    label: str
    quantity: int
    ok: bool
    detail: str


@dataclass(frozen=True)
class JobOutcome:
    name: str
    checks: list[CheckResult]
    ok: bool
    report: str


class ConfigError(RuntimeError):
    pass


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    if not isinstance(data, dict):
        raise ConfigError("config root must be a table")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def as_bool(value: Any, name: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{name} must be a boolean")


def as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


def as_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def normalize_root(path: Path) -> Path:
    if path.is_dir():
        return path / "bis2Buster.toml"
    return path


def validate_top_level(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    global_cfg = config.get("global")
    jobs = config.get("jobs")
    if not isinstance(global_cfg, dict):
        raise ConfigError("missing [global] table")
    if not isinstance(jobs, dict) or not jobs:
        raise ConfigError("missing [jobs] table")
    return global_cfg, jobs


def normalize_job_cfg(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("job entry must be a table")
    return raw


def parse_local_datetime(value: Any, name: str, timezone: ZoneInfo) -> datetime:
    text = as_str(value, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an ISO datetime") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def resolve_time_window(
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> TimeWindow:
    timezone_name = as_str(cfg.get("timezone", "America/Sao_Paulo"), "timezone")
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ConfigError(f"invalid timezone: {timezone_name}") from exc

    current = now.astimezone(timezone) if now else datetime.now(timezone)
    queue_age_hours = as_int(cfg.get("queue_age_hours", 3), "queue_age_hours")
    if queue_age_hours < 0:
        raise ConfigError("queue_age_hours must be greater than or equal to zero")

    start_value = cfg.get("data_inicio")
    end_value = cfg.get("data_fim")
    if start_value is not None or end_value is not None:
        if start_value is None or end_value is None:
            raise ConfigError("data_inicio and data_fim must be configured together")
        start = parse_local_datetime(start_value, "data_inicio", timezone)
        end = parse_local_datetime(end_value, "data_fim", timezone)
    else:
        mode = as_str(cfg.get("period_mode", "last_3_months"), "period_mode")
        if mode == "last_3_months":
            end = current
            start = add_months(end, -3)
        elif mode == "current_day":
            start = datetime.combine(current.date(), time.min, tzinfo=timezone)
            end = start + timedelta(days=1)
        elif mode == "last_24h":
            end = current
            start = end - timedelta(hours=24)
        else:
            raise ConfigError("period_mode must be last_3_months, current_day or last_24h")

    if start >= end:
        raise ConfigError("data_inicio must be before data_fim")

    queue_limit = current - timedelta(hours=queue_age_hours)
    return TimeWindow(start=start, end=end, queue_limit=queue_limit)


def mysql_datetime(value: datetime) -> str:
    return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def build_query_params(window: TimeWindow) -> dict[str, str]:
    return {
        "data_inicio": mysql_datetime(window.start),
        "data_fim": mysql_datetime(window.end),
        "fila_limite": mysql_datetime(window.queue_limit),
    }


def normalize_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {category: 0 for category in DEFAULT_CATEGORIES}
    for row in rows:
        title = row.get("titulo")
        if title not in counts:
            continue
        raw_quantity = row.get("quantidade")
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid quantity for {title}") from exc
        counts[title] = quantity
    return counts


def evaluate_counts(counts: dict[str, int]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for category in DEFAULT_CATEGORIES:
        quantity = counts.get(category, 0)
        ok = quantity == 0
        detail = "nenhum cupom encontrado" if ok else f"{quantity} cupom(ns) encontrado(s)"
        checks.append(CheckResult(category, quantity, ok, detail))
    return checks


def format_report(name: str, checks: list[CheckResult], ok: bool, window: TimeWindow) -> str:
    status = "OK" if ok else "FAIL"
    parts = [
        "BIS2 monitor OK" if ok else "BIS2 monitor falhou",
        "",
        f"- Job: {name}",
        f"- Status: {status}",
        f"- Periodo: {mysql_datetime(window.start)} -> {mysql_datetime(window.end)}",
        f"- Fila limite: {mysql_datetime(window.queue_limit)}",
        "- Checks:",
    ]
    for item in checks:
        mark = "ok" if item.ok else "fail"
        parts.append(f"  - {item.label}: {mark} ({item.detail})")
    return "\n".join(parts)


def connect_mysql(job_cfg: dict[str, Any], effective_cfg: dict[str, Any]) -> Any:
    try:
        import mysql.connector
    except ModuleNotFoundError as exc:
        raise RuntimeError("missing dependency: mysql-connector-python") from exc

    port = as_int(job_cfg.get("port", 3306), "port")
    connection_timeout = as_int(effective_cfg.get("mysql_timeout_seconds", 10), "mysql_timeout_seconds")
    return mysql.connector.connect(
        host=as_str(job_cfg.get("host"), "host"),
        port=port,
        database=as_str(job_cfg.get("database"), "database"),
        user=as_str(job_cfg.get("user"), "user"),
        password=as_str(job_cfg.get("password"), "password"),
        connection_timeout=connection_timeout,
    )


def fetch_coupon_counts(job_cfg: dict[str, Any], effective_cfg: dict[str, Any], window: TimeWindow) -> dict[str, int]:
    connection = connect_mysql(job_cfg, effective_cfg)
    try:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(CONSOLIDATED_QUERY, build_query_params(window))
            rows = cursor.fetchall()
        finally:
            cursor.close()
    finally:
        connection.close()
    return normalize_counts(rows)


def run_job(
    job_name: str,
    global_cfg: dict[str, Any],
    job_cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> JobOutcome:
    effective = deep_merge(global_cfg, job_cfg)
    window = resolve_time_window(effective, now=now)
    try:
        counts = fetch_coupon_counts(job_cfg, effective, window)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{job_name}: mysql query failed ({type(exc).__name__})") from exc
    checks = evaluate_counts(counts)
    ok = all(item.ok for item in checks)
    report = format_report(job_name, checks, ok, window)
    return JobOutcome(job_name, checks, ok, report)


def render_template(template: str, values: dict[str, Any]) -> str:
    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return template.format_map(SafeDict(values))


def build_notification_command(
    noticli_cfg: dict[str, Any],
    notify_cfg: dict[str, Any],
    *,
    report: str,
    job: str,
    status: str,
) -> list[str]:
    bin_path = as_str(noticli_cfg.get("noticli_bin", "noticli"), "noticli_bin")
    sender = as_str(noticli_cfg.get("sender", "bis2Buster"), "sender")
    category_default = "FAIL" if status == "FAIL" else "SUCCESS"
    category = as_str(notify_cfg.get("category", category_default), "category")
    if status == "FAIL":
        priority = as_str(notify_cfg.get("priority", "HIGH"), "priority")
    else:
        priority = None
    values = {"job": job, "status": status, "report": report}
    title = render_template(as_str(notify_cfg.get("title", "{job} {status}"), "title"), values)
    message = render_template(as_str(notify_cfg.get("message", "{report}"), "message"), values)

    command = [
        bin_path,
        "send",
        "--sender",
        sender,
        "--category",
        category,
        "--title",
        title,
        "--message",
        message,
    ]
    if priority:
        command += ["--priority", priority]
    config_path = noticli_cfg.get("noticli_config")
    if config_path:
        command[2:2] = ["--config", as_str(config_path, "noticli_config")]
    return command


def send_notification(
    noticli_cfg: dict[str, Any],
    notify_cfg: dict[str, Any],
    *,
    report: str,
    job: str,
    status: str,
) -> subprocess.CompletedProcess[str]:
    command = build_notification_command(
        noticli_cfg,
        notify_cfg,
        report=report,
        job=job,
        status=status,
    )
    return subprocess.run(command, capture_output=True, text=True, check=False)


def merge_notify_sections(global_cfg: dict[str, Any], job_cfg: dict[str, Any], outcome: str) -> dict[str, Any]:
    merged = deep_merge(global_cfg.get("notify", {}), job_cfg.get("notify", {}))
    if not isinstance(merged, dict):
        raise ConfigError("notify section must be a table")
    outcome_global = global_cfg.get("notify", {}).get(outcome, {}) if isinstance(global_cfg.get("notify"), dict) else {}
    outcome_job = job_cfg.get("notify", {}).get(outcome, {}) if isinstance(job_cfg.get("notify"), dict) else {}
    if not isinstance(outcome_global, dict) or not isinstance(outcome_job, dict):
        raise ConfigError("notify outcome section must be a table")
    merged = deep_merge(merged, {outcome: outcome_global})
    merged = deep_merge(merged, {outcome: outcome_job})
    result = merged.get(outcome, {})
    if not isinstance(result, dict):
        raise ConfigError("resolved notify section must be a table")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bis2Buster")
    default_config = Path(__file__).resolve().with_name("bis2Buster.toml")
    parser.add_argument("--config", default=str(default_config), help="path to bis2Buster.toml")
    args = parser.parse_args(argv)

    try:
        config_path = normalize_root(Path(args.config))
        config = load_toml(config_path)
        global_cfg, jobs = validate_top_level(config)

        any_check_failure = False
        any_notification_failure = False

        for job_name in sorted(jobs):
            job_cfg = normalize_job_cfg(jobs[job_name])
            if not as_bool(job_cfg.get("enabled", True), "enabled", default=True):
                continue
            outcome = run_job(job_name, global_cfg, job_cfg)
            print(outcome.report)

            notify_section = merge_notify_sections(global_cfg, job_cfg, "success" if outcome.ok else "error")
            status = "OK" if outcome.ok else "FAIL"
            notification = send_notification(
                deep_merge(global_cfg, job_cfg),
                notify_section,
                report=outcome.report,
                job=outcome.name,
                status=status,
            )
            if notification.returncode != 0:
                any_notification_failure = True
                stderr = notification.stderr.strip() or "notification failed"
                print(f"{outcome.name} notify fail | {stderr}", file=sys.stderr)

            if not outcome.ok:
                any_check_failure = True

        if any_notification_failure:
            return EXIT_NOTIFICATION_ERROR
        if any_check_failure:
            return EXIT_CHECK_FAILURE
        return EXIT_OK

    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except RuntimeError as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except FileNotFoundError as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
