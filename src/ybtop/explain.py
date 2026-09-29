"""EXPLAIN ANALYZE of a recorded statement, with its slowest recorded parameters.

Everything that executes comes from one of our snapshots: the statement text from
pg_stat_statements, the parameter values from yb_pg_stat_plans.max_exec_time_params,
the role and database from the QPM row. A request only NAMES a recorded row, plus
the EXPLAIN options and a timeout -- it never carries SQL or values.

Values are sent as bound parameters, never spliced into the SQL: they are whatever
an application once sent, so splicing them into a statement ybtop runs as a
superuser would hand SQL injection to anyone who can write to that application.
The inlined-literals text exists for display only.

A run is always one transaction that is rolled back. In outline -- the catalog
reads (the role's name and its settings) and set_config calls are left out:

    SET statement_timeout = <n>s           -- first, so the reads have a deadline too
    BEGIN [READ ONLY]                      -- READ ONLY unless it writes or locks rows
    SET LOCAL ROLE <role that ran it>      -- after that role's own ALTER ROLE settings
    SET LOCAL statement_timeout = <n>s     -- the client also cancels at <n>s
    yb_disable_transactional_writes off    -- checked, so the ROLLBACK undoes every write
    EXPLAIN (FORMAT JSON) <statement>      -- the plan only, to match against QPM's
    EXPLAIN (ANALYZE[, DIST][, DEBUG]) <statement>
    ROLLBACK
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# QPM keeps max_exec_time_params in a 256-byte slot. Text that does not fit is
# compressed; text that does not compress into it is cut to exactly 255 bytes,
# which can end mid-value or between the two quotes of an escaped '' -- so a
# 255-byte parameter string is never trusted.
PARAM_TEXT_SLOT_BYTES = 255

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 600

# A DEBUG dump of a large plan is tens of KB; past this it is a runaway, and it is
# held in the collector's memory for every run kept (ExplainRuns.MAX_KEPT).
MAX_PLAN_TEXT_CHARS = 500_000

# How long past its deadline a run may go silent before its slot is given up. By
# then statement_timeout, the client cancel and TCP keepalives have all had their
# chance; what is left is a node that stopped answering.
ABANDON_GRACE_S = 90

# Dead-peer detection for the replay's connection: without it a partition leaves
# the worker blocked in recv() for as long as the OS allows -- hours.
_CONNECT_OPTS = {
    "autocommit": True,
    "connect_timeout": 10,
    "application_name": "ybtop-explain",
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    "tcp_user_timeout": 30000,
}

# The marker keeps QPM from recording our run as the statement's new slowest
# execution -- the very thing we are reproducing.
SQL_PREFIX = "/* service:ybtop */ /* __YB_STAT_PLANS_SKIP */ "

# QPM records plans only for these command types; anything else is refused.
_READ_VERBS = {"SELECT", "WITH", "VALUES", "TABLE"}
_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "MERGE"}

_PARAM_HEAD = re.compile(r"\$([0-9]+) = ")
_PLACEHOLDER = re.compile(r"\$([0-9]+)")
_DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")
_WORD = re.compile(r"[A-Za-z]+")
# What PostgreSQL's lexer treats as whitespace; Unicode spaces are not.
_SQL_SPACE = " \t\n\r\f\v"
# The lexer folds only ASCII letters, so names are matched on this: re.IGNORECASE
# would also take "\u0131" (dotless i) for "i", which PostgreSQL does not.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

# Calls whose effect a ROLLBACK does not undo: they act on other sessions, the
# server, files, other databases, a sequence's current value (setval) or
# statistics. Checked on the code only (strings and comments blanked), in ASCII
# lower case, with only the whitespace PostgreSQL's lexer knows before the "(":
# anything else, a no-break space say, is part of the name. nextval() is let
# through: like a serial or identity default in a replayed INSERT, it leaves a
# gap in the sequence, as any rolled-back transaction does. A user-defined
# function can still hide such an effect.
_SIDE_EFFECT_CALL = re.compile(
    r'(?<![A-Za-z0-9_$\x80-\U0010ffff])"?('
    r"pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote"
    r"|pg_create_restore_point|pg_switch_wal|pg_(?:start|stop)_backup|pg_backup_(?:start|stop)"
    r"|pg_stat_statements_reset|pg_stat_reset[a-z_]*|pg_stat_clear_snapshot"
    r"|pg_(?:create|drop)_[a-z_]*replication_slot|pg_replication_origin_[a-z_]+"
    r"|pg_file_[a-z_]+|lo_export|lo_import"
    r"|dblink[a-z_]*|setval"
    r"|yb_pg_stat_plans_(?:reset|insert)[a-z_]*|yb_reset_analyze_statistics"
    r"|yb_cancel_transaction|yb_query_diagnostics|yb_increment_[a-z_]+"
    r')"?[ \t\n\r\f\v]*\('
)


def parse_param_text(text: Optional[str]) -> "dict[int, Optional[str]]":
    """Inverse of PostgreSQL's BuildParamLogString, the format QPM stores.

    "$1 = 'it''s', $2 = NULL" -> {1: "it's", 2: None}. Raises ValueError on anything
    else, including numbering that is not exactly $1..$k.
    """
    out: "dict[int, Optional[str]]" = {}
    if text is None or text == "":
        return out
    i, n = 0, len(text)
    while True:
        m = _PARAM_HEAD.match(text, i)
        if not m:
            raise ValueError("expected $N = at offset %d" % i)
        num = int(m.group(1))
        i = m.end()
        if text.startswith("NULL", i):
            val: Optional[str] = None
            i += 4
        elif text.startswith("'", i):
            i += 1
            parts = []
            while True:
                j = text.find("'", i)
                if j < 0:
                    raise ValueError("unterminated value for $%d" % num)
                parts.append(text[i:j])
                if text.startswith("''", j):
                    parts.append("'")
                    i = j + 2
                    continue
                i = j + 1
                break
            val = "".join(parts)
        else:
            raise ValueError("expected a quoted value or NULL for $%d" % num)
        if num in out:
            raise ValueError("$%d appears twice" % num)
        out[num] = val
        if i == n:
            break
        if not text.startswith(", ", i) or i + 2 == n:
            raise ValueError("expected ', ' after $%d" % num)
        i += 2
    if sorted(out) != list(range(1, len(out) + 1)):
        raise ValueError("parameters are not numbered $1..$%d" % len(out))
    return out


def _scan(
    sql: str,
    on_placeholder: Callable[[int, int, int], None],
    on_literal: "Optional[Callable[[int, int], None]]" = None,
) -> None:
    """Walk SQL outside quotes, comments and dollar-quoted bodies.

    Calls on_placeholder(start, end, number) for every $N parameter reference, and
    on_literal(start, end) for every string, comment and dollar-quoted body. A $N
    glued to an identifier (foo$1 is a legal name) is not a reference.
    """
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        start = i
        if c == "'":
            # E'...' strings honour backslash escapes; plain ones only ''.
            esc = i > 0 and sql[i - 1] in "eE" and (i < 2 or not _ident_char(sql[i - 2]))
            i += 1
            while i < n:
                if esc and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            if on_literal:
                on_literal(start, min(i, n))
        elif c == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
            i += 1
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            if on_literal:
                on_literal(start, i)
        elif c == "/" and sql.startswith("/*", i):
            # Block comments nest in PostgreSQL.
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if on_literal:
                on_literal(start, i)
        elif c == "$":
            if i > 0 and _ident_char(sql[i - 1]):
                i += 1
                continue
            m = _PLACEHOLDER.match(sql, i)
            if m:
                on_placeholder(i, m.end(), int(m.group(1)))
                i = m.end()
                continue
            m = _DOLLAR_QUOTE.match(sql, i)
            if m:
                close = sql.find(m.group(0), m.end())
                i = n if close < 0 else close + len(m.group(0))
                if on_literal:
                    on_literal(start, i)
                continue
            i += 1
        else:
            i += 1


def _ident_char(ch: str) -> bool:
    """PostgreSQL's identifier bytes: ASCII letters, digits, _ and $, and anything
    non-ASCII -- the lexer takes every byte from 0x80 up as part of a name."""
    return ch in "_$" or ("0" <= ch <= "9") or ("a" <= ch.lower() <= "z") or ord(ch) >= 0x80


def placeholders(sql: str) -> "set[int]":
    """Parameter numbers referenced in a statement."""
    found: "set[int]" = set()
    _scan(sql, lambda a, b, num: found.add(num))
    return found


def sql_literal(value: Optional[str]) -> str:
    """A standard-conforming SQL literal, for DISPLAY: never executed."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def inline_literals(sql: str, values: "dict[int, Optional[str]]") -> str:
    """The statement with each $N shown as its recorded value -- display only."""
    out = []
    last = 0

    def repl(a: int, b: int, num: int) -> None:
        nonlocal last
        if num in values:
            out.append(sql[last:a])
            out.append(sql_literal(values[num]))
            last = b

    _scan(sql, repl)
    out.append(sql[last:])
    return "".join(out)


