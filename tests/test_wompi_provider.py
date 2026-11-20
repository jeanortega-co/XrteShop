import hashlib
import json
import os
import unittest
from unittest.mock import patch

from wompi_provider import (
    PaymentProviderError,
    WompiPaymentProvider,
    payment_settings,
    validate_payment_method,
    verify_wompi_webhook,
)


def response(status_code, payload):
    class FakeResponse:
        ok = 200 <= status_code < 300

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

    return FakeResponse()


class WompiProviderTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "WOMPI_ENV": "sandbox",
                "WOMPI_PUBLIC_KEY": "pub_test_public",
                "WOMPI_PRIVATE_KEY": "prv_test_private",
                "WOMPI_EVENTS_SECRET": "test_events_secret",
                "WOMPI_INTEGRITY_SECRET": "test_integrity_secret",
            },
            clear=False,
        )
        self.environment.start()
        os.environ.pop("WOMPI_PRODUCTION_APPROVED", None)

    def tearDown(self):
        self.environment.stop()

    def test_sandbox_settings_and_production_gate(self):
        self.assertEqual(payment_settings()["api_root"], "https://sandbox.wompi.co/v1")
        os.environ.update(
            {
                "WOMPI_ENV": "production",
                "WOMPI_PUBLIC_KEY": "pub_prod_public",
                "WOMPI_PRIVATE_KEY": "prv_prod_private",
                "WOMPI_EVENTS_SECRET": "prod_events_secret",
                "WOMPI_INTEGRITY_SECRET": "prod_integrity_secret",
            }
        )
        with self.assertRaisesRegex(PaymentProviderError, "bloqueados"):
            payment_settings()
        os.environ["WOMPI_PRODUCTION_APPROVED"] = "1"
        self.assertEqual(payment_settings()["api_root"], "https://production.wompi.co/v1")

    def test_rejects_payment_methods_outside_allowlist(self):
        for method in ("daviplata", "pse", "cash", "amex", "mastercard"):
            with self.subTest(method=method):
                with self.assertRaises(PaymentProviderError):
                    validate_payment_method(method)

    def test_webhook_checksum_uses_dynamic_signed_properties(self):
        payload = {
            "event": "transaction.updated",
            "environment": "test",
            "timestamp": 1730000000,
            "data": {"transaction": {"reference": "XRTE-42", "status": "APPROVED"}},
            "signature": {
                "properties": ["transaction.reference", "transaction.status"],
            },
        }
        signed = "XRTE-42APPROVED1730000000test_events_secret"
        payload["signature"]["checksum"] = hashlib.sha256(signed.encode()).hexdigest()

        self.assertTrue(verify_wompi_webhook(payload, "test_events_secret"))
        self.assertTrue(
            verify_wompi_webhook(
                payload,
                "test_events_secret",
                payload["signature"]["checksum"],
            )
        )
        self.assertFalse(verify_wompi_webhook(payload, "wrong_secret"))
        self.assertFalse(
            verify_wompi_webhook(payload, "test_events_secret", "0" * 64)
        )

    def test_card_transaction_sends_only_token_not_card_data(self):
        sent = {}

        def fake_request(method, url, **kwargs):
            sent.update(method=method, url=url, **kwargs)
            return response(201, {"data": {"id": "wompi-tx-1", "status": "PENDING"}})

        with patch("wompi_provider.requests.request", side_effect=fake_request):
            transaction = WompiPaymentProvider().create_transaction(
                reference="XRTE-42",
                amount_in_cents=6600000,
                customer_email="buyer@example.com",
                payment_method="card_mastercard",
                acceptance_token="acceptance",
                accept_personal_auth="personal-auth",
                redirect_url="https://xrteshop.vercel.app/pedido/42/pago",
                card_token="tok_test_cardtoken",
                card_brand="MASTERCARD",
            )

        self.assertEqual(transaction["id"], "wompi-tx-1")
        self.assertEqual(sent["url"], "https://sandbox.wompi.co/v1/transactions")
        self.assertEqual(sent["json"]["amount_in_cents"], 6600000)
        self.assertEqual(
            sent["json"]["payment_method"],
            {"type": "CARD", "token": "tok_test_cardtoken", "installments": 1},
        )
        self.assertNotIn("number", json.dumps(sent["json"]))
        self.assertNotIn("cvc", json.dumps(sent["json"]))
        self.assertEqual(
            sent["json"]["signature"],
            hashlib.sha256(
                b"XRTE-426600000COPtest_integrity_secret"
            ).hexdigest(),
        )

    def test_nequi_transaction_requires_colombian_mobile(self):
        with self.assertRaisesRegex(PaymentProviderError, "10 dígitos"):
            WompiPaymentProvider().create_transaction(
                reference="XRTE-42",
                amount_in_cents=6600000,
                customer_email="buyer@example.com",
                payment_method="nequi",
                acceptance_token="acceptance",
                accept_personal_auth="personal-auth",
                redirect_url="https://xrteshop.vercel.app/pedido/42/pago",
                nequi_phone="300123",
            )

    def test_refund_uses_documented_amount_fields_and_idempotency(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/merchants/info"):
                return response(
                    200,
                    {
                        "data": {
                            "id": "merchant-1",
                            "presigned_acceptance": {
                                "acceptance_token": "accept",
                                "permalink": "https://wompi.co/terms",
                            },
                            "presigned_personal_data_auth": {
                                "acceptance_token": "personal",
                                "permalink": "https://wompi.co/privacy",
                            },
                        }
                    },
                )
            return response(202, {"refund_id": "refund-1", "status": "PENDING"})

        with patch("wompi_provider.requests.request", side_effect=fake_request):
            result = WompiPaymentProvider().request_refund(
                transaction_id="wompi-tx-1",
                amount_in_cents=50000,
                original_amount_in_cents=6600000,
                idempotency_key="XRTE-REF-42-9",
                reference="XRTE-42",
            )

        self.assertEqual(result["refund_id"], "refund-1")
        payload = calls[1][2]["json"]
        self.assertEqual(payload["transaction_id"], "wompi-tx-1")
        self.assertEqual(payload["amount_in_cents"], 50000)
        self.assertEqual(
            payload["total_freeze_amount"],
            payload["amount_in_cents"]
            + payload["fee_in_cents"]
            + payload["fee_vat_in_cents"],
        )
        self.assertEqual(payload["idempotency_key"], "XRTE-REF-42-9")
