"""Mercado Libre item previews through its public API."""

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests


MERCADOLIBRE_DOMAINS = {
    "mercadolibre.com.co",
    "www.mercadolibre.com.co",
    "articulo.mercadolibre.com.co",
}
MERCADOLIBRE_API_ROOT = "https://api.mercadolibre.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ITEM_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(MCO-?[0-9]{6,})(?![0-9])", re.I)


class MercadoLibreImportError(RuntimeError):
    """Raised when the official Mercado Libre API cannot provide a preview."""


def sanitize_mercadolibre_url(value):
    """Validate a Colombia Mercado Libre URL without requesting the supplied host."""
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("Ingresa un enlace de Mercado Libre Colombia válido.")
    value = value.strip()
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("El enlace contiene caracteres no permitidos.")
    item_match = ITEM_ID_PATTERN.fullmatch(value)
    if item_match:
        item_id = "MCO" + re.sub(r"\D", "", item_match.group(1))
        return f"https://articulo.mercadolibre.com.co/{item_id}"
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as error:
        raise ValueError("El enlace de Mercado Libre no es válido.") from error
    if (
        parsed.scheme.lower() != "https"
        or hostname not in MERCADOLIBRE_DOMAINS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError(
            "El enlace debe usar HTTPS y pertenecer a mercadolibre.com.co."
        )
    return urlunsplit(("https", hostname, parsed.path, parsed.query, ""))


def _official_supplier_url(item, item_id, safe_url):
    permalink = item.get("permalink")
    if isinstance(permalink, str):
        try:
            safe_permalink = sanitize_mercadolibre_url(permalink)
            permalink_item_id = _item_id_from_url(safe_permalink)
        except ValueError:
            pass
        else:
            if permalink_item_id == item_id:
                return safe_permalink
    return safe_url


def _item_id_from_url(safe_url):
    parsed = urlsplit(safe_url)
    query = parse_qs(parsed.query)
    for key in ("item_id", "itemId", "id"):
        candidate = (query.get(key) or [""])[0].strip()
        match = ITEM_ID_PATTERN.fullmatch(candidate)
        if match:
            return "MCO" + re.sub(r"\D", "", match.group(1))
    match = ITEM_ID_PATTERN.search(parsed.path)
    if not match:
        raise ValueError(
            "No se encontró el ID de una publicación en el enlace. "
            "Usa el enlace de un artículo, no el de una búsqueda."
        )
    return "MCO" + re.sub(r"\D", "", match.group(1))


def _read_json_response(response):
    try:
        if 300 <= response.status_code < 400:
            raise MercadoLibreImportError(
                "La API devolvió una redirección que no se seguirá por seguridad."
            )
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                size = int(content_length)
            except (TypeError, ValueError) as error:
                raise ValueError("La API devolvió un tamaño de respuesta no válido.") from error
            if size < 0 or size > MAX_RESPONSE_BYTES:
                raise MercadoLibreImportError(
                    "La respuesta de Mercado Libre supera el límite permitido."
                )

        chunks = []
        total_size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_RESPONSE_BYTES:
                raise MercadoLibreImportError(
                    "La respuesta de Mercado Libre supera el límite permitido."
                )
            chunks.append(chunk)
        return json.loads(b"".join(chunks).decode(response.encoding or "utf-8"))
    finally:
        response.close()


def _request_json(path):
    url = f"{MERCADOLIBRE_API_ROOT}{path}"
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=(5, 12),
            stream=True,
            allow_redirects=False,
        )
        return _read_json_response(response)
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else None
        if status_code == 404:
            raise MercadoLibreImportError(
                "La publicación no existe o ya no está disponible en Mercado Libre."
            ) from error
        if status_code in (401, 403):
            raise MercadoLibreImportError(
                "Mercado Libre no permitió consultar esta publicación por su API pública."
            ) from error
        raise MercadoLibreImportError(
            "Mercado Libre rechazó la consulta de la publicación."
        ) from error
    except (requests.RequestException, ValueError) as error:
        raise MercadoLibreImportError(
            "No se pudo consultar la API oficial de Mercado Libre."
        ) from error


def _valid_price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price.is_finite() and price > 0 else None


