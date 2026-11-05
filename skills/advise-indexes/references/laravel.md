# Laravel

## Where to read

- `database/migrations/*.php`: `Schema::create` / `Schema::table` blocks, `up()` only (ignore `down()`).
- `$table->foreignId('user_id')->constrained()` adds an unsigned big integer column and a foreign key constraint, not an index on PostgreSQL. `->index()` on the column adds one; MySQL InnoDB creates its own implicit index for the constraint.
- `$table->morphs('taggable')` adds the type and id columns and an index on both.
- `->unique()` and `$table->unique([...])` create unique indexes; `$table->index([...], 'name')` a plain one.
- Query sites: `Model::where('col', ...)`, `->orderBy('col')`, `->latest()` and `->oldest()` (default column `created_at`), `DB::table('t')->where(...)`, `firstWhere`, and relation queries (`hasMany` loads filter by the foreign key).

## Patterns

```php
$table->foreignId('customer_id')->index()->constrained();
$table->index(['customer_id', 'created_at']);
$table->index('created_at');   // needed by latest()
```

## Online creation

The schema builder has no concurrent option. PostgreSQL:

```php
class AddIndexInvoicesOnCustomerId extends Migration
{
    public $withinTransaction = false;

    public function up(): void
    {
        DB::statement('CREATE INDEX CONCURRENTLY invoices_customer_id_index ON invoices (customer_id)');
    }

    public function down(): void
    {
        DB::statement('DROP INDEX CONCURRENTLY IF EXISTS invoices_customer_id_index');
    }
}
```

`$withinTransaction` is available on recent Laravel versions, check yours. MySQL:

```php
DB::statement('ALTER TABLE invoices ADD INDEX invoices_customer_id_index (customer_id), ALGORITHM=INPLACE, LOCK=NONE');
```

## Redundancy

`->unique()` plus a later `$table->index('same_column')`, or `index(['a'])` next to `index(['a', 'b'])`, are duplicates or prefixes. Laravel names indexes `<table>_<columns>_<type>`; `dropIndex` takes that name or an array of columns. Never drop the unique one in favour of the plain one.
