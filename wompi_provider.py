import hashlib
import hmac
import os
import re
from abc import ABC, abstractmethod
from typing import Protocol

import requests


class PaymentProviderError(RuntimeError):
    pass


class PaymentOutcomeUnknown(PaymentProviderError):
    pass


class PaymentProvider(ABC):
    @abstractmethod
    def acceptance_info(self):
        raise NotImplementedError

    @abstractmethod
    def create_transaction(self, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def get_transaction(self, transaction_id):
        raise NotImplementedError

    @abstractmethod
    def request_refund(self, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def get_refund(self, refund_id):
        raise NotImplementedError

    @abstractmethod
    def verify_webhook(self, payload, checksum_header=None):
        raise NotImplementedError


class SplitPayoutProvider(Protocol):
    def create_payout(self, seller_id: int, amount_cop: int, reference: str) -> dict:
        ...


ALLOWED_PAYMENT_METHODS = frozenset(
    {"card_visa", "card_mastercard", "nequi"}
)
WOMPI_ENVIRONMENT_PREFIXES = {
    "sandbox": {
        "public": "pub_test_",
        "private": "prv_test_",
        "events": "test_events_",
        "integrity": "test_integrity_",
    },
    "production": {
        "public": "pub_prod_",
        "private": "prv_prod_",
        "events": "prod_events_",
        "integrity": "prod_integrity_",
    },
}
WOMPI_API_ROOTS = {
    "sandbox": "https://sandbox.wompi.co/v1",
    "production": "https://production.wompi.co/v1",
}


def payment_settings():
    environment = os.environ.get("WOMPI_ENV", "sandbox").strip().lower()
    if environment not in WOMPI_ENVIRONMENT_PREFIXES:
        raise PaymentProviderError("WOMPI_ENV debe ser sandbox o production.")
    if environment == "production" and os.environ.get(
        "WOMPI_PRODUCTION_APPROVED", "0"
    ).strip() != "1":
        raise PaymentProviderError(
            "Los pagos de producción siguen bloqueados hasta recibir confirmación explícita."
        )

    keys = {
        "public": os.environ.get("WOMPI_PUBLIC_KEY", "").strip(),
        "private": os.environ.get("WOMPI_PRIVATE_KEY", "").strip(),
        "events": os.environ.get("WOMPI_EVENTS_SECRET", "").strip(),
        "integrity": os.environ.get("WOMPI_INTEGRITY_SECRET", "").strip(),
    }
    prefixes = WOMPI_ENVIRONMENT_PREFIXES[environment]
    for name, prefix in prefixes.items():
        if not keys[name].startswith(prefix):
            raise PaymentProviderError(
                f"Configura la credencial Wompi de {name} correspondiente a {environment}."
            )

    return {
        "environment": environment,
        "public_key": keys["public"],
        "private_key": keys["private"],
        "events_secret": keys["events"],
        "integrity_secret": keys["integrity"],
        "api_root": WOMPI_API_ROOTS[environment],
    }


def validate_payment_method(value):
    if not isinstance(value, str) or value.strip().lower() not in ALLOWED_PAYMENT_METHODS:
        raise PaymentProviderError("El método de pago seleccionado no está permitido.")
    return value.strip().lower()


def wompi_environment():
    environment = os.environ.get("WOMPI_ENV", "sandbox").strip().lower()
    if environment not in WOMPI_API_ROOTS:
        raise PaymentProviderError("WOMPI_ENV debe ser sandbox o production.")
    return environment


def wompi_public_key():
    key = os.environ.get("WOMPI_PUBLIC_KEY", "").strip()
    environment = wompi_environment()
    if environment == "production" and os.environ.get(
        "WOMPI_PRODUCTION_APPROVED", "0"
    ).strip() != "1":
        return ""
    if not key.startswith(WOMPI_ENVIRONMENT_PREFIXES[environment]["public"]):
        return ""
    return key


def integrity_signature(reference, amount_in_cents, currency, secret):
    if (
        not isinstance(reference, str)
        or not reference
        or not isinstance(amount_in_cents, int)
        or isinstance(amount_in_cents, bool)
        or amount_in_cents <= 0
        or currency != "COP"
        or not secret
    ):
        raise PaymentProviderError("No se pudo preparar la firma de integridad Wompi.")
    message = f"{reference}{amount_in_cents}{currency}{secret}"
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _event_property_value(data, property_path):
    if not isinstance(property_path, str) or not property_path:
        raise ValueError("La propiedad firmada no es válida.")
    value = data
    for key in property_path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ValueError("Falta una propiedad requerida por la firma del evento.")
        value = value[key]
    if value is None or isinstance(value, (dict, list, bool)):
        raise ValueError("La propiedad firmada tiene un tipo no válido.")
    return str(value)


def verify_wompi_webhook(payload, events_secret, checksum_header=None):
    if not isinstance(payload, dict) or not events_secret:
        return False
    signature = payload.get("signature")
    data = payload.get("data")
    timestamp = payload.get("timestamp")
    if (
        not isinstance(signature, dict)
        or not isinstance(data, dict)
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
    ):
        return False

    properties = signature.get("properties")
    checksum = signature.get("checksum")
    if (
        not isinstance(properties, list)
        or not properties
        or len(properties) > 30
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum)
    ):
        return False
    if checksum_header and not hmac.compare_digest(
        checksum_header.strip().lower(), checksum.lower()
    ):
        return False

    try:
        signed_values = "".join(
            _event_property_value(data, property_path)
            for property_path in properties
        )
    except ValueError:
        return False
    signed_values += f"{timestamp}{events_secret}"
    expected = hashlib.sha256(signed_values.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected.lower(), checksum.lower())


class WompiPaymentProvider(PaymentProvider):
    def __init__(self):
        self.settings = payment_settings()

    def _request(self, method, path, *, payload=None, public=False, allow_conflict=False):
        headers = {"Accept": "application/json"}
        if public:
            headers["x-merchant-public-key"] = self.settings["public_key"]
        else:
            headers["Authorization"] = f"Bearer {self.settings['private_key']}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = requests.request(
                method,
                f"{self.settings['api_root']}{path}",
                headers=headers,
                json=payload,
                timeout=(5, 20),
            )
        except requests.Timeout as error:
            if method == "POST":
                raise PaymentOutcomeUnknown(
                    "Wompi no confirmó el resultado de la operación; requiere conciliación."
                ) from error
            raise PaymentProviderError("No se pudo consultar Wompi.") from error
        except requests.RequestException as error:
            if method == "POST":
                raise PaymentOutcomeUnknown(
                    "No se pudo confirmar si Wompi recibió la operación; requiere conciliación."
                ) from error
            raise PaymentProviderError("No se pudo consultar Wompi.") from error

        try:
            response_data = response.json()
        except ValueError as error:
            if method == "POST":
                raise PaymentOutcomeUnknown(
                    "Wompi devolvió una respuesta no interpretable; requiere conciliación."
                ) from error
            raise PaymentProviderError("Wompi devolvió una respuesta no válida.") from error
        if not isinstance(response_data, dict):
            raise PaymentProviderError("Wompi devolvió una respuesta no válida.")
        if not response.ok and not (allow_conflict and response.status_code == 409):
            if method == "POST" and response.status_code >= 500:
                raise PaymentOutcomeUnknown(
                    "Wompi devolvió un error de servidor; verifica el resultado antes de reintentar."
                )
            raise PaymentProviderError("Wompi rechazó la operación solicitada.")
        return response_data

    def acceptance_info(self):
        response = self._request("GET", "/merchants/info", public=True)
        data = response.get("data")
        if not isinstance(data, dict):
            raise PaymentProviderError("Wompi no devolvió los datos de aceptación del comercio.")
        acceptance = data.get("presigned_acceptance")
        personal_data = data.get("presigned_personal_data_auth")
        if not isinstance(acceptance, dict) or not isinstance(personal_data, dict):
            raise PaymentProviderError("Wompi no devolvió los contratos de aceptación requeridos.")
        fields = (
            acceptance.get("acceptance_token"),
            acceptance.get("permalink"),
            personal_data.get("acceptance_token"),
            personal_data.get("permalink"),
            data.get("id"),
        )
        if any(not isinstance(value, str) or not value.strip() for value in fields):
            raise PaymentProviderError("La información de aceptación de Wompi está incompleta.")
        return {
            "acceptance_token": fields[0],
            "acceptance_url": fields[1],
            "accept_personal_auth": fields[2],
            "personal_data_url": fields[3],
            "merchant_id": fields[4],
        }

    def create_transaction(
        self,
        *,
        reference,
        amount_in_cents,
        customer_email,
        payment_method,
        acceptance_token,
        accept_personal_auth,
        redirect_url,
        card_token=None,
        card_brand=None,
        nequi_phone=None,
    ):
        method = validate_payment_method(payment_method)
        transaction_method = {"type": "NEQUI"}
        payment_method_type = "NEQUI"
        if method in {"card_visa", "card_mastercard"}:
            expected_brand = "VISA" if method == "card_visa" else "MASTERCARD"
            if (
                not isinstance(card_token, str)
                or not re.fullmatch(r"tok_(?:test|prod)_[A-Za-z0-9_-]{1,160}", card_token)
                or card_brand != expected_brand
            ):
                raise PaymentProviderError("El token no corresponde a una tarjeta permitida.")
            transaction_method = {
                "type": "CARD",
                "token": card_token,
                "installments": 1,
            }
            payment_method_type = "CARD"
        else:
            if not isinstance(nequi_phone, str) or not re.fullmatch(r"3\d{9}", nequi_phone):
                raise PaymentProviderError(
                    "Ingresa un número colombiano de 10 dígitos registrado en Nequi."
                )
            transaction_method["phone_number"] = nequi_phone

        if (
            not isinstance(acceptance_token, str)
            or not acceptance_token
            or not isinstance(accept_personal_auth, str)
            or not accept_personal_auth
        ):
            raise PaymentProviderError("Acepta los documentos requeridos por Wompi para continuar.")
        if not isinstance(customer_email, str) or not customer_email.strip():
            raise PaymentProviderError("Falta el correo del comprador.")
        signature = integrity_signature(
            reference,
            amount_in_cents,
            "COP",
            self.settings["integrity_secret"],
        )
        payload = {
            "acceptance_token": acceptance_token,
            "accept_personal_auth": accept_personal_auth,
            "amount_in_cents": amount_in_cents,
            "currency": "COP",
            "customer_email": customer_email.strip(),
            "payment_method": transaction_method,
            "payment_method_type": payment_method_type,
            "reference": reference,
            "signature": signature,
            "redirect_url": redirect_url,
        }
        response = self._request("POST", "/transactions", payload=payload)
        transaction = response.get("data")
        if not isinstance(transaction, dict) or not transaction.get("id"):
            raise PaymentOutcomeUnknown(
                "Wompi no devolvió el identificador de la transacción; requiere conciliación."
            )
        return transaction

    def get_transaction(self, transaction_id):
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise PaymentProviderError("El identificador de transacción Wompi no es válido.")
        response = self._request(
            "GET",
            f"/transactions/{requests.utils.quote(transaction_id, safe='')}",
        )
        transaction = response.get("data")
        if not isinstance(transaction, dict):
            raise PaymentProviderError("Wompi no devolvió la transacción solicitada.")
        return transaction

    def request_refund(
        self,
        *,
        transaction_id,
        amount_in_cents,
        original_amount_in_cents,
        idempotency_key,
        reference,
    ):
        if (
            not isinstance(amount_in_cents, int)
            or isinstance(amount_in_cents, bool)
            or amount_in_cents <= 0
            or not isinstance(original_amount_in_cents, int)
            or isinstance(original_amount_in_cents, bool)
            or amount_in_cents > original_amount_in_cents
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 64
        ):
            raise PaymentProviderError("Los datos del reembolso Wompi no son válidos.")
        merchant = self.acceptance_info()
        payload = {
            "idempotency_key": idempotency_key,
            "transaction_id": transaction_id,
            "merchant_id": merchant["merchant_id"],
            "region": "CO",
            "currency": "COP",
            "amount_in_cents": amount_in_cents,
            "fee_in_cents": 0,
            "fee_vat_in_cents": 0,
            "total_freeze_amount": amount_in_cents,
            "refund_type": (
                "TOTAL"
                if amount_in_cents == original_amount_in_cents
                else "PARTIAL"
            ),
            "original_amount_in_cents": original_amount_in_cents,
            "references": [reference[:40]],
        }
        response = self._request(
            "POST", "/refunds", payload=payload, allow_conflict=True
        )
        refund = response.get("data", response)
        if not isinstance(refund, dict) or not (
            refund.get("id") or refund.get("refund_id")
        ):
            raise PaymentOutcomeUnknown(
                "Wompi no devolvió el identificador del reembolso; requiere conciliación."
            )
        return refund

    def get_refund(self, refund_id):
        if not isinstance(refund_id, str) or not refund_id.strip():
            raise PaymentProviderError("El identificador de reembolso Wompi no es válido.")
        response = self._request(
            "GET",
            f"/refunds/{requests.utils.quote(refund_id, safe='')}",
        )
        refund = response.get("data", response)
        if not isinstance(refund, dict):
            raise PaymentProviderError("Wompi no devolvió el reembolso solicitado.")
        return refund

    def verify_webhook(self, payload, checksum_header=None):
        if payload.get("environment") != (
            "test" if self.settings["environment"] == "sandbox" else "prod"
        ):
            return False
        return verify_wompi_webhook(
            payload,
            self.settings["events_secret"],
            checksum_header,
        )