def param_text_problem(params_text: Optional[str]) -> Optional[str]:
    """Why these recorded parameters cannot be used, or None."""
    if params_text is None or params_text == "":
        return None
    if len(params_text.encode("utf-8")) == PARAM_TEXT_SLOT_BYTES:
        return (
            "QPM cut this statement's slowest parameters short (they did not fit its "
            "255-byte slot), so they cannot be replayed."
        )
    try:
        values = parse_param_text(params_text)
    except ValueError:
        return "QPM's record of the slowest parameters could not be read."
    if values and all(v == "?" for v in values.values()):
        return (
            "QPM shows the parameters as '?': yb_pg_stat_plans_show_max_exec_params is "
            "off for ybtop's login."
        )
    return None


def bind_values(sql: str, params_text: Optional[str]) -> "tuple[Optional[list], Optional[str]]":
    """([values for $1..$k], None) to bind, or (None, why the statement can't be replayed)."""
    problem = param_text_problem(params_text)
    if problem:
        return None, problem
    values = parse_param_text(params_text)
    refs = placeholders(sql)
    if not refs:
        if values:
            return None, "the recorded parameters do not match the statement text"
        return [], None
    top = max(refs)
    missing = [k for k in range(1, top + 1) if k not in values]
    if missing:
        if not values:
            return None, (
                "pg_stat_statements replaced this statement's constants with $1.. and QPM "
                "recorded no parameter values for it, so there is nothing to replay."
            )
        # Values are always $1..$k, so what is missing is past them: constants that
        # pg_stat_statements numbered after the statement's bind parameters.
        return None, (
            "pg_stat_statements replaced a constant in this statement with $%d, and QPM "
            "records bind parameters only, so its value is unknown." % missing[0]
        )
    if max(values) > top:
        return None, "the recorded parameters do not match the statement text"
    return [values[k] for k in range(1, top + 1)], None


