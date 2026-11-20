import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


def sqlite_tables(connection):
    return [
        row[0]
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        )
    ]


def sqlite_counts(connection, tables):
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def check_source(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"La verificación SQLite falló: {integrity}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"La base SQLite tiene {len(violations)} referencias foráneas inválidas."
        )


def ordered_tables(connection, tables):
    table_set = set(tables)
    dependencies = {}
    for table in tables:
        dependencies[table] = {
            row[2]
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            if row[2] in table_set
        }

    ordered = []
    visiting = set()
    visited = set()

    def visit(table):
        if table in visiting:
            raise RuntimeError(f"Se detectó una dependencia cíclica en la tabla {table}.")
        if table in visited:
            return
        visiting.add(table)
        for dependency in sorted(dependencies[table]):
            visit(dependency)
        visiting.remove(table)
        visited.add(table)
        ordered.append(table)

    for table in sorted(tables):
        visit(table)
    return ordered


def snapshot_database(source_path):
    backup_root = ROOT / "backups" / "postgres-migration"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / timestamp / "pemu.sqlite3"
    backup_path.parent.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    return backup_path


def existing_target_data(connection, source_tables, additional_tables=()):
    rows = connection.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"""
    ).fetchall()
    target_tables = {row["table_name"] for row in rows}
    existing = []
    for table in sorted((set(source_tables) | set(additional_tables)) & target_tables):
        count = connection.execute(
            f'SELECT COUNT(*) AS count FROM "{table}"'
        ).fetchone()["count"]
        if count:
            existing.append((table, count))
    return existing


def target_columns(connection, table):
    rows = connection.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = %s
           ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def import_tables(source, target, tables):
    from psycopg import sql

    table_order = ordered_tables(source, tables)
    target_tables = {
        row["table_name"]
        for row in target.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"""
        ).fetchall()
    }
    missing_tables = sorted(set(tables) - target_tables)
    if missing_tables:
        raise RuntimeError(
            "El esquema PostgreSQL no contiene: " + ", ".join(missing_tables)
        )

    tables_to_clear = set(tables)
    if "product_images" in target_tables:
        tables_to_clear.add("product_images")
    truncate = sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(table) for table in sorted(tables_to_clear))
    )
    target.execute(truncate)

    source_counts = sqlite_counts(source, tables)
    for table in table_order:
        source_cursor = source.execute(f'SELECT * FROM "{table}"')
        source_column_names = [description[0] for description in source_cursor.description]
        available_target_columns = target_columns(target, table)
        missing_columns = sorted(set(source_column_names) - available_target_columns)
        if missing_columns:
            raise RuntimeError(
                f"El esquema PostgreSQL de {table} no contiene: "
                + ", ".join(missing_columns)
            )
        columns = source_column_names
        if not source_counts[table]:
            continue
        insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        target.cursor().executemany(insert, source_cursor.fetchall())

    schema = target.execute("SELECT current_schema() AS name").fetchone()["name"]
    identity_columns = target.execute(
        """SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = current_schema() AND is_identity = 'YES'"""
    ).fetchall()
    for identity in identity_columns:
        table = identity["table_name"]
        column = identity["column_name"]
        if table not in tables:
            continue
        sequence = target.execute(
            "SELECT pg_get_serial_sequence(%s, %s) AS name",
            (f"{schema}.{table}", column),
        ).fetchone()["name"]
        if not sequence:
            continue
        maximum = target.execute(
            sql.SQL("SELECT MAX({}) AS maximum FROM {}").format(
                sql.Identifier(column), sql.Identifier(table)
            )
        ).fetchone()["maximum"]
        if maximum is None:
            target.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            target.execute("SELECT setval(%s, %s, true)", (sequence, maximum))

    for table, expected_count in source_counts.items():
        imported_count = target.execute(
            f'SELECT COUNT(*) AS count FROM "{table}"'
        ).fetchone()["count"]
        if imported_count != expected_count:
            raise RuntimeError(
                f"El conteo no coincide en {table}: SQLite={expected_count}, "
                f"PostgreSQL={imported_count}."
            )
    return source_counts


