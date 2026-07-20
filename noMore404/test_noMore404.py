from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).with_name("noMore404.py")
SPEC = importlib.util.spec_from_file_location("noMore404", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NoMore404Tests(unittest.TestCase):
    def test_deep_merge_overrides_nested_sections(self) -> None:
        base = {
            "notify": {
                "category": "GLOBAL",
                "success": {"category": "SUCCESS", "message": "{report}"},
                "error": {"category": "FAIL", "priority": "HIGH", "message": "{report}"},
            }
        }
        override = {
            "notify": {
                "error": {"category": "DOMAIN_FAIL"},
            }
        }

        merged = MODULE.deep_merge(base, override)

        self.assertEqual(merged["notify"]["category"], "GLOBAL")
        self.assertEqual(merged["notify"]["error"]["category"], "DOMAIN_FAIL")
        self.assertEqual(merged["notify"]["error"]["priority"], "HIGH")
        self.assertEqual(merged["notify"]["success"]["category"], "SUCCESS")

    def test_format_report_is_multiline(self) -> None:
        checks = [
            MODULE.CheckResult("200", True, "ok"),
            MODULE.CheckResult("index", False, "fail 1800ms>1500ms"),
        ]

        report = MODULE.format_report("example.com", checks, False)

        self.assertEqual(
            report,
            "Website monitor falhou\n"
            "\n"
            "- Dominio: example.com\n"
            "- Status: FAIL\n"
            "- Checks:\n"
            "  - 200: ok (ok)\n"
            "  - index: fail (fail 1800ms>1500ms)",
        )

    def test_build_notification_command_omits_config_when_not_set(self) -> None:
        command = MODULE.build_notification_command(
            {
                "noticli_bin": "noticli",
                "sender": "noMore404",
            },
            {
                "category": "SUCCESS",
                "title": "{domain} {status}",
                "message": "{report}",
            },
            report="example.com OK\nchecks:\n- 200: ok (ok)",
            domain="example.com",
            status="OK",
        )

        self.assertEqual(
            command,
            [
                "noticli",
                "send",
                "--sender",
                "noMore404",
                "--category",
                "SUCCESS",
                "--title",
                "example.com OK",
                "--message",
                "example.com OK\nchecks:\n- 200: ok (ok)",
            ],
        )

    def test_build_notification_command_uses_optional_config(self) -> None:
        command = MODULE.build_notification_command(
            {
                "noticli_bin": "noticli",
                "noticli_config": "/tmp/noticli.json",
                "sender": "noMore404",
            },
            {
                "category": "SUCCESS",
                "title": "{domain} {status}",
                "message": "{report}",
            },
            report="example.com OK\nchecks:\n- 200: ok (ok)",
            domain="example.com",
            status="OK",
        )

        self.assertEqual(
            command,
            [
                "noticli",
                "send",
                "--config",
                "/tmp/noticli.json",
                "--sender",
                "noMore404",
                "--category",
                "SUCCESS",
                "--title",
                "example.com OK",
                "--message",
                "example.com OK\nchecks:\n- 200: ok (ok)",
            ],
        )

    def test_build_notification_command_defaults_failure_category_and_priority(self) -> None:
        command = MODULE.build_notification_command(
            {
                "noticli_bin": "noticli",
                "sender": "noMore404",
            },
            {
                "title": "{domain} {status}",
                "message": "{report}",
            },
            report="example.com FAIL\nchecks:\n- 200: fail (fail)",
            domain="example.com",
            status="FAIL",
        )

        self.assertEqual(
            command,
            [
                "noticli",
                "send",
                "--sender",
                "noMore404",
                "--category",
                "FAIL",
                "--title",
                "example.com FAIL",
                "--message",
                "example.com FAIL\nchecks:\n- 200: fail (fail)",
                "--priority",
                "HIGH",
            ],
        )


if __name__ == "__main__":
    unittest.main()
