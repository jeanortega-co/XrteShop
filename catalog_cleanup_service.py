"""Safe backup and non-entrepreneur catalog cleanup for CLI and admin."""

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


CONFIRMATION = "DELETE NON-ENTREPRENEUR PRODUCTS"

_DISPOSABLE_PRODUCT_REFERENCES = {
    "product_interactions",
    "user_cart_items",
    "user_cart_variant_items",
    "user_favorites",
    "product_variants",
}


def connect_readonly(database_path):
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"No existe la base de datos configurada: {database_path}")
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def backup_before_cleanup(database_path, root, image_root):
    root = Path(root).resolve()
    image_root = Path(image_root).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_directory = root / "backups" / "catalog-cleanup" / timestamp
    backup_directory.mkdir(parents=True, exist_ok=False)
    backup_database = backup_directory / "database.sqlite3"

    with closing(connect_readonly(database_path)) as connection:
        backup_connection = sqlite3.connect(backup_database)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()
        rows = connection.execute(
            "SELECT DISTINCT image FROM products WHERE image IS NOT NULL AND image != ''"
        ).fetchall()
    copied_images = []
    missing_images = []
    for row in rows:
        image_reference = row["image"]
        if image_reference.startswith(("https://", "http://")):
            continue
        image_path = Path(image_reference)
        parts = image_path.parts
        if parts and parts[0].lower() in {"imagenes", "images"}:
            relative = Path(*parts[1:])
        else:
            relative = image_path
        source = (image_root / relative).resolve()
        if image_root not in source.parents or not source.is_file():
            missing_images.append(image_reference)
            continue
        target = backup_directory / "images" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_images.append(image_reference)

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_backup": str(backup_database.relative_to(root)),
        "copied_local_product_images": sorted(set(copied_images)),
        "missing_or_unsupported_images": sorted(set(missing_images)),
        "database": str(Path(database_path).resolve()),
    }
    (backup_directory / "backup-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return backup_directory, manifest


def _product_references(connection, product_id):
    references = {}
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in tables:
        table_name = row["name"]
        escaped_table = table_name.replace('"', '""')
        foreign_keys = connection.execute(
            f'PRAGMA foreign_key_list("{escaped_table}")'
        ).fetchall()
        for foreign_key in foreign_keys:
            if foreign_key["table"] != "products":
                continue
            column_name = foreign_key["from"]
            escaped_column = column_name.replace('"', '""')
            count = connection.execute(
                f'SELECT COUNT(*) FROM "{escaped_table}" WHERE "{escaped_column}" = ?',
                (product_id,),
            ).fetchone()[0]
            if count:
                references[table_name] = references.get(table_name, 0) + count
    return references


def _delete_disposable_product_references(connection, product_id):
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    existing_tables = {row["name"] for row in tables}
    for table_name in _DISPOSABLE_PRODUCT_REFERENCES - {"product_variants"}:
        if table_name not in existing_tables:
            continue
        escaped_table = table_name.replace('"', '""')
        foreign_keys = connection.execute(
            f'PRAGMA foreign_key_list("{escaped_table}")'
        ).fetchall()
        product_columns = {
            foreign_key["from"]
            for foreign_key in foreign_keys
            if foreign_key["table"] == "products"
        }
        if table_name == "product_interactions":
            product_columns.add("product_id")
        for column_name in product_columns:
            escaped_column = column_name.replace('"', '""')
            connection.execute(
                f'DELETE FROM "{escaped_table}" WHERE "{escaped_column}" = ?',
                (product_id,),
            )

    if {"user_cart_variant_items", "product_variants"} <= existing_tables:
        connection.execute(
            """DELETE FROM user_cart_variant_items
               WHERE variant_id IN (
                   SELECT id FROM product_variants WHERE product_id = ?
               )""",
            (product_id,),
        )


def _cleanup_targets(connection):
    rows = connection.execute(
        """SELECT products.id, products.name, products.image,
                  products.origin, products.seller_id
           FROM products
           WHERE products.active = 1
             AND NOT (products.origin = 'emprendedor' OR products.seller_id IS NOT NULL)
             AND products.origin != 'proveedor'
           ORDER BY products.id"""
    ).fetchall()
    targets = []
    for row in rows:
        references = _product_references(connection, row["id"])
        order_count = references.get("order_items", 0)
        supplier_references = (
            references.get("supplier_products", 0)
            + references.get("supplier_orders", 0)
        )
        history_references = sum(
            count
            for table, count in references.items()
            if table not in _DISPOSABLE_PRODUCT_REFERENCES
        )
        targets.append(
            {
                "id": row["id"],
                "name": row["name"],
                "image": row["image"],
                "order_count": order_count,
                "supplier_references": supplier_references,
                "other_references": max(
                    0, history_references - order_count - supplier_references
                ),
                "action": "archive" if history_references else "delete",
            }
        )
    return targets


def catalog_cleanup_preview(connection):
    targets = _cleanup_targets(connection)
    entrepreneur_count = connection.execute(
        """SELECT COUNT(*) FROM products
           WHERE origin = 'emprendedor' OR seller_id IS NOT NULL"""
    ).fetchone()[0]
    entrepreneur_sellers = connection.execute(
        "SELECT COUNT(DISTINCT seller_id) FROM products WHERE seller_id IS NOT NULL"
    ).fetchone()[0]
    provider_count = connection.execute(
        """SELECT COUNT(*) FROM products
           WHERE active = 1 AND origin = 'proveedor' AND seller_id IS NULL"""
    ).fetchone()[0]
    excluded_seed_products = {
        row["product_id"]
        for row in connection.execute(
            "SELECT product_id FROM catalog_cleanup_exclusions"
        ).fetchall()
    }
    return {
        "targets": targets,
        "will_delete": sum(
            row["action"] == "delete" for row in targets
        ),
        "will_archive": sum(
            row["action"] == "archive" for row in targets
        ),
        "entrepreneur_products_untouched": entrepreneur_count,
        "entrepreneur_sellers_untouched": entrepreneur_sellers,
        "provider_products_preserved": provider_count,
        "deleted_seed_products_excluded_from_reseeding": sorted(excluded_seed_products),
    }


def apply_catalog_cleanup(database_path, expected_ids):
    database_path = Path(database_path).resolve()
    database = sqlite3.connect(database_path, timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    try:
        database.execute("BEGIN IMMEDIATE")
        targets = _cleanup_targets(database)
        actual_ids = [row["id"] for row in targets]
        if actual_ids != expected_ids:
            raise RuntimeError(
                "El catálogo cambió después del dry-run. No se borró nada; "
                "genera un nuevo backup y una nueva simulación."
            )
        deleted = []
        archived = []
        for row in targets:
            product_id = row["id"]
            _delete_disposable_product_references(database, product_id)
            if row["action"] == "archive":
                database.execute(
                    """UPDATE products SET active = 0, archived_at = CURRENT_TIMESTAMP
                       WHERE id = ?
                         AND NOT (origin = 'emprendedor' OR seller_id IS NOT NULL)
                         AND origin != 'proveedor'""",
                    (product_id,),
                )
                archived.append(product_id)
            else:
                if database.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type = 'table' AND name = 'external_product_translations'"""
                ).fetchone():
                    database.execute(
                        """DELETE FROM external_product_translations
                           WHERE product_id = ?""",
                        (product_id,),
                    )
                database.execute(
                    """INSERT OR IGNORE INTO catalog_cleanup_exclusions (product_id)
                       VALUES (?)""",
                    (product_id,),
                )
                cursor = database.execute(
                    """DELETE FROM products
                       WHERE id = ?
                         AND NOT (origin = 'emprendedor' OR seller_id IS NOT NULL)""",
                    (product_id,),
                )
                if cursor.rowcount:
                    deleted.append(product_id)
        database.commit()
        return {"deleted_ids": deleted, "archived_ids": archived}
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
