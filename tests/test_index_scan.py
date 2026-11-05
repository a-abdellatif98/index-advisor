import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "index_scan.py"
spec = importlib.util.spec_from_file_location("index_scan", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def project(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def run(tmp_path, files, engine="auto"):
    return scanner.scan(project(tmp_path, files), engine=engine)


def rules(findings, rule=None):
    return sorted((f.rule, f.table, f.column) for f in findings if rule in (None, f.rule))


RAILS_SCHEMA = """
ActiveRecord::Schema[7.0].define(version: 2024_01_01) do
  create_table "users", force: :cascade do |t|
    t.string "email"
    t.string "status"
    t.index ["email"], name: "index_users_on_email", unique: true
  end

  create_table "orders", force: :cascade do |t|
    t.bigint "user_id", null: false
    t.bigint "store_id"
    t.string "state"
    t.datetime "created_at"
    t.index ["state"], name: "index_orders_on_state"
    t.index ["state", "created_at"], name: "index_orders_on_state_and_created_at"
  end

  add_foreign_key "orders", "users"
end
"""


def test_rails_schema_fk_without_index(tmp_path):
    found = run(tmp_path, {"db/schema.rb": RAILS_SCHEMA})
    assert ("IDX-FK", "orders", "user_id") in rules(found)
    fk = [f for f in found if f.column == "user_id"][0]
    assert "InnoDB" in fk.message and fk.path == "db/schema.rb" and fk.line == 10


def test_rails_reference_like_column_without_constraint(tmp_path):
    found = run(tmp_path, {"db/schema.rb": RAILS_SCHEMA}, engine="mysql")
    assert ("IDX-FK", "orders", "store_id") in rules(found)
    assert ("IDX-FK", "orders", "user_id") not in rules(found)


def test_postgres_engine_flags_constraint_fk(tmp_path):
    found = run(tmp_path, {"db/schema.rb": RAILS_SCHEMA}, engine="postgres")
    assert ("IDX-FK", "orders", "user_id") in rules(found)


def test_rails_redundant_prefix_index(tmp_path):
    found = run(tmp_path, {"db/schema.rb": RAILS_SCHEMA})
    red = [f for f in found if f.rule == "IDX-REDUNDANT"]
    assert [(f.table, f.line) for f in red] == [("orders", 14)]
    assert "index_orders_on_state_and_created_at" in red[0].message


def test_unique_index_is_not_redundant(tmp_path):
    schema = """
create_table "a" do |t|
  t.string "x"
  t.string "y"
  t.index ["x"], name: "ux", unique: true
  t.index ["x", "y"], name: "xy"
end
"""
    assert not rules(run(tmp_path, {"db/schema.rb": schema}), "IDX-REDUNDANT")


def test_duplicate_index_reports_the_later_one(tmp_path):
    schema = """
create_table "a" do |t|
  t.string "x"
  t.index ["x"], name: "one"
  t.index ["x"], name: "two"
end
"""
    red = [f for f in run(tmp_path, {"db/schema.rb": schema}) if f.rule == "IDX-REDUNDANT"]
    assert [f.line for f in red] == [5]


def test_partial_and_expression_indexes_are_ignored(tmp_path):
    schema = """
create_table "a" do |t|
  t.string "x"
  t.index ["x"], name: "p", where: "(x IS NOT NULL)"
  t.index "lower((x)::text)", name: "e"
  t.index ["x", "y"], name: "xy"
end
"""
    assert not rules(run(tmp_path, {"db/schema.rb": schema}), "IDX-REDUNDANT")


def test_composite_primary_key_leading_column_covers(tmp_path):
    schema = """
create_table "m", primary_key: ["a_id", "b_id"] do |t|
  t.bigint "a_id", null: false
  t.bigint "b_id", null: false
end
"""
    found = rules(run(tmp_path, {"db/schema.rb": schema}), "IDX-FK")
    assert found == [("IDX-FK", "m", "b_id")]


def test_rails_migrations_when_no_schema(tmp_path):
    mig = """
class AddThings < ActiveRecord::Migration[7.0]
  def change
    create_table :posts do |t|
      t.references :author, foreign_key: true
      t.references :editor, index: false
      t.belongs_to :blog
      t.string :slug, index: { unique: true }
    end
    add_reference :comments, :post, foreign_key: true, index: false
    add_index :posts, :slug, name: "dup_slug"
    add_foreign_key :comments, :users
  end

  def down
    drop_table :posts
  end
end
"""
    found = run(tmp_path, {"db/migrate/20240101000000_add_things.rb": mig})
    assert rules(found, "IDX-FK") == [
        ("IDX-FK", "comments", "post_id"),
        ("IDX-FK", "comments", "user_id"),
        ("IDX-FK", "posts", "editor_id"),
    ]
    assert rules(found, "IDX-REDUNDANT") == [("IDX-REDUNDANT", "posts", "slug")]


def test_rails_polymorphic_reference_creates_type_id_index(tmp_path):
    mig = "def change\n  create_table :taggings do |t|\n    t.references :taggable, polymorphic: true\n  end\nend\n"
    assert not rules(run(tmp_path, {"db/migrate/1_x.rb": mig}), "IDX-FK")


def test_rails_remove_index_in_migration(tmp_path):
    mig = (
        "def change\n  create_table :posts do |t|\n    t.bigint :author_id\n  end\n"
        "  add_index :posts, :author_id\n  remove_index :posts, :author_id\nend\n"
    )
    assert rules(run(tmp_path, {"db/migrate/1_x.rb": mig}), "IDX-FK") == [("IDX-FK", "posts", "author_id")]


def test_rails_query_columns_without_index(tmp_path):
    files = {
        "db/schema.rb": RAILS_SCHEMA,
        "app/models/user.rb": "class User < ApplicationRecord\n  scope :active, -> { where(status: 'active') }\nend\n",
        "app/services/report.rb": (
            "Order.where(state: 'x').order(created_at: :desc)\n"
            "Order.where(store_id: 1)\n"
            "User.find_by(email: e)\n"
            "User.find_by_status_and_email(s, e)\n"
            "Order.order('created_at DESC, state')\n"
            "Order.find_by(id: 3)\n"
        ),
    }
    found = run(tmp_path, files, engine="mysql")
    got = rules(found, "IDX-QUERY")
    assert ("IDX-QUERY", "users", "status") in got
    assert ("IDX-QUERY", "orders", "created_at") in got
    assert ("IDX-QUERY", "users", "email") not in got
    assert ("IDX-QUERY", "orders", "state") not in got
    assert ("IDX-QUERY", "orders", "id") not in got
    assert ("IDX-QUERY", "orders", "store_id") not in got


def test_query_in_spec_and_unknown_model_ignored(tmp_path):
    files = {
        "db/schema.rb": RAILS_SCHEMA,
        "spec/a_spec.rb": "User.where(status: 'x')\n",
        "app/a.rb": "Ghost.where(status: 'x')\nwhere(status: 'x')\n",
    }
    assert not rules(run(tmp_path, files), "IDX-QUERY")


def test_suppress_marker(tmp_path):
    schema = RAILS_SCHEMA.replace('t.bigint "user_id", null: false', 't.bigint "user_id", null: false # index-advisor: ok')
    assert ("IDX-FK", "orders", "user_id") not in rules(run(tmp_path, {"db/schema.rb": schema}))


DJANGO_MODELS = """
from django.db import models

class Author(models.Model):
    name = models.TextField()
    email = models.EmailField(unique=True)

class Book(models.Model):
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    editor = models.ForeignKey(
        Author,
        on_delete=models.SET_NULL,
        db_index=False,
        null=True,
    )
    isbn = models.CharField(max_length=20)
    title = models.CharField(max_length=20, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["author"], name="b_author"),
            models.Index(fields=["isbn", "title"], name="b_isbn_title"),
        ]
        unique_together = (("isbn", "title"),)
"""


def test_django_models(tmp_path):
    files = {
        "library/models.py": DJANGO_MODELS,
        "library/views.py": (
            "Book.objects.filter(isbn=x).order_by('-title')\n"
            "Book.objects.filter(\n    editor=e,\n    title__icontains='a',\n)\n"
            "Author.objects.get(name=n)\n"
            "Book.objects.filter(author__name=n)\n"
        ),
    }
    found = run(tmp_path, files, engine="postgres")
    assert rules(found, "IDX-FK") == [("IDX-FK", "book", "editor_id")]
    red = [(f.table, f.message) for f in found if f.rule == "IDX-REDUNDANT"]
    assert len(red) == 2 and all(t == "book" for t, _ in red)
    assert rules(found, "IDX-QUERY") == [("IDX-QUERY", "author", "name")]


def test_django_migration_add_index_and_field(tmp_path):
    files = {
        "app/models.py": "class Thing(models.Model):\n    owner = models.ForeignKey(Owner, on_delete=models.CASCADE)\n    code = models.CharField(max_length=2)\n",
        "app/migrations/0002_x.py": (
            "operations = [\n"
            "    migrations.AddIndex(\n        model_name='thing',\n"
            "        index=models.Index(fields=['code'], name='thing_code'),\n    ),\n"
            "    migrations.AddIndex(\n        model_name='thing',\n"
            "        index=models.Index(fields=['owner'], name='thing_owner'),\n    ),\n]\n"
        ),
    }
    found = run(tmp_path, files)
    assert [(f.rule, f.column) for f in found if f.rule == "IDX-REDUNDANT"] == [("IDX-REDUNDANT", "owner_id")]


LARAVEL_MIGRATION = """<?php
return new class extends Migration {
    public function up(): void
    {
        Schema::create('invoices', function (Blueprint $table) {
            $table->id();
            $table->foreignId('customer_id')->constrained();
            $table->foreignId('team_id')
                ->constrained()
                ->cascadeOnDelete();
            $table->unsignedBigInteger('plan_id')->index();
            $table->unsignedBigInteger('region_id')->references('id')->on('regions');
            $table->string('number')->unique();
            $table->string('status');
            $table->morphs('billable');
            $table->index(['status', 'number']);
            $table->index('number');
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('invoices');
    }
};
"""


def test_laravel_migration(tmp_path):
    files = {
        "database/migrations/2024_01_01_create_invoices.php": LARAVEL_MIGRATION,
        "app/Http/InvoiceController.php": (
            "Invoice::where('status', 'open')\n    ->orderBy('number')\n    ->get();\n"
            "DB::table('invoices')->where('team_id', 1)->first();\n"
            "Invoice::latest()->get();\n"
        ),
    }
    found = run(tmp_path, files, engine="postgres")
    assert rules(found, "IDX-FK") == [
        ("IDX-FK", "invoices", "customer_id"),
        ("IDX-FK", "invoices", "region_id"),
        ("IDX-FK", "invoices", "team_id"),
    ]
    assert [(f.column) for f in found if f.rule == "IDX-REDUNDANT"] == ["number"]
    assert not rules(found, "IDX-QUERY")


PRISMA = """
datasource db {
  provider = "postgresql"
  url      = "should-never-be-parsed"
}

model User {
  id    Int    @id @default(autoincrement())
  email String @unique
  posts Post[]
}

model Post {
  id       Int    @id
  authorId Int
  author   User   @relation(fields: [authorId], references: [id])
  slug     String
  title    String

  @@index([slug, title])
  @@index([slug])
}
"""


def test_prisma_models_only(tmp_path):
    files = {
        "prisma/schema.prisma": PRISMA,
        "src/posts.ts": (
            "await prisma.post.findMany({\n  where: { title: 'x', author: { is: {} } },\n"
            "  orderBy: { authorId: 'asc' },\n});\n"
            "await prisma.user.findUnique({ where: { email } });\n"
        ),
    }
    found = run(tmp_path, files, engine="postgres")
    assert rules(found, "IDX-FK") == [("IDX-FK", "Post", "authorId")]
    assert [f.column for f in found if f.rule == "IDX-REDUNDANT"] == ["slug"]
    assert rules(found, "IDX-QUERY") == [("IDX-QUERY", "Post", "title")]
    assert not any("should-never" in f.message for f in found)


KNEX = """
exports.up = function (knex) {
  return knex.schema.createTable('tickets', (table) => {
    table.increments('id');
    table.integer('project_id').unsigned().references('id').inTable('projects');
    table.integer('owner_id').unsigned();
    table.integer('reporter_id').index();
    table.string('code').index();
    table.foreign('owner_id').references('users.id');
    table.index(['code', 'owner_id']);
  });
};

exports.down = function (knex) {
  return knex.schema.dropTable('tickets');
};
"""


def test_knex_migration(tmp_path):
    files = {
        "migrations/20240101_tickets.js": KNEX,
        "src/q.js": "const rows = await knex('tickets')\n  .where('owner_id', 1)\n  .orderBy('created', 'desc');\n",
    }
    found = run(tmp_path, files, engine="postgres")
    assert rules(found, "IDX-FK") == [("IDX-FK", "tickets", "owner_id"), ("IDX-FK", "tickets", "project_id")]
    assert rules(found, "IDX-REDUNDANT") == [("IDX-REDUNDANT", "tickets", "code")]
    assert not rules(found, "IDX-QUERY")


def test_secret_files_are_never_read(tmp_path, monkeypatch):
    files = {
        "db/schema.rb": RAILS_SCHEMA,
        ".env": "SECRET=1",
        "config/database.yml": "x: 1",
        "config/settings.py": "DATABASES = {}",
        "app/settings.py": "DATABASES = {}",
        "secrets.rb": "x",
    }
    project(tmp_path, files)
    read = []
    original = scanner.read_source
    monkeypatch.setattr(scanner, "read_source", lambda p: read.append(p.name) or original(p))
    scanner.scan(tmp_path)
    assert read == ["schema.rb"]


def test_skips_vendor_and_node_modules(tmp_path):
    files = {"vendor/db/schema.rb": RAILS_SCHEMA, "node_modules/x/schema.rb": RAILS_SCHEMA}
    assert run(tmp_path, files) == []


def test_clean_project_has_no_findings(tmp_path):
    schema = 'create_table "a" do |t|\n  t.string "x"\n  t.index ["x"], name: "i"\nend\n'
    assert run(tmp_path, {"db/schema.rb": schema}) == []


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_cli_exit_codes_and_json(tmp_path):
    project(tmp_path, {"db/schema.rb": RAILS_SCHEMA})
    out = cli("--json", "--engine", "postgres", str(tmp_path))
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert {"rule", "path", "line", "table", "column", "message"} <= set(data[0])
    text = cli(str(tmp_path))
    assert "db/schema.rb:10: [IDX-FK]" in text.stdout
    assert cli(str(tmp_path / "missing")).returncode == 2
    clean = tmp_path / "clean"
    project(clean, {"db/schema.rb": 'create_table "a" do |t|\n  t.string "x"\nend\n'})
    assert cli(str(clean)).returncode == 0


@pytest.mark.parametrize("table,model", [("users", "User"), ("categories", "Category"), ("boxes", "Box"), ("cases", "Case"), ("status", "Status"), ("line_items", "LineItem")])
def test_model_names_resolve_to_tables(table, model):
    schema = scanner.Schema()
    schema.table(table)
    assert schema.find(model).name == table
    assert schema.find("Nothing") is None


def test_descending_index_is_not_judged_redundant(tmp_path):
    models = (
        "class T(models.Model):\n    a = models.CharField(max_length=1)\n    b = models.CharField(max_length=1)\n"
        "    class Meta:\n        indexes = [models.Index(fields=['a']), models.Index(fields=['a', '-b'])]\n"
    )
    assert not rules(run(tmp_path, {"app/models.py": models}), "IDX-REDUNDANT")


def test_unreadable_paths_do_not_crash(tmp_path):
    project(tmp_path, {"db/schema.rb": RAILS_SCHEMA})
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "broken.rb").symlink_to(tmp_path / "nowhere.rb")
    (tmp_path / "app" / "dir.rb").mkdir()
    assert ("IDX-FK", "orders", "user_id") in rules(scanner.scan(tmp_path))


def test_django_model_with_custom_abstract_base(tmp_path):
    models = (
        "class Base(models.Model):\n    owner = models.ForeignKey(Owner, db_index=False, on_delete=models.CASCADE)\n"
        "    class Meta:\n        abstract = True\n\n"
        "class Book(Base):\n    title = models.CharField(max_length=1)\n"
    )
    assert rules(run(tmp_path, {"shop/models.py": models}), "IDX-FK") == [("IDX-FK", "book", "owner_id")]


def test_django_same_class_name_in_two_apps(tmp_path):
    fk = "class Profile(models.Model):\n    user = models.ForeignKey(User, on_delete=models.CASCADE, db_index=%s)\n"
    files = {"a/models.py": fk % "True", "b/models.py": fk % "False"}
    found = run(tmp_path, files)
    assert [(f.rule, f.path) for f in found] == [("IDX-FK", "b/models.py")]


def test_non_btree_and_prefix_indexes_are_not_redundant(tmp_path):
    models = (
        "class T(models.Model):\n    tags = models.TextField()\n"
        "    class Meta:\n        indexes = [GinIndex(fields=['tags'], name='g'), models.Index(fields=['tags'], name='p')]\n"
    )
    schema = (
        'create_table "a" do |t|\n  t.text "body"\n  t.index ["body"], name: "ft", type: :fulltext\n'
        '  t.index ["body"], name: "plain"\n  t.index ["body"], name: "pre", length: 10\n  t.index ["body", "x"], name: "w"\nend\n'
    )
    assert not rules(run(tmp_path, {"app/models.py": models}), "IDX-REDUNDANT")
    assert [f.message for f in run(tmp_path / "r", {"db/schema.rb": schema}) if f.rule == "IDX-REDUNDANT"][0].startswith("index plain")


def test_prisma_multiline_index_attribute(tmp_path):
    prisma = "model Post {\n  id Int @id\n  title String\n  slug String\n  @@index([\n    title,\n    slug\n  ])\n}\n"
    files = {"prisma/schema.prisma": prisma, "src/a.ts": "prisma.post.findMany({ where: { title: 'x' } })\n"}
    assert not rules(run(tmp_path, files), "IDX-QUERY")


def test_django_abstract_base_in_another_file(tmp_path):
    files = {
        "shop/base.py": "class Stamped(models.Model):\n    owner = models.ForeignKey(Owner, db_index=False, on_delete=models.CASCADE)\n    class Meta:\n        abstract = True\n",
        "shop/models.py": "from .base import Stamped\n\nclass Thing(Stamped):\n    name = models.CharField(max_length=1)\n\nclass ThingForm(ModelForm):\n    x = forms.CharField()\n",
    }
    found = run(tmp_path, files)
    assert [(f.rule, f.table, f.path) for f in found] == [("IDX-FK", "thing", "shop/base.py")]


def test_django_nested_models_package_shares_scope_with_migrations(tmp_path):
    files = {
        "shop/models/item.py": "class Item(models.Model):\n    owner = models.IntegerField()\n    class Meta:\n        indexes = [models.Index(fields=['owner', 'id'], name='o_id')]\n",
        "shop/migrations/0002_x.py": "operations = [\n    migrations.AddIndex(\n        model_name='item',\n        index=models.Index(fields=['owner'], name='o_u'),\n    ),\n]\n",
    }
    found = run(tmp_path, files)
    assert [(f.rule, f.table) for f in found] == [("IDX-REDUNDANT", "item")]


def test_django_same_base_name_in_two_apps(tmp_path):
    base = "class Base(models.Model):\n    owner = models.ForeignKey(Owner, db_index=%s, on_delete=models.CASCADE)\n    class Meta:\n        abstract = True\n"
    files = {
        "a/models/base.py": base % "False",
        "a/models/item.py": "class Item(Base):\n    n = models.IntegerField()\n",
        "z/models.py": "class Base(models.Model):\n    n = models.IntegerField()\n",
    }
    found = run(tmp_path, files)
    assert [(f.rule, f.table, f.path) for f in found] == [("IDX-FK", "item", "a/models/base.py")]
