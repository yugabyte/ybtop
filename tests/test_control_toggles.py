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

from ybtop.control import CONTROL_DEFAULTS, control_path, read_control, write_control


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


if __name__ == "__main__":
    unittest.main()
