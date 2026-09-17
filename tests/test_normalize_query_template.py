"""normalize_query_template: Python cases plus browser parity.

The browser (src/ybtop/web/app.js) carries an identical implementation; the parity test runs it
under node and is skipped when node is not installed.

Run:  python -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from ybtop.histogram import normalize_query_template  # noqa: E402

# (input, expected template)
CASES = [
    # comments
    ("/*rewritten_pid='42'*/ SELECT id FROM orders WHERE id = $1", "SELECT id FROM orders WHERE id = $N"),
    (
        "/*dddbs='api',ddps='api',traceparent='00-abc-01'*/\n-- POST /invoices/authorize\nSELECT id FROM orders WHERE id = $1",
        "SELECT id FROM orders WHERE id = $N",
    ),
    ("SELECT /*+ IndexScan(t) */ id FROM t WHERE id = $1 -- trailing tag", "SELECT /*+ IndexScan(t) */ id FROM t WHERE id = $N"),
    ("SELECT /*+ IndexScan(t) -- prefer idx */ id FROM t WHERE id = $1", "SELECT /*+ IndexScan(t) -- prefer idx */ id FROM t WHERE id = $N"),
    ("/*+ Leading((a b)) */ SELECT -- route\n a FROM t WHERE x = $1", "/*+ Leading((a b)) */ SELECT a FROM t WHERE x = $N"),
    ("/* a -- b */ SELECT 1", "SELECT 1"),
    ("/*+ Leading(a b) -- unterminated hint", "/*+ Leading(a b) -- unterminated hint"),
    ("SELECT 1\r\n-- win\r\nFROM t", "SELECT 1 FROM t"),
    ("SELECT 1 --\tc\nFROM t", "SELECT 1 FROM t"),
    ("SELECT 1 --", "SELECT 1"),
    ("SELECT 1--2", "SELECT 1"),
    ("SELECT a FROM t WHERE c IN ($1, -- note\n $2) AND d = $3", "SELECT a FROM t WHERE c IN (...) AND d = $N"),
    ("-- only a comment", ""),
    # `--` that is not a comment
    ("SELECT * FROM t WHERE code = 'A--1' AND x = $1", "SELECT * FROM t WHERE code = 'A--1' AND x = $N"),
    ("SELECT 'it''s -- fine', a FROM t -- real comment", "SELECT 'it''s -- fine', a FROM t"),
    ("SELECT E'a\\'--b', a FROM t -- real comment", "SELECT E'a\\'--b', a FROM t"),
    ("SELECT U&'d\\0061t -- not a comment', a FROM t -- r", "SELECT U&'d\\0061t -- not a comment', a FROM t"),
    ("SELECT '\U0001F642 -- x', a FROM t -- r", "SELECT '\U0001F642 -- x', a FROM t"),
    ("SELECT 'abc -- unterminated", "SELECT 'abc -- unterminated"),
    ('SELECT "weird--col", a FROM t WHERE b = $1', 'SELECT "weird--col", a FROM t WHERE b = $N'),
    ('SELECT "a""--b" FROM t -- r', 'SELECT "a""--b" FROM t'),
    ("COMMENT ON TABLE t IS 'why -- because'", "COMMENT ON TABLE t IS 'why -- because'"),
    (
        "CREATE FUNCTION f() RETURNS int AS $$ -- body note\n SELECT 1 $$ LANGUAGE sql -- outer",
        "CREATE FUNCTION f() RETURNS int AS $$ -- body note SELECT 1 $$ LANGUAGE sql",
    ),
    ("DO $$ BEGIN -- inside\n PERFORM 1; END $$ -- outer", "DO $$ BEGIN -- inside PERFORM 1; END $$"),
    ("SELECT $1::text || $$--$$ -- r", "SELECT $N::text || $$--$$"),
    # `$` inside identifiers is not a dollar quote
    ("SELECT foo$bar$ FROM t WHERE id = $1 -- route tag", "SELECT foo$bar$ FROM t WHERE id = $N"),
    ("SELECT a FROM t WHERE b$c = $1 -- route tag", "SELECT a FROM t WHERE b$c = $N"),
    # IN / VALUES / placeholders / whitespace
    ("SELECT * FROM t WHERE id IN (1, 2, 3)", "SELECT * FROM t WHERE id IN (...)"),
    ("SELECT * FROM t WHERE c IN (SELECT c FROM u WHERE d = $1)", "SELECT * FROM t WHERE c IN (SELECT c FROM u WHERE d = $N)"),
    (
        "UPDATE t AS x SET a = v.a FROM (VALUES ($1,'x'),($2,'y')) AS v(a,b) WHERE x.id = v.b",
        "UPDATE t AS x SET a = v.a FROM (VALUES (...)) AS v(a,b) WHERE x.id = v.b",
    ),
    ("SELECT a FROM t WHERE b = $1 AND c = $2", "SELECT a FROM t WHERE b = $N AND c = $N"),
    ("SELECT   a\n\tFROM t ", "SELECT a FROM t"),
    ("", ""),
    (None, ""),
]

NODE_PARITY_SCRIPT = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.APP_JS, "utf8");
const a = src.indexOf("const HIST_REWRITE_COMMENT_RE");
const b = src.indexOf("function queryTemplateKey(");
const norm = new Function(src.slice(a, b) + "\nreturn normalizeQueryTemplate;")();
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map((q) => norm(q))));
"""


class NormalizeQueryTemplateTest(unittest.TestCase):
    def test_python(self):
        for query, want in CASES:
            with self.subTest(query=query):
                self.assertEqual(normalize_query_template(query), want)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_browser_parity(self):
        app_js = os.path.join(ROOT, "src", "ybtop", "web", "app.js")
        run = subprocess.run(
            ["node", "-e", NODE_PARITY_SCRIPT],
            input=json.dumps([q for q, _ in CASES]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "APP_JS": app_js},
            check=True,
        )
        got = json.loads(run.stdout)
        self.assertEqual(len(got), len(CASES))
        for (query, want), js in zip(CASES, got):
            with self.subTest(query=query):
                self.assertEqual(js, want)


if __name__ == "__main__":
    unittest.main()
