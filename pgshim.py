"""Let the existing Flask app talk to Postgres without rewriting 262 queries.

The app was written against sqlite3: `?` placeholders, `conn.execute(...)` returning a
cursor, rows subscripted by column name, and `PRAGMA` statements in the migration path.
Rewriting all of that by hand would be a few hundred edits with a typo in every tenth
one, so this adapter presents the sqlite3 shape over psycopg instead. The call sites do
not change at all.

What it does:

* rewrites `?` to `%s`, skipping anything inside a quoted string so a literal question
  mark in text is left alone;
* returns dict rows, so `row["title"]` and `row.keys()` keep working;
* turns `PRAGMA` into a no-op, since Postgres has no such statement and the schema is
  defined once rather than migrated column by column;
* sets the request's user on the connection, which is what makes Row Level Security do
  the multi-tenancy. Every SELECT is filtered and every INSERT gets its `user_id` from
  `auth.uid()` without a single query mentioning the user.

That last point is the reason this approach is cheap. The alternative was adding a
`user_id` term to every query by hand, which is both a lot of edits and a security model
that fails silently the first time someone forgets one.
"""
import re

# Accounts this process has already given their starting rows to.
_PROVISIONED = set()

TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.I)

PLACEHOLDER = re.compile(
    r"""
      '(?:[^']|'')*'      # a single-quoted literal, '' being an escaped quote
    | "(?:[^"]|"")*"      # a double-quoted identifier
    | (\?)                # the placeholder we actually want
    """,
    re.VERBOSE,
)


def to_pyformat(sql, escape_percent=False):
    """`WHERE id=?` -> `WHERE id=%s`, leaving question marks inside strings alone.

    Naively replacing every `?` would corrupt any literal containing one, and the app
    does store free text.

    `escape_percent` doubles literal `%`, which is required whenever parameters are
    passed: psycopg then parses the statement for placeholders and rejects a bare `%`
    with "only '%s', '%b', '%t' are allowed as placeholders". Today's search happens to
    put its wildcards in the bound value rather than the SQL, so nothing currently
    trips this, but a future `LIKE '%x%'` would fail for a reason nobody would guess.
    It is deliberately not applied when there are no parameters, because psycopg does
    no placeholder parsing at all in that case and doubling would reach the database.
    """
    def keep(text):
        return text.replace("%", "%%") if escape_percent else text

    out, last = [], 0
    for m in PLACEHOLDER.finditer(sql):
        if m.group(1) is None:
            continue                       # a quoted section: it rides along in `keep`
        out.append(keep(sql[last:m.start(1)]))
        out.append("%s")
        last = m.end(1)
    out.append(keep(sql[last:]))
    return "".join(out)


class Cursor:
    """Thin wrapper so `.fetchone()` / `.fetchall()` behave as the app expects."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return None                       # sqlite-only; nothing in the app reads it


class Connection:
    """A psycopg connection wearing sqlite3's clothes."""

    def __init__(self, conn, user_id=None):
        self._conn = conn
        self.user_id = user_id
        if user_id:
            self.become(user_id)

    # -- the sqlite3 surface the app uses -----------------------------------
    def execute(self, sql, params=()):
        stripped = sql.lstrip().upper()
        if stripped.startswith("PRAGMA"):
            m = TABLE_INFO.match(sql)
            if m:
                # Not a no-op. The app uses `PRAGMA table_info` to ask which columns a
                # table actually has, and then builds SQL from the answer: links.py's
                # search picks its LIKE fields that way. Returning nothing made the
                # WHERE clause disappear entirely and the query a syntax error, which
                # is exactly the kind of silent breakage a bare no-op invites.
                cur = self._conn.cursor()
                cur.execute(
                    "select column_name as name, data_type as type,"
                    " case when is_nullable = 'NO' then 1 else 0 end as notnull,"
                    " column_default as dflt_value, 0 as pk"
                    " from information_schema.columns"
                    " where table_schema = 'public' and table_name = %s"
                    " order by ordinal_position",
                    (m.group(1),))
                return Cursor(cur)
            return Cursor(_Empty())       # foreign_keys, journal_mode and friends
        cur = self._conn.cursor()
        args = tuple(params) if params else None
        cur.execute(to_pyformat(sql, escape_percent=bool(args)), args)
        return Cursor(cur)

    def executescript(self, sql):
        """Only the schema uses this. Postgres is happy with several statements at once."""
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    # -- what makes RLS do the work ----------------------------------------
    def become(self, user_id):
        """Run as the signed-in user for the rest of this connection.

        `set_config` with is_local=false keeps it for the session rather than the
        transaction, because the app commits several times per request. The role change
        is what stops a bug in one query from reading someone else's rows: as
        `authenticated` the policies apply, whereas the owning role would bypass them.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, false)",
                ('{"sub":"%s","role":"authenticated"}' % user_id,),
            )
            cur.execute("set role authenticated")
        self.user_id = user_id
        self._provision()

    def _provision(self):
        """Give a new account the singleton row the app assumes already exists.

        On SQLite `init_db` inserts term_settings id=1 at startup. Postgres has one
        database for everyone, so the row has to be per user, and without it two things
        break: reads crash on `term["name"]`, and worse, `UPDATE term_settings ... WHERE
        id = 1` matches nothing, reports success, and stores nothing at all.

        Memoised per process, so this is one write the first time an account is seen
        rather than a round trip on every request.
        """
        if not self.user_id or self.user_id in _PROVISIONED:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into term_settings (id, name, start_date, end_date)"
                " values (1, '', '', '') on conflict do nothing")
        self._conn.commit()
        _PROVISIONED.add(self.user_id)

    def as_owner(self):
        """Drop back to the connecting role, for schema work and the nightly jobs."""
        with self._conn.cursor() as cur:
            cur.execute("reset role")
        self.user_id = None


class _Empty:
    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())

    rowcount = 0