def _first_verb(sql: str) -> str:
    """First keyword, past comments, whitespace and opening parentheses."""
    i, n = 0, len(sql)
    while i < n:
        if sql[i] in _SQL_SPACE or sql[i] == "(":
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        else:
            m = _WORD.match(sql, i)
            return m.group(0).upper() if m else ""
    return ""


_WRITE_PLAN_NODE = re.compile(r'"Node Type":\s*"(?:ModifyTable|LockRows)"')
_WRITE_PLAN_TEXT = re.compile(r"^\s*(?:->\s+)?(?:(?:Insert|Update|Delete|Merge) on |LockRows)", re.M)
_PLAN_OPERATION = re.compile(r'"Operation":\s*"(Insert|Update|Delete|Merge)"')
# FOR UPDATE / NO KEY UPDATE / SHARE / KEY SHARE. The recorded plan shows
# LockRows too, but a plan_ref can carry hints and no plan text.
_LOCKING_CLAUSE = re.compile(
    r"(?<![a-z0-9_$\x80-\U0010ffff])for[ \t\n\r\f\v]+"
    r"(no[ \t\n\r\f\v]+key[ \t\n\r\f\v]+update|update|key[ \t\n\r\f\v]+share|share)"
    r"(?![a-z0-9_$\x80-\U0010ffff])"
)
_QUOTED_NAME = re.compile(r'"(?:[^"]|"")*"?')


def locking_clause(sql: str) -> Optional[str]:
    """"UPDATE", "NO KEY UPDATE", "SHARE" or "KEY SHARE" if the statement locks rows
    by its text; strings, comments and quoted names are not looked at."""
    code = _QUOTED_NAME.sub(lambda m: " " * len(m.group(0)), _code_only(sql))
    m = _LOCKING_CLAUSE.search(code.translate(_ASCII_LOWER))
    return " ".join(m.group(1).upper().split()) if m else None


def statement_kind(sql: str, plan_text: Optional[str]) -> "tuple[Optional[str], str]":
    """("read" | "write" | None, label) -- None means this is not something to replay.

    "write" when the statement modifies or locks rows by its text (INSERT, UPDATE,
    DELETE, MERGE, a locking clause) or by its recorded plan (ModifyTable,
    LockRows): it then runs in a read-write transaction that is rolled back.
    Everything else runs READ ONLY, so a statement that writes after all (a
    function that writes, a data-modifying WITH its recorded plan does not show)
    fails instead of writing.
    """
    verb = _first_verb(sql)
    if verb not in _READ_VERBS and verb not in _WRITE_VERBS:
        return None, verb or "?"
    plan = plan_text or ""
    op = _PLAN_OPERATION.search(plan)
    lock = locking_clause(sql)
    writes = (
        verb in _WRITE_VERBS
        or lock is not None
        or bool(_WRITE_PLAN_NODE.search(plan))
        or bool(_WRITE_PLAN_TEXT.search(plan))
    )
    if verb in _WRITE_VERBS:
        label = verb
    elif op:
        label = verb + " … " + op.group(1).upper()
    elif writes:
        label = "SELECT … FOR " + (lock or "UPDATE") if verb == "SELECT" else verb + " (locks rows)"
    else:
        label = verb
    return ("write" if writes else "read"), label


