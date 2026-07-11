#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomllib


EXIT_OK = 0
EXIT_CHECK_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_NOTIFICATION_ERROR = 3
EXIT_RUNTIME_ERROR = 4


@dataclass(frozen=True)
class CheckResult:
    label: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class JobOutcome:
    domain: str
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


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"expected boolean, got {type(value).__name__}")


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
        return path / "noMore404.toml"
    return path


def normalize_job_name(job_name: str, job_cfg: dict[str, Any]) -> str:
    primary = job_cfg.get("primary_domain") or job_cfg.get("domain") or job_name
    return as_str(primary, "primary_domain")


def url_for(scheme: str, host: str, path: str) -> str:
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{scheme}://{host}{clean_path}"


def curl_probe(
    curl_bin: str,
    url: str,
    *,
    user_agent: str,
    timeout_ms: int,
    follow_redirects: bool,
) -> dict[str, Any]:
    timeout = max(timeout_ms / 1000.0, 0.1)
    args = [
        curl_bin,
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--max-time",
        f"{timeout:.3f}",
        "--connect-timeout",
        f"{timeout:.3f}",
        "--user-agent",
        user_agent,
        "--write-out",
        "%{http_code}\t%{url_effective}\t%{num_redirects}\t%{time_total}",
    ]
    if follow_redirects:
        args.append("--location")
    args.append(url)

    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    fields = stdout.split("\t") if stdout else []
    http_code = fields[0] if len(fields) > 0 else ""
    effective_url = fields[1] if len(fields) > 1 else url
    num_redirects = fields[2] if len(fields) > 2 else "0"
    time_total = fields[3] if len(fields) > 3 else "0"
    return {
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
        "http_code": http_code,
        "effective_url": effective_url,
        "num_redirects": num_redirects,
        "time_total": time_total,
    }


