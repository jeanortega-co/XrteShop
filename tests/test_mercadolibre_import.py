import os
import base64
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PEMU_DATABASE"] = str(Path(_TEST_DIRECTORY.name) / "mercadolibre-import-test.sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["PEMU_DATABASE_URL"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ.pop("VERCEL", None)

import app as application
from mercadolibre_service import (
    MAX_RESPONSE_BYTES,
    MercadoLibreImportError,
    preview_mercadolibre_product,
    sanitize_mercadolibre_url,
)
from mercadolibre_catalog_service import (
    publish_mercadolibre_product,
    sync_mercadolibre_product,
)


def mercadolibre_api_response(payload):
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.encoding = "utf-8"
    response.iter_content.return_value = [json.dumps(payload).encode("utf-8")]
    return response


def mercadolibre_oauth_response(payload):
    response = Mock()
    response.ok = True
    response.json.return_value = payload
    return response


def publishable_mercadolibre_product(product_id, stock=4):
    return {
        "supplier_platform": "mercadolibre",
        "supplier_product_id": product_id,
        "supplier_url": f"https://articulo.mercadolibre.com.co/{product_id}",
        "supplier_original_price": "10000",
        "supplier_currency": "COP",
        "supplier_stock_reported": stock,
        "name": f"Producto {product_id}",
        "description": "Descripción del producto",
        "image_url": "https://cdn.example.test/product.jpg",
        "image_urls": ["https://cdn.example.test/product.jpg"],
        "variants": [],
        "status": "active",
    }


class MercadoLibreServiceTests(unittest.TestCase):
    def test_sanitizes_colombia_links_and_rejects_unsafe_urls(self):
        self.assertEqual(
            sanitize_mercadolibre_url(
                "https://articulo.mercadolibre.com.co/MCO-1234567890-lampara.html#details"
            ),
            "https://articulo.mercadolibre.com.co/MCO-1234567890-lampara.html",
        )
        self.assertEqual(
            sanitize_mercadolibre_url("mco-1234567890"),
            "https://articulo.mercadolibre.com.co/MCO1234567890",
        )
        for unsafe_url in (
            "http://articulo.mercadolibre.com.co/MCO-1234567890-lampara.html",
            "https://mercadolibre.com.co.example.test/MCO-1234567890",
            "https://user:pass@mercadolibre.com.co/MCO-1234567890",
            "https://articulo.mercadolibre.com.co:8443/MCO-1234567890",
            "https://mercadolibre.com.mx/MCO-1234567890",
        ):
            with self.subTest(unsafe_url=unsafe_url), self.assertRaises(ValueError):
                sanitize_mercadolibre_url(unsafe_url)

    def test_fetches_item_and_description_only_from_official_api(self):
        item = {
            "id": "MCO1234567890",
            "title": "Lámpara de escritorio",
            "price": 60000,
            "currency_id": "COP",
            "available_quantity": 5,
            "status": "active",
            "category_id": "MCO1234",
            "seller_custom_field": "LAMP-001",
            "permalink": "https://articulo.mercadolibre.com.co/MCO-1234567890",
            "pictures": [
                {"secure_url": "https://http2.mlstatic.com/lamp-front.jpg"},
                {"secure_url": "https://http2.mlstatic.com/lamp-side.jpg"},
                {"url": "http://insecure.example/lamp.jpg"},
            ],
            "variations": [
                {
                    "id": 101,
                    "seller_custom_field": "LAMP-BLACK",
                    "attribute_combinations": [
                        {"name": "Color", "value_name": "Negro"}
                    ],
                    "price": 60000,
                    "available_quantity": 2,
                },
                {
                    "id": 102,
                    "seller_custom_field": "LAMP-WHITE",
                    "attribute_combinations": [
                        {"name": "Color", "value_name": "Blanco"}
                    ],
                    "price": 65000,
                    "available_quantity": 3,
                },
            ],
        }
        item_response = mercadolibre_api_response(item)
        description_response = mercadolibre_api_response(
            {"plain_text": "Lámpara LED para escritorio."}
        )
        original_url = (
            "https://articulo.mercadolibre.com.co/"
            "MCO-1234567890-lampara.html#details"
        )
        with patch(
            "mercadolibre_service.requests.get",
            side_effect=[item_response, description_response],
        ) as get:
            preview = preview_mercadolibre_product(original_url)

        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "https://api.mercadolibre.com/items/MCO1234567890",
                "https://api.mercadolibre.com/items/MCO1234567890/description",
            ],
        )
        self.assertTrue(all(call.kwargs["stream"] for call in get.call_args_list))
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in get.call_args_list))
        self.assertEqual(preview["supplier_platform"], "mercadolibre")
        self.assertEqual(preview["supplier_product_id"], "MCO1234567890")
        self.assertEqual(
            preview["supplier_url"],
            "https://articulo.mercadolibre.com.co/MCO-1234567890",
        )
        self.assertEqual(preview["supplier_original_price"], "60000")
        self.assertEqual(preview["supplier_currency"], "COP")
        self.assertEqual(preview["supplier_stock_reported"], 5)
        self.assertEqual(preview["description"], "Lámpara LED para escritorio.")
        self.assertEqual(
            preview["image_urls"],
            [
                "https://http2.mlstatic.com/lamp-front.jpg",
                "https://http2.mlstatic.com/lamp-side.jpg",
            ],
        )
        self.assertEqual(
            [variant["supplier_stock_reported"] for variant in preview["variants"]],
            [2, 3],
        )
        self.assertTrue(preview["can_publish"])
        self.assertEqual(preview["blockers"], [])

    def test_preview_accepts_item_id_without_fetching_description(self):
        item_response = mercadolibre_api_response(
            {
                "id": "MCO1234567890",
                "title": "Producto",
                "price": 10000,
                "currency_id": "COP",
                "available_quantity": 2,
                "status": "active",
                "permalink": "https://articulo.mercadolibre.com.co/MCO-1234567890",
                "pictures": [{"secure_url": "https://http2.mlstatic.com/item.jpg"}],
            }
        )
        with patch(
            "mercadolibre_service.requests.get",
            return_value=item_response,
        ) as get:
            preview = preview_mercadolibre_product(
                "MCO1234567890",
                include_description=False,
            )

        get.assert_called_once()
        self.assertEqual(
            get.call_args.args[0],
            "https://api.mercadolibre.com/items/MCO1234567890",
        )
        self.assertEqual(preview["supplier_product_id"], "MCO1234567890")
        self.assertEqual(preview["supplier_stock_reported"], 2)
        self.assertEqual(preview["description"], "")
        self.assertEqual(
            preview["supplier_url"],
            "https://articulo.mercadolibre.com.co/MCO-1234567890",
        )

    def test_missing_api_data_blocks_publication_without_inventing_a_variant(self):
        item_response = mercadolibre_api_response(
            {
                "id": "MCO1234567890",
                "status": "active",
                "currency_id": "COP",
                "pictures": [],
                "variations": [],
            }
        )
        description_response = mercadolibre_api_response({"plain_text": ""})
        with patch(
            "mercadolibre_service.requests.get",
            side_effect=[item_response, description_response],
        ):
            preview = preview_mercadolibre_product(
                "https://www.mercadolibre.com.co/MCO-1234567890"
            )

        self.assertFalse(preview["can_publish"])
        self.assertEqual(
            set(preview["missing_fields"]),
            {"name", "price", "images", "stock"},
        )
        self.assertEqual(preview["variants"], [])

    def test_incomplete_variation_data_is_marked_for_manual_completion(self):
        item_response = mercadolibre_api_response(
            {
                "id": "MCO1234567890",
                "title": "Producto",
                "price": 60000,
                "currency_id": "COP",
                "available_quantity": 4,
                "status": "active",
                "pictures": [{"secure_url": "https://http2.mlstatic.com/item.jpg"}],
                "variations": [
                    {
                        "id": 101,
                        "attribute_combinations": [
                            {"name": "Color", "value_name": "Azul"}
                        ],
                    }
                ],
            }
        )
        description_response = mercadolibre_api_response({"plain_text": ""})
        with patch(
            "mercadolibre_service.requests.get",
            side_effect=[item_response, description_response],
        ):
            preview = preview_mercadolibre_product(
                "https://www.mercadolibre.com.co/MCO-1234567890"
            )

        self.assertFalse(preview["can_publish"])
        self.assertEqual(
            set(preview["missing_fields"]),
            {"variant_price", "variant_stock"},
        )
        self.assertIsNone(preview["variants"][0]["supplier_original_price"])
        self.assertIsNone(preview["variants"][0]["supplier_stock_reported"])

    def test_rejects_oversized_api_responses(self):
        response = mercadolibre_api_response({})
        response.iter_content.return_value = [b"x" * (MAX_RESPONSE_BYTES + 1)]
        with (
            patch("mercadolibre_service.requests.get", return_value=response),
            self.assertRaisesRegex(MercadoLibreImportError, "supera el límite"),
        ):
            preview_mercadolibre_product(
                "https://www.mercadolibre.com.co/MCO-1234567890"
            )
        response.close.assert_called_once()