def explain_options(dist: bool, debug: bool) -> "list[str]":
    """EXPLAIN options in order. YugabyteDB ignores DEBUG without DIST, so DEBUG implies it."""
    opts = ["ANALYZE"]
    if dist or debug:
        opts.append("DIST")
    if debug:
        opts.append("DEBUG")
    return opts


def explain_sql(statement: str, options: "list[str]") -> str:
    return SQL_PREFIX + "EXPLAIN (" + ", ".join(options) + ") " + statement


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def clamp_timeout(value: Any) -> int:
    """Seconds, 1..MAX_TIMEOUT_S; blank or not a plain number -> the default, never 1 s."""
    text = str(value).strip(" \t\n\r\f\v") if value is not None and not isinstance(value, bool) else ""
    if not _NUMBER.fullmatch(text):
        return DEFAULT_TIMEOUT_S
    secs = float(text)
    if not math.isfinite(secs):
        return DEFAULT_TIMEOUT_S
    return max(1, min(MAX_TIMEOUT_S, int(math.floor(secs + 0.5))))


# ---------------------------------------------------------------------------
# Snapshot resolution
# ---------------------------------------------------------------------------


def statement_text(doc: dict, queryid: str, datname: Optional[str], prefer_node: Optional[str]) -> Optional[str]:
    """pg_stat_statements text for a queryid: this database on this node first.

    The same queryid can carry slightly different text per node (comments are kept
    verbatim), so the node that recorded the parameters is preferred.
    """
    per = ((doc.get("pg_stat_statements") or {}).get("per_node")) or {}
    nodes = sorted(per)
    if prefer_node in per:
        nodes.remove(prefer_node)
        nodes.insert(0, prefer_node)
    fallback = None
    for nid in nodes:
        for r in per.get(nid) or []:
            if str(r.get("queryid")) != str(queryid) or not r.get("query"):
                continue
            if datname is None or str(r.get("dbname") or "") == str(datname):
                return str(r["query"])
            if fallback is None:
                fallback = str(r["query"])
    return fallback


def find_qpm_row(
    doc: dict, *, queryid: str, planid: str, plan_ref: str, dbid: str, node: str, userid: str
) -> Optional[dict]:
    """The recorded row, by QPM's own key -- which includes the user.

    QPM keys on (database, user, queryid, planid): two roles running one
    statement get two rows, with different slowest values, on the same node.
    """
    qpm = doc.get("yb_pg_stat_plans") or {}
    for r in ((qpm.get("per_node") or {}).get(node)) or []:
        if (
            str(r.get("queryid")) == str(queryid)
            and str(r.get("planid")) == str(planid)
            and str(r.get("plan_ref")) == str(plan_ref)
            and str(r.get("dbid")) == str(dbid)
            and str(r.get("userid")) == str(userid)
        ):
            return r
    return None


def resolve_target(
    doc: dict, *, queryid: str, planid: str, plan_ref: str, dbid: str, node: str, userid: str
) -> "tuple[Optional[dict], Optional[str]]":
    """Everything a run needs, from the snapshot alone, or (None, why not)."""
    row = find_qpm_row(
        doc, queryid=queryid, planid=planid, plan_ref=plan_ref, dbid=dbid, node=node, userid=userid
    )
    if row is None:
        return None, "that execution is not recorded in this snapshot"
    if row.get("userid") is None or str(row.get("userid")) == "":
        # Without it the replay would run as ybtop's own login -- usually a superuser.
        return None, "QPM did not record which role ran this statement, so it is not replayed."
    qpm = doc.get("yb_pg_stat_plans") or {}
    databases = qpm.get("databases") or {}
    datname = databases.get(str(dbid))
    if not datname:
        return None, database_unknown_reason(databases, dbid)
    sql = statement_text(doc, queryid, datname, node)
    if not sql:
        return None, "pg_stat_statements text for this query_id is not in the snapshot"
    plan_text = ((qpm.get("plans") or {}).get(str(plan_ref)) or {}).get("plan")
    kind, label = statement_kind(sql, plan_text)
    if kind is None:
        return None, not_replayable_reason(sql, label)
    fn = side_effect_call(sql)
    if fn:
        return None, side_effect_reason(fn)
    params_text = row.get("max_exec_time_params")
    values, why = bind_values(sql, params_text)
    if values is None:
        return None, why
    shown = parse_param_text(params_text) if params_text else {}
    userid = row.get("userid")
    return {
        "queryid": str(queryid),
        "planid": str(planid),
        "plan_ref": str(plan_ref),
        "dbid": str(dbid),
        "datname": str(datname),
        "node": str(node),
        "userid": None if userid is None else str(userid),
        "role": ((qpm.get("roles") or {}).get(str(userid))) if userid is not None else None,
        "statement": sql,
        "values": values,
        "params_text": params_text,
        "sql_display": inline_literals(sql, shown),
        "kind": kind,
        "label": label,
        "recorded_max_ms": row.get("max_exec_time"),
    }, None


