#!/usr/bin/env python3
"""Write-column guard — static sweep of every INSERT/UPDATE
column name application code writes, asserting each one EXISTS in the real
Postgres schema.

WHY THIS EXISTS (read before touching): a hardcoded column list that names a
column the real table does not have fails on every insert, and a best-effort
try/except around the write hides it indefinitely. ``scripts/check_schema_drift.py``
cannot catch that: it diffs two DATABASES against each other, and both can
agree with each other while both disagree with the code.

DESIGN — deliberately STATIC, not runtime interception. A runtime
interceptor only observes code paths that tests actually exercise, and
``score_snapshots`` had zero ``tests/pg/`` coverage — interception would have
missed this exact bug. This script instead reads every write site in the
source, regardless of whether anything ever calls it in CI. Do not replace
this with instrumentation of live query execution; that reintroduces the
coverage gap this guard exists to close.

TWO extraction paths, because column lists are built two different ways:

1. LITERAL SQL — a plain string (or an f-string whose *column-list portion*
   has no ``{...}`` placeholder) containing ``INSERT INTO table (cols)`` or
   ``UPDATE table SET col = .., col = ..`` (including the ``DO UPDATE SET``
   half of an ``INSERT ... ON CONFLICT (...) DO UPDATE SET ...`` upsert — the
   conflict-target parens are explicitly NOT treated as an insert column
   list). Found by parsing every file with ``ast``, pulling out each
   string/f-string literal's *exact source text* (so f-string placeholders
   stay visible as literal ``{expr}`` for the "does this contain a brace"
   check), and regexing that text.

2. DYNAMIC-CONSTRUCTION REGISTRY — sites that interpolate a Python
   list/set/tuple (``{col_names}``, ``{set_clause}``) into the SQL. The
   literal path is structurally blind to these (that's exactly the shape of
   the silent-failure bug: ``col_names = ', '.join(cols)`` then
   ``f'INSERT INTO score_snapshots ({col_names}) ...'``). Each such site is
   listed explicitly in ``DYNAMIC_SITES`` below, resolved by *importing the
   module and reading the actual list/set/string object* the code uses —
   not a hand-copied duplicate that can drift from the source. Several
   sites needed a local variable hoisted to a module-level constant to make
   this possible; that refactor is documented at each call site and changes
   no behaviour (same object, just named and placed so it can be imported).

Usage:
    check_write_columns.py <postgres_dsn>

Exit 0 = every extracted column exists in the target schema (or is
allowlisted). Exit 1 = at least one column write targets a column that does
not exist — i.e. this would silently fail exactly like that earlier outage did.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SCHEMAS = ("public", "quant")
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# Marker substituted for an f-string `{expr}` placeholder when reconstructing
# a JoinedStr's text (see _StringSegmentCollector.visit_JoinedStr). Deliberately
# NOT a bare '{' — real SQL literals legitimately contain braces (e.g. a
# Postgres empty-jsonb default written as `cursor_data = '{}'`), and a plain
# '{' check would misfire on those and wrongly discard genuinely literal,
# parseable SQL as "dynamic".
_DYNAMIC_SENTINEL = '\x00DYNAMIC\x00'


# ─────────────────────────────────────────────────────────────────────────
# File selection
# ─────────────────────────────────────────────────────────────────────────

def _should_scan(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    parts = rel.parts
    if not parts or parts[0] in ('.venv', 'tests', '.git', 'node_modules'):
        return False
    if parts[0] == 'alembic' and len(parts) > 1 and parts[1] == 'versions':
        return False
    if parts[0] == 'scripts' and path.name.startswith('migrate_'):
        # scripts/migrate_to_postgres.py self-validates against
        # information_schema at runtime and is a one-shot bootstrap tool,
        # not an ongoing drift risk (see module docstring there).
        return False
    if path.name == 'check_write_columns.py':
        # This file's own docstring uses "INSERT INTO table (cols)" as
        # illustrative prose — not a real write site. Scanning it would be
        # self-referential noise, not a drift risk.
        return False
    return path.suffix == '.py'


def _iter_source_files() -> list[Path]:
    return sorted(p for p in REPO_ROOT.rglob('*.py') if _should_scan(p))


# ─────────────────────────────────────────────────────────────────────────
# Path 1: literal SQL extraction via AST
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    file: str
    line: int
    schema: str
    table: str
    column: str
    source: str  # 'literal' or the registry site label


class _StringSegmentCollector(ast.NodeVisitor):
    """Collects the exact source text of every top-level string / f-string
    literal in a module, without descending into a literal's own pieces
    (so an f-string's static text + its ``{expr}`` placeholders come back
    as ONE segment, not fragments)."""

    def __init__(self, source: str):
        self.source = source
        self.segments: list[tuple[int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            # Use the ALREADY-EVALUATED string, not get_source_segment.
            # Adjacent plain string literals (no f-prefix) are extremely
            # common in this codebase ('...' '...' split across lines) and
            # are merged by the parser into one Constant whose *source*
            # span still contains the intervening quote characters and
            # newline — regexing that raw span mangles the SQL mid-statement.
            # node.value is what Python actually concatenated; using it
            # sidesteps the whole problem. (JoinedStr — f-strings — still
            # needs the raw source text, handled separately below, so its
            # `{expr}` placeholders stay visible as literal braces.)
            self.segments.append((node.lineno, node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # Rebuild from the AST's own pieces rather than get_source_segment:
        # each literal Constant chunk contributes its EVALUATED text (same
        # reasoning as visit_Constant above — avoids source-level quote/
        # newline artifacts from adjacent-literal concatenation), and each
        # FormattedValue (an `{expr}` substitution) contributes a fixed
        # sentinel. We don't care what the expression IS, only that a
        # placeholder sits there — the sentinel still trips the "contains a
        # `{`" dynamic check the same way a real f-string placeholder would.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(_DYNAMIC_SENTINEL)
        text = ''.join(parts)
        if text:
            self.segments.append((node.lineno, text))
        # deliberately NOT calling generic_visit — we've already consumed
        # this f-string's pieces; descending would double-report them.


def _split_top_level(text: str, sep: str = ',') -> list[str]:
    """Split on `sep` only at paren-depth 0 and outside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                if quote == "'" and text[i + 1:i + 2] == "'":
                    buf.append("'")
                    i += 1
                else:
                    quote = None
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
        elif c == '(':
            depth += 1
            buf.append(c)
        elif c == ')':
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf:
        parts.append(''.join(buf))
    return parts