class MercadoLibreRouteTests(unittest.TestCase):
    def setUp(self):
        database_path = Path(_TEST_DIRECTORY.name) / f"{self._testMethodName}.sqlite3"
        application.app.config["DATABASE"] = str(database_path)
        application.initialize_database()
        with application.closing(application.connect_db()) as database:
            cursor = database.execute(
                """INSERT INTO users (name, email, password_hash, role)
                   VALUES ('Admin test', 'admin@example.test', 'not-used', 'admin')"""
            )
            self.admin_id = cursor.lastrowid
            database.commit()
        self.client = application.app.test_client()
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = self.admin_id
            user_session["csrf_token"] = "test-csrf-token"

    def test_public_catalog_response_disables_browser_caching(self):
        response = self.client.get("/api/productos")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_homepage_disables_caching_of_catalog_script_version(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("js/script.js?v=19", response.get_data(as_text=True))


    def test_admin_import_page_renders_mercadolibre_form_and_csrf_token(self):

        with patch.object(application, "ADMIN_EMAIL", "admin@example.test"):

            response = self.client.get("/admin/ml-import")
            dashboard = self.client.get("/admin")


        self.assertEqual(response.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        dashboard_html = dashboard.get_data(as_text=True)
        self.assertIn('href="/admin/ml-import"', dashboard_html)
        self.assertIn("Importar desde Mercado Libre", dashboard_html)
        self.assertNotIn("Importar desde CJ", dashboard_html)
        page = response.get_data(as_text=True)
        self.assertIn("Mercado Libre Colombia", page)
        self.assertIn('id="supplier-link-form"', page)
        self.assertIn('id="supplier-publish-button"', page)
        self.assertIn("Enlace o ID MCO", page)
        self.assertIn("Integrar a XrteShop", page)
        self.assertIn('type="text"', page)
        self.assertIn("envío a Colombia", page)

        self.assertIn('id="mercadolibre-account-status"', page)
        self.assertIn('href="/admin/mercadolibre/connect"', page)
        self.assertIn("comprar manualmente", page)
        self.assertLess(
            page.index('id="supplier-publish-button"'),
            page.index('id="supplier-preview-section"'),
        )
        self.assertIn('name="csrf-token" content="test-csrf-token"', page)


    def test_admin_can_connect_mercadolibre_account_with_one_time_oauth_state(self):
        encryption_key = base64.urlsafe_b64encode(b"oauth-test-key-material-32-bytes").decode().rstrip("=")
        settings = {
            "MERCADOLIBRE_CLIENT_ID": "test-client",
            "MERCADOLIBRE_CLIENT_SECRET": "test-secret",
            "MERCADOLIBRE_REDIRECT_URI": (
                "https://shop.example/admin/mercadolibre/oauth/callback"
            ),
            "MERCADOLIBRE_TOKEN_ENCRYPTION_KEY": encryption_key,
        }
        with (
            patch.dict(os.environ, settings),
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
        ):
            start = self.client.get("/admin/mercadolibre/connect")
            self.assertEqual(start.status_code, 302)
            authorization = urlsplit(start.location)
            self.assertEqual(
                authorization.netloc,
                "auth.mercadolibre.com.co",
            )
            state = parse_qs(authorization.query)["state"][0]
            with application.closing(application.connect_db()) as database:
                state_row = database.execute(
                    "SELECT state_hash FROM mercadolibre_oauth_states"
                ).fetchone()
                self.assertNotEqual(state_row["state_hash"], state)

            with patch(
                "mercadolibre_oauth.requests.request",
                side_effect=[
                    mercadolibre_oauth_response(
                        {
                            "access_token": "access-secret",
                            "refresh_token": "refresh-secret",
                            "expires_in": 21600,
                            "user_id": 12345,
                        }
                    ),
                    mercadolibre_oauth_response(
                        {"id": 12345, "nickname": "Vendedor CO"}
                    ),
                ],
            ) as request:
                callback = self.client.get(
                    "/admin/mercadolibre/oauth/callback",
                    query_string={"code": "authorization-code", "state": state},
                )
                self.assertEqual(callback.status_code, 302)
                self.assertIn("oauth=connected", callback.location)
                self.assertEqual(request.call_count, 2)

            account_response = self.client.get("/api/admin/mercadolibre/account")
            self.assertEqual(account_response.status_code, 200)
            account = account_response.get_json()
            self.assertTrue(account["connected"])
            self.assertEqual(account["external_user_id"], "12345")
            self.assertEqual(account["nickname"], "Vendedor CO")
            self.assertNotIn("access_token", account)
            self.assertNotIn("refresh_token", account)
            with application.closing(application.connect_db()) as database:
                saved = database.execute(
                    """SELECT encrypted_access_token, encrypted_refresh_token
                       FROM mercadolibre_oauth_accounts WHERE account_key = 'primary'"""
                ).fetchone()
                self.assertNotEqual(saved["encrypted_access_token"], "access-secret")
                self.assertNotEqual(saved["encrypted_refresh_token"], "refresh-secret")
                self.assertEqual(
                    application.mercadolibre_oauth.decrypt_token(
                        saved["encrypted_access_token"]
                    ),
                    "access-secret",
                )

            with patch("mercadolibre_oauth.requests.request") as request:
                repeated = self.client.get(
                    "/admin/mercadolibre/oauth/callback",
                    query_string={"code": "authorization-code", "state": state},
                )
            self.assertIn("oauth=invalid_state", repeated.location)
            request.assert_not_called()

    def test_mercadolibre_account_status_refreshes_expiring_tokens(self):
        encryption_key = base64.urlsafe_b64encode(b"oauth-test-key-material-32-bytes").decode().rstrip("=")
        settings = {
            "MERCADOLIBRE_CLIENT_ID": "test-client",
            "MERCADOLIBRE_CLIENT_SECRET": "test-secret",
            "MERCADOLIBRE_REDIRECT_URI": (
                "https://shop.example/admin/mercadolibre/oauth/callback"
            ),
            "MERCADOLIBRE_TOKEN_ENCRYPTION_KEY": encryption_key,
        }
        with patch.dict(os.environ, settings):
            with application.closing(application.connect_db()) as database:
                database.execute(
                    """INSERT INTO mercadolibre_oauth_accounts
                       (account_key, external_user_id, nickname, encrypted_access_token,
                        encrypted_refresh_token, token_expires_at)
                       VALUES ('primary', '12345', 'Vendedor CO', ?, ?, ?)""",
                    (
                        application.mercadolibre_oauth.encrypt_token("expired-access"),
                        application.mercadolibre_oauth.encrypt_token("old-refresh"),
                        "2000-01-01T00:00:00+00:00",
                    ),
                )
                database.commit()

            with (
                patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
                patch(
                    "mercadolibre_oauth.requests.request",
                    side_effect=[
                        mercadolibre_oauth_response(
                            {
                                "access_token": "new-access",
                                "refresh_token": "new-refresh",
                                "expires_in": 21600,
                                "user_id": 12345,
                            }
                        ),
                        mercadolibre_oauth_response(
                            {"id": 12345, "nickname": "Vendedor CO"}
                        ),
                    ],
                ),
            ):
                response = self.client.get("/api/admin/mercadolibre/account")

            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertNotIn("new-access", response.get_data(as_text=True))
            with application.closing(application.connect_db()) as database:
                saved = database.execute(
                    """SELECT encrypted_access_token, encrypted_refresh_token,
                              token_expires_at
                       FROM mercadolibre_oauth_accounts WHERE account_key = 'primary'"""
                ).fetchone()
            self.assertEqual(
                application.mercadolibre_oauth.decrypt_token(
                    saved["encrypted_access_token"]
                ),
                "new-access",
            )
            self.assertEqual(
                application.mercadolibre_oauth.decrypt_token(
                    saved["encrypted_refresh_token"]
                ),
                "new-refresh",
            )

    def test_mercadolibre_disconnect_requires_csrf_and_removes_local_tokens(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO mercadolibre_oauth_accounts
                   (account_key, external_user_id, encrypted_access_token,
                    encrypted_refresh_token, token_expires_at)
                   VALUES ('primary', '12345', 'encrypted', 'encrypted', '2099-01-01')"""
            )
            database.commit()
        with patch.object(application, "ADMIN_EMAIL", "admin@example.test"):
            rejected = self.client.post("/api/admin/mercadolibre/disconnect")
            response = self.client.post(
                "/api/admin/mercadolibre/disconnect",
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["connected"])
        with application.closing(application.connect_db()) as database:
            self.assertIsNone(
                database.execute(
                    """SELECT account_key FROM mercadolibre_oauth_accounts
                       WHERE account_key = 'primary'"""
                ).fetchone()
            )

    def test_admin_product_list_requires_admin(self):
        anonymous_client = application.app.test_client()
        with patch.object(application, "ADMIN_EMAIL", "admin@example.test"):
            response = anonymous_client.get("/api/admin/products")

        self.assertEqual(response.status_code, 404)

    def test_admin_can_delete_product_without_order_history(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge)
                   VALUES ('delete-me', 'Producto eliminable', 'Otros', '', '',
                           10000, 10000, 2, '')"""
            )
            database.commit()

        with patch.object(application, "ADMIN_EMAIL", "admin@example.test"):
            csrf_rejected = self.client.delete("/api/admin/products/delete-me")
            response = self.client.delete(
                "/api/admin/products/delete-me",
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(csrf_rejected.status_code, 403)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["action"], "deleted")
        with application.closing(application.connect_db()) as database:
            self.assertIsNone(
                database.execute(
                    "SELECT id FROM products WHERE id = 'delete-me'"
                ).fetchone()
            )
            audit = database.execute(
                """SELECT action, affected_resource FROM admin_audit_log
                   WHERE affected_resource = 'product:delete-me'"""
            ).fetchone()
            self.assertEqual(audit["action"], "delete_product")

    def test_admin_archives_product_with_order_history(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge)
                   VALUES ('ordered-product', 'Producto vendido', 'Otros', '', '',
                           10000, 10000, 2, '')"""
            )
            order = database.execute(
                """INSERT INTO orders (user_id, customer_name, address, city, total)
                   VALUES (?, 'Buyer', 'Street', 'Bogota', 10000)""",
                (self.admin_id,),
            )
            database.execute(
                """INSERT INTO order_items
                   (order_id, product_id, product_name, unit_price, quantity)
                   VALUES (?, 'ordered-product', 'Producto vendido', 10000, 1)""",
                (order.lastrowid,),
            )
            database.commit()

        with patch.object(application, "ADMIN_EMAIL", "admin@example.test"):
            response = self.client.delete(
                "/api/admin/products/ordered-product",
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["action"], "archived")
        with application.closing(application.connect_db()) as database:
            product = database.execute(
                "SELECT active, stock, archived_at FROM products WHERE id = ?",
                ("ordered-product",),
            ).fetchone()
            self.assertEqual(product["active"], 0)
            self.assertEqual(product["stock"], 0)
            self.assertIsNotNone(product["archived_at"])

    def test_admin_preview_rejects_non_mercadolibre_references(self):
        url = "https://example.test/product/123456789012"
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product") as preview_product,
        ):
            response = self.client.get(
                "/api/admin/supplier/preview",
                query_string={"url": url},
            )

        self.assertEqual(response.status_code, 400)
        preview_product.assert_not_called()
        self.assertIn("mercadolibre.com.co", response.get_json()["error"])

    def test_admin_mercadolibre_preview_requires_admin_and_returns_api_facts(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "can_publish": False,
            "blockers": ["Completa manualmente los datos faltantes: variants."],
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(
                application,
                "preview_mercadolibre_product",
                return_value=preview,
            ) as preview_product,
        ):
            response = self.client.get(
                "/api/admin/mercadolibre/preview",
                query_string={"url": url},
            )

        self.assertEqual(response.status_code, 200)
        preview_product.assert_called_once_with(url)
        self.assertEqual(response.get_json()["product"], preview)

    def test_admin_supplier_preview_routes_mercadolibre_and_calculates_cop_price(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "supplier_url": url,
            "supplier_currency": "USD",
            "supplier_original_price": "1.25",
            "image_urls": ["https://cdn.example.test/item.jpg"],
            "variants": [],
            "status": "active",
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=preview),
            patch.object(application, "fetch_usd_cop_rate", return_value=(Decimal("4000"), "fixed", False)),
        ):
            response = self.client.get(
                "/api/admin/supplier/preview",
                query_string={"url": url},
            )

        self.assertEqual(response.status_code, 200)
        product = response.get_json()["product"]
        self.assertEqual(product["supplier_cost_cop"], 5000)
        self.assertEqual(product["suggested_price"], 5500)

    def test_admin_supplier_preview_accepts_bare_item_id(self):
        item_id = "MCO1234567890"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": item_id,
            "supplier_currency": "COP",
            "supplier_original_price": "10000",
            "variants": [],
            "status": "active",
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(
                application,
                "preview_mercadolibre_product",
                return_value=preview,
            ) as preview_product,
        ):
            response = self.client.get(
                "/api/admin/supplier/preview",
                query_string={"url": item_id},
            )

        self.assertEqual(response.status_code, 200)
        preview_product.assert_called_once_with(item_id)

    def test_admin_supplier_publish_revalidates_ml_and_persists_margin_and_images(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "supplier_url": url,
            "supplier_original_price": "60000",
            "supplier_currency": "COP",
            "supplier_stock_reported": 5,
            "name": "Original title",
            "description": "Original description",
            "image_url": "https://cdn.example.test/item.jpg",
            "image_urls": [
                "https://cdn.example.test/item.jpg",
                "https://cdn.example.test/item-side.jpg",
            ],
            "variants": [],
            "status": "active",
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=preview) as fetch,
        ):
            response = self.client.post(
                "/api/admin/supplier/publish",
                json={
                    "url": url,
                    "overrides": {
                        "name": "Título editado",
                        "description": "Descripción editada",
                        "category": "Hogar",
                        "price": 1,
                    },
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        fetch.assert_called_once_with(url)
        result = response.get_json()
        self.assertEqual(result["price"], 66000)
        self.assertEqual(result["supplier_cost"], 60000)
        self.assertEqual(result["commission"], 6000)
        self.assertEqual(result["stock"], 5)
        with application.closing(application.connect_db()) as database:
            saved = database.execute(
                """SELECT name, category, description, price, supplier_cost,
                          supplier_platform, supplier_original_price,
                          supplier_currency, supplier_commission_percent,
                          supplier_url, stock
                   FROM products WHERE id = ?""",
                (result["product_id"],),
            ).fetchone()
            images = database.execute(
                """SELECT image_url FROM supplier_product_images
                   WHERE product_id = ? ORDER BY position""",
                (result["product_id"],),
            ).fetchall()
        self.assertEqual(saved["name"], "Título editado")
        self.assertEqual(saved["category"], "Hogar")
        self.assertEqual(saved["description"], "Descripción editada")
        self.assertEqual(saved["price"], 66000)
        self.assertEqual(saved["supplier_cost"], 60000)
        self.assertEqual(saved["supplier_platform"], "mercadolibre")
        self.assertEqual(saved["supplier_original_price"], "60000")
        self.assertEqual(saved["supplier_currency"], "COP")
        self.assertEqual(saved["supplier_commission_percent"], 10)
        self.assertEqual(saved["supplier_url"], url)
        self.assertEqual(saved["stock"], 5)
        self.assertEqual(
            [image["image_url"] for image in images],
            preview["image_urls"],
        )

    def test_admin_supplier_publish_rejects_non_mercadolibre_reference(self):
        url = "https://example.test/product/123456789016"
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product") as fetch,
        ):
            response = self.client.post(
                "/api/admin/supplier/publish",
                json={"url": url, "overrides": {}},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(response.status_code, 409)
        fetch.assert_not_called()

    def test_admin_supplier_publish_converts_usd_with_server_rate(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "supplier_url": url,
            "supplier_original_price": "15.00",
            "supplier_currency": "USD",
            "supplier_stock_reported": 2,
            "name": "Producto USD",
            "description": "",
            "image_url": "https://cdn.example.test/item.jpg",
            "image_urls": ["https://cdn.example.test/item.jpg"],
            "variants": [],
            "status": "active",
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=preview),
            patch.object(application, "fetch_usd_cop_rate", return_value=(Decimal("4000"), "fixed", False)),
        ):
            response = self.client.post(
                "/api/admin/supplier/publish",
                json={
                    "url": url,
                    "overrides": {
                        "name": "Producto USD",
                        "description": "",
                        "category": "Otros",
                    },
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["price"], 66000)
        with application.closing(application.connect_db()) as database:
            saved = database.execute(
                """SELECT supplier_cost, supplier_original_price,
                          supplier_currency, price
                   FROM products WHERE id = ?""",
                (response.get_json()["product_id"],),
            ).fetchone()
        self.assertEqual(saved["supplier_cost"], 60000)
        self.assertEqual(saved["supplier_original_price"], "15.00")
        self.assertEqual(saved["supplier_currency"], "USD")
        self.assertEqual(saved["price"], 66000)

    def test_new_supplier_product_is_visible_on_first_catalog_page(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "supplier_url": url,
            "supplier_original_price": "60000",
            "supplier_currency": "COP",
            "supplier_stock_reported": 5,
            "name": "Producto recién publicado",
            "description": "",
            "image_url": "https://cdn.example.test/item.jpg",
            "image_urls": ["https://cdn.example.test/item.jpg"],
            "variants": [],
            "status": "active",
        }
        with application.closing(application.connect_db()) as database:
            for index in range(12):
                database.execute(
                    """INSERT INTO products
                       (id, name, category, description, image, price, original_price,
                        stock, badge, active, seller_id, origin)
                       VALUES (?, ?, 'Hogar', '', 'seller.jpg', 1000, 1000,
                               1, '', 1, ?, 'emprendedor')""",
                    (f"seller-{index:02}", f"Producto vendedor {index}", self.admin_id),
                )
            database.commit()

        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=preview),
        ):
            published = self.client.post(
                "/api/admin/supplier/publish",
                json={
                    "url": url,
                    "overrides": {
                        "name": preview["name"],
                        "description": "",
                        "category": "Hogar",
                    },
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(published.status_code, 201, published.get_json())
        catalog = self.client.get("/api/productos?limit=12").get_json()
        self.assertEqual(catalog["total"], 13)
        self.assertEqual(catalog["products"][0]["id"], published.get_json()["product_id"])

    def test_admin_supplier_publish_requires_csrf_before_supplier_lookup(self):
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product") as fetch,
        ):
            response = self.client.post(
                "/api/admin/supplier/publish",
                json={"url": "https://articulo.mercadolibre.com.co/MCO-1234567890"},
            )

        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()

    def test_mercadolibre_publication_requires_manual_stock_only_when_api_omits_it(self):
        url = "https://articulo.mercadolibre.com.co/MCO-1234567890-item.html"
        preview = {
            "supplier_platform": "mercadolibre",
            "supplier_product_id": "MCO1234567890",
            "supplier_url": url,
            "supplier_original_price": "60000",
            "supplier_currency": "COP",
            "supplier_stock_reported": None,
            "name": "Original title",
            "description": "",
            "image_url": "https://cdn.example.test/item.jpg",
            "image_urls": ["https://cdn.example.test/item.jpg"],
            "variants": [],
            "status": "active",
        }
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=preview),
        ):
            response = self.client.post(
                "/api/admin/supplier/publish",
                json={
                    "url": url,
                    "overrides": {
                        "name": "Producto",
                        "description": "",
                        "category": "Otros",
                        "stock": 2,
                    },
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["stock"], 2)

        reported_stock = dict(preview, supplier_stock_reported=4)
        with (
            patch.object(application, "ADMIN_EMAIL", "admin@example.test"),
            patch.object(application, "preview_mercadolibre_product", return_value=reported_stock),
        ):
            rejected = self.client.post(
                "/api/admin/supplier/publish",
                json={
                    "url": url,
                    "overrides": {
                        "name": "Producto",
                        "description": "",
                        "category": "Otros",
                        "stock": 99,
                    },
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
        self.assertEqual(rejected.status_code, 409)

    def test_supplier_schema_migration_is_idempotent_and_backfills_platform(self):
        with application.closing(application.connect_db()) as database:
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge, active, origin, supplier_id, supplier_product_id)
                   VALUES ('legacy-ml', 'Legacy lamp', 'Hogar', '', 'lamp.jpg',
                           11000, 11000, 2, '', 1, 'proveedor',
                           'mercadolibre', 'MCO-legacy-id')"""
            )
            order = database.execute(
                """INSERT INTO orders (user_id, customer_name, address, city, total)
                   VALUES (?, 'Buyer', 'Street', 'Bogota', 11000)""",
                (self.admin_id,),
            )
            database.execute(
                """INSERT INTO order_items
                   (order_id, product_id, product_name, unit_price, quantity)
                   VALUES (?, 'legacy-ml', 'Legacy lamp', 11000, 1)""",
                (order.lastrowid,),
            )
            database.commit()

            for column in (
                "supplier_platform",
                "supplier_original_price",
                "supplier_currency",
                "supplier_commission_percent",
            ):
                database.execute(f"ALTER TABLE products DROP COLUMN {column}")
            for column in ("supplier_original_price", "supplier_currency"):
                database.execute(f"ALTER TABLE product_variants DROP COLUMN {column}")
            for column in (
                "supplier_platform",
                "supplier_original_price",
                "supplier_currency",
                "supplier_commission_percent",
                "supplier_commission",
            ):
                database.execute(f"ALTER TABLE order_items DROP COLUMN {column}")
            database.execute("DROP TABLE supplier_product_images")
            database.commit()

        application.initialize_database()
        application.initialize_database()

        with application.closing(application.connect_db()) as database:
            products = database.execute(
                "SELECT supplier_platform, supplier_cost FROM products WHERE id = 'legacy-ml'"
            ).fetchone()
            order_item = database.execute(
                """SELECT order_items.supplier_platform
                   FROM order_items WHERE order_items.product_id = 'legacy-ml'"""
            ).fetchone()
            product_columns = {
                row["name"] for row in database.execute("PRAGMA table_info(products)")
            }
            variant_columns = {
                row["name"] for row in database.execute("PRAGMA table_info(product_variants)")
            }
            order_item_columns = {
                row["name"] for row in database.execute("PRAGMA table_info(order_items)")
            }
            database.execute(
                """INSERT INTO supplier_product_images (product_id, image_url, position)
                   VALUES ('legacy-ml', 'https://cdn.example.test/lamp-2.jpg', 1)"""
            )
            database.commit()
            image = database.execute(
                """SELECT image_url, position FROM supplier_product_images
                   WHERE product_id = 'legacy-ml'"""
            ).fetchone()

        self.assertEqual(products["supplier_platform"], "mercadolibre")
        self.assertEqual(products["supplier_cost"], None)
        self.assertEqual(order_item["supplier_platform"], "mercadolibre")
        self.assertTrue({
            "supplier_platform",
            "supplier_original_price",
            "supplier_currency",
            "supplier_commission_percent",
        }.issubset(product_columns))
        self.assertTrue({"supplier_original_price", "supplier_currency"}.issubset(variant_columns))
        self.assertTrue({
            "supplier_platform",
            "supplier_original_price",
            "supplier_currency",
            "supplier_commission_percent",
            "supplier_commission",
        }.issubset(order_item_columns))
        self.assertEqual(image["image_url"], "https://cdn.example.test/lamp-2.jpg")
        self.assertEqual(image["position"], 1)

    def test_supplier_gallery_is_returned_for_public_product_details(self):
        product = publishable_mercadolibre_product("MCO123456789017")
        product["image_urls"] = [
            product["image_url"],
            "https://cdn.example.test/product-gallery.jpg",
        ]
        with application.closing(application.connect_db()) as database:
            imported = publish_mercadolibre_product(
                database,
                product,
                {"category": "Hogar"},
                Decimal("1"),
            )
            database.commit()

        response = self.client.get(f"/api/products/{imported['product_id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["product"]["image_urls"], product["image_urls"])

    def test_out_of_stock_product_is_deleted_without_orders_but_archived_with_history(self):
        products = [
            publishable_mercadolibre_product("MCO123456789013"),
            publishable_mercadolibre_product("MCO123456789014"),
        ]
        with application.closing(application.connect_db()) as database:
            imported = [
                publish_mercadolibre_product(
                    database,
                    product,
                    {"category": "Hogar"},
                    Decimal("1"),
                )
                for product in products
            ]
            cursor = database.execute(
                """INSERT INTO orders (user_id, customer_name, address, city, total)
                   VALUES (?, 'Buyer', 'Street', 'Bogota', 11000)""",
                (self.admin_id,),
            )
            database.execute(
                """INSERT INTO order_items
                   (order_id, product_id, product_name, unit_price, quantity)
                   VALUES (?, ?, 'Historical Mercado Libre product', 11000, 1)""",
                (cursor.lastrowid, imported[1]["product_id"]),
            )
            products_for_sync = {
                row["provider_product_id"]: row
                for row in application.list_mercadolibre_products_for_sync(database, 5)
            }
            deleted = sync_mercadolibre_product(
                database,
                products_for_sync["MCO123456789013"],
                dict(products[0], supplier_stock_reported=0),
                Decimal("1"),
            )
            retained = sync_mercadolibre_product(
                database,
                products_for_sync["MCO123456789014"],
                dict(products[1], supplier_stock_reported=0),
                Decimal("1"),
            )
            database.commit()
            deleted_count = database.execute(
                "SELECT COUNT(*) FROM products WHERE id = ?",
                (imported[0]["product_id"],),
            ).fetchone()[0]
            retained_product = database.execute(
                "SELECT active, stock FROM products WHERE id = ?",
                (imported[1]["product_id"],),
            ).fetchone()

        self.assertEqual(deleted["action"], "deleted")
        self.assertEqual(deleted_count, 0)
        self.assertEqual(retained["action"], "deactivated")
        self.assertEqual(retained_product["active"], 0)
        self.assertEqual(retained_product["stock"], 0)

    def test_inventory_sync_updates_stock_and_reapplies_the_ten_percent_margin(self):
        product = publishable_mercadolibre_product("MCO123456789018", stock=2)
        with application.closing(application.connect_db()) as database:
            imported = publish_mercadolibre_product(
                database,
                product,
                {"category": "Hogar"},
                Decimal("1"),
            )
            initial_product_price = database.execute(
                "SELECT price FROM products WHERE id = ?",
                (imported["product_id"],),
            ).fetchone()["price"]
            row = application.list_mercadolibre_products_for_sync(database, 1)[0]
            refreshed = dict(
                product,
                supplier_original_price="12000",
                supplier_stock_reported=3,
            )
            result = sync_mercadolibre_product(
                database,
                row,
                refreshed,
                Decimal("1"),
            )
            database.commit()
            saved = database.execute(
                """SELECT price, supplier_cost, stock, active
                   FROM products WHERE id = ?""",
                (imported["product_id"],),
            ).fetchone()

        self.assertEqual(initial_product_price, 11000)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(saved["price"], 13200)
        self.assertEqual(saved["supplier_cost"], 12000)
        self.assertEqual(saved["stock"], 3)
        self.assertEqual(saved["active"], 1)

    def test_variant_prices_keep_the_ten_percent_margin_during_import_and_sync(self):
        product = publishable_mercadolibre_product("MCO123456789020", stock=2)
        product["supplier_original_price"] = "1250"
        product["variants"] = [
            {
                "supplier_variant_id": "101",
                "name": "Rojo",
                "sku": "ITEM-RED",
                "supplier_original_price": "1250",
                "supplier_stock_reported": 2,
            }
        ]
        with application.closing(application.connect_db()) as database:
            imported = publish_mercadolibre_product(
                database,
                product,
                {"category": "Hogar"},
                Decimal("1"),
            )
            row = application.list_mercadolibre_products_for_sync(database, 1)[0]
            refreshed = dict(
                product,
                supplier_stock_reported=3,
                variants=[
                    dict(
                        product["variants"][0],
                        supplier_original_price="2000",
                        supplier_stock_reported=3,
                    )
                ],
            )
            result = sync_mercadolibre_product(
                database,
                row,
                refreshed,
                Decimal("1"),
            )
            database.commit()
            saved = database.execute(
                """SELECT products.price, products.stock, product_variants.price AS variant_price,
                          product_variants.stock AS variant_stock,
                          product_variants.supplier_stock
                   FROM products JOIN product_variants
                     ON product_variants.product_id = products.id
                   WHERE products.id = ?""",
                (imported["product_id"],),
            ).fetchone()

        self.assertEqual(result["action"], "updated")
        self.assertEqual(saved["price"], 2200)
        self.assertEqual(saved["stock"], 3)
        self.assertEqual(saved["variant_price"], 2200)
        self.assertEqual(saved["variant_stock"], 3)
        self.assertEqual(saved["supplier_stock"], 3)

    def test_unreported_inventory_does_not_change_published_availability(self):
        product = publishable_mercadolibre_product("MCO123456789019", stock=4)
        with application.closing(application.connect_db()) as database:
            imported = publish_mercadolibre_product(
                database,
                product,
                {"category": "Hogar"},
                Decimal("1"),
            )
            row = application.list_mercadolibre_products_for_sync(database, 1)[0]
            with self.assertRaisesRegex(ValueError, "no informó existencias"):
                sync_mercadolibre_product(
                    database,
                    row,
                    dict(product, supplier_stock_reported=None),
                    Decimal("1"),
                )
            saved = database.execute(
                "SELECT active, stock FROM products WHERE id = ?",
                (imported["product_id"],),
            ).fetchone()

        self.assertEqual(saved["active"], 1)
        self.assertEqual(saved["stock"], 4)

    def test_api_failure_does_not_turn_existing_stock_into_zero(self):
        product = publishable_mercadolibre_product("MCO123456789021", stock=4)
        with application.closing(application.connect_db()) as database:
            imported = publish_mercadolibre_product(
                database,
                product,
                {"category": "Hogar"},
                Decimal("1"),
            )
            database.commit()

        with patch.object(
            application,
            "preview_mercadolibre_product",
            side_effect=MercadoLibreImportError("API temporalmente no disponible."),
        ):
            result = application._run_mercadolibre_inventory_batch(1)

        with application.closing(application.connect_db()) as database:
            saved = database.execute(
                "SELECT active, stock FROM products WHERE id = ?",
                (imported["product_id"],),
            ).fetchone()
        self.assertEqual(result["products_updated"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(saved["active"], 1)
        self.assertEqual(saved["stock"], 4)


if __name__ == "__main__":
    unittest.main()
