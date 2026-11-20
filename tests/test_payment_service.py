import hashlib
import os
import unittest
from unittest.mock import patch

from payment_service import (
    PaymentOutcomeUnknown,
    PaymentServiceError,
    calculate_commission,
    payu_checkout_url,
    request_refund,
    validate_checkout_payment_method,
    verify_payu_payment_notification,
)


PAYU_ENV = {
    "PEMU_PAYMENT_PROVIDER": "payu",
    "PEMU_PAYU_MODE": "sandbox",
    "PEMU_PAYU_MERCHANT_ID": "merchant-test",
    "PEMU_PAYU_ACCOUNT_ID": "account-test",
    "PEMU_PAYU_API_LOGIN": "login-test",
    "PEMU_PAYU_API_KEY": "api-key-test",
    "PEMU_PAYU_CONFIRMATION_VERIFIED": "1",
    "PEMU_PAYU_CONFIRMATION_SIGNATURE_ALGORITHM": "md5",
}


class PaymentServiceTests(unittest.TestCase):
    def test_commission_uses_half_up_integer_rounding(self):
        from decimal import Decimal

        self.assertEqual(calculate_commission(100_000, Decimal("0.10")), 10_000)
        self.assertEqual(calculate_commission(5, Decimal("0.10")), 1)

    def test_commission_rejects_negative_amount(self):
        from decimal import Decimal

        with self.assertRaises(ValueError):
            calculate_commission(-1, Decimal("0.10"))

    @patch.dict(os.environ, PAYU_ENV, clear=False)
    def test_confirmation_signature_uses_payus_new_value_verbatim(self):
        fields = {
            "merchant_id": "merchant-test",
            "reference_sale": "XRTE-7",
            "new_value": "100000.0",
            "currency": "COP",
            "state_pol": "4",
        }
        source = "api-key-test~merchant-test~XRTE-7~100000.0~COP~4"
        fields["sign"] = hashlib.md5(source.encode("utf-8")).hexdigest()

        self.assertTrue(verify_payu_payment_notification(fields))
        fields["new_value"] = "100000.00"
        self.assertFalse(verify_payu_payment_notification(fields))

    @patch.dict(
        os.environ,
        {**PAYU_ENV, "PEMU_PAYU_CONFIRMATION_VERIFIED": "0"},
        clear=False,
    )
    def test_unconfirmed_payu_signature_fails_closed(self):
        with self.assertRaises(PaymentServiceError):
            verify_payu_payment_notification({})

    @patch.dict(os.environ, {**PAYU_ENV, "PEMU_PAYU_SANDBOX_CHECKOUT_URL": ""}, clear=False)
    def test_sandbox_checkout_requires_payu_confirmed_url(self):
        with self.assertRaises(PaymentServiceError):
            payu_checkout_url()

    @patch.dict(
        os.environ,
        {
            "ALLOWED_PAYMENT_METHODS": "card_visa,card_mastercard,nequi",
            "PEMU_PAYU_METHOD_RESTRICTION_VERIFIED": "0",
            "PEMU_PAYU_VERIFIED_METHODS": "",
        },
        clear=False,
    )
    def test_checkout_payment_methods_fail_closed_without_provider_restriction(self):
        with self.assertRaisesRegex(PaymentServiceError, "checkout está pausado"):
            validate_checkout_payment_method("nequi")

    @patch.dict(
        os.environ,
        {
            "ALLOWED_PAYMENT_METHODS": "card_visa,card_mastercard,nequi",
            "PEMU_PAYU_METHOD_RESTRICTION_VERIFIED": "1",
            "PEMU_PAYU_VERIFIED_METHODS": "card_visa,card_mastercard",
        },
        clear=False,
    )
    def test_checkout_rejects_method_not_verified_for_provider(self):
        with self.assertRaisesRegex(PaymentServiceError, "no está verificado"):
            validate_checkout_payment_method("nequi")
        self.assertEqual(validate_checkout_payment_method("card_visa"), "card_visa")

    @patch.dict(
        os.environ,
        {"ALLOWED_PAYMENT_METHODS": "card_visa,card_mastercard,nequi"},
        clear=False,
    )
    def test_checkout_rejects_method_outside_allowlist(self):
        with self.assertRaisesRegex(PaymentServiceError, "no está permitido"):
            validate_checkout_payment_method("pse")

    @patch.dict(os.environ, PAYU_ENV, clear=False)
    @patch("payment_service.requests.post")
    def test_partial_refund_uses_provider_api_and_cop_integer(self, post):
        post.return_value.status_code = 200
        post.return_value.ok = True
        post.return_value.json.return_value = {
            "code": "SUCCESS",
            "transactionResponse": {
                "transactionId": "refund-test-id",
                "state": "APPROVED",
            },
        }

        result = request_refund("XRTE-9", "original-payment-id", 1250, 5000, "test")

        self.assertEqual(result["provider_refund_id"], "refund-test-id")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["transaction"]["type"], "PARTIAL_REFUND")
        self.assertEqual(
            payload["transaction"]["additionalValues"]["TX_VALUE"],
            {"value": 1250, "currency": "COP"},
        )
        self.assertEqual(payload["transaction"]["parentTransactionId"], "original-payment-id")

    @patch.dict(os.environ, PAYU_ENV, clear=False)
    @patch("payment_service.requests.post")
    def test_server_error_refund_is_marked_ambiguous(self, post):
        post.return_value.status_code = 500
        post.return_value.ok = False

        with self.assertRaises(PaymentOutcomeUnknown):
            request_refund("XRTE-9", "original-payment-id", 1250, 5000, "test")


if __name__ == "__main__":
    unittest.main()
