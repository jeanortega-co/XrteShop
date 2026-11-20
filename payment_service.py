import hashlib
import hmac
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree
from urllib.parse import urlencode

import requests


class PaymentServiceError(RuntimeError):
    pass


class PaymentOutcomeUnknown(PaymentServiceError):
    pass


ALLOWED_PAYMENT_METHOD_NAMES = {
    "card_visa", "card_mastercard", "nequi",
}


def allowed_payment_methods():
    raw_methods = os.environ.get(
        "ALLOWED_PAYMENT_METHODS",
        "card_visa,card_mastercard,nequi",
    )
    methods = tuple(method.strip().lower() for method in raw_methods.split(",") if method.strip())
    if not methods or len(methods) != len(set(methods)) or set(methods) - ALLOWED_PAYMENT_METHOD_NAMES:
        raise PaymentServiceError(
            "ALLOWED_PAYMENT_METHODS debe contener únicamente card_visa,card_mastercard,nequi sin duplicados."
        )
    return methods


def verified_payu_payment_methods():
    raw_methods = os.environ.get("PEMU_PAYU_VERIFIED_METHODS", "")
    methods = tuple(method.strip().lower() for method in raw_methods.split(",") if method.strip())
    if len(methods) != len(set(methods)) or set(methods) - set(allowed_payment_methods()):
        raise PaymentServiceError(
            "PEMU_PAYU_VERIFIED_METHODS debe ser un subconjunto sin duplicados de ALLOWED_PAYMENT_METHODS."
        )
    return methods


def validate_checkout_payment_method(value):
    if not isinstance(value, str):
        raise PaymentServiceError("Selecciona un método de pago válido.")
    method = value.strip().lower()
    if method not in allowed_payment_methods():
        raise PaymentServiceError("El método de pago seleccionado no está permitido.")
    if os.environ.get("PEMU_PAYU_METHOD_RESTRICTION_VERIFIED", "0") != "1":
        raise PaymentServiceError(
            "El checkout está pausado: PayU aún no confirmó la restricción de métodos de pago para esta cuenta."
        )
    if method not in verified_payu_payment_methods():
        raise PaymentServiceError(
            "Ese método todavía no está verificado para el checkout de PayU."
        )
    return method


def gateway_fee_payer():
    payer = os.environ.get("GATEWAY_FEE_PAYER", "split_proportional").strip().lower()
    if payer not in {"platform", "seller", "split_proportional"}:
        raise PaymentServiceError(
            "GATEWAY_FEE_PAYER debe ser platform, seller o split_proportional."
        )
    return payer