def _clean_ident(raw: str) -> str | None:
    tok = raw.strip().strip('"').strip()
    if _IDENT_RE.match(tok):
        return tok
    return None


def _split_table(raw: str) -> tuple[str, str]:
    tok = raw.strip().replace('"', '')
    if '.' in tok:
        schema, table = tok.split('.', 1)
        return schema, table
    return 'public', tok


def _parse_insert_cols(cols_text: str) -> list[str] | None:
    if _DYNAMIC_SENTINEL in cols_text:
        return None  # dynamic — defer to the registry
    out = []
    for chunk in _split_top_level(cols_text):
        ident = _clean_ident(chunk)
        if ident is None:
            return None  # unparseable — bail on the whole list, don't guess
        out.append(ident)
    return out or None


def _parse_set_cols(set_text: str) -> list[str] | None:
    if _DYNAMIC_SENTINEL in set_text:
        return None  # dynamic — defer to the registry
    out = []
    for chunk in _split_top_level(set_text):
        lhs = _lhs_of_assignment(chunk)
        if lhs is None:
            return None
        ident = _clean_ident(lhs)
        if ident is None:
            return None
        out.append(ident)
    return out or None


def _lhs_of_assignment(chunk: str) -> str | None:
    """Return the column name left of a top-level `=` in `col = expr`,
    depth/quote aware so `x = COALESCE(a, b)` doesn't confuse the scan."""
    depth = 0
    quote: str | None = None
    i = 0
    n = len(chunk)
    while i < n:
        c = chunk[i]
        if quote:
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == '=' and depth == 0:
            prev = chunk[i - 1] if i > 0 else ''
            nxt = chunk[i + 1] if i + 1 < n else ''
            if prev in ('<', '>', '!', '=') or nxt == '=':
                i += 1
                continue
            return chunk[:i]
        i += 1
    return None


_INSERT_RE = re.compile(
    r'INSERT\s+INTO\s+(?P<table>"?[A-Za-z_][\w.]*"?)\s*\(\s*(?P<cols>[^()]*)\)',
    re.IGNORECASE | re.DOTALL,
)
_UPDATE_RE = re.compile(
    r'UPDATE\s+(?P<table>"?[A-Za-z_][\w.]*"?)\s+SET\s+(?P<setcols>.*?)'
    r'(?=\bWHERE\b|\bRETURNING\b|\Z)',
    re.IGNORECASE | re.DOTALL,
)
_CONFLICT_UPDATE_RE = re.compile(
    r'ON\s+CONFLICT\s*(?:\([^()]*\)|[A-Za-z_][\w, ]*)?\s*DO\s+UPDATE\s+SET\s+'
    r'(?P<setcols>.*?)(?=\bWHERE\b|\bRETURNING\b|\Z)',
    re.IGNORECASE | re.DOTALL,
)


