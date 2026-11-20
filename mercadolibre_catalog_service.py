"""Persistence helpers for manually reviewed Mercado Libre listings."""

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


SUPPLIER_ID = "mercadolibre"
COMMISSION_PERCENT = 10
ALLOWED_CATEGORIES = {
    "Accesorios",
    "Cuidado personal",
    "Electrónica",
    "Hogar",
    "Moda",
    "Otros",
}


def _local_product_id(provider_product_id):
    digest = hashlib.sha256(provider_product_id.encode("utf-8")).hexdigest()[:32]
    return f"ml-{digest}"


def _integer_stock(value, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"El inventario manual de {field_name} debe ser un entero no negativo.")
    return value


def _supplier_cost_cop(raw_price, currency, usd_cop_rate):
    try:
        amount = Decimal(str(raw_price))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Mercado Libre no devolvió un precio original válido.") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Mercado Libre no devolvió un precio original válido.")
    if currency == "USD":
        amount *= usd_cop_rate
    elif currency != "COP":
        raise ValueError("Solo se pueden publicar precios de origen COP o USD.")
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _published_price(supplier_cost):
    return int(
        (Decimal(supplier_cost) * Decimal("1.10")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _override_text(overrides, key, fallback, maximum_length):
    value = overrides.get(key, fallback)
    if not isinstance(value, str):
        raise ValueError(f"El campo {key} debe ser texto.")
    value = value.strip()
    if not value or len(value) > maximum_length:
        raise ValueError(f"El campo {key} está vacío o supera el límite permitido.")
    return value


def publish_mercadolibre_product(database, product, overrides, usd_cop_rate):
    """Revalidate and save API facts plus administrator edits as a supplier listing."""
    provider_id = product.get("supplier_product_id")
    supplier_url = product.get("supplier_url")
    currency = str(product.get("supplier_currency") or "").upper()
    if (
        not isinstance(provider_id, str)
        or not provider_id.startswith("MCO")
        or not isinstance(supplier_url, str)
        or not supplier_url.startswith("https://")
        or product.get("status") != "active"
    ):
        raise ValueError("La publicación de Mercado Libre no está disponible.")
    if not isinstance(overrides, dict):
        raise ValueError("Los cambios de la vista previa no tienen un formato válido.")
    if not isinstance(usd_cop_rate, Decimal) or not usd_cop_rate.is_finite() or usd_cop_rate <= 0:
        raise ValueError("No hay una tasa de conversión USD/COP válida.")

    name = _override_text(overrides, "name", product.get("name", ""), 200)
    description = overrides.get("description", product.get("description", ""))
    if not isinstance(description, str) or len(description) > 5000:
        raise ValueError("La descripción supera el límite permitido.")
    category = _override_text(overrides, "category", product.get("category") or "Otros", 40)
    if category not in ALLOWED_CATEGORIES:
        raise ValueError("Selecciona una categoría válida.")

    image_urls = []
    for candidate in product.get("image_urls") or [product.get("image_url")]:
        if (
            isinstance(candidate, str)
            and candidate.startswith("https://")
            and len(candidate) <= 2048
            and candidate not in image_urls
        ):
            image_urls.append(candidate)
    if not image_urls:
        raise ValueError("La API no proporcionó imágenes válidas para publicar.")

    variants = product.get("variants") or []
    submitted_stocks = overrides.get("variant_stocks", {})
    if not isinstance(submitted_stocks, dict):
        raise ValueError("Las existencias manuales por variante no son válidas.")
    provider_variant_ids = {
        str(variant.get("supplier_variant_id"))
        for variant in variants
        if variant.get("supplier_variant_id") is not None
    }
    if set(submitted_stocks) - provider_variant_ids:
        raise ValueError("No se pueden agregar ni modificar variantes del proveedor.")

    normalized_variants = []
    original_amounts = []
    total_stock = 0
    for variant in variants:
        variant_id = variant.get("supplier_variant_id")
        variant_name = variant.get("name")
        if (
            not isinstance(variant_id, str)
            or not variant_id
            or not isinstance(variant_name, str)
            or not variant_name.strip()
        ):
            raise ValueError("La API no proporcionó una identidad y nombre válidos para cada variante.")
        if variant.get("supplier_original_price") is None:
            raise ValueError(
                f"Falta el precio original de la variante {variant_name}; no se puede publicar."
            )
        supplier_cost = _supplier_cost_cop(
            variant["supplier_original_price"],
            currency,
            usd_cop_rate,
        )
        reported_stock = variant.get("supplier_stock_reported")
        if reported_stock is None:
            if variant_id not in submitted_stocks:
                raise ValueError(
                    f"Confirma manualmente las existencias de la variante {variant_name}."
                )
            stock = _integer_stock(submitted_stocks[variant_id], variant_name)
        else:
            stock = _integer_stock(reported_stock, variant_name)
            if variant_id in submitted_stocks:
                raise ValueError(
                    f"No se puede reemplazar el inventario confirmado de {variant_name}."
                )
        normalized_variants.append(
            {
                "provider_id": variant_id,
                "name": variant_name.strip()[:200],
                "sku": str(variant.get("sku") or "").strip()[:100],
                "original_price": str(variant["supplier_original_price"]),
                "supplier_cost": supplier_cost,
                "retail_price": _published_price(supplier_cost),
                "stock": stock,
            }
        )
        original_amounts.append(variant["supplier_original_price"])
        total_stock += stock

    if variants:
        supplier_cost = min(item["supplier_cost"] for item in normalized_variants)
        retail_price = min(item["retail_price"] for item in normalized_variants)
        original_price = str(min(Decimal(value) for value in original_amounts))
        reported_total = product.get("supplier_stock_reported")
        if reported_total is not None:
            reported_total = _integer_stock(reported_total, "total")
            if total_stock > reported_total:
                raise ValueError(
                    "Las existencias manuales por variante no pueden superar el total informado por Mercado Libre."
                )
    else:
        reported_stock = product.get("supplier_stock_reported")
        if reported_stock is None:
            if "stock" not in overrides:
                raise ValueError(
                    "Mercado Libre no informó existencias; confirma el inventario disponible."
                )
            total_stock = _integer_stock(overrides["stock"], "del producto")
        else:
            total_stock = _integer_stock(reported_stock, "del producto")
            if "stock" in overrides:
                raise ValueError("No se puede reemplazar el inventario confirmado por Mercado Libre.")
        original_price = str(product.get("supplier_original_price") or "")
        supplier_cost = _supplier_cost_cop(original_price, currency, usd_cop_rate)
        retail_price = _published_price(supplier_cost)

    if total_stock <= 0:
        raise ValueError("El producto no tiene existencias disponibles.")
    if variants and len(normalized_variants) != len(variants):
        raise ValueError("No se pueden publicar variantes incompletas.")

    product_id = _local_product_id(provider_id)
    existing = database.execute(
        "SELECT origin, supplier_id FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    if existing and (
        existing["origin"] != "proveedor"
        or existing["supplier_id"] != SUPPLIER_ID
    ):
        raise ValueError("El ID local ya está asociado a un producto de otro origen.")

    database.execute(
        """INSERT INTO products
           (id, name, category, description, image, price, original_price, stock,
            badge, active, origin, supplier_id, supplier_product_id,
            supplier_platform, supplier_original_price, supplier_currency,
            supplier_commission_percent, supplier_url, supplier_cost,
            supplier_shipping_estimate, supplier_updated_at, external_catalog,
            source_url, shipping_cost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Mercado Libre', 1, 'proveedor',
                   ?, ?, 'mercadolibre', ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP,
                   'Mercado Libre', ?, 0)
           ON CONFLICT(id) DO UPDATE SET
               name = excluded.name,
               category = excluded.category,
               description = excluded.description,
               image = excluded.image,
               price = excluded.price,
               original_price = excluded.original_price,
               stock = excluded.stock,
               badge = excluded.badge,
               active = 1,
               origin = 'proveedor',
               supplier_id = excluded.supplier_id,
               supplier_product_id = excluded.supplier_product_id,
               supplier_platform = excluded.supplier_platform,
               supplier_original_price = excluded.supplier_original_price,
               supplier_currency = excluded.supplier_currency,
               supplier_commission_percent = excluded.supplier_commission_percent,
               supplier_url = excluded.supplier_url,
               supplier_cost = excluded.supplier_cost,
               supplier_shipping_estimate = 0,
               supplier_updated_at = CURRENT_TIMESTAMP,
               external_catalog = excluded.external_catalog,
               source_url = excluded.source_url,
               shipping_cost = 0,
               archived_at = NULL""",
        (
            product_id,
            name,
            category,
            description,
            image_urls[0],
            retail_price,
            retail_price,
            total_stock,
            SUPPLIER_ID,
            provider_id,
            original_price,
            currency,
            COMMISSION_PERCENT,
            supplier_url,
            supplier_cost,
            supplier_url,
        ),
    )

    existing_variants = {
        row["supplier_variant_id"]: row["id"]
        for row in database.execute(
            """SELECT id, supplier_variant_id FROM product_variants
               WHERE product_id = ? AND supplier_variant_id IS NOT NULL""",
            (product_id,),
        )
    }
    for variant in normalized_variants:
        options_json = json.dumps(
            {"Variante": variant["name"]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        local_variant_id = existing_variants.get(variant["provider_id"])
        if local_variant_id:
            database.execute(
                """UPDATE product_variants
                   SET options_json = ?, stock = ?, price = ?, supplier_stock = ?,
                       supplier_variant_sku = ?, supplier_original_price = ?,
                       supplier_currency = ?
                   WHERE id = ?""",
                (
                    options_json,
                    variant["stock"],
                    variant["retail_price"],
                    variant["stock"],
                    variant["sku"] or None,
                    variant["original_price"],
                    currency,
                    local_variant_id,
                ),
            )
        else:
            database.execute(
                """INSERT INTO product_variants
                   (id, product_id, options_json, stock, price, supplier_variant_id,
                    supplier_variant_sku, supplier_stock, supplier_original_price,
                    supplier_currency)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    product_id,
                    options_json,
                    variant["stock"],
                    variant["retail_price"],
                    variant["provider_id"],
                    variant["sku"] or None,
                    variant["stock"],
                    variant["original_price"],
                    currency,
                ),
            )
    active_provider_variant_ids = {
        variant["provider_id"] for variant in normalized_variants
    }
    for provider_variant_id, local_variant_id in existing_variants.items():
        if provider_variant_id not in active_provider_variant_ids:
            database.execute(
                """UPDATE product_variants SET stock = 0, supplier_stock = 0
                   WHERE id = ?""",
                (local_variant_id,),
            )

    database.execute("DELETE FROM supplier_product_images WHERE product_id = ?", (product_id,))
    for position, image_url in enumerate(image_urls):
        database.execute(
            """INSERT INTO supplier_product_images (product_id, image_url, position)
               VALUES (?, ?, ?)""",
            (product_id, image_url, position),
        )
    database.execute(
        """INSERT INTO supplier_products
           (supplier_id, provider_product_id, product_id, supplier_cost,
            supplier_stock, currency, product_status, last_checked_at)
           VALUES (?, ?, ?, ?, ?, 'COP', 'approved', CURRENT_TIMESTAMP)
           ON CONFLICT(supplier_id, provider_product_id) DO UPDATE SET
               product_id = excluded.product_id,
               supplier_cost = excluded.supplier_cost,
               supplier_stock = excluded.supplier_stock,
               currency = 'COP',
               product_status = 'approved',
               last_checked_at = CURRENT_TIMESTAMP""",
        (SUPPLIER_ID, provider_id, product_id, supplier_cost, total_stock),
    )
    return {
        "product_id": product_id,
        "supplier_product_id": provider_id,
        "supplier_url": supplier_url,
        "active": True,
        "price": retail_price,
        "supplier_cost": supplier_cost,
        "commission": retail_price - supplier_cost,
        "stock": total_stock,
        "variants": len(normalized_variants),
    }


def list_mercadolibre_products_for_sync(database, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ValueError("El lote de sincronización debe contener entre 1 y 5 productos.")
    return database.execute(
        """SELECT supplier_products.id, supplier_products.provider_product_id,
                  supplier_products.product_id
           FROM supplier_products
           JOIN products ON products.id = supplier_products.product_id
           WHERE supplier_products.supplier_id = ?
           ORDER BY CASE WHEN supplier_products.last_checked_at IS NULL THEN 0 ELSE 1 END,
                    supplier_products.last_checked_at, supplier_products.id
           LIMIT ?""",
        (SUPPLIER_ID, limit),
    ).fetchall()


def _product_has_history(database, product_id):
    references = database.execute(
        """SELECT
             EXISTS(SELECT 1 FROM order_items WHERE product_id = ?) AS has_order_items,
             EXISTS(SELECT 1 FROM supplier_orders WHERE product_id = ?) AS has_supplier_orders,
             EXISTS(SELECT 1 FROM return_requests WHERE product_id = ?) AS has_returns,
             EXISTS(SELECT 1 FROM product_reviews WHERE product_id = ?) AS has_reviews,
             EXISTS(SELECT 1 FROM supplier_products
                    WHERE product_id = ? AND supplier_id != ?) AS has_other_supplier""",
        (product_id, product_id, product_id, product_id, product_id, SUPPLIER_ID),
    ).fetchone()
    return any(bool(references[key]) for key in references.keys())


def _unavailable_result(
    database,
    row,
    supplier_cost,
    supplier_stock,
    product_status,
    delete_without_history=True,
):
    product_id = row["product_id"]
    database.execute(
        """UPDATE product_variants SET stock = 0, supplier_stock = 0
           WHERE product_id = ?""",
        (product_id,),
    )
    if not delete_without_history or _product_has_history(database, product_id):
        database.execute(
            """UPDATE products SET active = 0, stock = 0,
                       archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP),
                       supplier_cost = ?, supplier_updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (supplier_cost, product_id),
        )
        database.execute(
            """UPDATE supplier_products SET supplier_cost = ?, supplier_stock = ?,
                       currency = 'COP', product_status = ?, last_checked_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (supplier_cost, supplier_stock, product_status, row["id"]),
        )
        return {"product_id": product_id, "action": "deactivated", "stock": 0}

    database.execute("DELETE FROM supplier_products WHERE product_id = ?", (product_id,))
    database.execute("DELETE FROM product_interactions WHERE product_id = ?", (product_id,))
    database.execute("DELETE FROM catalog_cleanup_exclusions WHERE product_id = ?", (product_id,))
    database.execute("DELETE FROM products WHERE id = ?", (product_id,))
    return {"product_id": product_id, "action": "deleted", "stock": 0}


def sync_mercadolibre_product(database, product_row, product, usd_cop_rate):
    """Refresh provider inventory and pricing without turning API failures into stock zero."""
    provider_id = product.get("supplier_product_id")
    supplier_url = product.get("supplier_url")
    currency = str(product.get("supplier_currency") or "").upper()
    if (
        not isinstance(provider_id, str)
        or not provider_id.startswith("MCO")
        or not isinstance(supplier_url, str)
        or not supplier_url.startswith("https://")
    ):
        raise ValueError("Mercado Libre no devolvió datos suficientes para sincronizar.")
    if provider_id != product_row["provider_product_id"]:
        raise ValueError("El ID sincronizado no coincide con el producto guardado.")

    current = database.execute(
        """SELECT products.active, products.supplier_cost,
                  supplier_products.product_status
           FROM products
           JOIN supplier_products ON supplier_products.product_id = products.id
           WHERE products.id = ? AND supplier_products.id = ?""",
        (product_row["product_id"], product_row["id"]),
    ).fetchone()
    if not current:
        raise ValueError("El producto importado ya no existe en el catálogo.")
    previous_supplier_cost = current["supplier_cost"] or 0

    variants = product.get("variants") or []
    if not isinstance(variants, list):
        raise ValueError("Mercado Libre devolvió variantes con formato no válido.")
    reported_variants = []
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("Mercado Libre devolvió una variante con formato no válido.")
        variant_id = variant.get("supplier_variant_id")
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError("Mercado Libre no devolvió el ID de una variante.")
        if variant.get("supplier_stock_reported") is None:
            raise ValueError("Mercado Libre no informó las existencias de cada variante.")
        reported_variants.append(
            (
                variant,
                _integer_stock(
                    variant["supplier_stock_reported"],
                    "de la variante",
                ),
            )
        )
    if variants:
        total_stock = sum(item[1] for item in reported_variants)
    else:
        reported_stock = product.get("supplier_stock_reported")
        if reported_stock is None:
            raise ValueError("Mercado Libre no informó existencias verificables.")
        total_stock = _integer_stock(reported_stock, "del producto")

    if total_stock == 0:
        return _unavailable_result(
            database,
            product_row,
            previous_supplier_cost,
            0,
            "out_of_stock",
        )
    if product.get("status") != "active":
        return _unavailable_result(
            database,
            product_row,
            previous_supplier_cost,
            total_stock,
            "unavailable",
            delete_without_history=False,
        )

    if (
        currency not in {"COP", "USD"}
        or not isinstance(usd_cop_rate, Decimal)
        or not usd_cop_rate.is_finite()
        or usd_cop_rate <= 0
    ):
        raise ValueError("No hay una tasa de conversión válida para sincronizar el precio.")

    normalized_variants = []
    if variants:
        seen_ids = set()
        for variant, stock in reported_variants:
            variant_id = variant.get("supplier_variant_id")
            name = variant.get("name")
            if (
                variant_id in seen_ids
                or not isinstance(name, str)
                or not name.strip()
                or variant.get("supplier_original_price") is None
            ):
                raise ValueError("Mercado Libre devolvió datos incompletos de variantes.")
            seen_ids.add(variant_id)
            supplier_cost = _supplier_cost_cop(
                variant["supplier_original_price"],
                currency,
                usd_cop_rate,
            )
            normalized_variants.append(
                {
                    "provider_id": variant_id,
                    "name": name.strip()[:200],
                    "sku": str(variant.get("sku") or "").strip()[:100],
                    "original_price": str(variant["supplier_original_price"]),
                    "supplier_cost": supplier_cost,
                    "retail_price": _published_price(supplier_cost),
                    "stock": stock,
                }
            )
        total_stock = sum(variant["stock"] for variant in normalized_variants)
        supplier_cost = min(variant["supplier_cost"] for variant in normalized_variants)
        retail_price = min(variant["retail_price"] for variant in normalized_variants)
        original_price = str(
            min(Decimal(variant["original_price"]) for variant in normalized_variants)
        )
    else:
        original_price = product.get("supplier_original_price")
        if original_price is None:
            raise ValueError("Mercado Libre no informó un precio verificable.")
        supplier_cost = _supplier_cost_cop(original_price, currency, usd_cop_rate)
        retail_price = _published_price(supplier_cost)

    if not variants and database.execute(
        "SELECT 1 FROM product_variants WHERE product_id = ? LIMIT 1",
        (product_row["product_id"],),
    ).fetchone():
        raise ValueError("La publicación dejó de informar variantes; no se modificó el catálogo.")

    next_active = bool(current["active"]) or current["product_status"] in {
        "out_of_stock",
        "unavailable",
    }
    image_urls = product.get("image_urls") or []
    image_url = image_urls[0] if image_urls else None
    database.execute(
        """UPDATE products SET price = ?, original_price = ?, stock = ?, active = ?,
                   archived_at = CASE WHEN ? = 1 THEN NULL ELSE archived_at END,
                   supplier_cost = ?, supplier_original_price = ?, supplier_currency = ?,
                   supplier_url = ?, supplier_updated_at = CURRENT_TIMESTAMP,
                   image = COALESCE(?, image)
           WHERE id = ?""",
        (
            retail_price,
            retail_price,
            total_stock,
            int(next_active),
            int(next_active),
            supplier_cost,
            original_price,
            currency,
            supplier_url,
            image_url,
            product_row["product_id"],
        ),
    )
    if normalized_variants:
        existing_variants = {
            row["supplier_variant_id"]: row["id"]
            for row in database.execute(
                """SELECT id, supplier_variant_id FROM product_variants
                   WHERE product_id = ? AND supplier_variant_id IS NOT NULL""",
                (product_row["product_id"],),
            )
        }
        for variant in normalized_variants:
            options_json = json.dumps(
                {"Variante": variant["name"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            local_variant_id = existing_variants.get(variant["provider_id"])
            if local_variant_id:
                database.execute(
                    """UPDATE product_variants
                       SET options_json = ?, stock = ?, price = ?, supplier_stock = ?,
                           supplier_variant_sku = ?, supplier_original_price = ?,
                           supplier_currency = ?
                       WHERE id = ?""",
                    (
                        options_json,
                        variant["stock"],
                        variant["retail_price"],
                        variant["stock"],
                        variant["sku"] or None,
                        variant["original_price"],
                        currency,
                        local_variant_id,
                    ),
                )
            else:
                database.execute(
                    """INSERT INTO product_variants
                       (id, product_id, options_json, stock, price, supplier_variant_id,
                        supplier_variant_sku, supplier_stock, supplier_original_price,
                        supplier_currency)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        product_row["product_id"],
                        options_json,
                        variant["stock"],
                        variant["retail_price"],
                        variant["provider_id"],
                        variant["sku"] or None,
                        variant["stock"],
                        variant["original_price"],
                        currency,
                    ),
                )
        active_provider_ids = {
            variant["provider_id"] for variant in normalized_variants
        }
        for provider_variant_id, local_variant_id in existing_variants.items():
            if provider_variant_id not in active_provider_ids:
                database.execute(
                    "UPDATE product_variants SET stock = 0, supplier_stock = 0 WHERE id = ?",
                    (local_variant_id,),
                )

    database.execute(
        """UPDATE supplier_products SET supplier_cost = ?, supplier_stock = ?,
                   currency = 'COP', product_status = 'approved',
                   last_checked_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (supplier_cost, total_stock, product_row["id"]),
    )
    return {
        "product_id": product_row["product_id"],
        "action": "updated",
        "stock": total_stock,
        "price": retail_price,
    }


__all__ = [
    "list_mercadolibre_products_for_sync",
    "publish_mercadolibre_product",
    "sync_mercadolibre_product",
]
