# Rails

## Where to read

- `db/schema.rb` is the current state: `create_table` blocks with `t.index [...], name:, unique: true`, and `add_foreign_key` lines at the end. Prefer it over migrations. With `structure.sql` or no schema file, read `db/migrate` in order, including `remove_index`.
- `add_foreign_key "orders", "users"` means `orders.user_id` is a constraint; `t.bigint "user_id"` alone is just a column.
- Query sites: `where(user_id: x)`, `find_by(email:)`, `find_by_status_and_email`, `order(created_at: :desc)`, scopes, `has_many` (every association load is `WHERE fk = ?`), polymorphic `*_type`/`*_id` pairs (index `[type, id]`).

## Patterns

```ruby
# FK and lookups
add_index :orders, :user_id
add_index :orders, [:user_id, :created_at]         # filter on user, sort by date
add_index :orders, :state, where: "state = 'open'"  # PostgreSQL only
add_reference :orders, :store, foreign_key: true     # indexed by default
```

## Online creation

PostgreSQL:

```ruby
class AddIndexOrdersOnUserId < ActiveRecord::Migration[7.0]
  disable_ddl_transaction!

  def change
    add_index :orders, :user_id, algorithm: :concurrently
  end
end
```

`add_reference ..., index: { algorithm: :concurrently }` works the same way. Keep one concurrent index per migration so a failure leaves a clear state, and `remove_index ..., algorithm: :concurrently` for drops.

MySQL: use the explicit form, which fails instead of locking.

```ruby
def up
  execute "ALTER TABLE orders ADD INDEX index_orders_on_user_id (user_id), ALGORITHM=INPLACE, LOCK=NONE"
end

def down
  remove_index :orders, :user_id
end
```

The MySQL adapter also accepts an `algorithm:` option on `add_index`; check the version you run before relying on it.

## Redundancy

An index `[:user_id]` next to `[:user_id, :created_at]` is a prefix duplicate: drop the narrow one after checking usage. Rails names indexes `index_<table>_on_<cols>`, so the drop is `remove_index :orders, name: "index_orders_on_user_id"`. A unique index on the same column is never the one to drop.