def host_and_scheme(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    return parsed.hostname or "", parsed.scheme or ""


def format_ms(seconds_text: str) -> int:
    try:
        return int(round(float(seconds_text) * 1000))
    except ValueError:
        return 0


def check_http_200(job: dict[str, Any], curl_bin: str, user_agent: str, timeout_ms: int) -> CheckResult:
    scheme = as_str(job.get("http_scheme", "https"), "http_scheme")
    host = as_str(job["primary_domain"], "primary_domain")
    path = as_str(job.get("path", "/"), "path")
    probe = curl_probe(
        curl_bin,
        url_for(scheme, host, path),
        user_agent=user_agent,
        timeout_ms=timeout_ms,
        follow_redirects=False,
    )
    if probe["returncode"] != 0:
        return CheckResult("200", False, f"curl rc={probe['returncode']}")
    ok = probe["http_code"] == "200"
    detail = "ok" if ok else f"fail({probe['http_code'] or 'no-code'})"
    return CheckResult("200", ok, detail)


def check_http_to_https(job: dict[str, Any], curl_bin: str, user_agent: str, timeout_ms: int) -> CheckResult:
    host = as_str(job["primary_domain"], "primary_domain")
    path = as_str(job.get("path", "/"), "path")
    probe = curl_probe(
        curl_bin,
        url_for("http", host, path),
        user_agent=user_agent,
        timeout_ms=timeout_ms,
        follow_redirects=True,
    )
    if probe["returncode"] != 0:
        return CheckResult("http->https", False, f"curl rc={probe['returncode']}")
    final_host, final_scheme = host_and_scheme(probe["effective_url"])
    redirects = int(probe["num_redirects"] or "0")
    ok = redirects > 0 and final_host == host and final_scheme == "https"
    if ok:
        return CheckResult("http->https", True, "ok")
    return CheckResult(
        "http->https",
        False,
        f"fail({final_scheme or '-'}://{final_host or '-'}:{probe['num_redirects']})",
    )


def check_index_time(job: dict[str, Any], curl_bin: str, user_agent: str, timeout_ms: int) -> CheckResult:
    scheme = as_str(job.get("http_scheme", "https"), "http_scheme")
    host = as_str(job["primary_domain"], "primary_domain")
    path = as_str(job.get("path", "/"), "path")
    max_ms = as_int(job.get("index_max_ms", 1500), "index_max_ms")
    probe = curl_probe(
        curl_bin,
        url_for(scheme, host, path),
        user_agent=user_agent,
        timeout_ms=timeout_ms,
        follow_redirects=True,
    )
    if probe["returncode"] != 0:
        return CheckResult("index", False, f"curl rc={probe['returncode']}")
    duration_ms = format_ms(probe["time_total"])
    ok = probe["http_code"] == "200" and duration_ms <= max_ms
    if ok:
        return CheckResult("index", True, f"ok {duration_ms}ms")
    return CheckResult("index", False, f"fail {duration_ms}ms>{max_ms}ms")


def check_redirect_rule(
    rule: dict[str, Any],
    curl_bin: str,
    user_agent: str,
    timeout_ms: int,
) -> CheckResult:
    source = as_str(rule["source"], "redirect source")
    target = as_str(rule["target"], "redirect target")
    source_scheme = as_str(rule.get("source_scheme", "http"), "redirect source_scheme")
    target_scheme = rule.get("target_scheme")
    path = as_str(rule.get("path", "/"), "redirect path")
    probe = curl_probe(
        curl_bin,
        url_for(source_scheme, source, path),
        user_agent=user_agent,
        timeout_ms=timeout_ms,
        follow_redirects=True,
    )
    label = f"{source}->{target}"
    if probe["returncode"] != 0:
        return CheckResult(label, False, f"curl rc={probe['returncode']}")

    final_host, final_scheme = host_and_scheme(probe["effective_url"])
    redirects = int(probe["num_redirects"] or "0")
    target_host, _ = host_and_scheme(url_for(target_scheme or final_scheme or source_scheme, target, path))
    scheme_ok = True if target_scheme is None else final_scheme == target_scheme
    ok = redirects > 0 and final_host == target_host and scheme_ok
    if ok:
        return CheckResult(label, True, "ok")
    return CheckResult(
        label,
        False,
        f"fail({final_scheme or '-'}://{final_host or '-'}:{redirects})",
    )


def collect_redirect_rules(job: dict[str, Any]) -> list[dict[str, Any]]:
    rules = job.get("redirects", [])
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ConfigError("redirects must be an array of tables")
    normalized: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ConfigError("redirect rules must be tables")
        normalized.append(rule)
    return normalized


def run_job(
    job_name: str,
    global_cfg: dict[str, Any],
    job_cfg: dict[str, Any],
) -> JobOutcome:
    primary_domain = normalize_job_name(job_name, job_cfg)
    effective = deep_merge(global_cfg, job_cfg)
    curl_bin = as_str(effective.get("curl_bin", "curl"), "curl_bin")
    user_agent = as_str(effective.get("user_agent", "noMore404/1.0"), "user_agent")
    timeout_ms = as_int(effective.get("http_timeout_ms", 5000), "http_timeout_ms")

    checks: list[CheckResult] = []
    if as_bool(effective.get("check_http_200")):
        checks.append(check_http_200(effective, curl_bin, user_agent, timeout_ms))
    if as_bool(effective.get("check_http_to_https")):
        checks.append(check_http_to_https(effective, curl_bin, user_agent, timeout_ms))
    if as_bool(effective.get("check_index_time")):
        checks.append(check_index_time(effective, curl_bin, user_agent, timeout_ms))
    if as_bool(effective.get("check_redirects")):
        redirect_rules = collect_redirect_rules(effective)
        if not redirect_rules:
            raise ConfigError(f"{job_name}: check_redirects enabled without redirects")
        for rule in redirect_rules:
            checks.append(check_redirect_rule(rule, curl_bin, user_agent, timeout_ms))

    if not checks:
        checks.append(CheckResult("checks", False, "none"))

    ok = all(item.ok for item in checks)
    report = format_report(primary_domain, checks, ok)
    return JobOutcome(primary_domain, checks, ok, report)


def format_report(domain: str, checks: list[CheckResult], ok: bool) -> str:
    status = "OK" if ok else "FAIL"
    parts = [f"{domain} {status}", "checks:"]
    for item in checks:
        mark = "ok" if item.ok else "fail"
        parts.append(f"- {item.label}: {mark} ({item.detail})")
    return "\n".join(parts)


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
    domain: str,
    status: str,
) -> list[str]:
    bin_path = as_str(noticli_cfg.get("noticli_bin", "noticli"), "noticli_bin")
    sender = as_str(noticli_cfg.get("sender", "noMore404"), "sender")
    category_default = "FAIL" if status == "FAIL" else "SUCCESS"
    priority_default = "HIGH" if status == "FAIL" else None
    category = as_str(notify_cfg.get("category", category_default), "category")
    priority_value = notify_cfg.get("priority", priority_default)
    priority = as_str(priority_value, "priority") if priority_value else None
    title = render_template(as_str(notify_cfg.get("title", "{domain}"), "title"), {
        "domain": domain,
        "status": status,
        "report": report,
    })
    message = render_template(as_str(notify_cfg.get("message", "{report}"), "message"), {
        "domain": domain,
        "status": status,
        "report": report,
    })

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
    domain: str,
    status: str,
) -> subprocess.CompletedProcess[str]:
    command = build_notification_command(
        noticli_cfg,
        notify_cfg,
        report=report,
        domain=domain,
        status=status,
    )
    return subprocess.run(command, capture_output=True, text=True, check=False)


def validate_top_level(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    global_cfg = config.get("global")
    jobs = config.get("jobs")
    if not isinstance(global_cfg, dict):
        raise ConfigError("missing [global] table")
    if not isinstance(jobs, dict) or not jobs:
        raise ConfigError("missing [jobs] table")
    return global_cfg, jobs


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


def normalize_job_cfg(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("job entry must be a table")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noMore404")
    default_config = Path(__file__).resolve().with_name("noMore404.toml")
    parser.add_argument("--config", default=str(default_config), help="path to noMore404.toml")
    args = parser.parse_args(argv)

    try:
        config_path = normalize_root(Path(args.config))
        config = load_toml(config_path)
        global_cfg, jobs = validate_top_level(config)

        any_check_failure = False
        any_notification_failure = False

        for job_name in sorted(jobs):
            job_cfg = normalize_job_cfg(jobs[job_name])
            outcome = run_job(job_name, global_cfg, job_cfg)
            print(outcome.report)

            notify_section = merge_notify_sections(global_cfg, job_cfg, "success" if outcome.ok else "error")
            status = "OK" if outcome.ok else "FAIL"
            notification = send_notification(
                deep_merge(global_cfg, job_cfg),
                notify_section,
                report=outcome.report,
                domain=outcome.domain,
                status=status,
            )
            if notification.returncode != 0:
                any_notification_failure = True
                stderr = notification.stderr.strip() or "notification failed"
                print(f"{outcome.domain} notify fail | {stderr}", file=sys.stderr)

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
    except FileNotFoundError as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
