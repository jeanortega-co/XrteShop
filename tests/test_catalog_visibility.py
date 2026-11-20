import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PEMU_DATABASE"] = str(Path(_TEST_DIRECTORY.name) / "cj-catalog-test.sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["PEMU_DATABASE_URL"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ.pop("VERCEL", None)

import app as application


class CatalogVisibilityTests(unittest.TestCase):
    def setUp(self):
        database_path = Path(_TEST_DIRECTORY.name) / f"{self._testMethodName}.sqlite3"
        application.app.config["DATABASE"] = str(database_path)
        application.initialize_database()
        with application.closing(application.connect_db()) as database:
            database.execute("DELETE FROM seller_accounts")
            database.execute("DELETE FROM users")
            cursor = database.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Seller Test", "seller@example.test", "not-used"),
            )
            self.seller_id = cursor.lastrowid
            database.execute(
                """INSERT INTO seller_accounts
                   (seller_id, provider, provider_account_id, verification_status)
                   VALUES (?, 'payu', 'beneficiary-test', 'verified')""",
                (self.seller_id,),
            )
            database.commit()

        self.client = application.app.test_client()
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = self.seller_id

    def test_public_catalog_excludes_owner_and_remote_reference_products(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge, active, seller_id, origin)
                   VALUES ('owner-test', 'Owner product', 'Hogar', 'Owner item',
                           'owner.jpg', 1000, 1000, 1, '', 1, NULL, 'propio')"""
            )
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge, active, seller_id, origin)
                   VALUES ('seller-test', 'Seller product', 'Hogar', 'Seller item',
                           'seller.jpg', 2000, 2000, 1, '', 1, ?, 'emprendedor')""",
                (self.seller_id,),
            )
            database.commit()

        response = self.client.get("/api/productos")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual([product["id"] for product in payload["products"]], ["seller-test"])
        self.assertEqual(payload["total"], 1)
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('id="featured-products-grid"', home.get_data(as_text=True))
        self.assertIn('id="catalogo"', home.get_data(as_text=True))
        legacy_catalog = self.client.get("/api/products")
        self.assertEqual(
            [product["id"] for product in legacy_catalog.get_json()["products"]],
            ["seller-test"],
        )
        recommendations = self.client.get("/api/products/recommendations")
        self.assertEqual(
            [product["id"] for product in recommendations.get_json()["products"]],
            ["seller-test"],
        )
        self.assertEqual(
            self.client.get("/api/products/owner-test").status_code,
            404,
        )

    def test_existing_sample_products_are_archived_without_reseeding(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge, active, origin)
                   VALUES ('airpods', 'Sample product', 'Electrónica', '',
                           'sample.jpg', 1000, 1000, 1, '', 1, 'propio')"""
            )
            database.commit()

        application.initialize_database()

        with application.closing(application.connect_db()) as database:
            archived = database.execute(
                "SELECT active, stock, archived_at FROM products WHERE id = 'airpods'"
            ).fetchone()
            self.assertEqual(archived["active"], 0)
            self.assertEqual(archived["stock"], 0)
            self.assertIsNotNone(archived["archived_at"])
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM products WHERE id = 'reloj-hombre'"
                ).fetchone()[0],
                0,
            )



    def test_legal_policies_remain_linkable_with_sandbox_notice(self):
        response = self.client.get("/politicas")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Modo pruebas", page)
        self.assertIn('id="terminos"', page)
        self.assertIn("Ley 1581 de 2012", page)
        self.assertIn("Ley 1480 de 2011", page)
        self.assertIn('id="envios"', page)
        self.assertIn('id="pagos"', page)
        self.assertIn("[COMPLETAR: NIT o cédula]", page)
        self.assertIn("revisión de un abogado", page)



if __name__ == "__main__":
    unittest.main()