def database_unknown_reason(databases: dict, dbid: str) -> str:
    """Why the snapshot has no name for a plan's database.

    The list is read once per snapshot, before the plans are; a cluster always
    has databases, so an empty list is one that could not be read.
    """
    if not databases:
        return (
            "this snapshot has no list of databases (reading it failed), so this plan's "
            "database (oid %s) is unknown; try a later snapshot" % dbid
        )
    return (
        "this plan's database (oid %s) is not in the snapshot's list of databases: it was "
        "dropped before the snapshot, or created while it was taken" % dbid
    )


def _code_only(sql: str) -> str:
    """The statement with strings, comments and dollar-quoted bodies blanked out."""
    chars = list(sql)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            chars[k] = " "

    _scan(sql, lambda a, b, num: None, blank)
    return "".join(chars)


def side_effect_call(sql: str) -> Optional[str]:
    """The first call in the statement whose effect a rollback would not undo."""
    m = _SIDE_EFFECT_CALL.search(_code_only(sql).translate(_ASCII_LOWER))
    return m.group(1) if m else None


def not_replayable_reason(sql: str, label: str) -> str:
    """Why statement_kind refused a statement."""
    if str(sql).lstrip(_SQL_SPACE).startswith("<"):
        return "pg_stat_statements does not show this statement's text to ybtop's login."
    return (
        "Only SELECT, WITH, VALUES, TABLE, INSERT, UPDATE, DELETE and MERGE statements are "
        "replayed; this one starts with %s." % (label or "?")
    )


def side_effect_reason(fn: str) -> str:
    return (
        "This statement calls %s(), whose effect a rollback does not undo, so it is not "
        "replayed." % fn
    )


def split_node(node: str) -> "tuple[str, str]":
    """"10.0.0.1:5433" -> ("10.0.0.1", "5433"); "[::1]:5433" -> ("::1", "5433")."""
    host, _, port = str(node).rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("node %r is not host:port" % node)
    return host.strip("[]"), port


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_EXECUTION_TIME = re.compile(r"^\s*Execution Time:\s*([0-9.]+)\s*ms", re.M)
_PLANNING_TIME = re.compile(r"^\s*Planning Time:\s*([0-9.]+)\s*ms", re.M)


def _ms(pattern: "re.Pattern[str]", text: str) -> Optional[float]:
    m = pattern.search(text or "")
    return float(m.group(1)) if m else None


class _Failed(Exception):
    def __init__(self, message: str, sqlstate: Optional[str] = None):
        super().__init__(message)
        self.sqlstate = sqlstate


def _exec(pgconn: Any, sql: str, values: "Optional[list]" = None) -> Any:
    """One statement over the extended protocol: bound values, and never more than one statement."""
    from psycopg import pq

    res = pgconn.exec_params(
        sql.encode("utf-8"),
        [None if v is None else v.encode("utf-8") for v in (values or [])],
    )
    if res.status not in (pq.ExecStatus.TUPLES_OK, pq.ExecStatus.COMMAND_OK):
        msg = (res.error_message or b"").decode("utf-8", "replace").strip()
        state = res.error_field(pq.DiagnosticField.SQLSTATE)
        raise _Failed(msg or "statement failed", state.decode() if state else None)
    return res


# A no-op on clusters without the setting: there is then no row to set.
_TXN_WRITES_OFF = (
    "SELECT set_config(name, 'off', true) FROM pg_settings WHERE name = 'yb_disable_transactional_writes'"
)

MAX_NOTICES = 50