def _extract_literal(file_rel: str, lineno: int, text: str) -> list[Finding]:
    findings: list[Finding] = []

    insert_matches = list(_INSERT_RE.finditer(text))
    for m in insert_matches:
        schema, table = _split_table(m.group('table'))
        cols = _parse_insert_cols(m.group('cols'))
        if cols is None:
            continue
        for col in cols:
            findings.append(Finding(file_rel, lineno, schema, table, col, 'literal'))

    for m in _UPDATE_RE.finditer(text):
        schema, table = _split_table(m.group('table'))
        cols = _parse_set_cols(m.group('setcols'))
        if cols is None:
            continue
        for col in cols:
            findings.append(Finding(file_rel, lineno, schema, table, col, 'literal'))

    if insert_matches:
        # ON CONFLICT ... DO UPDATE SET belongs to the nearest preceding
        # INSERT INTO in the same literal — NOT a second free-standing
        # UPDATE, and NOT the conflict-target parens themselves.
        for m in _CONFLICT_UPDATE_RE.finditer(text):
            owner = max((im for im in insert_matches if im.start() <= m.start()),
                        key=lambda im: im.start(), default=insert_matches[0])
            schema, table = _split_table(owner.group('table'))
            cols = _parse_set_cols(m.group('setcols'))
            if cols is None:
                continue
            for col in cols:
                findings.append(Finding(file_rel, lineno, schema, table, col, 'literal'))

    return findings


def _literal_findings() -> list[Finding]:
    out: list[Finding] = []
    for path in _iter_source_files():
        try:
            source = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        if 'INSERT' not in source.upper() and 'UPDATE' not in source.upper():
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        collector = _StringSegmentCollector(source)
        collector.visit(tree)
        file_rel = str(path.relative_to(REPO_ROOT))
        for lineno, text in collector.segments:
            upper = text.upper()
            if 'INSERT' not in upper and 'UPDATE' not in upper:
                continue
            out.extend(_extract_literal(file_rel, lineno, text))
    return out


# ─────────────────────────────────────────────────────────────────────────
# Path 2: dynamic-construction registry
# ─────────────────────────────────────────────────────────────────────────

def _cols_from_module(module_path: str, symbol: str, extra: tuple = ()) -> list[str]:
    """Import `module_path` and read `symbol` off it — the SAME object the
    runtime code uses to build its column list, not a transcribed copy."""
    mod = importlib.import_module(module_path)
    obj = getattr(mod, symbol)
    if isinstance(obj, str):
        cols = [c.strip().strip('"') for c in obj.split(',')]
    else:
        cols = list(obj)
    return sorted(set(cols) | set(extra))


@dataclass
class DynamicSite:
    site: str          # human-readable "file::function" label for reports
    table: str
    columns: list[str] = field(default_factory=list)
    schema: str = 'public'