def _valid_stock(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        return int(value)
    return None


def _safe_images(item):
    raw_pictures = item.get("pictures")
    if not isinstance(raw_pictures, list):
        return []
    images = []
    for picture in raw_pictures[:40]:
        if not isinstance(picture, dict):
            continue
        image_url = picture.get("secure_url") or picture.get("url")
        if (
            isinstance(image_url, str)
            and image_url.startswith("https://")
            and len(image_url) <= 2048
            and image_url not in images
        ):
            images.append(image_url)
    return images


def _normalize_variants(raw_variations, currency):
    if not isinstance(raw_variations, list):
        return []
    if len(raw_variations) > 100:
        raise MercadoLibreImportError(
            "La publicación contiene demasiadas variantes para una vista previa segura."
        )
    variants = []
    for variation in raw_variations:
        if not isinstance(variation, dict):
            raise MercadoLibreImportError(
                "Mercado Libre devolvió una variante con formato no válido."
            )
        variant_id = variation.get("id")
        if isinstance(variant_id, bool) or not isinstance(variant_id, (int, str)):
            variant_id = None
        variant_id = str(variant_id).strip() if variant_id is not None else ""

        attributes = variation.get("attribute_combinations")
        labels = []
        if isinstance(attributes, list):
            for attribute in attributes[:20]:
                if not isinstance(attribute, dict):
                    continue
                label = str(attribute.get("value_name") or "").strip()
                if label:
                    labels.append(label[:80])

        variants.append(
            {
                "supplier_variant_id": variant_id or None,
                "name": " / ".join(labels)[:200],
                "sku": str(variation.get("seller_custom_field") or "").strip()[:100],
                "supplier_original_price": str(
                    _valid_price(variation.get("price")) or ""
                ) or None,
                "supplier_currency": currency,
                "supplier_stock_reported": _valid_stock(
                    variation.get("available_quantity")
                ),
            }
        )
    return variants


def preview_mercadolibre_product(product_url, include_description=True):
    """Return item facts and publication blockers using only Mercado Libre's API."""
    safe_url = sanitize_mercadolibre_url(product_url)
    item_id = _item_id_from_url(safe_url)
    item = _request_json(f"/items/{item_id}")
    if not isinstance(item, dict):
        raise MercadoLibreImportError(
            "Mercado Libre devolvió una publicación con formato no válido."
        )
    returned_id = item.get("id")
    returned_match = (
        ITEM_ID_PATTERN.fullmatch(str(returned_id))
        if isinstance(returned_id, (int, str)) and not isinstance(returned_id, bool)
        else None
    )
    normalized_returned_id = (
        "MCO" + re.sub(r"\D", "", returned_match.group(1))
        if returned_match
        else ""
    )
    if normalized_returned_id != item_id:
        raise MercadoLibreImportError(
            "El ID devuelto por Mercado Libre no coincide con el enlace consultado."
        )

    currency = str(item.get("currency_id") or "").strip().upper()
    price = _valid_price(item.get("price"))
    item_stock = _valid_stock(item.get("available_quantity"))
    images = _safe_images(item)
    variants = _normalize_variants(item.get("variations"), currency)

    warnings = []
    description = ""
    if include_description:
        try:
            description_data = _request_json(f"/items/{item_id}/description")
            plain_text = (
                description_data.get("plain_text")
                if isinstance(description_data, dict)
                else None
            )
            if isinstance(plain_text, str):
                description = plain_text.strip()[:5000]
            else:
                warnings.append("La API de Mercado Libre no devolvió una descripción utilizable.")
        except MercadoLibreImportError as error:
            warnings.append(str(error))

    missing_fields = []
    raw_title = item.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if not title:
        missing_fields.append("name")
    if price is None:
        missing_fields.append("price")
    if not images:
        missing_fields.append("images")
    if variants:
        if any(variant["supplier_stock_reported"] is None for variant in variants):
            missing_fields.append("variant_stock")
        if any(variant["supplier_original_price"] is None for variant in variants):
            missing_fields.append("variant_price")
        if any(variant["supplier_variant_id"] is None for variant in variants):
            missing_fields.append("variant_id")
        if any(not variant["name"] for variant in variants):
            missing_fields.append("variant_name")

    if variants and all(
        variant["supplier_stock_reported"] is not None for variant in variants
    ):
        total_stock = sum(
            variant["supplier_stock_reported"] for variant in variants
        )
    else:
        total_stock = item_stock

    blockers = []
    if missing_fields:
        blockers.append(
            "Completa manualmente los datos faltantes: "
            + ", ".join(missing_fields)
            + "."
        )
    if total_stock is None:
        if "stock" not in missing_fields:
            missing_fields.append("stock")
            blockers.append("Mercado Libre no informó existencias disponibles.")
    elif total_stock <= 0:
        blockers.append("La publicación no tiene existencias disponibles.")
    if item.get("status") != "active":
        blockers.append("La publicación no está activa en Mercado Libre.")
    if currency not in {"COP", "USD"}:
        blockers.append(
            f"La moneda informada ({currency or 'desconocida'}) no se puede convertir a COP."
        )

    return {
        "supplier_platform": "mercadolibre",
        "supplier_product_id": item_id,
        "supplier_url": _official_supplier_url(item, item_id, safe_url),
        "name": title[:200],
        "description": description,
        "category": "",
        "category_id": str(item.get("category_id") or "").strip(),
        "image_url": images[0] if images else "",
        "image_urls": images,
        "supplier_original_price": str(price) if price is not None else None,
        "supplier_currency": currency or None,
        "supplier_stock_reported": total_stock,
        "supplier_sku": str(item.get("seller_custom_field") or "").strip()[:100],
        "variants": variants,
        "missing_fields": missing_fields,
        "blockers": blockers,
        "warnings": warnings,
        "can_publish": not blockers,
        "status": str(item.get("status") or "").strip(),
    }


__all__ = [
    "MAX_RESPONSE_BYTES",
    "MERCADOLIBRE_DOMAINS",
    "MercadoLibreImportError",
    "preview_mercadolibre_product",
    "sanitize_mercadolibre_url",
]
