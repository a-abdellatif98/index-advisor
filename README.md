# index-advisor

A plugin that finds missing and redundant database indexes from your schema and migration files: foreign keys with no index, columns used in `WHERE`, `ORDER BY` and unique lookups with no index, and indexes another index already covers.

It pairs with these sibling plugins:

- [n-plus-one-hunter](https://github.com/a-abdellatif98/n-plus-one-hunter): a per-row query is only cheap when the foreign key it filters on is indexed, so fix the N+1 and then index the key.
- [query-plan-explainer](https://github.com/a-abdellatif98/query-plan-explainer): confirm with `EXPLAIN` that the new index is used.
- [db-migration-guard](https://github.com/a-abdellatif98/db-migration-guard) and [backfill-planner](https://github.com/a-abdellatif98/backfill-planner): review the migration that adds the index and any data job around it.

## What it looks for

- **Foreign keys with no index**: PostgreSQL never adds one, MySQL InnoDB adds one implicitly when a real constraint exists
- **Reference columns with no constraint** (`user_id` as a plain column): no engine indexes these
- **Queried columns with no index**: `where`, `find_by`, `filter`, `order_by`, `orderBy`, `findUnique`, `latest()`
- **Redundant indexes**: a leading prefix of another index or of the primary key, and exact duplicates
- **The migration**, in the online form for your engine: `CREATE INDEX CONCURRENTLY` on PostgreSQL, `ALGORITHM=INPLACE, LOCK=NONE` on MySQL

Frameworks: Rails (`db/schema.rb`, migrations), Django (models, migrations), Laravel (migrations), Prisma (`model` blocks only) and Knex migrations.

## Installation

```
/plugin install index-advisor@claude-plugins-official
```

## Usage

### Slash command

```
/advise-indexes
/advise-indexes orders
/advise-indexes app/models/invoice.rb
```

It reads the schema, migrations and the code that queries them (read-only tools only) and outputs numbered findings, each with `file:line`, the reason, and the migration as code.

### Skill

The `advise-indexes` skill activates when you talk about missing, slow, redundant or duplicate indexes, on Claude.ai as well as in Claude Code. References:

| Reference | Covers |
|---|---|
| `engines.md` | PostgreSQL vs MySQL on foreign keys, partial indexes, redundancy checks and online creation |
| `rails.md` | `add_index`, `add_reference`, `algorithm: :concurrently`, `disable_ddl_transaction!` |
| `django.md` | `db_index`, `Meta.indexes`, `UniqueConstraint`, `AddIndexConcurrently`, `atomic = False` |
| `laravel.md` | `foreignId`, `index`, `morphs`, `$withinTransaction`, raw concurrent statements |
| `prisma-node.md` | `@@index`, relation fields, `migrate dev --create-only`; Knex `index`, `transaction: false` |

### Static scanner

```
python3 scripts/index_scan.py path/to/project
python3 scripts/index_scan.py --engine postgres --json path/to/project
```

A deterministic heuristic scan. It reads `db/schema.rb` (or `db/migrate` when there is no schema file), Django `models.py` and migrations, `database/migrations/*.php`, Prisma `model` blocks and Knex migrations, then the application code for queries. Rules: `IDX-FK`, `IDX-QUERY`, `IDX-REDUNDANT`. `--engine mysql` drops foreign keys that have a real constraint, `--engine postgres` keeps them. Exit code is 1 when it finds something, 0 when clean, 2 for a bad path. Put `index-advisor: ok` on a line to suppress a finding.

It is a heuristic: it attributes a query to a table only when the model or table is named in the same statement, it ignores spec and test folders, it does not follow dropped columns or tables outside Rails `remove_index`, and it cannot see raw SQL files, `structure.sql` or the Prisma datasource. Confirm each hit with `EXPLAIN`.

## Safety

The plugin ships no hooks. The command only has Read, Glob and Grep. Nothing reads `.env`, the Prisma datasource, database config or other credentials, and nothing runs your code: the scanner skips secret-looking files by name, only reads source files, and you run it yourself.

## Development

```
pip install pytest pyflakes
python3 -m pytest -q tests
python3 -m pyflakes scripts tests
```

## License

MIT
