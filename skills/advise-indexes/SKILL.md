---
name: advise-indexes
description: "This skill should be used when the user asks which database indexes are missing, unused or redundant, or reviews a schema, model or migration for indexing, for example \"missing index\", \"add an index\", \"unindexed foreign key\", \"slow where clause\", \"slow order by\", \"redundant index\", \"duplicate index\", \"composite index\", \"index_together\", \"add_index\", \"db_index\", \"@@index\", \"$table->index\", \"CREATE INDEX CONCURRENTLY\", \"algorithm: :concurrently\", \"AddIndexConcurrently\", or when a new foreign key, filter column or sort column is added. Finds foreign keys with no index, queried columns with no index and prefix-covered indexes, and proposes a safe migration for PostgreSQL or MySQL."
version: 1.0.0
---

# Advise Indexes

An index trades write cost and disk for read speed. A missing one makes a query scan the table, a redundant one slows every write for nothing. This skill finds both from the schema and the code, and proposes a migration that does not lock the table.

## What to look for

1. **Foreign key with no index.** The referencing column (`orders.user_id`) has no index starting with it. Joins and "all orders of this user" scan the table, and in PostgreSQL deleting a parent row scans the child table. Engine rule: `references/engines.md`.
2. **Reference column with no constraint.** A `*_id` column, a `references`/`belongs_to`/`foreignId` column with no index. No engine adds an index for a column that has no foreign key constraint.
3. **Queried column with no index.** A column in `where`, `find_by`, `filter`, `order`, `order_by`, `orderBy` or a unique lookup (`find_by(email:)`, `findUnique`) that no index starts with.
4. **Composite opportunity.** The same query filters on `a` and sorts or ranges on `b`: one index on `(a, b)` beats two single-column indexes. Equality columns first, then the range or sort column.
5. **Redundant index.** An index whose columns are a leading prefix of another index (or of the primary key), or an exact duplicate. A unique index is not redundant just because a wider index exists, it enforces uniqueness.
6. **Indexes that do not apply.** Partial, expression and non-btree indexes (GIN, GiST, hash) answer different queries: never call them redundant on column names alone.

## Workflow

1. **Detect framework and engine.** Read the schema files (`references/rails.md`, `django.md`, `laravel.md`, `prisma-node.md`). Ask for the engine when it is not stated: PostgreSQL and MySQL differ on foreign keys, partial indexes and online creation.
2. **Build the index map** per table: columns, primary key, foreign keys, every index with column order, uniqueness and any condition. When there is only migrations, replay them in order. The scanner honours only Rails `remove_index`; check Laravel `dropIndex`, Knex `dropIndex` and Django `RemoveIndex` by hand.
3. **Check foreign keys**, then **queried columns** in the code, then **redundancy**.
4. **Judge value before recommending.** A column with two values (a flag, a state), a table of a few hundred rows, or a rarely run report rarely justifies an index. Say so instead of adding one.
5. **Check the write side.** Each index slows inserts and updates on its table. Prefer one composite index over several overlapping ones.
6. **Propose the migration** in the project's framework, in the online form for the engine (below and `references/engines.md`).
7. **Confirm.** Run `EXPLAIN` before and after (the `query-plan-explainer` plugin reads the output). For a drop, check usage statistics first: `references/engines.md`.

## Creating an index without blocking writes

| Engine | Form |
|---|---|
| PostgreSQL | `CREATE INDEX CONCURRENTLY`, outside a transaction. A failed run leaves an INVALID index: drop it and retry |
| MySQL (InnoDB) | `ALGORITHM=INPLACE, LOCK=NONE` on the `ALTER TABLE`, which errors out instead of silently locking |

| Framework | PostgreSQL | MySQL |
|---|---|---|
| Rails | `add_index ..., algorithm: :concurrently` with `disable_ddl_transaction!` | `execute` with the `ALTER TABLE` form |
| Django | `AddIndexConcurrently` with `atomic = False` | `RunSQL` with the `ALTER TABLE` form |
| Laravel | `DB::statement('CREATE INDEX CONCURRENTLY ...')` with `$withinTransaction = false` | `DB::statement` with the `ALTER TABLE` form |
| Prisma | edit the generated SQL migration, one statement per file | same, with the `ALTER TABLE` form |
| Knex | `knex.raw(...)` with `exports.config = { transaction: false }` | `knex.raw` with the `ALTER TABLE` form |

Details and caveats per framework are in the references.

## The scanner

`scripts/index_scan.py` is a deterministic heuristic scan that reads Rails (`db/schema.rb`, or `db/migrate` when there is no schema file), Django (`models.py`, migrations with `AddIndex`, `AddField`, `AddConstraint`), Laravel migrations, Prisma `model` blocks and Knex migrations, then reads the application code for queries. Rule ids: `IDX-FK`, `IDX-QUERY`, `IDX-REDUNDANT`. `--engine postgres|mysql` removes the findings the engine already handles. Add `index-advisor: ok` on a line to suppress it. It only attributes a query to a table when the model or table is named on the same statement, and it does not follow dropped columns or tables, raw SQL files or structure.sql, so treat each hit as a lead and confirm with `EXPLAIN`.
