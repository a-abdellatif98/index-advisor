# PostgreSQL and MySQL differences

## Foreign keys

- **PostgreSQL** does not create an index on the referencing column of a foreign key. The referenced column needs a unique constraint or primary key, the referencing side gets nothing. Without an index, joins on the key and `DELETE`/`UPDATE` of a parent row scan the child table.
- **MySQL InnoDB** requires an index on foreign key columns and creates one automatically when none exists. An index you add later may replace that implicit one, so an explicit index is still worth declaring for clarity.
- A column that merely ends in `_id` with no constraint gets no automatic index on either engine.
- A Prisma datasource using `relationMode = "prisma"` creates no foreign key constraints at all, so no engine adds the index. The scanner cannot read the datasource: pass `--engine` and check that setting yourself.

## Which framework indexes what by default

| Framework | Reference column | Notes |
|---|---|---|
| Rails | `t.references` / `add_reference` add an index (Rails 5 and later) unless `index: false` | `add_foreign_key` alone adds none; `t.bigint "user_id"` adds none |
| Django | `ForeignKey` is indexed unless `db_index=False`; `OneToOneField` is unique | `unique_together` and `Meta.indexes` are separate indexes |
| Laravel | `foreignId()` and `constrained()` add a column and a constraint, not an index on PostgreSQL | `morphs()` adds a `(type, id)` index |
| Prisma | relation scalar fields are not indexed by Prisma | add `@@index([authorId])` |
| Knex | `references()` and `foreign()` add a constraint only | add `.index()` or `table.index([...])` |

## Choosing columns

- A composite index serves queries on its leading columns only: `(a, b)` helps `a` and `a, b`, not `b` alone.
- Equality columns first, then the range or sort column.
- A btree index can be scanned backwards, so `ORDER BY x DESC` on a plain `(x)` index works in PostgreSQL. Mixed directions across columns need a matching index; MySQL 8 supports descending index parts.
- A unique constraint already creates a unique index. Do not add a second plain index on the same columns.
- Low-cardinality columns (booleans, a handful of states) rarely help on their own. A partial index fits better where the engine has them.

## Engine-only features

- **PostgreSQL**: partial indexes (`WHERE`), expression indexes, `INCLUDE` columns, GIN and GiST for JSON, arrays and full text.
- **MySQL**: no partial indexes, functional indexes from 8.0.13, invisible indexes in 8.0, prefix lengths required for long `TEXT` and `BLOB` columns.

## Redundancy checks before a drop

- A unique index is not redundant because a wider index exists.
- Look at operator class, sort order, `INCLUDE` columns and partial conditions: the scanner compares column names only.
- PostgreSQL usage: `pg_stat_user_indexes.idx_scan` (counts since the last statistics reset, check the reset time, and check replicas separately).
- MySQL usage: the `sys.schema_unused_indexes` view, or make the index invisible (`ALTER TABLE ... ALTER INDEX name INVISIBLE`) in 8.0 and watch before dropping.
- Dropping a prefix index that backs a MySQL foreign key is safe only when the wider index starts with the same columns.

## Online creation

- **PostgreSQL**: `CREATE INDEX CONCURRENTLY` does not block writes, scans the table twice and takes longer. It cannot run inside a transaction block. If it fails it leaves an INVALID index: `DROP INDEX CONCURRENTLY`, then retry. Plain `CREATE INDEX` blocks writes for the whole build. `DROP INDEX CONCURRENTLY` also avoids a long lock.
- **MySQL InnoDB**: creating a secondary index is an online operation. Write `ALGORITHM=INPLACE, LOCK=NONE` explicitly so the statement fails rather than silently taking a lock. Very large tables still need disk and time, and replicas apply the change serially, so watch replication lag. Tools such as gh-ost or pt-online-schema-change exist for cases the built-in path cannot cover.
- Adding a foreign key constraint is a separate lock question: `db-migration-guard` covers it.
