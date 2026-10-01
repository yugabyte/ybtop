"""Hint-text handling for plan pinning (src/ybtop/queries.py).

pg_hint_plan's hint table stores the hint body without the ``/*+ ... */`` wrapper.
The YugabyteDB docs strip it with ``substring(hints from 5 for length-7)``, which
silently shifts by a character if the spacing differs; these cover the tolerant
version, because a hint pinned one character short is a corrupt hint.

Run:  python -m unittest discover -s tests
"""

import unittest

from ybtop.queries import (
    HINT_TABLE_APPLICATION_NAME,
    HINT_TABLE_GUCS,
    _HINT_GUC_NAME_OK,
    strip_hint_wrapper,
)


class StripHintWrapperTest(unittest.TestCase):
    def test_strips_standard_wrapper(self):
        self.assertEqual(
            strip_hint_wrapper("/*+ IndexOnlyScan(reports idx) Set(geqo false) */"),
            "IndexOnlyScan(reports idx) Set(geqo false)",
        )

    def test_strips_wrapper_with_no_inner_spaces(self):
        # The docs' substring(from 5) would eat the "I" here.
        self.assertEqual(strip_hint_wrapper("/*+IndexScan(t)*/"), "IndexScan(t)")

    def test_strips_wrapper_with_extra_whitespace(self):
        self.assertEqual(strip_hint_wrapper("/*+    Leading((a b))    */"), "Leading((a b))")

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(strip_hint_wrapper("  /*+ NestLoop(a b) */\n"), "NestLoop(a b)")

    def test_leaves_unwrapped_text_alone(self):
        self.assertEqual(strip_hint_wrapper("IndexScan(t)"), "IndexScan(t)")

    def test_empty_and_none_are_empty(self):
        self.assertEqual(strip_hint_wrapper(""), "")
        self.assertEqual(strip_hint_wrapper(None), "")
        self.assertEqual(strip_hint_wrapper("/*+ */"), "")
        self.assertEqual(strip_hint_wrapper("/*+*/"), "")

    def test_inner_block_comment_markers_are_preserved(self):
        # Only the outermost wrapper is removed; the body is passed through as-is.
        self.assertEqual(
            strip_hint_wrapper("/*+ IndexScan(t) /* inner */ */"),
            "IndexScan(t) /* inner */",
        )

    def test_pins_under_empty_application_name(self):
        # pg_hint_plan treats the empty application_name as "any client"; a
        # non-empty value here would make pinned hints apply to nothing.
        self.assertEqual(HINT_TABLE_APPLICATION_NAME, "")


class HintTableGucNamesTest(unittest.TestCase):
    """GUC names are interpolated into ALTER DATABASE as SQL text.

    A GUC name is not an identifier, so it cannot be quoted like one; the safety
    property is that these values are module constants matching a strict pattern,
    never anything from a request. set_hint_table_gucs re-checks the pattern at
    call time so a later edit cannot quietly make this an injection point.
    """

    def test_both_required_gucs_are_listed(self):
        self.assertEqual(
            set(HINT_TABLE_GUCS),
            {
                "pg_hint_plan.enable_hint_table",
                "pg_hint_plan.yb_use_query_id_for_hinting",
            },
        )

    def test_every_listed_guc_passes_the_guard(self):
        for guc in HINT_TABLE_GUCS:
            self.assertRegex(guc, _HINT_GUC_NAME_OK)

    def test_guard_rejects_anything_that_could_carry_sql(self):
        for bad in (
            "pg_hint_plan.enable_hint_table = on; DROP DATABASE yugabyte",
            "pg_hint_plan.x --",
            "other_ext.setting",
            "pg_hint_plan.",
            "pg_hint_plan.Bad_Case",
            "pg_hint_plan.a b",
            'pg_hint_plan."x"',
            "",
        ):
            self.assertNotRegex(bad, _HINT_GUC_NAME_OK, bad)


if __name__ == "__main__":
    unittest.main()


