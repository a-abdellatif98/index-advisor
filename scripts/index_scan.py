#!/usr/bin/env python3
"""Heuristic static scan for missing and redundant database indexes (Rails, Django, Laravel, Prisma, Knex)."""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "vendor",
    "__pycache__", "tmp", "log", "storage", "dist", "build", "coverage",
}
USAGE_SKIP_DIRS = {"spec", "test", "tests", "__tests__", "config", "db", "database", "migrations", "migrate"}
CODE_EXTS = {".rb", ".py", ".php", ".js", ".ts", ".mjs", ".cjs", ".mts", ".jsx", ".tsx"}
JS_EXTS = CODE_EXTS - {".rb", ".py", ".php"}
MAX_BYTES = 1_000_000
MARKER = "index-advisor: ok"

FK_RULE = "IDX-FK"
REDUNDANT_RULE = "IDX-REDUNDANT"
QUERY_RULE = "IDX-QUERY"

RB_TYPES = {
    "string", "text", "integer", "bigint", "float", "decimal", "numeric", "datetime", "timestamp", "timestamptz",
    "time", "date", "boolean", "binary", "json", "jsonb", "uuid", "inet", "citext", "hstore", "smallint",
    "tinyint", "blob", "serial", "bigserial", "money", "column",
}
BUILDER_TYPES = {
    "string", "text", "integer", "bigInteger", "unsignedBigInteger", "unsignedInteger", "bigint", "uuid", "char",
    "boolean", "date", "dateTime", "datetime", "timestamp", "decimal", "float", "double", "json", "jsonb", "enum",
    "enu", "ulid", "smallInteger", "tinyInteger", "mediumInteger", "binary", "ipAddress", "year", "time",
}
LARAVEL = {
    "pk": {"id", "increments", "bigIncrements", "mediumIncrements", "smallIncrements", "tinyIncrements", "integerIncrements"},
    "ref": {"foreignId", "foreignUuid", "foreignUlid"},
    "morphs": {"morphs", "nullableMorphs", "uuidMorphs", "ulidMorphs", "nullableUuidMorphs", "nullableUlidMorphs"},
    "fk_chain": re.compile(r"->\s*(?:constrained|references)\s*\("),
    "sep": r"->",
}
KNEX = {
    "pk": {"increments", "bigIncrements"},
    "ref": set(),
    "morphs": set(),
    "fk_chain": re.compile(r"\.\s*(?:references|inTable)\s*\("),
    "sep": r"\.",
}

RAILS_ALLOWED = {
    "where", "not", "find_by", "find_by!", "exists?", "order", "reorder", "rewhere", "find_or_create_by",
    "find_or_initialize_by", "joins", "includes", "preload", "eager_load", "left_joins", "left_outer_joins",
    "select", "group", "limit", "offset", "distinct", "unscoped", "all", "lock", "having", "merge", "or", "pluck",
    "count", "sum", "average", "maximum", "minimum", "first", "last", "take", "ids", "find_each", "in_batches",
}
DJANGO_ALLOWED = {
    "objects", "filter", "exclude", "get", "order_by", "all", "select_related", "prefetch_related", "distinct",
    "values", "values_list", "only", "defer", "exists", "count", "first", "last", "annotate", "get_or_create",
    "update_or_create", "using", "iterator",
}
DJANGO_LOOKUPS = {
    "exact", "iexact", "in", "gt", "gte", "lt", "lte", "contains", "icontains", "startswith", "istartswith",
    "endswith", "iendswith", "isnull", "range", "regex", "iregex",
}
LARAVEL_FILTERS = {
    "where", "orWhere", "whereIn", "whereNotIn", "whereNull", "whereNotNull", "whereDate", "whereBetween",
    "whereNot", "firstWhere", "whereLike",
}
LARAVEL_ALLOWED = LARAVEL_FILTERS | {
    "orderBy", "orderByDesc", "latest", "oldest", "with", "select", "join", "leftJoin", "limit", "take", "skip",
    "offset", "get", "first", "paginate", "count", "exists", "pluck", "query", "distinct", "groupBy", "having",
}
KNEX_FILTERS = {
    "where", "andWhere", "orWhere", "whereIn", "whereNotIn", "whereNull", "whereNotNull", "whereBetween", "whereNot",
}
KNEX_ALLOWED = KNEX_FILTERS | {"orderBy", "first", "select", "limit", "offset", "count", "pluck", "del", "update", "distinct"}
PRISMA_METHODS = (
    "findMany|findFirst|findFirstOrThrow|findUnique|findUniqueOrThrow|count|updateMany|deleteMany|aggregate|groupBy|update|delete"
)


@dataclass
class Index:
    columns: tuple
    unique: bool
    name: str
    path: str
    line: int
    ordered: bool = False
    implicit: bool = False
    suppressed: bool = False


@dataclass
class Column:
    name: str
    path: str
    line: int
    fk: bool = False
    ref_like: bool = False
    suppressed: bool = False


