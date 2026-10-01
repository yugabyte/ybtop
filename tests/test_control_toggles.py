"""Viewer-writable collection toggles (src/ybtop/control.py).

The collector re-reads this file every checkpoint, so it must never be able to
kill the loop: a missing, truncated, malformed or wrong-typed file has to read as
defaults rather than raise. Unknown keys must not be persisted, since the file is
written from an HTTP request.

Run:  python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from ybtop.cli import apply_query_plans_flag, query_plans_wanted
from ybtop.control import (
    CONTROL_DEFAULTS,
    ControlLocked,
    control_path,
    read_control,
    read_control_locks,
    write_control,
)


class ControlTogglesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_absent_file_reads_as_defaults(self):
        self.assertEqual(read_control(self.dir), dict(CONTROL_DEFAULTS))
        self.assertFalse(read_control(self.dir)["query_plans"])

    def test_write_then_read_round_trips(self):
        self.assertEqual(write_control(self.dir, {"query_plans": True})["query_plans"], True)
        self.assertTrue(read_control(self.dir)["query_plans"])
        write_control(self.dir, {"query_plans": False})
        self.assertFalse(read_control(self.dir)["query_plans"])

    def test_unknown_keys_are_not_persisted(self):
        write_control(self.dir, {"query_plans": True, "latency_histograms": True, "evil": 1})
        doc = json.loads(control_path(self.dir).read_text(encoding="utf-8"))
        self.assertNotIn("latency_histograms", doc)
        self.assertNotIn("evil", doc)
        self.assertTrue(doc["query_plans"])

    def test_write_stamps_update_time(self):
        write_control(self.dir, {"query_plans": True})
        doc = json.loads(control_path(self.dir).read_text(encoding="utf-8"))
        self.assertIn("updated_utc", doc)
        self.assertTrue(doc["updated_utc"].endswith("+00:00"))

    def test_malformed_file_reads_as_defaults(self):
        # A hand-edited or half-written file must not take the collector down.
        for bad in ("{ not json", "", "null", '["a", "list"]', '"a string"', "42"):
            control_path(self.dir).write_text(bad, encoding="utf-8")
            self.assertEqual(read_control(self.dir), dict(CONTROL_DEFAULTS), bad)

    def test_non_boolean_values_are_coerced(self):
        control_path(self.dir).write_text('{"query_plans": "yes"}', encoding="utf-8")
        self.assertIs(read_control(self.dir)["query_plans"], True)
        control_path(self.dir).write_text('{"query_plans": 0}', encoding="utf-8")
        self.assertIs(read_control(self.dir)["query_plans"], False)

    def test_write_preserves_untouched_toggles(self):
        write_control(self.dir, {"query_plans": True})
        # An update naming no toggles leaves the saved state alone.
        self.assertTrue(write_control(self.dir, {})["query_plans"])
        self.assertTrue(read_control(self.dir)["query_plans"])

    def test_unreadable_directory_reads_as_defaults(self):
        missing = self.dir / "does-not-exist"
        self.assertEqual(read_control(missing), dict(CONTROL_DEFAULTS))

    def test_write_to_readonly_dir_raises_oserror(self):
        ro = self.dir / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            with self.assertRaises(OSError):
                write_control(ro, {"query_plans": True})
        finally:
            os.chmod(ro, 0o700)

    def test_write_leaves_no_temp_files_behind(self):
        write_control(self.dir, {"query_plans": True})
        leftovers = [p.name for p in self.dir.iterdir() if p.name.startswith(".ybtop.control.")]
        self.assertEqual(leftovers, [])


class NoSnapshotQueryPlansTest(unittest.TestCase):
    """watch --no-snapshot-query-plans: never collect, whatever the viewer's toggle says."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_flag_locks_collection_off(self):
        apply_query_plans_flag(self.dir, False)
        self.assertEqual(read_control_locks(self.dir), {"query_plans": "--no-snapshot-query-plans"})
        with self.assertRaises(ControlLocked) as cm:
            write_control(self.dir, {"query_plans": True})  # the viewer's switch
        self.assertEqual(cm.exception.flag, "--no-snapshot-query-plans")
        self.assertFalse(read_control(self.dir)["query_plans"])
        write_control(self.dir, {"query_plans": False})  # not a change: fine
        self.assertFalse(query_plans_wanted(self.dir, False))

    def test_the_flag_wins_over_a_file_rewritten_after_startup(self):
        apply_query_plans_flag(self.dir, False)
        control_path(self.dir).write_text('{"query_plans": true}', encoding="utf-8")
        self.assertFalse(query_plans_wanted(self.dir, False))
        self.assertTrue(query_plans_wanted(self.dir, None))

    def test_a_run_without_the_flag_lifts_the_lock(self):
        apply_query_plans_flag(self.dir, False)
        apply_query_plans_flag(self.dir, None)
        self.assertEqual(read_control_locks(self.dir), {})
        self.assertFalse(read_control(self.dir)["query_plans"])  # the value stays; only the lock goes
        write_control(self.dir, {"query_plans": True})
        self.assertTrue(query_plans_wanted(self.dir, None))

    def test_snapshot_query_plans_only_seeds_the_toggle(self):
        apply_query_plans_flag(self.dir, True)
        self.assertTrue(query_plans_wanted(self.dir, True))
        write_control(self.dir, {"query_plans": False})
        self.assertFalse(query_plans_wanted(self.dir, True))

    def test_a_request_cannot_set_or_clear_a_lock(self):
        apply_query_plans_flag(self.dir, False)
        write_control(self.dir, {"locked": {}})
        write_control(self.dir, {})
        self.assertEqual(read_control_locks(self.dir), {"query_plans": "--no-snapshot-query-plans"})

    def test_no_flag_and_no_lock_leaves_the_directory_alone(self):
        apply_query_plans_flag(self.dir, None)
        self.assertFalse(control_path(self.dir).exists())

    def test_malformed_locks_read_as_none(self):
        for bad in ('{"locked": "x"}', '{"locked": ["query_plans"]}', '{"locked": {"evil": "x"}}', "nope"):
            control_path(self.dir).write_text(bad, encoding="utf-8")
            self.assertEqual(read_control_locks(self.dir), {}, bad)


if __name__ == "__main__":
    unittest.main()
