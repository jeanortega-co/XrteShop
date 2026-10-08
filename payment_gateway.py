import os
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken


API_ROOT = "https://api.mercadopago.com"
AUTHORIZATION_URL = "https://auth.mercadopago.com/authorization"


class PaymentGatewayError(RuntimeError):
    pass


def encryption():
    key = os.environ.get("PEMU_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise PaymentGatewayError("Falta configurar PEMU_TOKEN_ENCRYPTION_KEY.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise PaymentGatewayError("PEMU_TOKEN_ENCRYPTION_KEY no es una clave Fernet válida.") from error


def encrypt_token(value):
    if not value:
        raise PaymentGatewayError("Mercado Pago no entregó un token válido.")
    return encryption().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(value):
    if not value:
        raise PaymentGatewayError("La cuenta de cobro no está conectada.")
    try:
        return encryption().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError) as error:
        raise PaymentGatewayError("No se pudo descifrar el token de Mercado Pago.") from error


def public_url(path):
    base_url = os.environ.get("PEMU_PUBLIC_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise PaymentGatewayError("Configura PEMU_PUBLIC_URL con el dominio HTTPS público de la tienda.")
    return f"{base_url}{path}"


def marketplace_credentials():
    client_id = os.environ.get("PEMU_MP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PEMU_MP_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise PaymentGatewayError("Configura las credenciales de aplicación de Mercado Pago.")
    return client_id, client_secret


def authorization_url(state):
    client_id, _ = marketplace_credentials()
    return f"{AUTHORIZATION_URL}?{urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'platform_id': 'mp',
        'state': state,
        'redirect_uri': public_url('/mercadopago/oauth/callback'),
    })}"


def request_json(method, path, access_token=None, payload=None):
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = requests.request(
            method,
            f"{API_ROOT}{path}",
            headers=headers,
            json=payload,
            timeout=(5, 20),
        )
    except requests.RequestException as error:
        raise PaymentGatewayError("No se pudo conectar con Mercado Pago. Inténtalo de nuevo.") from error
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise PaymentGatewayError("Mercado Pago devolvió una respuesta no válida.") from error
    if not response.ok:
        message = data.get("message") or data.get("error") or "Solicitud rechazada por Mercado Pago."
        raise PaymentGatewayError(str(message)[:240])
    if not isinstance(data, dict):
        raise PaymentGatewayError("Mercado Pago devolvió una respuesta no válida.")
    return data


def exchange_authorization_code(code):
    client_id, client_secret = marketplace_credentials()
    return request_json(
        "POST",
        "/oauth/token",
        payload={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": public_url("/mercadopago/oauth/callback"),
        },
    )


def refresh_seller_token(refresh_token):
    client_id, client_secret = marketplace_credentials()
    return request_json(
        "POST",
        "/oauth/token",
        payload={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def create_preference(access_token, payload):
    return request_json("POST", "/checkout/preferences", access_token, payload)


def get_payment(access_token, payment_id):
    return request_json("GET", f"/v1/payments/{payment_id}", access_token)