def _rows(res: Any) -> "list[Optional[str]]":
    out = []
    for r in range(res.ntuples):
        v = res.get_value(r, 0)
        out.append(None if v is None else v.decode("utf-8", "replace"))
    return out


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def run_explain(
    dsn: str,
    target: dict,
    options: "list[str]",
    timeout_s: int,
    *,
    on_cancel_ready: "Optional[Callable[[Callable[[], None]], None]]" = None,
    connect: "Optional[Callable[..., Any]]" = None,
    stop: "Optional[threading.Event]" = None,
    may_analyze: "Optional[Callable[[], bool]]" = None,
) -> dict:
    """Run one EXPLAIN ANALYZE as described in the module docstring. Never raises.

    `stop` is set when the user cancels. It is checked between steps, so a cancel
    that lands while the connection is still being made stops the run before
    anything executes; `on_cancel_ready` hands over the cancel for a statement
    that is already running. `may_analyze` is asked last, right before the
    ANALYZE; False stops the run there (see ExplainRuns._work).
    """
    import psycopg

    result: "dict[str, Any]" = {
        "plan_text": None,
        "plan_json": None,
        "execution_ms": None,
        "planning_ms": None,
        "notices": [],
        "role": None,
        "role_settings": [],
        "error": None,
        "sqlstate": None,
        "timed_out": False,
        "cancelled": False,
    }
    cancelled_by: "dict[str, str]" = {}
    timer: Optional[threading.Timer] = None
    conn = None

    def stop_requested() -> None:
        if stop is not None and stop.is_set():
            cancelled_by.setdefault("by", "user")
            raise _Failed("Cancelled.", "57014")

    try:
        stop_requested()
        conn = (connect or psycopg.connect)(dsn, **_CONNECT_OPTS)
        pgconn = conn.pgconn
        # Taken before the statement starts: PQcancel on a cancel object is safe
        # from another thread, poking the busy connection is not.
        canceller = pgconn.get_cancel()

        def cancel(reason: str = "user") -> None:
            cancelled_by.setdefault("by", reason)
            try:
                canceller.cancel()
            except Exception:  # noqa: BLE001 - best effort
                pass

        if on_cancel_ready:
            on_cancel_ready(lambda: cancel("user"))
        stop_requested()
        # Session-wide first, so the role lookup and settings have a deadline too;
        # SET LOCAL below states it again for the transaction.
        _exec(pgconn, "SET statement_timeout = '%ds'" % int(timeout_s))
        def on_notice(d: Any) -> None:
            if len(result["notices"]) < MAX_NOTICES:
                result["notices"].append(("%s: %s" % (d.severity or "NOTICE", d.message_primary or ""))[:500])

        conn.add_notice_handler(on_notice)

        rolname = None
        if target.get("userid") is not None:
            rows = _rows(_exec(pgconn, SQL_PREFIX + "SELECT rolname FROM pg_roles WHERE oid = $1::oid", [target["userid"]]))
            if not rows or not rows[0]:
                raise _Failed("the role that ran this statement (oid %s) no longer exists" % target["userid"])
            rolname = rows[0]
        result["role"] = rolname

        _exec(pgconn, "BEGIN" + (" READ ONLY" if target["kind"] == "read" else ""))
        if rolname:
            # Settings first, while still the login: some are superuser-only, and
            # the role's own login would have been granted them at connect time.
            result["role_settings"] = _apply_role_settings(pgconn, target["userid"], target["dbid"])
            _exec(pgconn, "SET LOCAL ROLE " + _quote_ident(rolname))
        _exec(pgconn, "SET LOCAL statement_timeout = '%ds'" % int(timeout_s))
        # With yb_disable_transactional_writes on -- which a database, the login or
        # the role can set -- YugabyteDB writes DML outside the transaction and the
        # ROLLBACK below undoes nothing. Force it off and check it took.
        _exec(pgconn, SQL_PREFIX + _TXN_WRITES_OFF)
        state = _rows(_exec(pgconn, SQL_PREFIX + "SELECT current_setting('yb_disable_transactional_writes', true)"))
        if state and state[0] not in (None, "", "off"):
            raise _Failed(
                "yb_disable_transactional_writes stayed on for this session, so a write "
                "could not be rolled back; not replayed."
            )

        stmt = target["statement"]
        values = target["values"]
        # Plan only, nothing executes: the chosen plan's shape, to match against
        # the plans QPM recorded. Runs first so a timed-out ANALYZE still shows it.
        shape = _rows(_exec(pgconn, SQL_PREFIX + "EXPLAIN (FORMAT JSON) " + stmt, values))
        result["plan_json"] = shape[0] if shape else None

        stop_requested()
        if may_analyze is not None and not may_analyze():
            cancelled_by.setdefault("by", "user")
            raise _Failed("Cancelled.", "57014")
        # Same deadline on both sides: statement_timeout on the server, and a
        # cancel from here in case the server does not honour it in time.
        timer = threading.Timer(float(timeout_s), lambda: cancel("timeout"))
        timer.daemon = True
        timer.start()
        lines = _rows(_exec(pgconn, explain_sql(stmt, options), values))
        text = "\n".join(line or "" for line in lines)
        if len(text) > MAX_PLAN_TEXT_CHARS:
            text = text[:MAX_PLAN_TEXT_CHARS] + "\n... (truncated)"
        result["plan_text"] = text
        result["execution_ms"] = _ms(_EXECUTION_TIME, text)
        result["planning_ms"] = _ms(_PLANNING_TIME, text)
    except _Failed as exc:
        result["error"] = str(exc)
        result["sqlstate"] = exc.sqlstate
    except Exception as exc:  # noqa: BLE001 - connection trouble etc. is a result, not a crash
        result["error"] = str(exc).strip() or exc.__class__.__name__
    finally:
        if timer is not None:
            timer.cancel()
        if conn is not None:
            try:
                _exec(conn.pgconn, "ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if result["sqlstate"] == "25006":
        # "cannot execute UPDATE in a read-only transaction", and the like.
        if target.get("kind") == "read":
            why = (
                "Refused: this statement ran READ ONLY, since neither its text nor its "
                "recorded plan writes or locks rows, but it tried to (a function that "
                "writes, say). "
            )
        else:
            why = (
                "Refused: the write ran in a read-only transaction -- is "
                "default_transaction_read_only on for ybtop's login or this database? "
            )
        result["error"] = why + str(result["error"])
    if result["sqlstate"] == "57014":
        by = cancelled_by.get("by")
        if by == "user":
            result["cancelled"] = True
            result["error"] = "Cancelled."
        else:
            result["timed_out"] = True
            result["error"] = "Stopped at the %d s statement timeout." % int(timeout_s)
    return result


def _apply_role_settings(pgconn: Any, userid: str, dbid: str) -> "list[str]":
    """Apply the role's own ALTER ROLE ... SET values, as its login would have them.

    SET ROLE switches privileges but not settings; without this a role with its own
    search_path could resolve a table name to a different table than the
    application does. Only settings that can change mid-session are applied
    (extension placeholders and connection-time ones are skipped), in PostgreSQL's
    order: role, then role-in-database, so the more specific one wins.
    """
    sql = (
        SQL_PREFIX
        + "SELECT s.setting FROM ("
        + " SELECT 1 AS prio, unnest(setconfig) AS setting FROM pg_db_role_setting"
        + "  WHERE setrole = $1::oid AND setdatabase = 0"
        + " UNION ALL"
        + " SELECT 2, unnest(setconfig) FROM pg_db_role_setting"
        + "  WHERE setrole = $1::oid AND setdatabase = $2::oid) s"
        + " ORDER BY s.prio"
    )
    entries = [e for e in _rows(_exec(pgconn, sql, [str(userid), str(dbid)])) if e and "=" in e]
    if not entries:
        return []
    known = set(
        _rows(_exec(pgconn, SQL_PREFIX + "SELECT name FROM pg_settings WHERE context IN ('user', 'superuser')"))
    )
    applied = []
    for entry in entries:
        name, _, value = entry.partition("=")
        # Never let the role override the chosen timeout or the READ ONLY guard, and
        # skip what cannot change once the transaction has run a statement.
        if name not in known or name in (
            "statement_timeout",
            "transaction_read_only",
            "default_transaction_read_only",
            "transaction_isolation",
            "transaction_deferrable",
            "yb_disable_transactional_writes",
        ):
            continue
        _exec(pgconn, SQL_PREFIX + "SELECT set_config($1, $2, true)", [name, value])
        if name not in applied:
            applied.append(name)
    return applied


# ---------------------------------------------------------------------------
# Runs: one at a time per collector, the latest kept per statement
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExplainRuns:
    """In-memory run registry. One run in flight at a time: each is a real
    execution of a statement's slowest case on a production cluster."""

    MAX_KEPT = 25

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, dict]" = OrderedDict()
        self._active: Optional[str] = None
        self._cancels: "dict[str, Callable[[], None]]" = {}

    def _reap(self) -> None:
        """Give up on an active run that has gone silent well past its deadline.

        Called under the lock. Without this one hung node would hold the only slot
        until the collector restarts. The run is stopped, and its statement
        cancelled if it has a connection; a worker that gets an answer later stops
        at its next step and never starts the ANALYZE (see may_analyze in _work).
        Its result is dropped.
        """
        if self._active is None:
            return
        run = self._runs.get(self._active)
        if run is None:
            self._active = None
            return
        # From the ANALYZE once it has started: that is what its timeout bounds.
        since = run.get("_analyze_t0", run["_t0"])
        limit = run["timeout_s"] + ABANDON_GRACE_S + _CONNECT_OPTS["connect_timeout"]
        if time.monotonic() - since < limit:
            return
        run.update(
            {
                "state": "error",
                "finished_utc": _utc_now(),
                "elapsed_ms": round((time.monotonic() - run["_t0"]) * 1000.0, 1),
                "error": (
                    "No answer from %s for %d s past the %d s timeout, so ybtop stopped "
                    "waiting. The statement may still be running there: look for "
                    "application_name 'ybtop-explain' in pg_stat_activity."
                    % (run["node"], ABANDON_GRACE_S, run["timeout_s"])
                ),
            }
        )
        run["_stop"].set()
        fn = self._cancels.pop(self._active, None)
        self._active = None
        if fn:
            # Its node is not answering, so the cancel may block: send it from a
            # thread of its own, not under the lock or in the request.
            threading.Thread(target=fn, name="ybtop-explain-cancel", daemon=True).start()

    @staticmethod
    def key(dbid: str, queryid: str) -> str:
        return "%s|%s" % (dbid, queryid)

    def _public(self, run: dict) -> dict:
        out = {k: v for k, v in run.items() if not k.startswith("_")}
        if run.get("state") == "running":
            # Elapsed on the collector's clock: the browser's may be skewed.
            out["run_s"] = round(time.monotonic() - run["_t0"], 1)
        return out

    def active(self) -> Optional[dict]:
        with self._lock:
            self._reap()
            if self._active is None:
                return None
            run = self._runs.get(self._active)
            return self._public(run) if run else None

    def latest(self, dbid: str, queryids: "list[str]") -> Optional[dict]:
        """Newest run among these statements (a canonical family spans several)."""
        with self._lock:
            self._reap()
            best = None
            for q in queryids:
                run = self._runs.get(self.key(dbid, q))
                if run and (best is None or run["started_utc"] > best["started_utc"]):
                    best = run
            return self._public(best) if best else None

    def start(self, target: dict, options: "list[str]", timeout_s: int, work: Callable[[dict, Callable, Callable], dict]) -> "tuple[Optional[dict], Optional[dict]]":
        """(run, None) once started, or (None, the run already in flight)."""
        k = self.key(target["dbid"], target["queryid"])
        with self._lock:
            self._reap()
            if self._active is not None:
                busy = self._runs.get(self._active)
                return None, (self._public(busy) if busy else None)
            run = {
                "id": uuid.uuid4().hex[:12],
                "state": "running",
                "started_utc": _utc_now(),
                "finished_utc": None,
                "timeout_s": int(timeout_s),
                "options": list(options),
                "queryid": target["queryid"],
                "planid": target["planid"],
                "plan_ref": target["plan_ref"],
                "dbid": target["dbid"],
                "datname": target["datname"],
                "node": target["node"],
                "kind": target["kind"],
                "label": target["label"],
                "role": target.get("role"),
                "params_text": target.get("params_text"),
                "sql_display": target["sql_display"],
                "recorded_max_ms": target.get("recorded_max_ms"),
                "snapshot_file": target.get("snapshot_file"),
                "_t0": time.monotonic(),
                "_stop": threading.Event(),
            }
            self._runs.pop(k, None)
            self._runs[k] = run
            while len(self._runs) > self.MAX_KEPT:
                oldest = next(iter(self._runs))
                if oldest == self._active:
                    break
                self._runs.pop(oldest)
            self._active = k
        threading.Thread(target=self._work, args=(k, run, work), name="ybtop-explain", daemon=True).start()
        with self._lock:
            return self._public(run), None

    def cancel(self, dbid: str, queryid: str) -> Optional[dict]:
        k = self.key(dbid, queryid)
        with self._lock:
            run = self._runs.get(k)
            live = self._active == k and run is not None
            fn = self._cancels.get(k) if live else None
            if live:
                # Seen between steps even before the statement (or the connection) exists.
                run["_stop"].set()
            out = self._public(run) if run else None
        if fn:
            fn()
        return out

    def _work(self, k: str, run: dict, work: Callable[[dict, Callable, Callable], dict]) -> None:
        def cancel_ready(fn: Callable[[], None]) -> None:
            with self._lock:
                # Once reaped, the slot's cancel may belong to a newer run of the
                # same statement; this worker is stopped by run["_stop"] instead.
                if run["state"] == "running":
                    self._cancels[k] = fn

        def may_analyze() -> bool:
            # Under the lock _reap and cancel take when they set _stop, so a run
            # given up on or cancelled never starts its ANALYZE: one at a time holds.
            with self._lock:
                if run["_stop"].is_set():
                    return False
                run["_analyze_t0"] = time.monotonic()
                return True

        try:
            result = work(run, cancel_ready, may_analyze)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc) or exc.__class__.__name__}
        with self._lock:
            if run["state"] != "running":
                # Reaped while silent; its slot may already belong to another run.
                return
            run.update(result)
            run["elapsed_ms"] = round((time.monotonic() - run["_t0"]) * 1000.0, 1)
            run["finished_utc"] = _utc_now()
            run["state"] = (
                "timeout" if result.get("timed_out")
                else "cancelled" if result.get("cancelled")
                else "error" if result.get("error")
                else "done"
            )
            self._cancels.pop(k, None)
            if self._active == k:
                self._active = None
