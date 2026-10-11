"""Journal tag columns: JSON text in the database, lists in API responses.

``trade_journal.tags`` and ``mistake_tags`` are TEXT columns holding a JSON
array. Every journal API (REST and MCP) hands clients the decoded list, and
every writer serialises a list before it reaches the column.
"""

import json

TAG_COLUMNS = ('tags', 'mistake_tags')


def with_tag_lists(row):
    """Copy of a journal row whose tag columns are lists.

    A missing or malformed value becomes ``[]``; ``None`` stays ``None``.
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for column in TAG_COLUMNS:
        value = out.get(column)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            out[column] = decoded if isinstance(decoded, list) else []
    return out


def dump_json_list(value):
    """JSON text for a list bound to a TEXT column; anything else unchanged."""
    return json.dumps(value) if isinstance(value, (list, tuple)) else value