class PinStateFromStatusTest(unittest.TestCase):
    """What the viewer's pin row may offer, derived from qpm_status().

    The case that motivated this: a fresh cluster ships pg_hint_plan but has not
    created it, and the viewer used to answer with "Run: CREATE EXTENSION" -- an
    instruction to leave the page. It must now come back as installable.
    """

    @staticmethod
    def _st(**kw):
        base = {
            "hint_plan_available": True,
            "hint_plan_installed": False,
            "hint_table_present": False,
            "enable_hint_table": "off",
            "use_query_id_for_hinting": "off",
        }
        base.update(kw)
        return base

    def setUp(self):
        from ybtop.serve import pin_state_from_status

        self.f = pin_state_from_status

    def test_fresh_cluster_is_installable_not_a_dead_end(self):
        s = self.f(self._st(), None)
        self.assertFalse(s["available"])
        self.assertTrue(s["installable"])
        self.assertNotIn("CREATE EXTENSION", s["reason"])

    def test_installed_extension_is_ready(self):
        s = self.f(self._st(hint_plan_installed=True, hint_table_present=True), None)
        self.assertTrue(s["available"])
        self.assertFalse(s["installable"])
        self.assertIsNone(s["reason"])

    def test_extension_not_shipped_is_neither(self):
        s = self.f(self._st(hint_plan_available=False), None)
        self.assertFalse(s["available"])
        self.assertFalse(s["installable"])
        self.assertIn("not available", s["reason"])

    def test_half_installed_is_not_offered_a_noop_install(self):
        # CREATE EXTENSION IF NOT EXISTS would do nothing here, so do not offer it.
        s = self.f(self._st(hint_plan_installed=True, hint_table_present=False), None)
        self.assertFalse(s["available"])
        self.assertFalse(s["installable"])
        self.assertIn("missing", s["reason"])

    def test_pinned_row_is_reported(self):
        ready = self._st(hint_plan_installed=True, hint_table_present=True)
        self.assertTrue(self.f(ready, {"hints": "SeqScan(t)"})["pinned"])
        self.assertEqual(self.f(ready, {"hints": "SeqScan(t)"})["hints"], "SeqScan(t)")
        self.assertFalse(self.f(ready, None)["pinned"])

    def test_guc_values_are_parsed_case_insensitively(self):
        on = self.f(self._st(enable_hint_table="ON", use_query_id_for_hinting="on"), None)
        self.assertTrue(on["prereq"]["enable_hint_table"])
        self.assertTrue(on["prereq"]["use_query_id_for_hinting"])
        off = self.f(self._st(enable_hint_table=None, use_query_id_for_hinting=""), None)
        self.assertFalse(off["prereq"]["enable_hint_table"])
        self.assertFalse(off["prereq"]["use_query_id_for_hinting"])


class PinTargetDatabaseTest(unittest.TestCase):
    """Pinning must land in the plan's own database.

    Hint tables and ALTER DATABASE settings are per database. The collector's seed
    connection is one database (often `yugabyte`) while the workload may run in
    another, so resolving the target from the seed pinned into a database the
    query never runs in -- no effect, reported as success.
    """

    DOC = {
        "yb_pg_stat_plans": {
            "plans": {"aaaa": {"plan": "[]", "hints": "/*+ SeqScan(t) */"}},
            "databases": {"16640": "app"},
            "per_node": {
                "n1:5433": [
                    {"queryid": "7", "planid": "9", "plan_ref": "aaaa", "dbid": "16640"},
                    {"queryid": "7", "planid": "9", "plan_ref": "aaaa", "dbid": "16385"},
                ]
            },
        }
    }

    def test_finds_plan_only_with_its_database(self):
        from ybtop.serve import find_recorded_plan

        self.assertEqual(
            find_recorded_plan(self.DOC, "7", "9", "aaaa", "16640"), (True, "/*+ SeqScan(t) */")
        )
        self.assertEqual(find_recorded_plan(self.DOC, "7", "9", "aaaa", "99999"), (False, None))
        self.assertEqual(find_recorded_plan(self.DOC, "7", "9", "bbbb", "16640"), (False, None))

    def test_dsn_is_repointed_at_the_target_database(self):
        import psycopg.conninfo as ci
        from ybtop.serve import dsn_for_database

        seed = ci.make_conninfo(
            host="10.0.0.5", port="5433", user="yugabyte", password="p@ss:w/rd", dbname="yugabyte",
            sslmode="require",
        )
        d = ci.conninfo_to_dict(dsn_for_database(seed, "app"))
        self.assertEqual(d["dbname"], "app")
        self.assertEqual(d["host"], "10.0.0.5")
        self.assertEqual(d["password"], "p@ss:w/rd")
        self.assertEqual(d["sslmode"], "require")


class QueryidHintKeysTest(unittest.TestCase):
    """Only queryid-keyed hint rows can be matched to a statement row."""

    def test_keeps_queryids_drops_text_keys_dedupes_and_sorts(self):
        from ybtop.queries import queryid_hint_keys

        self.assertEqual(
            queryid_hint_keys(["42", " -7 ", "SELECT * FROM t WHERE a = ?", "42", None, "", "1e3"]),
            ["-7", "42"],
        )

    def test_empty(self):
        from ybtop.queries import queryid_hint_keys

        self.assertEqual(queryid_hint_keys([]), [])
        self.assertEqual(queryid_hint_keys(None), [])
