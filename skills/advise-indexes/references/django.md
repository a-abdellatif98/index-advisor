# Django

## Where to read

- `models.py` (and `models/` packages): fields, `ForeignKey`, `Meta.indexes`, `Meta.constraints`, `unique_together`, `index_together`.
- Migrations: `AddIndex`, `AddField`, `AddConstraint`, `RunSQL`. The model file is the source of truth; migrations show what was applied.
- `ForeignKey` creates an index unless `db_index=False`. `OneToOneField` is unique. A plain `CharField(unique=True)` or `db_index=True` is indexed.
- Query sites: `.filter(col=...)`, `.get(...)`, `.exclude(...)`, `.order_by('-created')`. Lookups such as `__icontains` on PostgreSQL need a different index type (trigram) than a plain btree, and `__startswith` or `__icontains` with a leading wildcard will not use a btree at all.

## Patterns

```python
class Meta:
    indexes = [
        models.Index(fields=["author", "-created"], name="book_author_created"),
        models.Index(fields=["state"], condition=models.Q(state="open"), name="book_open"),  # partial: PostgreSQL
    ]
    constraints = [models.UniqueConstraint(fields=["isbn"], name="book_isbn_uniq")]
```

Prefer `Meta.indexes` and `UniqueConstraint` over `index_together` (deprecated, removed in Django 5.1) and `unique_together` (the docs call it likely to be deprecated). `Index(fields=[...])` accepts `-field` for descending order; give indexes explicit names.

## Online creation

PostgreSQL:

```python
from django.contrib.postgres.operations import AddIndexConcurrently

class Migration(migrations.Migration):
    atomic = False
    operations = [
        AddIndexConcurrently("book", models.Index(fields=["isbn"], name="book_isbn_idx")),
    ]
```

`RemoveIndexConcurrently` is the matching drop. MySQL has no such operation: use `RunSQL` with `ALTER TABLE ... ADD INDEX ..., ALGORITHM=INPLACE, LOCK=NONE` and a `reverse_sql`, and keep `state_operations` in step so the model state matches.

## Redundancy

`db_index=True` on a field plus an `Index(fields=[that field])`, or `Index(fields=["a"])` next to `unique_together = ("a", "b")`, are duplicates or prefixes. Remove the declared one in `Meta` and let a migration drop it (`RemoveIndex`). Conditions (`condition=`), `include=` and `opclasses=` change what an index is for: do not treat those as redundant on field names alone.
