---
description: Find missing and redundant database indexes from schema and migration files, and propose a safe migration for each
argument-hint: [path, table or model name, or description]
allowed-tools: [Read, Glob, Grep]
---

# Advise Indexes

Find foreign keys and queried columns that lack an index, and indexes that another index already covers, following the `advise-indexes` skill.

## Arguments

The user invoked this command with: $ARGUMENTS

Treat the argument as a path, a table or model name, or free text. If it is empty, ask which table, model or endpoint to look at, or default to the schema and migration files of the whole project.

## Instructions

1. **Detect the framework and the engine** with Glob and Read: Rails (`db/schema.rb`, `db/migrate`), Django (`manage.py`, `models.py`, `migrations/`), Laravel (`database/migrations`), Prisma (`prisma/schema.prisma`), Knex (`knexfile` and a `migrations/` folder of JS or TS files). The engine (PostgreSQL or MySQL) matters: ask the user if it is not stated, and do not guess from credentials.

   Never open `.env`, `.env.*`, `config/database.yml`, `settings.py` `DATABASES`, `config/database.php`, the Prisma `datasource` block, credential files, or anything holding tokens or database URLs. In `schema.prisma` read only the `model` blocks.

2. **Build the index map**: for each table list its columns, primary key, foreign keys and every index (columns in order, unique or not, partial or not). Migrations only: replay them in order, honouring removed indexes.

3. **Foreign keys**: report each FK column (and each `*_id` reference column) that no index starts with. Apply the engine rule in `skills/advise-indexes/references/engines.md`.

4. **Queried columns**: Grep the code for `where`, `find_by`, `order`, `filter`, `order_by`, `orderBy`, `findMany`, `findUnique` and unique lookups. Report columns with no index starting with them, and suggest a composite index when the same query filters on one column and sorts on another. Say what selectivity or table size makes it worth it.

5. **Redundant indexes**: report an index whose columns are a leading prefix of another index (or of the primary key), and exact duplicates. A unique index is never redundant for that reason. Skip partial, expression and non-btree indexes.

6. **Propose the migration** in the project's framework, using the concurrent or online form for the engine (`skills/advise-indexes/references/`).

## Output format

### Index findings: `target`

**Detected:** framework, engine, tables read.

**Missing:** numbered, each with `file:line`, the column, the query or constraint that needs it, and the migration as code.

**Redundant:** numbered, the index to drop, the index that covers it, and how to check it is unused first.

**Not a problem:** columns you checked that are covered, tiny tables, low-cardinality flags (say why).

**Scanner:** mention that `python3 scripts/index_scan.py --engine postgres <project root>` (run from the plugin directory by the user) gives a heuristic first pass, exit code 1 when it finds something. Confirm each hit.

Do not run anything. Read code, produce findings and migrations only.