def import_legacy_seller_images(source, target):
    stored_paths = set()
    if "product_images" in sqlite_tables(source):
        stored_paths = {
            row[0] for row in source.execute("SELECT image_path FROM product_images")
        }
    products = source.execute(
        """SELECT id, image FROM products
           WHERE image LIKE 'imagenes/vendedores/%'"""
    ).fetchall()
    copied = 0
    content_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    for product_id, image_path in products:
        if image_path in stored_paths:
            continue
        parts = image_path.split("/")
        if len(parts) != 3 or parts[:2] != ["imagenes", "vendedores"]:
            raise RuntimeError(
                f"La ruta de imagen del producto {product_id} no es válida."
            )
        filename = parts[2]
        local_path = ROOT / "static" / "images" / "vendedores" / filename
        if not local_path.is_file():
            raise RuntimeError(
                f"Falta la imagen local del producto {product_id}; "
                "se canceló la migración para evitar una publicación rota."
            )
        content_type = content_types.get(local_path.suffix.lower())
        if not content_type:
            raise RuntimeError(
                f"El formato de imagen del producto {product_id} no está permitido."
            )
        cursor = target.execute(
            """INSERT INTO product_images
               (image_path, product_id, image_data, content_type)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (image_path) DO NOTHING""",
            (image_path, product_id, local_path.read_bytes(), content_type),
        )
        copied += cursor.rowcount
    return copied


def main():
    parser = argparse.ArgumentParser(
        description="Migra la base SQLite local de XrteShop a PostgreSQL."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "pemu.sqlite3",
        help="Ruta a la base SQLite (por defecto: pemu.sqlite3).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Crea el respaldo e importa los datos a PostgreSQL.",
    )
    parser.add_argument(
        "--confirm-overwrite",
        action="store_true",
        help="Autoriza reemplazar datos existentes en las tablas de XrteShop del destino.",
    )
    args = parser.parse_args()

    source_path = args.source.resolve()
    if not source_path.is_file():
        parser.error(f"No existe la base SQLite: {source_path}")
    if args.confirm_overwrite and not args.apply:
        parser.error("--confirm-overwrite solo se usa junto con --apply.")
    database_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("PEMU_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    ).strip()

    with sqlite3.connect(source_path) as source:
        check_source(source)
        tables = sqlite_tables(source)
        counts = sqlite_counts(source, tables)

    print("Resumen de SQLite:")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    if not args.apply:
        print("Simulación completada; no se modificó ninguna base de datos.")
        return 0
    if not database_url:
        parser.error(
            "Configura DATABASE_URL en el entorno o en .env; no pases secretos como argumentos."
        )

    from psycopg.rows import dict_row

    os.environ["PEMU_SKIP_DATABASE_INIT"] = "1"
    os.environ["PEMU_HTTPS"] = "0"
    import app as application

    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as target:
        existing = existing_target_data(target, tables, {"product_images"})
        if existing and not args.confirm_overwrite:
            details = ", ".join(f"{table}={count}" for table, count in existing)
            raise RuntimeError(
                "La base PostgreSQL destino ya contiene datos de XrteShop "
                f"({details}). Revisa el destino y usa --confirm-overwrite solo "
                "si autorizas sustituirlos."
            )

    backup_path = snapshot_database(source_path)
    print(f"Respaldo SQLite creado: {backup_path.relative_to(ROOT)}")
    application.initialize_database()

    with sqlite3.connect(backup_path) as source, psycopg.connect(
        database_url, row_factory=dict_row, connect_timeout=10
    ) as target:
        check_source(source)
        with target.transaction():
            migrated = import_tables(source, target, sqlite_tables(source))
            migrated_images = import_legacy_seller_images(source, target)

    print("Migración verificada:")
    for table, count in migrated.items():
        print(f"  {table}: {count}")
    print(f"  legacy_seller_images_copied: {migrated_images}")
    print("No se imprimieron ni exportaron datos personales.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, sqlite3.Error, psycopg.Error, RuntimeError) as error:
        print(f"Error de migración: {error}", file=sys.stderr)
        raise SystemExit(1)
