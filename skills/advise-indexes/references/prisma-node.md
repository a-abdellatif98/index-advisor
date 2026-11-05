# Prisma and Knex

## Prisma

Read only the `model` blocks of `schema.prisma`. Never read or quote the `datasource` block.

- `@id`, `@unique`, `@@id([a, b])`, `@@unique([a, b])` and `@@index([a, b])` define indexes. `@@index([a(sort: Desc)])` sets order.
- A relation `author User @relation(fields: [authorId], references: [id])` makes `authorId` a foreign key. Prisma does not index it for you: add `@@index([authorId])`. MySQL InnoDB would create an implicit index, but being explicit keeps the schema portable.
- Query sites: `prisma.post.findMany({ where: { title }, orderBy: { createdAt: 'desc' } })`, `findUnique`, `findFirst`, `count`, `groupBy`. Relation filters (`where: { author: { ... } }`) hit the related table.

```prisma
model Post {
  id       Int    @id
  authorId Int
  author   User   @relation(fields: [authorId], references: [id])
  slug     String

  @@index([authorId, slug])
}
```

Online creation: `prisma migrate dev --create-only` writes the SQL without applying it. For PostgreSQL edit the file to `CREATE INDEX CONCURRENTLY`, keep it as the only statement in that migration (the command cannot run inside a transaction block), and check against your Prisma version. For MySQL use the `ALTER TABLE ... ADD INDEX ..., ALGORITHM=INPLACE, LOCK=NONE` form.

## Knex

- `table.index(['a', 'b'], 'name')`, `.index()` and `.unique()` on a column, `table.unique([...])`.
- `table.integer('user_id').references('id').inTable('users')` or `table.foreign('user_id').references('users.id')` adds a constraint only: add `.index()` as well on PostgreSQL.
- Query sites: `knex('orders').where('user_id', 1).orderBy('created_at')`, `.whereIn`, `.first()`.

Online creation for PostgreSQL: turn the transaction off for that migration file and use raw SQL.

```js
exports.config = { transaction: false };

exports.up = (knex) =>
  knex.raw('CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_user_id_idx ON orders (user_id)');

exports.down = (knex) => knex.raw('DROP INDEX CONCURRENTLY IF EXISTS orders_user_id_idx');
```

MySQL: `knex.raw('ALTER TABLE orders ADD INDEX orders_user_id_idx (user_id), ALGORITHM=INPLACE, LOCK=NONE')`.

## Redundancy

`@@index([slug])` next to `@@index([slug, title])`, or `.index()` on a column that also leads a composite `table.index([...])`, is a prefix duplicate. `@unique` / `.unique()` indexes are not dropped for that reason.