def payment_settings():
    provider = os.environ.get("PEMU_PAYMENT_PROVIDER", "payu").strip().lower()
    mode = os.environ.get("PEMU_PAYU_MODE", "sandbox").strip().lower()
    try:
        commission_rate = Decimal(os.environ.get("COMMISSION_RATE", "0.10"))
        hold_days = int(os.environ.get("PAYOUT_HOLD_DAYS", "7"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PaymentServiceError("La configuración de comisión o retención no es válida.") from error
    if provider != "payu":
        raise PaymentServiceError("El proveedor configurado no es compatible con los pagos de marketplace.")
    if mode not in {"sandbox", "production"}:
        raise PaymentServiceError("PEMU_PAYU_MODE debe ser sandbox o production.")
    if not Decimal("0") <= commission_rate <= Decimal("1"):
        raise PaymentServiceError("COMMISSION_RATE debe estar entre 0 y 1.")
    if not 0 <= hold_days <= 90:
        raise PaymentServiceError("PAYOUT_HOLD_DAYS debe estar entre 0 y 90.")
    return {
        "provider": provider,
        "mode": mode,
        "commission_rate": commission_rate,
        "hold_days": hold_days,
        "allowed_payment_methods": allowed_payment_methods(),
        "verified_payment_methods": verified_payu_payment_methods(),
        "gateway_fee_payer": gateway_fee_payer(),
        "merchant_id": os.environ.get("PEMU_PAYU_MERCHANT_ID", "").strip(),
        "account_id": os.environ.get("PEMU_PAYU_ACCOUNT_ID", "").strip(),
        "api_login": os.environ.get("PEMU_PAYU_API_LOGIN", "").strip(),
        "api_key": os.environ.get("PEMU_PAYU_API_KEY", "").strip(),
        "public_key": os.environ.get("PEMU_PAYU_PUBLIC_KEY", "").strip(),
        "signature_key": os.environ.get("PEMU_PAYU_API_KEY", "").strip(),
        "payouts_approved": os.environ.get("PEMU_PAYU_PAYOUTS_APPROVED", "0") == "1",
        "payout_api_verified": os.environ.get("PEMU_PAYU_PAYOUT_API_VERIFIED", "0") == "1",
        "payout_auth_scheme": os.environ.get("PEMU_PAYU_PAYOUT_AUTH_SCHEME", "").strip().lower(),
        "production_approved": os.environ.get("PEMU_PAYU_PRODUCTION_APPROVED", "0") == "1",
        "confirmation_verified": os.environ.get("PEMU_PAYU_CONFIRMATION_VERIFIED", "0") == "1",
        "confirmation_algorithm": os.environ.get(
            "PEMU_PAYU_CONFIRMATION_SIGNATURE_ALGORITHM", ""
        ).strip().lower(),
        "confirmation_secret": os.environ.get("PEMU_PAYU_CONFIRMATION_SECRET", "").strip(),
        "sandbox_checkout_url": os.environ.get("PEMU_PAYU_SANDBOX_CHECKOUT_URL", "").strip(),
    }


def require_payment_credentials(settings=None):
    settings = settings or payment_settings()
    required = ("merchant_id", "account_id", "api_login", "api_key", "signature_key")
    if any(not settings[name] for name in required):
        raise PaymentServiceError("La cuenta de PayU no está configurada para recibir pagos.")
    if not settings["confirmation_verified"]:
        raise PaymentServiceError(
            "La recepción de pagos está bloqueada hasta confirmar con PayU la firma del webhook."
        )
    if settings["confirmation_algorithm"] not in {"md5", "hmac_sha256"}:
        raise PaymentServiceError(
            "Configura el algoritmo de firma confirmado por PayU (md5 o hmac_sha256)."
        )
    if (
        settings["confirmation_algorithm"] == "hmac_sha256"
        and not settings["confirmation_secret"]
    ):
        raise PaymentServiceError("Falta configurar la clave de firma HMAC de PayU.")
    if settings["mode"] == "production":
        if not settings["production_approved"] or not os.environ.get("PEMU_HTTPS", "0") == "1":
            raise PaymentServiceError(
                "La producción está bloqueada hasta aprobar PayU y configurar HTTPS."
            )
        if not settings["payouts_approved"]:
            raise PaymentServiceError(
                "PayU Payouts no está habilitado para la cuenta de XrteShop."
            )
    return settings


def calculate_commission(amount, rate):
    if not isinstance(amount, int) or amount < 0:
        raise ValueError("El monto debe ser un entero no negativo en COP.")
    return int(
        (Decimal(amount) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def payu_payment_fields(order_id, amount, email, response_url, confirmation_url):
    settings = require_payment_credentials()
    reference = f"XRTE-{order_id}"
    amount_text = str(int(amount))
    signature = hashlib.md5(
        (
            f"{settings['signature_key']}~{settings['merchant_id']}~{reference}"
            f"~{amount_text}~COP"
        ).encode("utf-8")
    ).hexdigest()
    return {
        "merchantId": settings["merchant_id"],
        "accountId": settings["account_id"],
        "description": f"Compra XrteShop #{order_id}",
        "referenceCode": reference,
        "amount": amount_text,
        "tax": "0",
        "taxReturnBase": "0",
        "currency": "COP",
        "signature": signature,
        "test": "1" if settings["mode"] == "sandbox" else "0",
        "buyerEmail": email,
        "responseUrl": response_url,
        "confirmationUrl": confirmation_url,
    }


def verify_payu_payment_notification(data):
    settings = require_payment_credentials()
    if not settings["confirmation_verified"]:
        raise PaymentServiceError(
            "PayU no ha confirmado la firma de notificaciones para esta cuenta."
        )
    required = ("merchant_id", "reference_sale", "new_value", "currency", "state_pol", "sign")
    if any(not str(data.get(field, "")).strip() for field in required):
        return False
    if str(data["merchant_id"]) != settings["merchant_id"] or data["currency"] != "COP":
        return False
    try:
        Decimal(str(data["new_value"]))
    except InvalidOperation:
        return False
    signature_string = (
        f"{settings['signature_key']}~{data['merchant_id']}~{data['reference_sale']}"
        f"~{data['new_value']}~{data['currency']}~{data['state_pol']}"
    )
    algorithm = settings["confirmation_algorithm"]
    if algorithm == "md5":
        expected = hashlib.md5(signature_string.encode("utf-8")).hexdigest()
    elif algorithm == "hmac_sha256" and settings["confirmation_secret"]:
        expected = hmac.new(
            settings["confirmation_secret"].encode("utf-8"),
            signature_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    else:
        raise PaymentServiceError(
            "Configura el algoritmo de firma confirmado por PayU (md5 o hmac_sha256)."
        )
    return hmac.compare_digest(expected.lower(), str(data["sign"]).lower())


def payu_checkout_url():
    settings = payment_settings()
    mode = settings["mode"]
    if mode == "sandbox":
        sandbox_url = settings["sandbox_checkout_url"]
        if not sandbox_url.startswith("https://"):
            raise PaymentServiceError(
                "Configura la URL sandbox WebCheckout confirmada directamente por PayU."
            )
        return sandbox_url
    return "https://checkout.payulatam.com/ppp-web-gateway-payu/"


def _payout_api_root(mode):
    return (
        "https://sandbox-transfers.payulatam.com"
        if mode == "sandbox"
        else "https://transfers.payulatam.com"
    )


def _payout_access_token(settings):
    root = _payout_api_root(settings["mode"])
    url = f"{root}/v1.0/authenticate?{urlencode({
        'accountId': settings['account_id'],
        'apiKey': settings['api_key'],
        'apiLogin': settings['api_login'],
    })}"
    try:
        response = requests.post(url, timeout=(5, 20))
    except requests.RequestException as error:
        raise PaymentServiceError(
            "No se pudo determinar si PayU recibió la solicitud de payout."
        ) from error
    if not response.ok:
        raise PaymentServiceError("PayU rechazó la autenticación para Payouts.")
    try:
        token = response.json().get("token")
    except (ValueError, AttributeError) as error:
        raise PaymentServiceError("PayU devolvió una respuesta de autenticación no válida.") from error
    if not token:
        raise PaymentServiceError("PayU no entregó un token para Payouts.")
    return token


def request_payout(beneficiary_id, amount, reference):
    settings = require_payment_credentials()
    if not settings["payouts_approved"]:
        raise PaymentServiceError("PayU Payouts no está aprobado para esta cuenta.")
    if not settings["payout_api_verified"] or settings["payout_auth_scheme"] not in {"raw", "bearer"}:
        raise PaymentServiceError(
            "Confirma con PayU el formato de autenticación de Payouts antes de enviar transferencias."
        )
    if not beneficiary_id or len(str(beneficiary_id)) > 36:
        raise PaymentServiceError("El vendedor no tiene un beneficiario PayU verificado.")
    if not settings["public_key"]:
        raise PaymentServiceError("Falta configurar la llave pública de Payouts de PayU.")
    if not isinstance(amount, int) or amount <= 0:
        raise PaymentServiceError("El payout debe ser un monto entero positivo en COP.")
    token = _payout_access_token(settings)
    root = _payout_api_root(settings["mode"])
    url = (
        f"{root}/v1.0/supplier-transfers/"
        f"{settings['merchant_id']}/{settings['account_id']}"
    )
    payload = {
        "transfers": [
            {
                "value": amount,
                "bankAccount": {"id": str(beneficiary_id)},
                "description": str(reference)[:100],
            }
        ]
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": (
                    token if settings["payout_auth_scheme"] == "raw" else f"Bearer {token}"
                ),
                "PublicKey": settings["public_key"],
                "Accept": "application/json",
            },
            timeout=(5, 25),
        )
    except requests.Timeout as error:
        raise PaymentOutcomeUnknown(
            "Resultado de payout desconocido; reconcilia con PayU antes de reintentar."
        ) from error
    except requests.RequestException as error:
        raise PaymentOutcomeUnknown(
            "No se pudo confirmar la recepción del payout; requiere conciliación con PayU."
        ) from error
    try:
        result = response.json()
    except ValueError as error:
        raise PaymentOutcomeUnknown(
            "PayU devolvió una respuesta de payout no válida; requiere conciliación."
        ) from error
    if response.status_code >= 500:
        raise PaymentOutcomeUnknown(
            "PayU devolvió un error de servidor después de solicitar el payout; requiere conciliación."
        )
    if not response.ok:
        raise PaymentServiceError("PayU rechazó la solicitud de payout.")
    successful = result.get("successfulItems") or []
    if result.get("totalSuccessful") == 0:
        raise PaymentServiceError("PayU no creó el payout; revisa el resultado en el panel.")
    if result.get("totalSuccessful") != 1 or len(successful) != 1:
        raise PaymentOutcomeUnknown(
            "PayU devolvió un resultado de payout inconsistente; requiere conciliación manual."
        )
    item = successful[0]
    account = item.get("bankAccount") or {}
    return {
        "provider_transfer_id": str(item.get("paymentOrderId") or ""),
        "provider_beneficiary_id": str(account.get("id") or beneficiary_id),
        "status": str(item.get("processingStatus") or ""),
    }


