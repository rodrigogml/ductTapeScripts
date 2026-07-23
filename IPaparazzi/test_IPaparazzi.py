from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("IPaparazzi.py")
SPEC = importlib.util.spec_from_file_location("IPaparazzi", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


VALID_IP = "8.8.8.8"
OTHER_VALID_IP = "9.9.9.9"


def logger_for_tests() -> logging.Logger:
    logger = logging.getLogger("IPaparazzi-tests")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def make_record(**overrides: object) -> object:
    values = {
        "zone_id": "zone-1",
        "name": "home.example.com",
        "proxied": False,
        "ttl": 120,
        "enabled": True,
    }
    values.update(overrides)
    return MODULE.RecordConfig(**values)


def make_config(root: Path, **overrides: object) -> object:
    record = make_record()
    account = MODULE.AccountConfig("primary", "secret-token", (record,))
    notifications = MODULE.NotificationConfig(
        enabled=False,
        binary="noticli",
        sender="IPaparazzi",
        config_path=None,
        events={
            "changed": MODULE.NotificationEventConfig("SUCCESS", "Changed", "{report}"),
            "error": MODULE.NotificationEventConfig("FAIL", "Error", "{report}", "HIGH"),
            "recovered": MODULE.NotificationEventConfig("SUCCESS", "Recovered", "{report}"),
        },
    )
    values = {
        "config_path": root / "IPaparazzi.toml",
        "state_file": root / "IPaparazzi.state.json",
        "log_file": root / "IPaparazzi.log",
        "lock_file": root / "IPaparazzi.lock",
        "reconcile_hours": 24,
        "request_timeout_seconds": 1,
        "request_retries": 1,
        "retry_delay_seconds": 0.0,
        "lock_stale_minutes": 60,
        "log_max_bytes": 1024,
        "log_backup_count": 2,
        "accounts": (account,),
        "notifications": notifications,
    }
    values.update(overrides)
    return MODULE.AppConfig(**values)


class FakeCloudflareClient:
    def __init__(self, remote: dict[str, object] | Exception) -> None:
        self.remote = remote
        self.find_calls = 0
        self.update_calls: list[tuple[str, str, object, str]] = []

    def find_a_record(self, zone_id: str, name: str) -> dict[str, object]:
        self.find_calls += 1
        if isinstance(self.remote, Exception):
            raise self.remote
        return dict(self.remote)

    def update_a_record(
        self,
        zone_id: str,
        record_id: str,
        record: object,
        public_ip: str,
    ) -> dict[str, object]:
        self.update_calls.append((zone_id, record_id, record, public_ip))
        return {"id": record_id, "content": public_ip}


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class ConfigurationTests(unittest.TestCase):
    def test_load_config_supports_multiple_accounts_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "IPaparazzi.toml"
            config_path.write_text(
                """
[global]
reconcile_hours = 12

[global.notifications]
enabled = false

[providers.cloudflare]

[[providers.cloudflare.accounts]]
name = "one"
api_token = "token-one"

[[providers.cloudflare.accounts.records]]
zone_id = "zone-one"
name = "Home.Example.com."
proxied = false
ttl = 120

[[providers.cloudflare.accounts]]
name = "two"
api_token = "token-two"

[[providers.cloudflare.accounts.records]]
zone_id = "zone-two"
name = "web.example.net"
proxied = true
ttl = 1
""".strip(),
                encoding="utf-8",
            )

            config = MODULE.load_config(config_path)

            self.assertEqual(config.reconcile_hours, 12)
            self.assertEqual(len(config.accounts), 2)
            self.assertEqual(config.accounts[0].records[0].name, "home.example.com")
            self.assertTrue(config.accounts[1].records[0].proxied)
            self.assertEqual(config.state_file, root / "IPaparazzi.state.json")

    def test_model_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "IPaparazzi.toml"
            config_path.write_text(
                """
[global]
[providers.cloudflare]
[[providers.cloudflare.accounts]]
name = "one"
api_token = "CHANGE_ME"
[[providers.cloudflare.accounts.records]]
zone_id = "zone"
name = "home.example.com"
proxied = false
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MODULE.ConfigError, "model api_token"):
                MODULE.load_config(config_path)

    def test_proxied_record_requires_auto_ttl(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigError, "ttl = 1"):
            MODULE.parse_record(
                {
                    "zone_id": "zone",
                    "name": "web.example.com",
                    "proxied": True,
                    "ttl": 120,
                },
                "one",
                1,
            )

    def test_ttl_below_cloudflare_minimum_is_rejected(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigError, "at least 60"):
            MODULE.parse_record(
                {
                    "zone_id": "zone",
                    "name": "web.example.com",
                    "proxied": False,
                    "ttl": 30,
                },
                "one",
                1,
            )

    def test_notification_rejects_unknown_placeholder(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigError, "unsupported placeholder"):
            MODULE.parse_notification_event(
                {"error": {"message": "{unknown}"}},
                "error",
            )

    def test_record_requires_fully_qualified_name(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigError, "fully qualified"):
            MODULE.parse_record(
                {"zone_id": "zone", "name": "home", "proxied": False},
                "one",
                1,
            )


class PublicIpTests(unittest.TestCase):
    def test_cloudflare_trace_parser_extracts_ipv4(self) -> None:
        source = MODULE.IpSource("cloudflare", "https://example", "cloudflare-trace")

        result = MODULE.parse_source_ip(source, "fl=123\nip=8.8.8.8\ncolo=GRU\n")

        self.assertEqual(result, VALID_IP)

    def test_private_ipv4_is_rejected(self) -> None:
        source = MODULE.IpSource("test", "https://example")

        with self.assertRaisesRegex(MODULE.PublicIpError, "non-public"):
            MODULE.parse_source_ip(source, "192.168.1.10")

    def test_two_of_three_sources_form_consensus(self) -> None:
        responses = {
            MODULE.IP_SOURCES[0].url: f"ip={VALID_IP}\n",
            MODULE.IP_SOURCES[1].url: f"{VALID_IP}\n",
            MODULE.IP_SOURCES[2].url: f"{OTHER_VALID_IP}\n",
        }

        ip, results = MODULE.discover_public_ipv4(
            fetcher=lambda url, timeout: responses[url],
            retries=1,
            retry_delay_seconds=0,
        )

        self.assertEqual(ip, VALID_IP)
        self.assertEqual(len(results), 3)

    def test_one_failed_source_still_allows_consensus(self) -> None:
        def fetcher(url: str, timeout: int) -> str:
            if url == MODULE.IP_SOURCES[2].url:
                raise TimeoutError("timeout")
            if url == MODULE.IP_SOURCES[0].url:
                return f"ip={VALID_IP}\n"
            return VALID_IP

        ip, results = MODULE.discover_public_ipv4(
            fetcher=fetcher,
            retries=1,
            retry_delay_seconds=0,
        )

        self.assertEqual(ip, VALID_IP)
        self.assertIsNotNone(results[2].error)

    def test_source_retries_after_transient_failure(self) -> None:
        attempts: dict[str, int] = {}

        def fetcher(url: str, timeout: int) -> str:
            attempts[url] = attempts.get(url, 0) + 1
            if attempts[url] == 1:
                raise TimeoutError("temporary")
            return f"ip={VALID_IP}\n" if "cdn-cgi" in url else VALID_IP

        ip, results = MODULE.discover_public_ipv4(
            fetcher=fetcher,
            retries=2,
            retry_delay_seconds=0,
        )

        self.assertEqual(ip, VALID_IP)
        self.assertTrue(all(item.ip == VALID_IP for item in results))
        self.assertTrue(all(count == 2 for count in attempts.values()))

    def test_three_different_sources_do_not_form_consensus(self) -> None:
        responses = {
            MODULE.IP_SOURCES[0].url: "ip=8.8.8.8\n",
            MODULE.IP_SOURCES[1].url: "9.9.9.9\n",
            MODULE.IP_SOURCES[2].url: "1.1.1.1\n",
        }

        with self.assertRaisesRegex(MODULE.PublicIpError, "two-vote consensus"):
            MODULE.discover_public_ipv4(
                fetcher=lambda url, timeout: responses[url],
                retries=1,
                retry_delay_seconds=0,
            )


class StateTests(unittest.TestCase):
    def test_recent_matching_state_skips_reconciliation(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        entry = {"last_ip": VALID_IP, "confirmed_at": (now - timedelta(hours=1)).isoformat()}

        result = MODULE.should_reconcile(
            entry,
            VALID_IP,
            now=now,
            reconcile_hours=24,
            force=False,
        )

        self.assertFalse(result)

    def test_ip_change_forces_reconciliation(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        entry = {"last_ip": OTHER_VALID_IP, "confirmed_at": now.isoformat()}

        self.assertTrue(
            MODULE.should_reconcile(
                entry,
                VALID_IP,
                now=now,
                reconcile_hours=24,
                force=False,
            )
        )

    def test_configuration_change_forces_reconciliation(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        entry = {
            "last_ip": VALID_IP,
            "confirmed_at": now.isoformat(),
            "proxied": False,
            "ttl": 120,
        }

        self.assertTrue(
            MODULE.should_reconcile(
                entry,
                VALID_IP,
                now=now,
                reconcile_hours=24,
                force=False,
                desired_proxied=True,
                desired_ttl=1,
            )
        )

    def test_expired_confirmation_forces_reconciliation(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        entry = {"last_ip": VALID_IP, "confirmed_at": (now - timedelta(hours=24)).isoformat()}

        self.assertTrue(
            MODULE.should_reconcile(
                entry,
                VALID_IP,
                now=now,
                reconcile_hours=24,
                force=False,
            )
        )

    def test_state_round_trip_uses_versioned_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = MODULE.empty_state()
            state["meta"]["last_public_ip"] = VALID_IP

            MODULE.save_state(path, state)
            loaded = MODULE.load_state(path)

            self.assertEqual(loaded, state)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_invalid_state_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "cannot read state"):
                MODULE.load_state(path)

    def test_lock_rejects_concurrent_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.lock"

            with MODULE.exclusive_lock(path, 60):
                with self.assertRaises(MODULE.AlreadyRunningError):
                    with MODULE.exclusive_lock(path, 60):
                        self.fail("second lock should not be acquired")

            self.assertFalse(path.exists())

    def test_stale_lock_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.lock"
            path.write_text("stale", encoding="utf-8")
            old = datetime.now().timestamp() - 120 * 60
            MODULE.os.utime(path, (old, old))

            with MODULE.exclusive_lock(path, 60):
                self.assertTrue(path.exists())

            self.assertFalse(path.exists())


class CloudflareTests(unittest.TestCase):
    def test_record_comparison_detects_content_proxy_and_ttl_changes(self) -> None:
        desired = make_record()

        self.assertFalse(
            MODULE.record_needs_update(
                {"content": VALID_IP, "proxied": False, "ttl": 120},
                desired,
                VALID_IP,
            )
        )
        self.assertTrue(
            MODULE.record_needs_update(
                {"content": OTHER_VALID_IP, "proxied": False, "ttl": 120},
                desired,
                VALID_IP,
            )
        )
        self.assertTrue(
            MODULE.record_needs_update(
                {"content": VALID_IP, "proxied": True, "ttl": 1},
                desired,
                VALID_IP,
            )
        )

    def test_cloudflare_get_uses_bearer_token_and_exact_record(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: int) -> FakeHttpResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHttpResponse(
                {
                    "success": True,
                    "result": [
                        {
                            "id": "record-1",
                            "name": "home.example.com",
                            "content": VALID_IP,
                            "proxied": False,
                            "ttl": 120,
                        }
                    ],
                }
            )

        client = MODULE.CloudflareClient("token-value", 7)
        with patch.object(MODULE, "urlopen", side_effect=fake_urlopen):
            record = client.find_a_record("zone-1", "home.example.com")

        request = captured["request"]
        self.assertEqual(record["id"], "record-1")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-value")
        self.assertNotIn("token-value", request.full_url)
        self.assertIn("type=A", request.full_url)

    def test_cloudflare_patch_sends_desired_record(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: int) -> FakeHttpResponse:
            captured["request"] = request
            return FakeHttpResponse(
                {
                    "success": True,
                    "result": {"id": "record-1", "content": VALID_IP},
                }
            )

        client = MODULE.CloudflareClient("token-value", 7)
        desired = make_record(proxied=True, ttl=1)
        with patch.object(MODULE, "urlopen", side_effect=fake_urlopen):
            client.update_a_record("zone-1", "record-1", desired, VALID_IP)

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(body["content"], VALID_IP)
        self.assertTrue(body["proxied"])
        self.assertEqual(body["ttl"], 1)

    def test_cloudflare_refuses_ambiguous_records(self) -> None:
        client = MODULE.CloudflareClient("token-value", 7)
        duplicate = {"id": "record", "name": "home.example.com"}
        with patch.object(client, "_request", return_value=[duplicate, duplicate]):
            with self.assertRaisesRegex(MODULE.ProviderError, "multiple A records"):
                client.find_a_record("zone-1", "home.example.com")

    def test_reconciliation_updates_changed_record_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            state = MODULE.empty_state()
            now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
            client = FakeCloudflareClient(
                {"id": "record-1", "content": OTHER_VALID_IP, "proxied": False, "ttl": 120}
            )

            outcomes = MODULE.reconcile_records(
                config,
                state,
                VALID_IP,
                now=now,
                force=False,
                logger=logger_for_tests(),
                client_factory=lambda token, timeout: client,
            )

            self.assertEqual(outcomes[0].status, "updated")
            self.assertEqual(len(client.update_calls), 1)
            self.assertEqual(next(iter(state["records"].values()))["last_ip"], VALID_IP)

    def test_recent_state_avoids_cloudflare_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            record = config.accounts[0].records[0]
            now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
            state = MODULE.empty_state()
            state["records"][MODULE.record_state_key("primary", record)] = {
                "last_ip": VALID_IP,
                "confirmed_at": now.isoformat(),
                "proxied": False,
                "ttl": 120,
            }
            client = FakeCloudflareClient({})

            outcomes = MODULE.reconcile_records(
                config,
                state,
                VALID_IP,
                now=now,
                force=False,
                logger=logger_for_tests(),
                client_factory=lambda token, timeout: client,
            )

            self.assertEqual(outcomes[0].status, "skipped")
            self.assertEqual(client.find_calls, 0)

    def test_missing_record_is_isolated_from_other_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_record(name="missing.example.com")
            second = make_record(name="ok.example.com")
            account = MODULE.AccountConfig("primary", "secret", (first, second))
            config = make_config(root, accounts=(account,))
            class PerRecordClient:
                def find_a_record(self, zone_id: str, name: str) -> dict[str, object]:
                    if name.startswith("missing"):
                        raise MODULE.ProviderError("A record does not exist")
                    return {"id": "record-2", "content": VALID_IP, "proxied": False, "ttl": 120}

                def update_a_record(self, *args: object) -> dict[str, object]:
                    raise AssertionError("matching record must not be updated")

            outcomes = MODULE.reconcile_records(
                config,
                MODULE.empty_state(),
                VALID_IP,
                now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
                force=False,
                logger=logger_for_tests(),
                client_factory=lambda token, timeout: PerRecordClient(),
            )

            self.assertEqual([item.status for item in outcomes], ["error", "confirmed"])

    def test_provider_failure_invalidates_recent_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            record = config.accounts[0].records[0]
            key = MODULE.record_state_key("primary", record)
            now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
            state = MODULE.empty_state()
            state["records"][key] = {
                "last_ip": VALID_IP,
                "confirmed_at": now.isoformat(),
                "proxied": False,
                "ttl": 120,
            }
            client = FakeCloudflareClient(MODULE.ProviderError("temporary failure"))

            outcomes = MODULE.reconcile_records(
                config,
                state,
                VALID_IP,
                now=now,
                force=True,
                logger=logger_for_tests(),
                client_factory=lambda token, timeout: client,
            )

            self.assertEqual(outcomes[0].status, "error")
            self.assertIsNone(state["records"][key]["confirmed_at"])
            self.assertTrue(
                MODULE.should_reconcile(
                    state["records"][key],
                    VALID_IP,
                    now=now + timedelta(minutes=15),
                    reconcile_hours=24,
                    force=False,
                    desired_proxied=False,
                    desired_ttl=120,
                )
            )


class NotificationTests(unittest.TestCase):
    def test_success_report_uses_one_status_or_result_per_line(self) -> None:
        summary = MODULE.RunSummary(
            VALID_IP,
            (MODULE.SourceResult("cloudflare", VALID_IP, None),),
            (MODULE.RecordOutcome("key", "account", "home.example.com", "updated", "done"),),
        )

        report = MODULE.format_report(summary)

        self.assertEqual(
            report.splitlines(),
            [
                "IPaparazzi monitor OK",
                "",
                "- Status: OK",
                f"- Public IPv4: {VALID_IP}",
                "- Sources:",
                f"  - cloudflare: {VALID_IP}",
                "- DNS records:",
                "  - account/home.example.com: updated (done)",
            ],
        )

    def test_error_report_has_failure_status_and_separate_error_lines(self) -> None:
        summary = MODULE.RunSummary(
            None,
            (MODULE.SourceResult("cloudflare", None, "timeout"),),
            (),
            ("no IPv4 consensus", "provider unavailable"),
        )

        report = MODULE.format_report(summary)

        self.assertEqual(report.splitlines()[0], "IPaparazzi monitor falhou")
        self.assertIn("- Status: FAIL", report.splitlines())
        self.assertIn("  - no IPv4 consensus", report.splitlines())
        self.assertIn("  - provider unavailable", report.splitlines())

    def test_error_has_priority_over_change_and_recovery(self) -> None:
        summary = MODULE.RunSummary(
            VALID_IP,
            (),
            (MODULE.RecordOutcome("key", "account", "name", "error", "failed"),),
        )

        self.assertEqual(MODULE.choose_notification_event(summary, True), "error")

    def test_recovery_has_priority_over_change(self) -> None:
        summary = MODULE.RunSummary(
            VALID_IP,
            (),
            (MODULE.RecordOutcome("key", "account", "name", "updated", "done"),),
        )

        self.assertEqual(MODULE.choose_notification_event(summary, True), "recovered")

    def test_normal_no_change_run_does_not_notify(self) -> None:
        summary = MODULE.RunSummary(
            VALID_IP,
            (),
            (MODULE.RecordOutcome("key", "account", "name", "skipped", "recent"),),
        )

        self.assertIsNone(MODULE.choose_notification_event(summary, False))

    def test_notification_command_does_not_contain_api_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory)).notifications

            command = MODULE.build_notification_command(config, "error", "failed")

            self.assertNotIn("secret-token", command)
            self.assertIn("HIGH", command)

    def test_normal_events_use_noticli_default_normal_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory)).notifications

            command = MODULE.build_notification_command(config, "changed", "updated")

            self.assertNotIn("--priority", command)
            self.assertEqual(config.sender, "IPaparazzi")

    def test_missing_noticli_is_reported_as_process_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = make_config(Path(directory)).notifications
            config = MODULE.NotificationConfig(
                enabled=True,
                binary="missing-noticli",
                sender=base.sender,
                config_path=None,
                events=base.events,
            )
            with patch.object(MODULE.subprocess, "run", side_effect=FileNotFoundError("missing")):
                result = MODULE.send_notification(config, "error", "failed")

            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 127)
            self.assertIn("FileNotFoundError", result.stderr)


class ApplicationTests(unittest.TestCase):
    def test_no_consensus_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            responses = {
                MODULE.IP_SOURCES[0].url: "ip=8.8.8.8\n",
                MODULE.IP_SOURCES[1].url: "9.9.9.9\n",
                MODULE.IP_SOURCES[2].url: "1.1.1.1\n",
            }

            exit_code = MODULE.run_application(
                config,
                logger=logger_for_tests(),
                fetcher=lambda url, timeout: responses[url],
                client_factory=lambda token, timeout: self.fail("provider must not be created"),
                now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(exit_code, MODULE.EXIT_NO_CONSENSUS)
            self.assertTrue(MODULE.load_state(config.state_file)["meta"]["last_run_had_errors"])

    def test_successful_run_persists_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            client = FakeCloudflareClient(
                {"id": "record-1", "content": VALID_IP, "proxied": False, "ttl": 120}
            )

            def fetcher(url: str, timeout: int) -> str:
                return f"ip={VALID_IP}\n" if "cdn-cgi" in url else VALID_IP

            exit_code = MODULE.run_application(
                config,
                logger=logger_for_tests(),
                fetcher=fetcher,
                client_factory=lambda token, timeout: client,
                now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            )

            state = MODULE.load_state(config.state_file)
            self.assertEqual(exit_code, MODULE.EXIT_OK)
            self.assertFalse(state["meta"]["last_run_had_errors"])
            self.assertEqual(next(iter(state["records"].values()))["last_ip"], VALID_IP)

    def test_notification_failure_has_dedicated_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = make_config(root)
            notifications = MODULE.NotificationConfig(
                enabled=True,
                binary="noticli",
                sender="IPaparazzi",
                config_path=None,
                events=base.notifications.events,
            )
            config = make_config(root, notifications=notifications)
            client = FakeCloudflareClient(
                {"id": "record-1", "content": OTHER_VALID_IP, "proxied": False, "ttl": 120}
            )

            def fetcher(url: str, timeout: int) -> str:
                return f"ip={VALID_IP}\n" if "cdn-cgi" in url else VALID_IP

            with patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "notification failed"),
            ):
                exit_code = MODULE.run_application(
                    config,
                    logger=logger_for_tests(),
                    fetcher=fetcher,
                    client_factory=lambda token, timeout: client,
                    now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
                )

            self.assertEqual(exit_code, MODULE.EXIT_NOTIFICATION_ERROR)


if __name__ == "__main__":
    unittest.main()
