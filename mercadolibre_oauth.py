import base64
import os
from urllib.parse import urlencode, urlparse

import requests
from cryptography.exceptions import InvalidTag

import admin_security


API_ROOT = "https://api.mercadolibre.com"
AUTHORIZATION_ROOT = "https://auth.mercadolibre.com.co/authorization"


class MercadoLibreOAuthError(RuntimeError):
    pass


def _configuration():
    client_id = os.environ.get("MERCADOLIBRE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MERCADOLIBRE_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("MERCADOLIBRE_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise MercadoLibreOAuthError(
            "Configura las credenciales de Mercado Libre y la URL de retorno OAuth."
        )
    parsed = urlparse(redirect_uri)
    if (
        parsed.scheme != "https"
        and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1"}
        )
    ) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise MercadoLibreOAuthError(
            "La URL de retorno OAuth debe usar HTTPS (se permite localhost en desarrollo)."
        )
    return client_id, client_secret, redirect_uri


def authorization_url(state):
    client_id, _client_secret, redirect_uri = _configuration()
    return f"{AUTHORIZATION_ROOT}?{urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
    })}"


def _request_json(method, url, *, payload=None, access_token=None):
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            data=payload,
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException as error:
        raise MercadoLibreOAuthError(
            "No se pudo conectar con Mercado Libre para completar OAuth."
        ) from error
    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as error:
        raise MercadoLibreOAuthError(
            "Mercado Libre devolvió una respuesta OAuth no válida."
        ) from error
    if not response.ok or not isinstance(data, dict):
        raise MercadoLibreOAuthError(
            "Mercado Libre rechazó la autorización de la cuenta."
        )
    return data


def _validated_tokens(data, previous_refresh_token=None):
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token") or previous_refresh_token
    try:
        expires_in = int(data.get("expires_in"))
    except (TypeError, ValueError) as error:
        raise MercadoLibreOAuthError(
            "Mercado Libre no devolvió una expiración válida para el token."
        ) from error
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 8192
        or not isinstance(refresh_token, str)
        or not refresh_token
        or len(refresh_token) > 8192
        or not 1 <= expires_in <= 31_536_000
    ):
        raise MercadoLibreOAuthError(
            "Mercado Libre no devolvió tokens válidos para la cuenta."
        )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "user_id": str(data.get("user_id") or "")[:100],
    }


def exchange_authorization_code(code):
    client_id, client_secret, redirect_uri = _configuration()
    if not isinstance(code, str) or not code or len(code) > 4096:
        raise MercadoLibreOAuthError("El código de autorización no es válido.")
    data = _request_json(
        "POST",
        f"{API_ROOT}/oauth/token",
        payload={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    return _validated_tokens(data)


def refresh_access_token(refresh_token):
    client_id, client_secret, _redirect_uri = _configuration()
    data = _request_json(
        "POST",
        f"{API_ROOT}/oauth/token",
        payload={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    return _validated_tokens(data, previous_refresh_token=refresh_token)


def account_profile(access_token):
    data = _request_json(
        "GET",
        f"{API_ROOT}/users/me",
        access_token=access_token,
    )
    user_id = data.get("id")
    if user_id is None:
        raise MercadoLibreOAuthError(
            "No se pudo confirmar la cuenta autorizada de Mercado Libre."
        )
    return {
        "user_id": str(user_id)[:100],
        "nickname": str(data.get("nickname") or "")[:120],
    }


def _encryption_key():
    configured = os.environ.get("MERCADOLIBRE_TOKEN_ENCRYPTION_KEY", "").strip()
    if not configured:
        raise MercadoLibreOAuthError(
            "Configura MERCADOLIBRE_TOKEN_ENCRYPTION_KEY para guardar los tokens."
        )
    try:
        key = base64.b64decode(
            configured + "=" * (-len(configured) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise MercadoLibreOAuthError(
            "MERCADOLIBRE_TOKEN_ENCRYPTION_KEY debe ser Base64 URL-safe de 32 bytes."
        ) from error
    if len(key) != 32:
        raise MercadoLibreOAuthError(
            "MERCADOLIBRE_TOKEN_ENCRYPTION_KEY debe codificar exactamente 32 bytes."
        )
    return key


def encrypt_token(token):
    try:
        return admin_security.encrypt_secret(token, _encryption_key())
    except (ValueError, TypeError) as error:
        raise MercadoLibreOAuthError(
            "No se pudo cifrar el token de Mercado Libre."
        ) from error


def decrypt_token(token):
    try:
        return admin_security.decrypt_secret(token, _encryption_key())
    except (InvalidTag, ValueError, TypeError, UnicodeError) as error:
        raise MercadoLibreOAuthError(
            "No se pudo descifrar el token de Mercado Libre."
        ) from error