def request_refund(reference, transaction_id, amount, total_amount, reason):
    settings = require_payment_credentials()
    if not reference or not transaction_id:
        raise PaymentServiceError("Falta la referencia o transacción original de PayU.")
    if not isinstance(amount, int) or not isinstance(total_amount, int) or not 0 < amount <= total_amount:
        raise PaymentServiceError("El monto de reembolso debe ser un entero COP válido.")
    partial = amount < total_amount
    transaction = {
        "order": {"id": reference},
        "parentTransactionId": transaction_id,
        "reason": str(reason or "Reembolso solicitado por XrteShop")[:255],
        "type": "PARTIAL_REFUND" if partial else "REFUND",
    }
    if partial:
        transaction["additionalValues"] = {
            "TX_VALUE": {"value": amount, "currency": "COP"}
        }
    payload = {
        "language": "es",
        "command": "SUBMIT_TRANSACTION",
        "merchant": {
            "apiKey": settings["api_key"],
            "apiLogin": settings["api_login"],
        },
        "transaction": transaction,
        "test": settings["mode"] == "sandbox",
    }
    root = (
        "https://sandbox.api.payulatam.com"
        if settings["mode"] == "sandbox"
        else "https://api.payulatam.com"
    )
    try:
        response = requests.post(
            f"{root}/payments-api/4.0/service.cgi",
            json=payload,
            headers={"Accept": "application/json"},
            timeout=(5, 25),
        )
    except requests.RequestException as error:
        raise PaymentOutcomeUnknown(
            "PayU no confirmó el resultado del reembolso; concilia el caso antes de repetirlo."
        ) from error
    if response.status_code >= 500:
        raise PaymentOutcomeUnknown(
            "PayU devolvió un error de servidor después de solicitar el reembolso; requiere conciliación."
        )
    if not response.ok:
        raise PaymentServiceError("PayU rechazó la solicitud de reembolso.")
    try:
        result = response.json()
    except ValueError:
        try:
            root_element = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as error:
            raise PaymentOutcomeUnknown(
                "PayU devolvió una respuesta de reembolso no interpretable; requiere conciliación."
            ) from error
        result = {
            "code": root_element.findtext("code", ""),
            "transactionResponse": {
                "transactionId": root_element.findtext("transactionResponse/transactionId", ""),
                "state": root_element.findtext("transactionResponse/state", ""),
                "responseCode": root_element.findtext("transactionResponse/responseCode", ""),
            },
        }
    transaction_result = result.get("transactionResponse") or {}
    state = str(transaction_result.get("state", "")).upper()
    code = str(result.get("code", "")).upper()
    if code != "SUCCESS":
        raise PaymentServiceError("PayU no aceptó el reembolso.")
    if state != "APPROVED":
        raise PaymentOutcomeUnknown(
            "El reembolso no tiene estado final aprobado; concilia con PayU antes de repetirlo."
        )
    refund_id = str(transaction_result.get("transactionId") or "")
    if not refund_id:
        raise PaymentOutcomeUnknown(
            "PayU aprobó el reembolso, pero no devolvió un identificador; requiere conciliación."
        )
    return {
        "provider_refund_id": refund_id,
        "status": state,
    }