def _build_dynamic_sites() -> list[DynamicSite]:
    # NOT covered, and deliberately not faked: ~20 collectors in
    # quant/collectors/*.py (binance_daily, deribit_options, fear_greed,
    # defillama, cot, fred_macro, google_trends, etc.) funnel through a
    # genuinely generic upsert — quant/collectors/base.py::upsert_rows()
    # (and binance_daily.py::_upsert_derivatives / deribit_options.py::
    # _upsert_options[_batch], thin wrappers around the same shape) — where
    # `all_cols = list(rows[0].keys())` and `table` are BOTH runtime
    # parameters. There is no local allowed-set or module constant to hoist;
    # the real column names live in dict-literals scattered across ~20 call
    # sites several lines upstream of the DB call, per-collector, sometimes
    # conditionally. Covering this reliably needs either enumerating every
    # row-dict construction site (a follow-up task) or a different
    # technique — it is explicitly OUT OF SCOPE here and reported as a
    # residual gap rather than given a fabricated column list.
    sites = [
        # --- The original silent-failure site itself. `cols` was a local list; hoisted to
        # module-level SCORE_SNAPSHOT_COLUMNS so it can be imported here. ---
        DynamicSite(
            'carrac/content_db.py::insert_score_snapshot',
            'score_snapshots',
            _cols_from_module('carrac.content_db', 'SCORE_SNAPSHOT_COLUMNS'),
        ),
        DynamicSite(
            'carrac/plays/models.py::update_play',
            'plays',
            # `allowed` hoisted to PLAY_UPDATE_FIELDS; `updated_at` is always
            # appended literally after the allowlist filter (see source).
            _cols_from_module('carrac.plays.models', 'PLAY_UPDATE_FIELDS', extra=('updated_at',)),
        ),
        DynamicSite(
            'carrac/scenarios/models.py::update_scenario',
            'scenarios',
            _cols_from_module('carrac.scenarios.models', 'SCENARIO_UPDATE_FIELDS', extra=('updated_at',)),
        ),
        DynamicSite(
            'carrac/ai/context_composer.py::update_recipe',
            'context_recipes',
            # No natural local collection here — `updates` is built from
            # per-field literal branches (`if name is not None: updates.append('name = ?')`
            # etc.), not a single iterated list/set, so there is nothing to
            # hoist without inventing an unused constant. Columns below are
            # hand-transcribed from the function body;
            # keep in sync by hand if that function's fields change.
            ['name', 'blocks', 'is_default', 'updated_at'],
        ),
        DynamicSite(
            'carrac/content_db.py::drawing_update',
            'drawings',
            # `allowed_fields` hoisted to DRAWING_UPDATE_FIELDS; `updated_at`
            # and `version` are always appended literally after the filter.
            _cols_from_module('carrac.content_db', 'DRAWING_UPDATE_FIELDS',
                               extra=('updated_at', 'version')),
        ),
        # NOTE: the method here is `trade_update`, writing
        # `trade_journal`; no method named `update_journal_entry` exists in this file.
        DynamicSite(
            'carrac/content_db.py::trade_update',
            'trade_journal',
            # `allowed` hoisted to TRADE_UPDATE_FIELDS; TRADE_UPDATE_COLUMNS
            # additionally includes `rr_planned`, a derived value the method
            # injects into `updates` after the allowlist filter.
            _cols_from_module('carrac.content_db', 'TRADE_UPDATE_COLUMNS'),
        ),
        DynamicSite(
            'carrac/content_db.py::update_session',
            'journal_sessions',
            _cols_from_module('carrac.content_db', 'SESSION_UPDATE_FIELDS'),
        ),
        DynamicSite(
            'carrac/content_db.py::update_playbook',
            'journal_playbooks',
            _cols_from_module('carrac.content_db', 'PLAYBOOK_UPDATE_FIELDS'),
        ),
        # play_shares has TWO independent column sources. This is the first:
        # the module constant used by the legacy-schema-copy INSERT ... SELECT
        # (carrac/plays/shares.py::_ensure_shares_table), an f-string so the literal path
        # can't see it. The second source — the hand-written literal INSERT
        # in `_mint_share` — needs no registry entry: it's a
        # plain triple-quoted string with a static column list, so the
        # literal-SQL path (path 1) extracts it directly.
        DynamicSite(
            'carrac/plays/shares.py::_SHARES_COLS (legacy-copy INSERT)',
            'play_shares',
            _cols_from_module('carrac.plays.shares', '_SHARES_COLS'),
        ),
        # --- Bonus coverage beyond the minimum list: both
        # transition_status() functions build their SET clause from a
        # Python list of literal 'col = ?' strings joined into an f-string
        # (dynamic — the literal path can't see it, same shape as the rest
        # of this registry). Columns hand-transcribed from source
        # keep in sync by hand if those functions change. ---
        DynamicSite(
            'carrac/plays/models.py::transition_status',
            'plays',
            ['status', 'status_changed_at', 'updated_at', 'resolved_at', 'resolution'],
        ),
        DynamicSite(
            'carrac/scenarios/models.py::transition_status',
            'scenarios',
            ['status', 'status_changed_at', 'updated_at', 'resolved_at',
             'outcome', 'outcome_notes', 'is_active'],
        ),
        DynamicSite(
            'carrac/plays/models.py::persist_condition_state',
            'plays',
            # `sets` is a small literal list built conditionally
            # ('conditions = ?', 'updated_at = ?', + 'invalidation = ?'),
            # not a single reusable collection — hand-transcribed
            # same rationale as context_composer.update_recipe.
            ['conditions', 'updated_at', 'invalidation'],
        ),
    ]
    return sites


def _registry_findings() -> list[Finding]:
    out: list[Finding] = []
    for site in _build_dynamic_sites():
        for col in site.columns:
            out.append(Finding(site.site, 0, site.schema, site.table, col, f'registry:{site.site}'))
    return out


# ─────────────────────────────────────────────────────────────────────────
# Allowlist — every entry is a hole; keep this short and justified.
# ─────────────────────────────────────────────────────────────────────────

