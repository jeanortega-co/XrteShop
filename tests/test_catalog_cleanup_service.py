import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from catalog_cleanup_service import (
    apply_catalog_cleanup,
    backup_before_cleanup,
    catalog_cleanup_preview,
    connect_readonly,
)


class CatalogCleanupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "catalog.sqlite3"
        self.image_root = self.root / "static" / "images"
        self.image_root.mkdir(parents=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                image TEXT,
                origin TEXT NOT NULL,
                seller_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT
            );
            CREATE TABLE suppliers (id TEXT PRIMARY KEY);
            CREATE TABLE catalog_cleanup_exclusions (
                product_id TEXT PRIMARY KEY,
                excluded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE supplier_products (
                product_id TEXT REFERENCES products(id)
            );
            CREATE TABLE supplier_orders (
                product_id TEXT REFERENCES products(id)
            );
            CREATE TABLE orders (id INTEGER PRIMARY KEY);
            CREATE TABLE order_items (
                product_id TEXT NOT NULL REFERENCES products(id)
            );
            CREATE TABLE product_interactions (
                product_id TEXT NOT NULL REFERENCES products(id)
            );
            INSERT INTO products (id, name, origin) VALUES
                ('unused', 'Sin ventas', 'propio'),
                ('sold', 'Con pedido', 'propio'),
                ('supplier-linked', 'Vinculado', 'propio'),
                ('provider-empty', 'Proveedor sin referencias', 'proveedor'),
                ('provider-order', 'Proveedor con pedido', 'proveedor'),
                ('seller-item', 'Emprendedor', 'emprendedor'),
                ('seller-origin', 'Vendedor con origen legado', 'propio');
            UPDATE products SET seller_id = 10 WHERE id = 'seller-origin';
            INSERT INTO order_items (product_id) VALUES ('sold');
            INSERT INTO supplier_products (product_id) VALUES ('supplier-linked');
            INSERT INTO supplier_orders (product_id) VALUES ('provider-order');
            INSERT INTO product_interactions (product_id) VALUES ('unused');
            """
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_preview_preserves_supplier_products_and_targets_legacy_catalog(self):
        with closing(connect_readonly(self.database_path)) as database:
            preview = catalog_cleanup_preview(database)

        self.assertEqual(preview["will_delete"], 1)
        self.assertEqual(preview["will_archive"], 2)
        self.assertEqual(preview["entrepreneur_products_untouched"], 2)
        self.assertEqual(preview["provider_products_preserved"], 2)
        self.assertEqual(preview["deleted_seed_products_excluded_from_reseeding"], [])
        self.assertEqual(
            {item["id"]: item["action"] for item in preview["targets"]},
            {
                "unused": "delete",
                "sold": "archive",
                "supplier-linked": "archive",
            },
        )

    def test_cleanup_preserves_supplier_products_history_and_sellers(self):
        result = apply_catalog_cleanup(
            self.database_path,
            ["sold", "supplier-linked", "unused"],
        )
        self.assertEqual(result["deleted_ids"], ["unused"])
        self.assertEqual(result["archived_ids"], ["sold", "supplier-linked"])
        remaining = self.connection.execute(
            "SELECT id, active FROM products ORDER BY id"
        ).fetchall()
        self.assertEqual(
            remaining,
            [
                ("provider-empty", 1),
                ("provider-order", 1),
                ("seller-item", 1),
                ("seller-origin", 1),
                ("sold", 0),
                ("supplier-linked", 0),
            ],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM product_interactions WHERE product_id = 'unused'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT product_id FROM catalog_cleanup_exclusions"
            ).fetchall(),
            [("unused",)],
        )
        with closing(connect_readonly(self.database_path)) as database:
            preview = catalog_cleanup_preview(database)
        self.assertEqual(preview["targets"], [])
        self.assertEqual(preview["entrepreneur_products_untouched"], 2)

    def test_cleanup_rejects_stale_preview_without_changes(self):
        with self.assertRaises(RuntimeError):
            apply_catalog_cleanup(self.database_path, ["old-preview"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            7,
        )

    def test_backup_copies_database_and_local_images(self):
        image_path = self.image_root / "catalog.png"
        image_path.write_bytes(b"image")
        self.connection.execute(
            "UPDATE products SET image = 'catalog.png' WHERE id = 'unused'"
        )
        self.connection.commit()

        backup_directory, manifest = backup_before_cleanup(
            self.database_path,
            self.root,
            self.image_root,
        )

        self.assertTrue((backup_directory / "database.sqlite3").is_file())
        self.assertEqual(
            (backup_directory / "images" / "catalog.png").read_bytes(),
            b"image",
        )
        self.assertIn("catalog.png", manifest["copied_local_product_images"])


if __name__ == "__main__":
    unittest.main()
