"""Plan a fresh replicated schema from reviewed system.tables metadata.

No SQL is executed here. Load base-table history before installing materialized
views, otherwise historical rollups would be generated a second time.
"""
import re

NAME = re.compile(r'[a-z][a-z0-9_]{0,100}\Z')
ENGINES = {'MergeTree', 'ReplacingMergeTree', 'SummingMergeTree', 'AggregatingMergeTree'}


def top_level_tokens(sql):
    """Only unquoted top-level words; column expressions cannot become DDL."""
    i, depth = 0, 0
    while i < len(sql):
        c = sql[i]
        if c in "'\"`":
            quote = c; i += 1
            while i < len(sql):
                if sql[i] == '\\':
                    i += 2; continue
                if sql[i] == quote:
                    if i+1 < len(sql) and sql[i+1] == quote:
                        i += 2; continue
                    i += 1; break
                i += 1
            else:
                raise ValueError('Unterminated SQL quote')
        elif sql.startswith('--', i) or sql.startswith('/*', i) or c == ';':
            raise ValueError('Expected one canonical CREATE without comments or terminator')
        elif c == '(':
            depth += 1; i += 1
        elif c == ')':
            depth -= 1; i += 1
            if depth < 0:
                raise ValueError('Unbalanced SQL')
        elif c.isalpha() or c == '_':
            end = i+1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == '_'):
                end += 1
            if depth == 0:
                yield sql[i:end], i, end
            i = end
        else:
            i += 1
    if depth:
        raise ValueError('Unbalanced SQL')


def replicated_ddl(row, cell):
    name, engine, ddl = row['name'], row['engine'], row['create_table_query']
    if not NAME.fullmatch(name) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', cell):
        raise ValueError('Invalid schema or cell identity')
    words = list(top_level_tokens(ddl))
    if engine == 'MaterializedView':
        if not ddl.startswith('CREATE MATERIALIZED VIEW zenplus.'+name+' '):
            raise ValueError('Unexpected materialized view identity')
        if not re.search(r'\bTO zenplus\.[a-z][a-z0-9_]*\b', ddl) or any(w[0] == 'ENGINE' for w in words):
            raise ValueError('Materialized views must use an explicit replicated target')
        return ddl
    if engine not in ENGINES or not ddl.startswith('CREATE TABLE zenplus.'+name+' '):
        raise ValueError('Unsupported table engine or identity')
    matches = [w for w in words if w[0] == 'ENGINE']
    if len(matches) != 1:
        raise ValueError('Expected exactly one top-level table engine')
    _, start, _ = matches[0]
    match = re.match(r'ENGINE\s*=\s*'+engine+r'\b', ddl[start:])
    if not match:
        raise ValueError('Engine metadata and definition disagree')
    end = start+match.end()
    # The private server profile owns UUID-based default replica paths. The
    # Replicated database rejects explicit path arguments by default; let it
    # allocate and propagate table UUIDs, preserving only engine-specific args.
    return ddl[:start]+'ENGINE = Replicated'+engine+ddl[end:]


def plan(rows, cell):
    if not rows or len({row['name'] for row in rows}) != len(rows):
        raise ValueError('Schema inventory is empty or duplicated')
    tables, views = [], []
    for row in rows:
        item = {'name': row['name'], 'sql': replicated_ddl(row, cell)}
        (views if row['engine'] == 'MaterializedView' else tables).append(item)
    return {'tables': tables, 'views_after_history_import': views}
