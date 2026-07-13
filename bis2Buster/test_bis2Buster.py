from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


MODULE_PATH = Path(__file__).with_name("bis2Buster.py")
SPEC = importlib.util.spec_from_file_location("bis2Buster", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Bis2BusterTests(unittest.TestCase):
    def test_deep_merge_overrides_nested_sections(self) -> None:
        base = {
            "notify": {
                "success": {"category": "SUCCESS", "message": "{report}"},
                "error": {"category": "FAIL", "priority": "HIGH", "message": "{report}"},
            }
        }
        override = {"notify": {"error": {"title": "{job} DOWN"}}}

        merged = MODULE.deep_merge(base, override)

        self.assertEqual(merged["notify"]["success"]["category"], "SUCCESS")
        self.assertEqual(merged["notify"]["error"]["category"], "FAIL")
        self.assertEqual(merged["notify"]["error"]["priority"], "HIGH")
        self.assertEqual(merged["notify"]["error"]["title"], "{job} DOWN")

    def test_current_day_window_and_queue_limit(self) -> None:
        now = datetime(2026, 7, 13, 15, 30, tzinfo=ZoneInfo("America/Sao_Paulo"))

        window = MODULE.resolve_time_window(
            {
                "timezone": "America/Sao_Paulo",
                "period_mode": "current_day",
                "queue_age_hours": 3,
            },
            now=now,
        )

        self.assertEqual(MODULE.mysql_datetime(window.start), "2026-07-13 00:00:00")
        self.assertEqual(MODULE.mysql_datetime(window.end), "2026-07-14 00:00:00")
        self.assertEqual(MODULE.mysql_datetime(window.queue_limit), "2026-07-13 12:30:00")

    def test_default_window_uses_last_three_months(self) -> None:
        now = datetime(2026, 7, 13, 15, 30, tzinfo=ZoneInfo("America/Sao_Paulo"))

        window = MODULE.resolve_time_window(
            {
                "timezone": "America/Sao_Paulo",
                "queue_age_hours": 3,
            },
            now=now,
        )

        self.assertEqual(MODULE.mysql_datetime(window.start), "2026-04-13 15:30:00")
        self.assertEqual(MODULE.mysql_datetime(window.end), "2026-07-13 15:30:00")
        self.assertEqual(MODULE.mysql_datetime(window.queue_limit), "2026-07-13 12:30:00")

    def test_last_three_months_clamps_day_when_month_is_shorter(self) -> None:
        now = datetime(2026, 5, 31, 8, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

        window = MODULE.resolve_time_window({"timezone": "America/Sao_Paulo"}, now=now)

        self.assertEqual(MODULE.mysql_datetime(window.start), "2026-02-28 08:00:00")
        self.assertEqual(MODULE.mysql_datetime(window.end), "2026-05-31 08:00:00")

    def test_explicit_window_requires_start_and_end(self) -> None:
        with self.assertRaises(MODULE.ConfigError):
            MODULE.resolve_time_window({"data_inicio": "2026-01-01T00:00:00"})

    def test_disabled_job_can_be_detected_from_config(self) -> None:
        self.assertFalse(MODULE.as_bool(False, "enabled", default=True))
        self.assertTrue(MODULE.as_bool(None, "enabled", default=True))

    def test_build_query_params_are_named_and_formatted(self) -> None:
        timezone = ZoneInfo("America/Sao_Paulo")
        window = MODULE.TimeWindow(
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone),
            end=datetime(2026, 1, 2, 0, 0, tzinfo=timezone),
            queue_limit=datetime(2026, 1, 1, 21, 0, tzinfo=timezone),
        )

        params = MODULE.build_query_params(window)

        self.assertIn("%(data_inicio)s", MODULE.CONSOLIDATED_QUERY)
        self.assertIn("%(data_fim)s", MODULE.CONSOLIDATED_QUERY)
        self.assertIn("%(fila_limite)s", MODULE.CONSOLIDATED_QUERY)
        self.assertEqual(
            params,
            {
                "data_inicio": "2026-01-01 00:00:00",
                "data_fim": "2026-01-02 00:00:00",
                "fila_limite": "2026-01-01 21:00:00",
            },
        )

    def test_normalize_counts_fills_missing_categories(self) -> None:
        counts = MODULE.normalize_counts(
            [
                {"titulo": "Problemas na SEFAZ", "quantidade": 2},
                {"titulo": "Cupons em Fila", "quantidade": "1"},
            ]
        )

        self.assertEqual(counts["Problemas na SEFAZ"], 2)
        self.assertEqual(counts["Cupons em Fila"], 1)
        self.assertEqual(counts["SEFAZ Offline (Direto)"], 0)
        self.assertEqual(counts["SEFAZ Offline (Corrigidos)"], 0)

    def test_evaluate_counts_marks_any_quantity_as_failure(self) -> None:
        checks = MODULE.evaluate_counts(
            {
                "Problemas na SEFAZ": 0,
                "Cupons em Fila": 1,
                "SEFAZ Offline (Direto)": 0,
                "SEFAZ Offline (Corrigidos)": 0,
            }
        )

        self.assertTrue(checks[0].ok)
        self.assertFalse(checks[1].ok)
        self.assertEqual(checks[1].detail, "1 cupom(ns) encontrado(s)")

    def test_format_report_is_multiline_and_succinct(self) -> None:
        timezone = ZoneInfo("America/Sao_Paulo")
        window = MODULE.TimeWindow(
            start=datetime(2026, 7, 13, 0, 0, tzinfo=timezone),
            end=datetime(2026, 7, 14, 0, 0, tzinfo=timezone),
            queue_limit=datetime(2026, 7, 13, 10, 0, tzinfo=timezone),
        )
        checks = [
            MODULE.CheckResult("Problemas na SEFAZ", 0, True, "nenhum cupom encontrado"),
            MODULE.CheckResult("Cupons em Fila", 2, False, "2 cupom(ns) encontrado(s)"),
        ]

        report = MODULE.format_report("bis2-producao", checks, False, window)

        self.assertEqual(
            report,
            "bis2-producao FAIL\n"
            "periodo: 2026-07-13 00:00:00 -> 2026-07-14 00:00:00\n"
            "fila_limite: 2026-07-13 10:00:00\n"
            "checks:\n"
            "- Problemas na SEFAZ: ok (nenhum cupom encontrado)\n"
            "- Cupons em Fila: fail (2 cupom(ns) encontrado(s))",
        )

    def test_build_notification_command_omits_success_priority(self) -> None:
        command = MODULE.build_notification_command(
            {"noticli_bin": "noticli", "sender": "bis2Buster"},
            {
                "category": "SUCCESS",
                "priority": "HIGH",
                "title": "{job} {status}",
                "message": "{report}",
            },
            report="bis2 OK",
            job="bis2",
            status="OK",
        )

        self.assertEqual(
            command,
            [
                "noticli",
                "send",
                "--sender",
                "bis2Buster",
                "--category",
                "SUCCESS",
                "--title",
                "bis2 OK",
                "--message",
                "bis2 OK",
            ],
        )

    def test_build_notification_command_defaults_failure_priority(self) -> None:
        command = MODULE.build_notification_command(
            {"noticli_bin": "noticli", "sender": "bis2Buster"},
            {"title": "{job} {status}", "message": "{report}"},
            report="bis2 FAIL",
            job="bis2",
            status="FAIL",
        )

        self.assertEqual(command[-2:], ["--priority", "HIGH"])
        self.assertIn("FAIL", command)


if __name__ == "__main__":
    unittest.main()
