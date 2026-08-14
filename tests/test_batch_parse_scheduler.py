"""Regression tests for offline batch-parse scheduling behavior."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import OT_ui


class _Timer:
    def __init__(self):
        self.active = False
        self.start_calls: list[int] = []
        self.stop_calls = 0

    def isActive(self) -> bool:
        return self.active

    def start(self, milliseconds: int) -> None:
        self.active = True
        self.start_calls.append(milliseconds)

    def stop(self) -> None:
        self.active = False
        self.stop_calls += 1


class _Progress:
    def __init__(self):
        self.values: list[int] = []

    def setValue(self, value: int) -> None:
        self.values.append(value)


class _BatchParseHarness:
    schedule_next_parse_wave_if_needed = OT_ui.MainWindow.schedule_next_parse_wave_if_needed
    dispatch_next_parse_wave = OT_ui.MainWindow.dispatch_next_parse_wave
    finish_batch_parse_item = OT_ui.MainWindow.finish_batch_parse_item

    def __init__(self):
        self._task_stop_requested = False
        self.running_parse_workers: dict[int, object] = {}
        self.running_parse_sources: dict[int, Path] = {}
        self.batch_parse_queue: list[Path] = []
        self._batch_parse_next_wave_earliest = 0.0
        self._batch_parse_waiting_for_wave = False
        self._batch_parse_timer = _Timer()
        self._batch_parse_wave_size = 2
        self._batch_parse_wave_index = 0
        self._batch_parse_wave_interval_seconds = 60.0
        self._batch_parse_total = 2
        self._batch_parse_done = 0
        self._batch_parse_failed = 0
        self._batch_parse_success_markdowns: list[Path] = []
        self._batch_parse_translation_accepting_sources = False
        self._batch_parse_active_status: dict[str, str] = {}
        self._batch_translate_total = 0
        self._batch_layout_translate_total = 0
        self.current_original_path = None
        self.worker = None
        self.progress = _Progress()
        self.logs: list[str] = []
        self.finished_count = 0
        self.started_paths: list[Path] = []
        self.enqueued_translations: list[Path] = []

    def append_log(self, message: str) -> None:
        self.logs.append(message)

    def finish_batch_parse(self) -> None:
        self.finished_count += 1

    def confirm_duplicate_parse(self, _path: Path) -> bool:
        return True

    def start_batch_parse_file(self, path: Path) -> None:
        self.started_paths.append(path)

    def update_batch_progress_panel(self) -> None:
        pass

    def enqueue_parsed_document_for_translation(self, path: Path) -> None:
        self.enqueued_translations.append(path)


class BatchParseSchedulerTests(unittest.TestCase):
    def test_completed_item_waits_for_next_wave_without_name_error(self):
        window = _BatchParseHarness()
        first = Path("first.pdf")
        second = Path("second.pdf")
        window.running_parse_workers[1] = object()
        window.running_parse_sources[1] = first
        window.batch_parse_queue = [second]
        window._batch_parse_next_wave_earliest = 101.2

        with patch.object(OT_ui.time, "monotonic", return_value=100.0):
            window.finish_batch_parse_item(1, True, "解析完成", "first.cleaned.md")

        self.assertEqual(window._batch_parse_done, 1)
        self.assertEqual(window._batch_parse_success_markdowns, [Path("first.cleaned.md")])
        self.assertEqual(window._batch_parse_timer.start_calls, [1200])
        self.assertIn("下一批解析将在 2 秒后启动。", window.logs)

    def test_wait_notice_is_logged_once_while_timer_is_rescheduled(self):
        window = _BatchParseHarness()
        window.batch_parse_queue = [Path("next.pdf")]
        window._batch_parse_next_wave_earliest = 102.0

        with patch.object(OT_ui.time, "monotonic", return_value=100.0):
            window.schedule_next_parse_wave_if_needed()
            window.schedule_next_parse_wave_if_needed()

        self.assertEqual(window.logs, ["下一批解析将在 2 秒后启动。"])
        self.assertEqual(window._batch_parse_timer.start_calls, [2000, 2000])
        self.assertEqual(window._batch_parse_timer.stop_calls, 1)

    def test_due_wave_dispatches_the_configured_number_of_files(self):
        window = _BatchParseHarness()
        window.batch_parse_queue = [Path("one.pdf"), Path("two.pdf"), Path("three.pdf")]
        window._batch_parse_wave_interval_seconds = 45.0

        with patch.object(OT_ui.time, "monotonic", return_value=200.0):
            window.schedule_next_parse_wave_if_needed()

        self.assertEqual(window.started_paths, [Path("one.pdf"), Path("two.pdf")])
        self.assertEqual(window.batch_parse_queue, [Path("three.pdf")])
        self.assertEqual(window._batch_parse_wave_index, 1)
        self.assertEqual(window._batch_parse_next_wave_earliest, 245.0)

    def test_stop_or_empty_queue_finishes_batch_without_dispatching(self):
        stopped = _BatchParseHarness()
        stopped._task_stop_requested = True
        stopped.batch_parse_queue = [Path("next.pdf")]
        stopped.schedule_next_parse_wave_if_needed()

        empty = _BatchParseHarness()
        empty.schedule_next_parse_wave_if_needed()

        self.assertEqual(stopped.finished_count, 1)
        self.assertEqual(empty.finished_count, 1)
        self.assertEqual(stopped.started_paths, [])
        self.assertEqual(empty.started_paths, [])

    def test_successful_parse_enters_translation_queue_before_parse_batch_ends(self):
        window = _BatchParseHarness()
        window._batch_parse_translation_accepting_sources = True
        window._batch_translate_total = 2
        window._batch_layout_translate_total = 2
        source = Path("first.pdf")
        window.running_parse_workers[1] = object()
        window.running_parse_sources[1] = source
        window.batch_parse_queue = [Path("second.pdf")]

        with patch.object(OT_ui.time, "monotonic", return_value=0.0):
            window.finish_batch_parse_item(1, True, "解析完成", "first.cleaned.md")

        self.assertEqual(window.enqueued_translations, [Path("first.cleaned.md")])
        self.assertEqual(window.started_paths, [Path("second.pdf")])

    def test_mineru_uses_ten_slots_when_legacy_default_is_one(self):
        window = SimpleNamespace(settings=SimpleNamespace(batch_concurrency=1))

        self.assertEqual(OT_ui.MainWindow.configured_batch_concurrency(window), 10)
