import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PEMU_DATABASE"] = str(Path(_TEST_DIRECTORY.name) / "wompi-test.sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["PEMU_DATABASE_URL"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ.pop("VERCEL", None)

import app as application


class WompiCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.database_path = Path(_TEST_DIRECTORY.name) / f"{self._testMethodName}.sqlite3"
        application.app.config["DATABASE"] = str(self.database_path)
        application.app.config["TESTING"] = True
        application.DATABASE_URL = ""
        application.PAYMENT_PROVIDER = "wompi"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "PEMU_DATABASE_URL": "",
                "POSTGRES_URL": "",
                "WOMPI_ENV": "sandbox",
                "WOMPI_PUBLIC_KEY": "pub_test_public",
                "WOMPI_PRIVATE_KEY": "prv_test_private",
                "WOMPI_EVENTS_SECRET": "test_events_secret",
                "WOMPI_INTEGRITY_SECRET": "test_integrity_secret",
                "WOMPI_PRODUCTION_APPROVED": "0",
                "PEMU_PUBLIC_URL": "https://xrteshop.vercel.app",
                "ALLOWED_PAYMENT_METHODS": "card_visa,card_mastercard,nequi",
                "ADMIN_EMAIL": "jeanortegasaavedra@gmail.com",
                "ACCOUNTS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
            },
            clear=False,
        )
        self.environment.start()
        application.ADMIN_EMAIL = "jeanortegasaavedra@gmail.com"
        application.initialize_database()
        with closing(application.connect_db()) as database:
            buyer_cursor = database.execute(
                """INSERT INTO users
                   (name, email, password_hash, role)
                   VALUES (?, ?, ?, 'admin')""",
                (
                    "Buyer Test",
                    "jeanortegasaavedra@gmail.com",
                    application.hash_password("BuyerPass123"),
                ),
            )
            self.buyer_id = buyer_cursor.lastrowid
            seller_cursor = database.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("Seller Test", "seller@example.test", "not-used"),
            )
            self.seller_id = seller_cursor.lastrowid
            database.execute(
                """INSERT INTO products
                   (id, name, category, description, image, price, original_price,
                    stock, badge, active, seller_id, origin)
                   VALUES ('wompi-product', 'Demo product', 'Hogar', 'Demo',
                           'demo.jpg', 10000, 10000, 5, '', 1, ?, 'emprendedor')""",
                (self.seller_id,),
            )
            database.execute(
                """INSERT INTO user_cart_items (user_id, product_id, quantity)
                   VALUES (?, 'wompi-product', 1)""",
                (self.buyer_id,),
            )
            database.execute(
                "UPDATE users SET cart_initialized = 1 WHERE id = ?",
                (self.buyer_id,),
            )
            database.commit()

        self.mail_patch = patch.object(application, "send_email_message", return_value=True)
        self.mail_patch.start()
        self.addCleanup(self.mail_patch.stop)
        self.client = application.app.test_client()
        csrf = self.client.get("/api/session").get_json()["csrf_token"]
        login = self.client.post(
            "/api/auth/login",
            json={
                "email": "jeanortegasaavedra@gmail.com",
                "password": "BuyerPass123",
            },
            headers={"Origin": "http://localhost", "X-CSRF-Token": csrf},
        )
        self.assertEqual(login.status_code, 200, login.get_json())
        csrf = self.client.get("/api/session").get_json()["csrf_token"]
        self.headers = {"Origin": "http://localhost", "X-CSRF-Token": csrf}
        self.state = {
            "transaction_status": "PENDING",
            "refund_status": "PENDING",
            "transaction_payload": None,
            "refund_payload": None,
            "calls": [],
        }
        self.gateway_patch = patch(
            "wompi_provider.requests.request",
            side_effect=self.fake_wompi_request,
        )
        self.gateway_patch.start()
        self.addCleanup(self.gateway_patch.stop)
        self.addCleanup(self.environment.stop)

    def fake_wompi_request(self, method, url, **kwargs):
        self.state["calls"].append((method, url, kwargs))
        if url.endswith("/merchants/info"):
            return self.fake_response(
                200,
                {
                    "data": {
                        "id": "merchant-test",
                        "presigned_acceptance": {
                            "acceptance_token": "acceptance-test",
                            "permalink": "https://wompi.co/terms",
                        },
                        "presigned_personal_data_auth": {
                            "acceptance_token": "personal-test",
                            "permalink": "https://wompi.co/privacy",
                        },
                    }
                },
            )
        if method == "POST" and url.endswith("/transactions"):
            self.state["transaction_payload"] = kwargs["json"]
            return self.fake_response(
                201,
                {"data": {"id": "wompi-tx-1", "status": "PENDING"}},
            )
        if method == "GET" and "/transactions/" in url:
            sent = self.state["transaction_payload"]
            transaction_type = sent["payment_method_type"]
            if transaction_type == "NEQUI":
                payment_method = {"type": "NEQUI"}
            else:
                token_brand = (
                    "VISA"
                    if sent["payment_method"]["token"] == "tok_test_visa"
                    else "MASTERCARD"
                )
                payment_method = {"type": "CARD", "extra": {"brand": token_brand}}
            return self.fake_response(
                200,
                {
                    "data": {
                        "id": "wompi-tx-1",
                        "reference": sent["reference"],
                        "amount_in_cents": sent["amount_in_cents"],
                        "currency": sent["currency"],
                        "status": self.state["transaction_status"],
                        "payment_method_type": transaction_type,
                        "payment_method": payment_method,
                    }
                },
            )
        if method == "POST" and url.endswith("/refunds"):
            self.state["refund_payload"] = kwargs["json"]
            return self.fake_response(
                202,
                {"refund_id": "wompi-refund-1", "status": "PENDING"},
            )
        if method == "GET" and "/refunds/" in url:
            payload = self.state["refund_payload"]
            return self.fake_response(
                200,
                {
                    "refund_id": "wompi-refund-1",
                    "transaction_id": payload["transaction_id"],
                    "amount_in_cents": payload["amount_in_cents"],
                    "status": self.state["refund_status"],
                },
            )
        raise AssertionError(f"Unexpected Wompi request: {method} {url}")

    @staticmethod
    def fake_response(status_code, payload):
        class FakeResponse:
            ok = 200 <= status_code < 300

            def __init__(self):
                self.status_code = status_code

            def json(self):
                return payload

        return FakeResponse()

    def checkout(self, method):
        body = {
            "payment_method": method,
            "acceptance_token": "acceptance-test",
            "accept_personal_auth": "personal-test",
            "name": "Buyer Test",
            "phone": "3001234567",
            "address": "Calle 1 # 2-3",
            "department": "Cundinamarca",
            "city": "Bogotá",
            "postal_code": "",
            "delivery_notes": "",
            "coupon_code": "",
        }
        if method == "card_visa":
            body.update(card_token="tok_test_visa", card_brand="VISA")
        elif method == "card_mastercard":
            body.update(card_token="tok_test_mastercard", card_brand="MASTERCARD")
        elif method == "nequi":
            body["nequi_phone"] = "3001234567"
        response = self.client.post(
            "/api/checkout",
            json=body,
            headers=self.headers,
        )
        return response

    def signed_event(self, event_type, data, properties, timestamp):
        payload = {
            "event": event_type,
            "environment": "test",
            "timestamp": timestamp,
            "data": data,
            "signature": {"properties": properties},
        }
        signed = ""
        for path in properties:
            value = data
            for key in path.split("."):
                value = value[key]
            signed += str(value)
        signed += f"{timestamp}test_events_secret"
        checksum = hashlib.sha256(signed.encode()).hexdigest()
        payload["signature"]["checksum"] = checksum
        return payload, checksum

    def send_transaction_event(self, status, timestamp=1730000000):
        self.state["transaction_status"] = status
        payload, checksum = self.signed_event(
            "transaction.updated",
            {
                "transaction": {
                    "id": "wompi-tx-1",
                    "reference": "XRTE-1",
                    "status": status,
                }
            },
            ["transaction.reference", "transaction.status"],
            timestamp,
        )
        return self.client.post(
            "/api/webhooks/wompi",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-Event-Checksum": checksum},
        )

    def test_card_approved_only_by_signed_webhook_and_records_seller_ledger(self):
        checkout = self.checkout("card_visa")
        self.assertEqual(checkout.status_code, 201, checkout.get_json())
        with closing(application.connect_db()) as database:
            order = database.execute("SELECT * FROM orders").fetchone()
            self.assertEqual(order["payment_status"], "pending")
            self.assertEqual(order["payment_provider"], "wompi")
            self.assertEqual(order["total"], 10000)
            self.assertEqual(order["provider_payment_id"], "wompi-tx-1")

        webhook = self.send_transaction_event("APPROVED")
        self.assertEqual(webhook.status_code, 200, webhook.get_json())
        with closing(application.connect_db()) as database:
            order = database.execute("SELECT * FROM orders").fetchone()
            self.assertEqual(order["payment_status"], "approved")
            ledger = database.execute(
                """SELECT entry_type, amount_cop, status FROM payment_ledger
                   WHERE seller_id = ? ORDER BY entry_type""",
                (self.seller_id,),
            ).fetchall()
            self.assertEqual(
                [(row["entry_type"], row["amount_cop"]) for row in ledger],
                [("entrepreneur_payable", 9000), ("xrteshop_commission", 1000)],
            )
            self.assertEqual(
                next(row["status"] for row in ledger if row["entry_type"] == "entrepreneur_payable"),
                "pendiente_de_pago_al_emprendedor",
            )

    def test_admin_can_mark_pending_seller_ledger_paid_with_audit_details(self):
        self.assertEqual(self.checkout("card_visa").status_code, 201)
        self.assertEqual(self.send_transaction_event("APPROVED").status_code, 200)
        with closing(application.connect_db()) as database:
            split_id = database.execute(
                "SELECT id FROM payment_splits WHERE seller_id = ?",
                (self.seller_id,),
            ).fetchone()["id"]

        response = self.client.post(
            f"/api/admin/payouts/{split_id}/mark-paid",
            json={"payment_date": "2026-03-10", "payment_reference": "BANK-REF-123"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        with closing(application.connect_db()) as database:
            payable = database.execute(
                """SELECT amount_cop, status, payment_date, payment_reference
                   FROM payment_ledger WHERE entry_key = ?""",
                (f"seller-payable:{split_id}",),
            ).fetchone()
            split = database.execute(
                "SELECT transfer_status, provider_transfer_id FROM payment_splits WHERE id = ?",
                (split_id,),
            ).fetchone()
            audit = database.execute(
                """SELECT action, amount, provider_reference FROM payment_audit_logs
                   WHERE action = 'seller_payout_manually_marked_paid'""",
            ).fetchone()
            self.assertEqual(
                (payable["amount_cop"], payable["status"], payable["payment_date"], payable["payment_reference"]),
                (9000, "pagado_al_emprendedor", "2026-03-10", "BANK-REF-123"),
            )
            self.assertEqual((split["transfer_status"], split["provider_transfer_id"]), ("paid", "BANK-REF-123"))
            self.assertEqual((audit["action"], audit["amount"], audit["provider_reference"]), (
                "seller_payout_manually_marked_paid", 9000, "BANK-REF-123"
            ))

    def test_legacy_payment_confirmation_endpoints_are_blocked(self):
        response = self.client.post("/api/seller/nequi-orders/1/confirm", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 410)
        response = self.client.post("/api/webhooks/payments", json={})
        self.assertEqual(response.status_code, 410)
        response = self.client.post("/mercadopago/webhook", json={})
        self.assertEqual(response.status_code, 410)
        response = self.client.get("/mercadopago/connect")
        self.assertEqual(response.status_code, 410)
        response = self.client.get("/mercadopago/oauth/callback")
        self.assertEqual(response.status_code, 410)
        response = self.client.get("/api/seller/payout-account")
        self.assertEqual(response.status_code, 410)
        response = self.client.post("/api/seller/payout-account/request", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 410)

    def test_card_rejected_by_wompi_does_not_mark_order_paid(self):
        self.assertEqual(self.checkout("card_mastercard").status_code, 201)
        webhook = self.send_transaction_event("DECLINED")
        self.assertEqual(webhook.status_code, 200, webhook.get_json())
        with closing(application.connect_db()) as database:
            order = database.execute("SELECT payment_status FROM orders").fetchone()
            product = database.execute(
                "SELECT stock FROM products WHERE id = 'wompi-product'"
            ).fetchone()
            self.assertEqual(order["payment_status"], "failed")
            self.assertEqual(product["stock"], 5)

    def test_nequi_approved_by_webhook(self):
        self.assertEqual(self.checkout("nequi").status_code, 201)
        webhook = self.send_transaction_event("APPROVED")
        self.assertEqual(webhook.status_code, 200, webhook.get_json())
        with closing(application.connect_db()) as database:
            order = database.execute("SELECT payment_status FROM orders").fetchone()
            self.assertEqual(order["payment_status"], "approved")

    def test_nequi_rejected_by_webhook(self):
        self.assertEqual(self.checkout("nequi").status_code, 201)
        webhook = self.send_transaction_event("DECLINED")
        self.assertEqual(webhook.status_code, 200, webhook.get_json())
        with closing(application.connect_db()) as database:
            order = database.execute("SELECT payment_status FROM orders").fetchone()
            self.assertEqual(order["payment_status"], "failed")

    def test_duplicate_approved_webhook_is_idempotent(self):
        self.assertEqual(self.checkout("card_visa").status_code, 201)
        first = self.send_transaction_event("APPROVED", timestamp=1730000000)
        second = self.send_transaction_event("APPROVED", timestamp=1730000001)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])
        with closing(application.connect_db()) as database:
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM payment_ledger WHERE entry_type = 'entrepreneur_payable'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 1)

    def test_invalid_webhook_checksum_is_rejected(self):
        self.assertEqual(self.checkout("card_visa").status_code, 201)
        payload = {
            "event": "transaction.updated",
            "environment": "test",
            "timestamp": 1730000000,
            "data": {"transaction": {"id": "wompi-tx-1"}},
            "signature": {
                "properties": ["transaction.id"],
                "checksum": "0" * 64,
            },
        }
        response = self.client.post(
            "/api/webhooks/wompi",
            json=payload,
            headers={"X-Event-Checksum": "0" * 64},
        )
        self.assertEqual(response.status_code, 401)
        with closing(application.connect_db()) as database:
            self.assertEqual(
                database.execute("SELECT payment_status FROM orders").fetchone()[0],
                "pending",
            )
            self.assertEqual(database.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 0)

    def test_forbidden_payment_method_is_rejected_before_order_creation(self):
        response = self.client.post(
            "/api/checkout",
            json={"payment_method": "pse"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        with closing(application.connect_db()) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertFalse(any(url.endswith("/transactions") for _, url, _ in self.state["calls"]))

    def test_refund_is_pending_until_signed_wompi_webhook(self):
        self.assertEqual(self.checkout("nequi").status_code, 201)
        self.assertEqual(self.send_transaction_event("APPROVED").status_code, 200)
        with closing(application.connect_db()) as database:
            split_id = database.execute(
                "SELECT id FROM payment_splits WHERE seller_id = ?",
                (self.seller_id,),
            ).fetchone()["id"]
        request_refund = self.client.post(
            "/api/admin/orders/1/refunds",
            json={"amount": 10000, "reason": "Prueba de sandbox"},
            headers=self.headers,
        )
        self.assertEqual(request_refund.status_code, 202, request_refund.get_json())
        with closing(application.connect_db()) as database:
            self.assertEqual(
                database.execute("SELECT payment_status FROM orders").fetchone()[0],
                "approved",
            )
            refund = database.execute("SELECT * FROM payment_refunds").fetchone()
            self.assertEqual(refund["status"], "pending")
            self.assertEqual(refund["provider_refund_id"], "wompi-refund-1")

        self.state["refund_status"] = "APPROVED"
        refund_event, checksum = self.signed_event(
            "refund.updated",
            {
                "refund": {
                    "refund_id": "wompi-refund-1",
                    "status": "APPROVED",
                }
            },
            ["refund.refund_id", "refund.status"],
            1730000002,
        )
        response = self.client.post(
            "/api/webhooks/wompi",
            data=json.dumps(refund_event),
            content_type="application/json",
            headers={"X-Event-Checksum": checksum},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        with closing(application.connect_db()) as database:
            self.assertEqual(
                database.execute("SELECT payment_status FROM orders").fetchone()[0],
                "refunded",
            )
            self.assertEqual(
                database.execute(
                    "SELECT amount_cop FROM payment_ledger WHERE entry_type = 'customer_refund'"
                ).fetchone()[0],
                -10000,
            )
            seller_payable = database.execute(
                """SELECT amount_cop, status FROM payment_ledger
                   WHERE entry_key = ?""",
                (f"seller-payable:{split_id}",),
            ).fetchone()
            self.assertEqual(
                (seller_payable["amount_cop"], seller_payable["status"]),
                (0, "ajustado_por_reembolso"),
            )
            self.assertEqual(
                database.execute(
                    """SELECT amount_cop FROM payment_ledger
                       WHERE entry_type = 'seller_refund_adjustment'"""
                ).fetchone()[0],
                -9000,
            )
            self.assertEqual(
                database.execute(
                    """SELECT amount_cop FROM payment_ledger
                       WHERE entry_type = 'xrteshop_commission_reversal'"""
                ).fetchone()[0],
                -1000,
            )