# (file suffix, table) -> reason. `file` matches if the finding's file path
# ENDS WITH this string (so both "carrac/foo.py" and bare "foo.py" work).
#
# carrac/grounding/observability.py (hallucination_candidates) was removed in
# log_numeric_drift was never wired to any call site, so the
# module was deleted rather than kept as an allowlisted exception.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _is_allowlisted(f: Finding) -> str | None:
    for (file_suffix, table), reason in ALLOWLIST.items():
        if f.file.endswith(file_suffix) and f.table == table:
            return reason
    return None


# ─────────────────────────────────────────────────────────────────────────
# Validation against the real schema
# ─────────────────────────────────────────────────────────────────────────

def _schema_columns(conn) -> dict[tuple[str, str], set[str]]:
    """Read column names for the checked schemas off an ALREADY-OPEN
    connection. Callers own the connection's lifecycle — this never opens
    or closes one, so tests/pg/test_write_columns.py can pass in the shared
    `pg_conn` fixture instead of building a second connection path."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = ANY(%s)
        """,
        (list(_SCHEMAS),),
    )
    out: dict[tuple[str, str], set[str]] = {}
    for sch, tbl, col in cur.fetchall():
        out.setdefault((sch, tbl), set()).add(col)
    return out


@dataclass
class Report:
    problems: list[str]
    allowlisted_hits: list[str]
    n_findings: int
    n_literal: int
    n_registry: int
    n_sites: int


def evaluate(conn) -> Report:
    """Run the full sweep against an already-open Postgres connection and
    return a structured report. No I/O beyond the one query in
    _schema_columns — this is what both `main()` (CLI/CI) and the pytest
    wrapper in tests/pg/ call, so there is exactly one code path."""
    schema_cols = _schema_columns(conn)

    findings = _literal_findings() + _registry_findings()

    seen: set[tuple] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.file, f.line, f.schema, f.table, f.column)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    findings = deduped

    problems: list[str] = []
    allowlisted_hits: list[str] = []
    unknown_tables: dict[str, list[Finding]] = {}

    for f in findings:
        cols = schema_cols.get((f.schema, f.table))
        if cols is None:
            reason = _is_allowlisted(f)
            if reason:
                allowlisted_hits.append(f"{f.file}:{f.line} → {f.schema}.{f.table} (ALLOWLISTED: {reason})")
                continue
            unknown_tables.setdefault(f"{f.schema}.{f.table}", []).append(f)
            continue
        if f.column not in cols:
            reason = _is_allowlisted(f)
            if reason:
                allowlisted_hits.append(
                    f"{f.file}:{f.line} → {f.schema}.{f.table}.{f.column} (ALLOWLISTED: {reason})"
                )
                continue
            loc = f"{f.file}:{f.line}" if f.line else f.file
            problems.append(f"{loc} → {f.schema}.{f.table}.{f.column}  [{f.source}]")

    for table_name, flist in unknown_tables.items():
        locs = ", ".join(f"{f.file}:{f.line}" if f.line else f.file for f in flist[:5])
        problems.append(
            f"UNKNOWN TABLE {table_name} referenced by: {locs}"
            + (f" (+{len(flist) - 5} more)" if len(flist) > 5 else "")
        )

    n_sites = len({(f.file, f.line) if f.line else f.file for f in findings})
    return Report(
        problems=sorted(set(problems)),
        allowlisted_hits=sorted(set(allowlisted_hits)),
        n_findings=len(findings),
        n_literal=sum(1 for f in findings if f.source == 'literal'),
        n_registry=sum(1 for f in findings if f.source != 'literal'),
        n_sites=n_sites,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dsn", metavar="postgres_dsn",
                        help="connection string of the database to check")
    args = parser.parse_args()

    import psycopg2

    conn = psycopg2.connect(args.dsn, connect_timeout=5)
    try:
        report = evaluate(conn)
    finally:
        conn.close()

    print(f"Scanned {report.n_findings} column writes across {report.n_sites} statement sites "
          f"({report.n_literal} literal, {report.n_registry} via registry).")
    if report.allowlisted_hits:
        print(f"\n{len(report.allowlisted_hits)} allowlisted (skipped):")
        for line in report.allowlisted_hits:
            print(f"  • {line}")

    if report.problems:
        print(f"\nFAIL — {len(report.problems)} column write(s) target columns that do not "
              f"exist in the schema (this is the silent-failure bug class):")
        for p in report.problems:
            print(f"  • {p}")
        return 1

    print("\nOK — every extracted write-site column exists in the target schema.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