@dataclass
class Table:
    name: str
    columns: dict = field(default_factory=dict)
    indexes: list = field(default_factory=list)
    pk: tuple = ("id",)
    fields: dict = field(default_factory=dict)


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    table: str
    column: str
    message: str


def singular(word):
    word = word.lower()
    if word.endswith("ies"):
        return word[:-3] + "y"
    if re.search(r"(?:s|x|z|ch|sh)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def normalize(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def variants(word):
    w = normalize(word)
    out = [w, w[:-1], w[:-2], w + "s", w + "es"]
    if w.endswith("ies"):
        out.append(w[:-3] + "y")
    if w.endswith("y"):
        out.append(w[:-1] + "ies")
    return out


class Schema:
    def __init__(self):
        self.tables = {}
        self._by_base = None

    def find(self, name):
        if self._by_base is None:
            self._by_base = {}
            for key, table in self.tables.items():
                self._by_base.setdefault(key.split("@")[0], []).append(table)
        for v in variants(name):
            found = self._by_base.get(v)
            if found:
                return found[0] if len(found) == 1 else None
        return None

    def table(self, name, scope=None):
        key = normalize(name) + (f"@{scope}" if scope else "")
        self._by_base = None
        if key not in self.tables:
            self.tables[key] = Table(name)
        return self.tables[key]

    def column(self, table, name, path, line, text="", poly=False, **flags):
        col = table.columns.get(name)
        if col is None:
            col = table.columns[name] = Column(name, path, line, suppressed=MARKER in text)
        for key, value in flags.items():
            setattr(col, key, getattr(col, key) or value)
        if name.endswith("_id") and name != "id" and not poly:
            col.ref_like = True
        return col

    def index(self, table, columns, unique, name, path, line, text="", ordered=False, implicit=False, skip_declared=False):
        columns = tuple(columns)
        if not columns:
            return
        if skip_declared and any(
            (i.columns == columns and i.unique == unique and not i.implicit) or (name and i.name == name)
            for i in table.indexes
        ):
            return
        table.indexes.append(Index(columns, unique, name, path, line, ordered, implicit, MARKER in text))


def read_source(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def is_secret_name(name):
    lowered = name.lower()
    return (
        lowered.startswith((".env", "secrets", "credentials", "settings"))
        or lowered in {"database.yml", "database.yaml", "master.key"}
        or lowered.endswith((".pem", ".key"))
    )


def iter_files(root):
    if root.is_file():
        yield root, root.name
        return
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            path = Path(folder) / name
            if not is_secret_name(name) and path.is_file():
                yield path, path.relative_to(root).as_posix()


def classify(rel):
    p = PurePosixPath(rel)
    parts = p.parts
    if rel.endswith("db/schema.rb"):
        return "rails_schema"
    if p.suffix == ".rb" and "migrate" in parts[:-1] and "db" in parts[:-1]:
        return "rails_migration"
    if p.suffix == ".py" and "migrations" in parts[:-1] and p.name != "__init__.py":
        return "django_migration"
    if p.suffix == ".py" and (p.name == "models.py" or "models" in parts[:-1]):
        return "django_model"
    if p.suffix == ".php" and "migrations" in parts[:-1]:
        return "laravel"
    if p.suffix == ".prisma":
        return "prisma"
    if p.suffix in JS_EXTS and "migrations" in parts[:-1]:
        return "knex"
    return None


def balanced(text, i):
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack, quote, j = [], None, i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\":
                j += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in ")]}":
            if stack:
                stack.pop()
            if not stack:
                return j
        j += 1
    return len(text)


def split_top(text):
    pieces, depth, quote, start = [], 0, None, 0
    for j, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(text[start:j])
            start = j + 1
    pieces.append(text[start:])
    return [p for p in pieces if p.strip()]


def top_level_keys(block, depth_wanted=1):
    keys = []
    for depth, piece in _segments(block):
        m = re.match(r"\s*([A-Za-z_]\w*)\s*(?::|$)", piece)
        if depth == depth_wanted and m:
            keys.append(m.group(1))
    return keys


def _segments(block):
    depth, start, quote = 0, 0, None
    for j, ch in enumerate(block):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "{[(}])":
            if depth >= 1:
                yield depth, block[start:j]
            depth += 1 if ch in "{[(" else -1
            start = j + 1
        elif ch == "," and depth >= 1:
            yield depth, block[start:j]
            start = j + 1


def line_at(start_line, text, pos):
    return start_line + text.count("\n", 0, pos)


def names_in(text):
    return re.findall(r"['\"`]([A-Za-z_]\w*)['\"`]", text)


def statements(entries):
    out, buf, start, depth = [], "", 0, 0
    for no, raw in entries:
        stripped = raw.strip()
        if not stripped:
            continue
        if not buf:
            start = no
        buf += " " + stripped
        depth += sum(stripped.count(c) for c in "([{") - sum(stripped.count(c) for c in ")]}")
        if depth <= 0 and stripped.endswith(";"):
            out.append((start, buf.strip()))
            buf, depth = "", 0
    if buf.strip():
        out.append((start, buf.strip()))
    return out


# ---------- Rails ----------

RB_TABLE = re.compile(r"^(create_table|change_table)\s*\(?\s*[:\"'](\w+)[\"']?(.*?)\s*do\s*\|(\w+)\|")
RB_ID_ARG = re.compile(r"\s*\(?\s*[:\"'](\w+)")
RB_ITEM = r"(?::\w+|\"\w+\"|'\w+')"
RB_LIST = re.compile(rf"\s*\[\s*({RB_ITEM}(?:\s*,\s*{RB_ITEM})*)\s*,?\s*\]")
RB_SINGLE = re.compile(rf"\s*\(?\s*({RB_ITEM})\s*(?:,|\)|$)")


def rb_columns(rest):
    m = RB_LIST.match(rest) or RB_SINGLE.match(rest)
    return tuple(re.findall(r"\w+", m.group(1))) if m else None


def rb_index(schema, table, rest, path, line, text):
    cols = rb_columns(rest)
    if not cols or re.search(r"\bwhere:", rest) or re.search(r"\busing:\s*:?(?!btree)\w", rest) \
            or re.search(r"\b(?:type|length|opclass):", rest):
        return
    name = re.search(r"name:\s*[\"'](\w+)", rest)
    ordered = bool(re.search(r"\border:", rest))
    schema.index(table, cols, bool(re.search(r"\bunique:\s*true", rest)), name.group(1) if name else None,
                 path, line, text, ordered=ordered)


def rb_reference(schema, table, name, rest, path, line, text):
    poly = bool(re.search(r"polymorphic:\s*(?:true|\{)", rest))
    fk = bool(re.search(r"foreign_key:\s*(?!false|nil)\S", rest))
    schema.column(table, f"{name}_id", path, line, text, fk=fk and not poly, ref_like=not poly, poly=poly)
    if poly:
        schema.column(table, f"{name}_type", path, line, text)
    if re.search(r"\bindex:\s*false", rest):
        return
    unique = bool(re.search(r"\bindex:\s*\{[^}]*unique:\s*true", rest) or re.search(r"\bunique:\s*true", rest))
    cols = (f"{name}_type", f"{name}_id") if poly else (f"{name}_id",)
    schema.index(table, cols, unique, None, path, line, text, implicit=True)


def parse_rails(schema, rel, text, migration):
    if migration:
        text = re.split(r"^\s*def down\b", text, maxsplit=1, flags=re.M)[0]
    current, var = None, None
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        m = RB_TABLE.match(s)
        if m:
            current, var = schema.table(m.group(2)), m.group(4)
            if m.group(1) == "create_table":
                opts = m.group(3)
                pk = re.search(r"primary_key:\s*\[([^\]]*)\]", opts)
                if re.search(r"\bid:\s*false", opts):
                    current.pk = ()
                elif pk:
                    current.pk = tuple(re.findall(r"\w+", pk.group(1)))
                else:
                    current.pk = ("id",)
            continue
        if current is not None:
            if s == "end":
                current = None
                continue
            m = re.match(rf"{var}\.(\w+)(.*)$", s)
            if m:
                rb_table_call(schema, current, m.group(1), m.group(2), rel, no, raw)
            continue
        rb_top_call(schema, s, rel, no, raw)


def rb_table_call(schema, table, method, rest, path, line, text):
    if method == "index":
        rb_index(schema, table, rest, path, line, text)
    elif method in {"references", "belongs_to"}:
        m = RB_ID_ARG.match(rest)
        if m:
            rb_reference(schema, table, m.group(1), rest, path, line, text)
    elif method == "timestamps":
        for name in ("created_at", "updated_at"):
            schema.column(table, name, path, line, text)
    elif method in RB_TYPES:
        m = RB_ID_ARG.match(rest)
        if m:
            name = m.group(1)
            schema.column(table, name, path, line, text)
            if re.search(r"\bindex:\s*(?:true|\{)", rest):
                unique = bool(re.search(r"\bindex:\s*\{[^}]*unique:\s*true", rest))
                schema.index(table, (name,), unique, None, path, line, text, implicit=True)


def rb_top_call(schema, s, path, line, text):
    m = re.match(r"(add_index|add_reference|add_belongs_to|add_foreign_key|add_column|remove_index)\s*\(?\s*[:\"'](\w+)[\"']?\s*,?(.*)$", s)
    if not m:
        return
    call, name, rest = m.groups()
    table = schema.table(name)
    if call == "add_index":
        rb_index(schema, table, rest, path, line, text)
    elif call in {"add_reference", "add_belongs_to"}:
        m = RB_ID_ARG.match(rest)
        if m:
            rb_reference(schema, table, m.group(1), rest, path, line, text)
    elif call == "add_column":
        m = RB_ID_ARG.match(rest)
        if m:
            schema.column(table, m.group(1), path, line, text)
    elif call == "add_foreign_key":
        target = RB_ID_ARG.match(rest)
        explicit = re.search(r"column:\s*[:\"'](\w+)", rest)
        col = explicit.group(1) if explicit else (singular(target.group(1)) + "_id" if target else None)
        if col:
            schema.column(table, col, path, line, text, fk=True, ref_like=True)
    elif call == "remove_index":
        cols = rb_columns(rest)
        if not cols:
            explicit = re.search(r"column:\s*(\[[^\]]*\]|[:\"']\w+[\"']?)", rest)
            cols = rb_columns(explicit.group(1)) if explicit else None
        by_name = re.search(r"name:\s*[\"'](\w+)", rest)
        table.indexes = [
            i for i in table.indexes
            if not ((cols and i.columns == cols) or (by_name and i.name == by_name.group(1)))
        ]


# ---------- Django ----------

def python_statements(text):
    out, buf, start, depth = [], "", 0, 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if not buf:
            start, indent = no, len(raw) - len(raw.lstrip())
        buf += ("\n" if buf else "") + raw.strip()
        depth += sum(raw.count(c) for c in "([{") - sum(raw.count(c) for c in ")]}")
        if depth <= 0:
            out.append((start, indent, buf))
            buf, depth = "", 0
    return out


def django_field(schema, table, name, kind, args, path, line, text):
    suppressed = MARKER in text
    if kind in {"ForeignKey", "OneToOneField"}:
        db_column = re.search(r"db_column\s*=\s*['\"](\w+)", args)
        col = db_column.group(1) if db_column else f"{name}_id"
        table.fields[name] = col
        constraint = not re.search(r"db_constraint\s*=\s*False", args)
        schema.column(table, col, path, line, text, fk=constraint, ref_like=True)
        indexed = kind == "OneToOneField" or not re.search(r"db_index\s*=\s*False", args)
        if indexed:
            schema.index(table, (col,), kind == "OneToOneField", None, path, line, text, implicit=True)
        return
    table.fields[name] = name
    schema.column(table, name, path, line, text)
    table.columns[name].suppressed = table.columns[name].suppressed or suppressed
    if re.search(r"primary_key\s*=\s*True", args):
        table.pk = (name,)
    elif re.search(r"\bunique\s*=\s*True", args):
        schema.index(table, (name,), True, None, path, line, text, implicit=True)
    elif re.search(r"db_index\s*=\s*True", args):
        schema.index(table, (name,), False, None, path, line, text, implicit=True)


def django_columns(table, fields_text):
    names = [n for n in re.findall(r"['\"](-?\w+)['\"]", fields_text)]
    ordered = any(n.startswith("-") for n in names)
    return tuple(table.fields.get(n.lstrip("-"), n.lstrip("-")) for n in names), ordered


def django_meta(schema, table, stmt, path, line, text, skip_declared=False):
    key = stmt.split("=", 1)[0].strip()
    body = stmt.split("=", 1)[1]
    if key == "indexes" or key == "constraints":
        marker = "Index(" if key == "indexes" else "UniqueConstraint("
        for seg in re.split(r"(?<![A-Za-z])" + re.escape(marker), body)[1:]:
            fields_m = re.search(r"fields\s*=\s*\[([^\]]*)\]", seg)
            if not fields_m or re.search(r"condition=|opclasses=|include=", seg):
                continue
            cols, ordered = django_columns(table, fields_m.group(1))
            name = re.search(r"name\s*=\s*['\"](\w+)", seg)
            schema.index(table, cols, key == "constraints", name.group(1) if name else None, path, line, text,
                         ordered=ordered, skip_declared=skip_declared)
    elif key in {"unique_together", "index_together"}:
        nested = re.search(r"[\(\[]\s*[\(\[]", body)
        groups = re.findall(r"[\(\[]([^\(\)\[\]]*)[\)\]]", body) if nested else [body]
        for group in groups:
            cols, ordered = django_columns(table, group)
            schema.index(table, cols, key == "unique_together", None, path, line, text, ordered=ordered)


def app_scope(rel):
    parts = PurePosixPath(rel).parts[:-1]
    if parts and parts[-1] in {"models", "migrations"}:
        parts = parts[:-1]
    return "/".join(parts)


def django_classes(text):
    classes, current = {}, None
    for start, indent, stmt in python_statements(text):
        head = re.match(r"class\s+(\w+)\s*(?:\((.*?)\))?\s*:", stmt)
        if head and indent == 0:
            bases = re.findall(r"[\w.]+", head.group(2) or "")
            current = classes[head.group(1)] = {"bases": bases, "body": [], "abstract": False}
        elif current is not None:
            current["body"].append((start, indent, stmt))
            if re.match(r"abstract\s*=\s*True", stmt):
                current["abstract"] = True
    return classes


def django_is_model(name, classes, seen=()):
    bases = classes[name]["bases"]
    return any(
        b.split(".")[-1] == "Model" or "Model" in b.split(".")[-1]
        or (b in classes and b not in seen and django_is_model(b, classes, seen + (name,)))
        for b in bases
    )


def django_body(name, classes, seen=()):
    own = classes[name]["body"]
    inherited = []
    for b in classes[name]["bases"]:
        if b in classes and b not in seen and classes[b]["abstract"]:
            inherited += django_body(b, classes, seen + (name,))
    return inherited + own


def parse_django_model(schema, rel, text):
    classes = django_classes(text)
    scope = app_scope(rel)
    for name in classes:
        if classes[name]["abstract"] or not django_is_model(name, classes):
            continue
        table = schema.table(name.lower(), scope)
        in_meta, meta_indent = False, 0
        for start, indent, stmt in django_body(name, classes):
            head = re.match(r"class\s+(\w+)\s*(?:\((.*?)\))?\s*:", stmt)
            if head and head.group(1) == "Meta":
                in_meta, meta_indent = True, indent
                continue
            if in_meta and indent <= meta_indent:
                in_meta = False
            if in_meta:
                if "=" in stmt:
                    django_meta(schema, table, stmt, rel, start, stmt)
                continue
            m = re.match(r"(\w+)\s*=\s*(?:\w+\.)*(\w+)\((.*)\)\s*$", stmt, re.S)
            if m and (m.group(2) in {"ForeignKey", "OneToOneField"} or m.group(2).endswith("Field")):
                django_field(schema, table, m.group(1), m.group(2), m.group(3), rel, start, stmt)


def parse_django_migration(schema, rel, text):
    for m in re.finditer(r"migrations\.(AddIndex|AddField|AddConstraint)\(", text):
        end = balanced(text, m.end() - 1)
        seg = text[m.end():end]
        model = re.search(r"model_name\s*=\s*['\"](\w+)", seg)
        if not model:
            continue
        table = schema.table(model.group(1), app_scope(rel))
        line = line_at(1, text, m.start())
        if m.group(1) == "AddField":
            name = re.search(r"\bname\s*=\s*['\"](\w+)", seg)
            fld = re.search(r"field\s*=\s*(?:\w+\.)*(\w+)\(", seg)
            if name and fld:
                args = seg[fld.end():]
                django_field(schema, table, name.group(1), fld.group(1), args, rel, line, seg)
        else:
            key = "indexes" if m.group(1) == "AddIndex" else "constraints"
            django_meta(schema, table, f"{key} = [{seg}]", rel, line, seg, skip_declared=True)


# ---------- Laravel and Knex ----------

def parse_builder(schema, rel, text, cfg, block_re, end_re):
    entries, table, var = [], None, None
    lines = text.splitlines()
    for no, raw in enumerate(lines, 1):
        if table is None:
            m = block_re.search(raw)
            if m:
                table, var, entries = schema.table(m.group(1)), m.group(2), []
            continue
        if end_re.match(raw.strip()):
            apply_builder(schema, table, var, statements(entries), rel, cfg)
            table = None
        else:
            entries.append((no, raw))


def apply_builder(schema, table, var, stmts, path, cfg):
    sep = cfg["sep"]
    for line, stmt in stmts:
        prefix = r"\$" + var if cfg is LARAVEL else var
        m = re.match(rf"{prefix}{sep}(\w+)\((.*?)\)(.*)$", stmt.rstrip(";"), re.S)
        if not m:
            continue
        method, args, chain = m.groups()
        names = names_in(args)
        listed = re.match(r"\s*\[([^\]]*)\]", args)
        cols = tuple(names_in(listed.group(1))) if listed else tuple(names[:1])
        if method in {"index", "unique", "primary"}:
            if method == "primary":
                table.pk = cols
            elif cols:
                schema.index(table, cols, method == "unique", None, path, line, stmt)
        elif method == "foreign":
            if len(cols) == 1:
                schema.column(table, cols[0], path, line, stmt, fk=True, ref_like=True)
        elif method in cfg["morphs"] and names:
            type_col, id_col = f"{names[0]}_type", f"{names[0]}_id"
            schema.column(table, type_col, path, line, stmt)
            schema.column(table, id_col, path, line, stmt, poly=True)
            schema.index(table, (type_col, id_col), False, None, path, line, stmt, implicit=True)
        elif method in {"timestamps", "softDeletes"}:
            for name in ("created_at", "updated_at") if method == "timestamps" else ("deleted_at",):
                schema.column(table, name, path, line, stmt)
        elif method in cfg["pk"] or method in cfg["ref"] or method in BUILDER_TYPES:
            name = names[0] if names else "id"
            if method in cfg["pk"]:
                table.pk = (name,)
            fk = bool(cfg["fk_chain"].search(chain))
            schema.column(table, name, path, line, stmt, fk=fk, ref_like=fk or method in cfg["ref"])
            if re.search(sep + r"\s*primary\s*\(", chain):
                table.pk = (name,)
            if re.search(sep + r"\s*index\s*\(", chain):
                schema.index(table, (name,), False, None, path, line, stmt, implicit=True)
            if re.search(sep + r"\s*unique\s*\(", chain):
                schema.index(table, (name,), True, None, path, line, stmt, implicit=True)


LARAVEL_BLOCK = re.compile(r"Schema::(?:create|table)\(\s*['\"](\w+)['\"]\s*,\s*(?:static\s+)?(?:function|fn)\s*\(\s*(?:Blueprint\s+)?\$(\w+)")
KNEX_BLOCK = re.compile(r"\.(?:createTable|alterTable|table)\(\s*['\"`](\w+)['\"`]\s*,\s*(?:async\s*)?(?:function\s*)?\(?\s*(\w+)\s*\)?\s*(?:=>\s*)?\{")


def parse_laravel(schema, rel, text):
    text = re.split(r"function\s+down\s*\(", text, maxsplit=1)[0]
    parse_builder(schema, rel, text, LARAVEL, LARAVEL_BLOCK, re.compile(r"^\}\s*\)"))


def parse_knex(schema, rel, text):
    text = re.split(r"(?:exports\.down\s*=|export\s+(?:async\s+)?function\s+down\b)", text, maxsplit=1)[0]
    parse_builder(schema, rel, text, KNEX, KNEX_BLOCK, re.compile(r"^\}\s*\)"))


# ---------- Prisma ----------

def parse_prisma(schema, rel, text):
    blocks = list(re.finditer(r"^model\s+(\w+)\s*\{(.*?)^\}", text, re.M | re.S))
    models = {b.group(1) for b in blocks}
    for block in blocks:
        table = schema.table(block.group(1))
        table.pk = ()
        base = line_at(1, text, block.start(2))
        for m in re.finditer(r"@@(index|unique|id)\(\s*\[([^\]]*)\]", block.group(2)):
            inner = re.sub(r"\([^)]*\)", "", m.group(2))
            cols = tuple(re.findall(r"\w+", inner))
            if m.group(1) == "id":
                table.pk = cols
            else:
                schema.index(table, cols, m.group(1) == "unique", None, rel, line_at(base, block.group(2), m.start()),
                             "", ordered="sort:" in m.group(2) and "Desc" in m.group(2))
        for offset, raw in enumerate(block.group(2).split("\n")):
            line = base + offset
            s = raw.strip()
            m = re.match(r"(\w+)\s+(\w+)(\?|\[\])?\s*(.*)$", s)
            if not m or s.startswith("@@"):
                continue
            name, kind, mod, attrs = m.groups()
            relation = re.search(r"@relation\([^)]*fields:\s*\[([^\]]*)\]", attrs)
            if kind in models:
                owned = re.findall(r"\w+", relation.group(1)) if relation else []
                if len(owned) == 1:
                    schema.column(table, owned[0], rel, line, raw, fk=True, ref_like=True)
                continue
            schema.column(table, name, rel, line, raw)
            if "@id" in attrs:
                table.pk = (name,)
            elif "@unique" in attrs:
                schema.index(table, (name,), True, None, rel, line, raw, implicit=True)


# ---------- Query usage ----------

def logical_lines(text):
    out, buf, start, depth, count = [], None, 0, 0, 0
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        joins = buf is not None and count < 30 and (
            depth > 0 or s.startswith((".", "->", "&.")) or buf.endswith((".", "->"))
        ) and not s.startswith("..")
        if joins:
            buf += " " + s
            count += 1
        else:
            if buf is not None:
                out.append((start, buf))
            buf, start, depth, count = s, no, 0, 0
        depth = max(0, depth + sum(s.count(c) for c in "([{") - sum(s.count(c) for c in ")]}"))
    if buf is not None:
        out.append((start, buf))
    return out


def parse_chain(text, i, allowed):
    calls = []
    name_re = re.compile(r"\s*(\w+[?!]?)")
    sep_re = re.compile(r"\s*(?:&?\.|->)")
    while True:
        m = name_re.match(text, i)
        if not m:
            break
        name, j, args = m.group(1), m.end(), ""
        if j < len(text) and text[j] == "(":
            end = balanced(text, j)
            args, j = text[j + 1:end], end + 1
        if name not in allowed and not name.startswith("find_by_"):
            break
        calls.append((name, args))
        s = sep_re.match(text, j)
        if not s:
            break
        i = s.end()
    return calls


def strip_nested(text):
    for _ in range(3):
        text = re.sub(r"\{[^{}]*\}|\[[^\[\]]*\]", "", text)
    return text


def rails_columns(name, args):
    if name.startswith("find_by_"):
        return [(c, "lookup") for c in name[len("find_by_"):].split("_and_")]
    if name in {"order", "reorder"}:
        hash_keys = re.findall(r"(\w+):\s*:(?:asc|desc)\b", args)
        rest = re.sub(r"\w+:\s*:\w+", "", args)
        cols = hash_keys + re.findall(r":(\w+)", rest)
        for s in re.findall(r"['\"]([^'\"]*)['\"]", rest):
            for part in s.split(","):
                m = re.fullmatch(r"\s*(?:\w+\.)?(\w+)(?:\s+(?:asc|desc))?\s*", part, re.I)
                if m:
                    cols.append(m.group(1))
        return [(c, "order") for c in cols]
    kind = "lookup" if name.startswith("find_") or name == "exists?" else "filter"
    sql = re.findall(r"['\"]\s*(?:\w+\.)?(\w+)\s*(?:=|<|>|!=|\bIN\b|\bLIKE\b|\bILIKE\b|\bIS\b|\bBETWEEN\b)", args, re.I)
    plain = re.sub(r"(['\"]).*?\1", "", strip_nested(args))
    keys = re.findall(r"(?:^|[,\s({])(\w+):(?!:)", plain)
    return [(c, kind) for c in sql + keys]


def django_usage_columns(name, args):
    cols = []
    for piece in split_top(args):
        if name == "order_by":
            m = re.fullmatch(r"\s*['\"]-?(\w+)['\"]\s*", piece)
            if m:
                cols.append((m.group(1), "order"))
            continue
        m = re.match(r"\s*(\w+)\s*=(?!=)", piece)
        if not m:
            continue
        parts = m.group(1).split("__")
        if parts[-1] in DJANGO_LOOKUPS:
            parts = parts[:-1]
        if len(parts) == 1 and parts[0] != "pk":
            cols.append((parts[0], "lookup" if name == "get" else "filter"))
    return cols


def builder_usage_columns(name, args, filters, js=False):
    if name in {"latest", "oldest"}:
        m = re.match(r"\s*['\"](\w+)['\"]", args)
        return [(m.group(1) if m else "created_at", "order")]
    if name in filters or name in {"orderBy", "orderByDesc"}:
        kind = "order" if name.startswith("order") else ("lookup" if name == "firstWhere" else "filter")
        m = re.match(r"\s*['\"](?:\w+\.)?(\w+)['\"]", args)
        if m:
            return [(m.group(1), kind)]
        if js and args.strip().startswith("{"):
            return [(k, kind) for k in top_level_keys(args.strip())]
    return []


def resolve_receiver(schema, text, pattern):
    for m in re.finditer(pattern, text):
        table = schema.find(m.group(1))
        if table:
            return table, m.end()
    return None, 0


def line_usages(schema, text, ext, cls):
    if ext == ".rb":
        table, start = resolve_receiver(schema, text, r"\b([A-Z]\w*)\s*\.\s*(?=[a-z])")
        if not table and cls:
            m = re.search(r"(?<![\w.@:])(?:where|order|find_by|exists\?|reorder|rewhere)(?=[\s(.])", text)
            table, start = schema.find(cls), (m.start() if m else -1)
            if start < 0:
                return table, []
        return table, [c for n, a in parse_chain(text, start, RAILS_ALLOWED) for c in rails_columns(n, a)] if table else []
    if ext == ".py":
        table, start = resolve_receiver(schema, text, r"\b([A-Z]\w*)\s*\.\s*(?=objects\b)")
        calls = parse_chain(text, start, DJANGO_ALLOWED) if table else []
        return table, [c for n, a in calls for c in django_usage_columns(n, a)]
    if ext == ".php":
        table, start = resolve_receiver(schema, text, r"\b([A-Z]\w*)::(?=\w)")
        if not table:
            m = re.search(r"\bDB::table\(\s*['\"](\w+)['\"]\s*\)\s*->", text)
            table, start = (schema.find(m.group(1)), m.end()) if m else (None, 0)
        if not table and cls and re.search(r"\$this->|\b(?:static|self)::", text):
            m = re.search(r"(?:\$this->|\b(?:static|self)::)", text)
            table, start = schema.find(cls), m.end()
        calls = parse_chain(text, start, LARAVEL_ALLOWED) if table else []
        return table, [c for n, a in calls for c in builder_usage_columns(n, a, LARAVEL_FILTERS)]
    m = re.search(r"\b(?:knex|db|trx)\(\s*['\"](\w+)['\"]\s*\)\s*\.", text)
    table = schema.find(m.group(1)) if m else None
    calls = parse_chain(text, m.end(), KNEX_ALLOWED) if table else []
    return table, [c for n, a in calls for c in builder_usage_columns(n, a, KNEX_FILTERS, js=True)]


def prisma_usages(schema, text):
    for m in re.finditer(rf"\b\w+\.(\w+)\.(?:{PRISMA_METHODS})\s*\(", text):
        table = schema.find(m.group(1))
        if not table:
            continue
        args = text[m.end():balanced(text, m.end() - 1)]
        found = []
        where = re.search(r"\bwhere\s*:\s*\{", args)
        if where:
            block = args[where.end() - 1:balanced(args, where.end() - 1) + 1]
            found += [(k, "filter") for k in top_level_keys(block) if k not in {"AND", "OR", "NOT"}]
        order = re.search(r"\borderBy\s*:\s*([\[{])", args)
        if order:
            block = args[order.end() - 1:balanced(args, order.end() - 1) + 1]
            found += [(k, "order") for k in top_level_keys(block, 1 if order.group(1) == "{" else 2)]
        yield table, found, line_at(1, text, m.start())


def collect_usages(schema, rel, text, ext):
    usages = []
    if ext in JS_EXTS:
        for table, found, line in prisma_usages(schema, text):
            usages += [(table, c, kind, line) for c, kind in found]
    cls = None
    for no, line in logical_lines(text):
        m = re.match(r"(?:abstract\s+|final\s+)?class\s+(\w+)", line)
        if m:
            cls = m.group(1)
        if MARKER in line:
            continue
        table, cols = line_usages(schema, line, ext, cls)
        usages += [(table, c, kind, no) for c, kind in cols]
    return [(t, c, k, l, rel) for t, c, k, l in usages]


# ---------- Analysis ----------

def covered_leads(table):
    leads = {i.columns[0] for i in table.indexes}
    if table.pk:
        leads.add(table.pk[0])
    return leads


def fk_findings(table, engine):
    leads, out = covered_leads(table), []
    for col in table.columns.values():
        if not (col.fk or col.ref_like) or col.name in leads or col.suppressed:
            continue
        if col.fk and engine == "mysql":
            continue
        if col.fk:
            note = {
                "postgres": "PostgreSQL does not index foreign keys, so joins and parent deletes scan this table.",
                "auto": "PostgreSQL does not index foreign keys (joins and parent deletes scan this table); MySQL InnoDB creates one implicitly.",
            }[engine]
            msg = f"{table.name}.{col.name} is a foreign key with no index starting with it. {note}"
        else:
            msg = (f"{table.name}.{col.name} looks like a reference (name or declaration) with no index and no foreign key "
                   "constraint, so no engine adds one.")
        out.append(Finding(FK_RULE, col.path, col.line, table.name, col.name, msg))
    return out


def label(index):
    return index.name or ",".join(index.columns)


def redundant_findings(table):
    pk = Index(table.pk, True, "the primary key", "", 0) if table.pk else None
    pool = ([pk] if pk else []) + table.indexes
    order = {id(i): n for n, i in enumerate(pool)}
    out = []
    for a in table.indexes:
        if a.ordered or a.suppressed:
            continue
        for b in pool:
            if b is a or b.ordered or b.columns[:len(a.columns)] != a.columns:
                continue
            same_len = len(b.columns) == len(a.columns)
            if a.unique and not (same_len and b.unique):
                continue
            if same_len and a.unique == b.unique and order[id(b)] > order[id(a)]:
                continue
            verb = "duplicates" if same_len else "is a prefix of"
            msg = (f"index {label(a)} on {table.name} ({','.join(a.columns)}) {verb} {label(b)} "
                   f"({','.join(b.columns)}); confirm with usage stats, then drop the narrower one.")
            out.append(Finding(REDUNDANT_RULE, a.path, a.line, table.name, ",".join(a.columns), msg))
            break
    return out


def query_findings(schema, usages, reported):
    grouped = {}
    for table, col_name, kind, line, rel in usages:
        column = table.fields.get(col_name, col_name)
        if column not in table.columns or column in covered_leads(table) or (table.name, column) in reported:
            continue
        entry = grouped.setdefault((table.name, column), {"n": 0, "kinds": set(), "at": (rel, line)})
        entry["n"] += 1
        entry["kinds"].add(kind)
    out = []
    for (name, column), entry in grouped.items():
        rel, line = entry["at"]
        kinds = "/".join(sorted(entry["kinds"]))
        msg = (f"{name}.{column} is used for {kinds} {entry['n']} time(s) in code but no index starts with it; "
               "check selectivity before adding one.")
        out.append(Finding(QUERY_RULE, rel, line, name, column, msg))
    return out


PARSERS = {
    "rails_schema": lambda s, r, t: parse_rails(s, r, t, False),
    "rails_migration": lambda s, r, t: parse_rails(s, r, t, True),
    "django_model": parse_django_model,
    "django_migration": parse_django_migration,
    "laravel": parse_laravel,
    "knex": parse_knex,
    "prisma": parse_prisma,
}
PARSE_ORDER = ["rails_schema", "rails_migration", "django_model", "django_migration", "laravel", "knex", "prisma"]


def scan(root, engine="auto"):
    root = Path(root)
    files = [(p, rel, classify(rel)) for p, rel in iter_files(root)]
    kinds = {k for _, _, k in files}
    if "rails_schema" in kinds:
        files = [f for f in files if f[2] != "rails_migration"]
    schema = Schema()
    for kind in PARSE_ORDER:
        for path, rel, k in files:
            if k == kind:
                PARSERS[kind](schema, rel, read_source(path))
    findings = []
    for table in schema.tables.values():
        findings += redundant_findings(table)
    reported = set()
    for table in schema.tables.values():
        fks = fk_findings(table, engine)
        reported |= {(table.name, f.column) for f in fks}
        findings += fks
    usages = []
    for path, rel, k in files:
        parts = PurePosixPath(rel).parts[:-1]
        if k in {"rails_schema", "rails_migration", "django_migration", "laravel", "knex", "prisma"}:
            continue
        if path.suffix in CODE_EXTS and not (set(parts) & USAGE_SKIP_DIRS):
            usages += collect_usages(schema, rel, read_source(path), path.suffix)
    findings += query_findings(schema, usages, reported)
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule, f.column))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find missing and redundant database indexes.")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--engine", choices=["auto", "postgres", "mysql"], default="auto")
    args = parser.parse_args(argv)
    root = Path(args.path)
    if not root.exists():
        print(f"no such path: {root}", file=sys.stderr)
        return 2
    findings = scan(root, args.engine)
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: [{f.rule}] {f.message}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
