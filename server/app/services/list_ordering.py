"""Literal search ranking and deterministic natural ordering for asset lists.

SQL expressions passed here are application-owned allowlisted columns, never
request values. Search terms always travel as bound parameters.
"""
import re
from sqlalchemy import literal_column, text
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.ext.compiler import compiles


class NaturalOrder(FunctionElement):
    inherit_cache = True


@compiles(NaturalOrder)
def _compile_natural(element, compiler, **kwargs):
    return natural_sql(compiler.process(list(element.clauses)[0], **kwargs))


def patterns(query: str) -> dict[str, str]:
    value = query.strip().lower()
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {"list_exact": value, "list_prefix": escaped + "%", "list_contains": "%" + escaped + "%"}


def ip_query(query: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{0,3}){1,3}\.?", query.strip())
                or (":" in query and re.fullmatch(r"[0-9a-fA-F:]+", query.strip())))


def contains_sql(columns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"lower(CAST({c} AS text)) LIKE :list_contains ESCAPE '\\'" for c in columns) + ")"


def rank_sql(columns: tuple[str, ...]) -> str:
    exact = " OR ".join(f"lower(CAST({c} AS text)) = :list_exact" for c in columns)
    prefix = " OR ".join(f"lower(CAST({c} AS text)) LIKE :list_prefix ESCAPE '\\'" for c in columns)
    return f"CASE WHEN {exact} THEN 0 WHEN {prefix} THEN 1 ELSE 2 END"


def natural_sql(column: str) -> str:
    # Each number sorts by significant digit count, then digits. No integer
    # conversion/overflow or truncated padding, even for long identifiers.
    return f"""CASE WHEN {column} IS NULL THEN NULL ELSE ARRAY(
        SELECT CASE WHEN piece[1] ~ '^[0-9]+$'
          THEN '0' || lpad(length(ltrim(piece[1], '0'))::text, 6, '0') || ltrim(piece[1], '0')
          ELSE '1' || piece[1] END
        FROM regexp_matches(lower(CAST({column} AS text)), '[0-9]+|[^0-9]+', 'g') AS parts(piece)
    ) END"""


def natural(column):
    return NaturalOrder(literal_column(column) if isinstance(column, str) else column)


def rank(columns: tuple[str, ...], query: str):
    return text(rank_sql(columns)).bindparams(**{k: v for k, v in patterns(query).items() if k != 'list_contains'})
