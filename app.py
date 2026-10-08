import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from payment_gateway import (
    PaymentGatewayError,
    authorization_url,
    create_preference,
    decrypt_token,
    encrypt_token,
    exchange_authorization_code,
    get_payment,
    public_url,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = Flask(
    __name__,
    static_folder=ROOT / "static",
    template_folder=ROOT / "templates",
)
app.config.update(
    SECRET_KEY=os.environ.get("PEMU_SECRET_KEY", "local-development-key-change-before-deploy"),
    DATABASE=os.environ.get("PEMU_DATABASE", str(ROOT / "pemu.sqlite3")),
    SESSION_COOKIE_NAME="pemu_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PEMU_HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    MAIL_SERVER=os.environ.get("PEMU_SMTP_HOST", "").strip(),
    MAIL_PORT=int(os.environ.get("PEMU_SMTP_PORT", "587") or "587"),
    MAIL_USERNAME=os.environ.get("PEMU_SMTP_USERNAME", "").strip(),
    MAIL_PASSWORD=os.environ.get("PEMU_SMTP_PASSWORD", "").replace(" ", "").strip(),
    MAIL_DEFAULT_SENDER=os.environ.get("PEMU_SMTP_FROM", "").strip(),
    MAIL_USE_TLS=os.environ.get("PEMU_SMTP_USE_TLS", "1") == "1",
)

CATEGORIES = ("Accesorios", "Cuidado personal", "Electrónica", "Hogar", "Moda", "Otros")
MARKETPLACE_FEE_PERCENT = 10
EXTERNAL_CATALOG_URL = (
    "https://dummyjson.com/products?limit=0"
    "&select=id,title,description,category,price,discountPercentage,thumbnail,brand,stock"
)
EXTERNAL_CATALOG_TTL_SECONDS = 3600
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
EXCHANGE_RATE_TTL_SECONDS = 21600
TRANSLATION_URL = "https://api.mymemory.translated.net/get"
_external_catalog_lock = Lock()
_external_catalog_cache = {"products": [], "expires_at": 0.0}
_exchange_rate_lock = Lock()
_exchange_rate_cache = {"rate": None, "updated_at": None, "expires_at": 0.0}

PRODUCTS = [
    ("airpods", "Audífonos inalámbricos", "Electrónica", "Sonido nítido para todos tus días.", "imagenes/black-wireless-headphones-isolated-on-transparent-background-ai-generated-png.webp", 80000, 100000, 18, "Más vendido"),
    ("reloj-hombre", "Reloj clásico", "Accesorios", "Diseño atemporal con correa ajustable.", "imagenes/img2.webp", 120000, 150000, 12, "-20%"),
    ("set-reloj", "Set de reloj", "Accesorios", "Un detalle especial para regalar.", "imagenes/img3.jpg", 160000, 200000, 8, "-20%"),
    ("vgr-660", "VGR 660", "Cuidado personal", "Cortadora recargable de precisión.", "imagenes/img5.webp", 180000, 190000, 9, "Precio especial"),
    ("maquina-vgr", "Máquina VGR", "Cuidado personal", "Rendimiento confiable para tu rutina.", "imagenes/img4.jpg", 190000, 220000, 7, "-14%"),
    ("trimer-kemei", "Trimmer Kemei", "Cuidado personal", "Acabado preciso y cómodo.", "imagenes/img6.webp", 180000, 200000, 11, "-10%"),
    ("pc-gamer", "PC Gamer", "Electrónica", "Potencia lista para jugar y crear.", "imagenes/png-transparent-megaport-pc-gamer-amd-fx-6100-gaming-computer-desktop-computers-computer-cases-housings-computer-electronics-computer-laptop-thumbnail.png", 2499900, 2899900, 4, "Selección Pemu"),
    ("chaqueta-cuero", "Chaqueta de cuero", "Moda", "Una silueta clásica para todos los días.", "imagenes/chaqueta.cuero.png", 189900, 219900, 6, "-14%"),
    ("vestido", "Vestido urbano", "Moda", "Una pieza versátil, cómoda y ligera.", "imagenes/vestido.webp", 149900, 179900, 10, "-17%"),
    ("nike-tn", "Tenis urbanos", "Moda", "Comodidad y estilo para moverte.", "imagenes/nike tn.webp", 320000, 360000, 5, "-11%"),
]


def connect_db():
    database = sqlite3.connect(app.config["DATABASE"], timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def initialize_database():
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db()) as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                verification_code TEXT,
                verification_code_expires_at TEXT,
                password_reset_code TEXT,
                password_reset_expires_at TEXT,
                mp_user_id TEXT,
                mp_access_token TEXT,
                mp_refresh_token TEXT,
                mp_token_expires_at TEXT,
                nequi_phone TEXT,
                buyer_nequi_phone TEXT,
                preferred_payment_method TEXT NOT NULL DEFAULT 'auto',
                personalization_enabled INTEGER NOT NULL DEFAULT 0,
                cart_initialized INTEGER NOT NULL DEFAULT 0,
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                delivery_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                image TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                original_price INTEGER NOT NULL CHECK (original_price >= price),
                stock INTEGER NOT NULL CHECK (stock >= 0),
                badge TEXT NOT NULL,
                flash_sale_percent INTEGER NOT NULL DEFAULT 0,
                flash_sale_ends_at TEXT,
                seller_id INTEGER REFERENCES users(id),
                external_catalog TEXT,
                source_url TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS external_product_translations (
                product_id TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                description_es TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_cart_items (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                customer_name TEXT NOT NULL,
                address TEXT NOT NULL,
                city TEXT NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Recibido',
                payment_status TEXT NOT NULL DEFAULT 'pending',
                phone TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                delivery_notes TEXT NOT NULL DEFAULT '',
                expires_at TEXT,
                stock_released INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                product_name TEXT NOT NULL,
                unit_price INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0)
            );
            CREATE TABLE IF NOT EXISTS payment_splits (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                seller_id INTEGER REFERENCES users(id),
                amount INTEGER NOT NULL CHECK (amount > 0),
                preference_id TEXT,
                init_point TEXT,
                payment_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                payment_method TEXT NOT NULL DEFAULT 'mercadopago',
                nequi_phone TEXT,
                shipping_status TEXT NOT NULL DEFAULT 'preparing',
                shipping_carrier TEXT,
                tracking_number TEXT,
                estimated_delivery_at TEXT,
                shipped_at TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (order_id, seller_id)
            );
            CREATE TABLE IF NOT EXISTS discount_coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                code TEXT NOT NULL UNIQUE,
                discount_percent INTEGER NOT NULL,
                max_discount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'issued',
                reserved_order_id INTEGER REFERENCES orders(id),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS product_interactions (
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                action TEXT NOT NULL CHECK (action IN ('view', 'cart_add', 'search_result')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS return_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                split_id TEXT NOT NULL REFERENCES payment_splits(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                reason TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'received',
                free_return INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (order_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS seller_commissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES users(id),
                split_id TEXT NOT NULL UNIQUE REFERENCES payment_splits(id),
                amount INTEGER NOT NULL CHECK (amount >= 0),
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TEXT
            );
            """
        )
        user_columns = {row["name"] for row in database.execute("PRAGMA table_info(users)")}
        for column, definition in {
            "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "verification_code": "TEXT",
            "verification_code_expires_at": "TEXT",
            "password_reset_code": "TEXT",
            "password_reset_expires_at": "TEXT",
            "mp_user_id": "TEXT",
            "mp_access_token": "TEXT",
            "mp_refresh_token": "TEXT",
            "mp_token_expires_at": "TEXT",
            "nequi_phone": "TEXT",
            "buyer_nequi_phone": "TEXT",
            "preferred_payment_method": "TEXT NOT NULL DEFAULT 'auto'",
            "personalization_enabled": "INTEGER NOT NULL DEFAULT 0",
            "cart_initialized": "INTEGER NOT NULL DEFAULT 0",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "address": "TEXT NOT NULL DEFAULT ''",
            "city": "TEXT NOT NULL DEFAULT ''",
            "department": "TEXT NOT NULL DEFAULT ''",
            "postal_code": "TEXT NOT NULL DEFAULT ''",
            "delivery_notes": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in user_columns:
                database.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        order_columns = {row["name"] for row in database.execute("PRAGMA table_info(orders)")}
        order_migrations = {
            "payment_status": "TEXT NOT NULL DEFAULT 'pending'",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "department": "TEXT NOT NULL DEFAULT ''",
            "postal_code": "TEXT NOT NULL DEFAULT ''",
            "delivery_notes": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT",
            "stock_released": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, definition in order_migrations.items():
            if column not in order_columns:
                database.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
        split_columns = {row["name"] for row in database.execute("PRAGMA table_info(payment_splits)")}
        for column, definition in {
            "payment_method": "TEXT NOT NULL DEFAULT 'mercadopago'",
            "nequi_phone": "TEXT",
            "shipping_status": "TEXT NOT NULL DEFAULT 'preparing'",
            "shipping_carrier": "TEXT",
            "tracking_number": "TEXT",
            "estimated_delivery_at": "TEXT",
            "shipped_at": "TEXT",
            "delivered_at": "TEXT",
        }.items():
            if column not in split_columns:
                database.execute(f"ALTER TABLE payment_splits ADD COLUMN {column} {definition}")
        database.execute(
            """UPDATE orders SET payment_status = 'approved'
               WHERE status = 'Recibido' AND payment_status = 'pending'"""
        )
        product_columns = {row["name"] for row in database.execute("PRAGMA table_info(products)")}
        migrations = {
            "seller_id": "INTEGER REFERENCES users(id)",
            "external_catalog": "TEXT",
            "source_url": "TEXT",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "flash_sale_percent": "INTEGER NOT NULL DEFAULT 0",
            "flash_sale_ends_at": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in product_columns:
                database.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")
        database.execute("CREATE INDEX IF NOT EXISTS products_seller_id ON products(seller_id)")
        database.execute("CREATE INDEX IF NOT EXISTS product_interactions_user ON product_interactions(user_id, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS discount_coupons_user ON discount_coupons(user_id, status, expires_at)")
        database.executemany(
            """INSERT OR IGNORE INTO products
               (id, name, category, description, image, price, original_price, stock, badge)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            PRODUCTS,
        )
        database.commit()


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_error():
    token = session.get("csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if token and supplied and secrets.compare_digest(token, supplied):
        return None

    origin = request.headers.get("Origin") or request.headers.get("Referer", "")
    parsed_origin = urlparse(origin)
    request_origin = urlparse(request.host_url)
    if (
        parsed_origin.scheme in ("http", "https")
        and parsed_origin.scheme == request_origin.scheme
        and parsed_origin.netloc == request_origin.netloc
    ):
        return None
    return jsonify(error="La sesión expiró. Recarga la página e inténtalo de nuevo."), 403


def signed_in_user(database):
    user_id = session.get("user_id")
    if not user_id:
        return None
    return database.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()


def account_profile_payload(user):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "phone": user["phone"] or "",
        "address": user["address"] or "",
        "city": user["city"] or "",
        "department": user["department"] or "",
        "postal_code": user["postal_code"] or "",
        "delivery_notes": user["delivery_notes"] or "",
        "buyer_nequi_phone": user["buyer_nequi_phone"] or "",
        "preferred_payment_method": user["preferred_payment_method"] or "auto",
        "personalization_enabled": bool(user["personalization_enabled"]),
    }


def normalize_email(value):
    return str(value or "").strip().lower()


def generate_email_code(length=6):
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def email_code_expiry(minutes=15):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def send_email_message(recipient, subject, body):
    smtp_host = app.config.get("MAIL_SERVER", "").strip()
    sender = app.config.get("MAIL_DEFAULT_SENDER", "").strip()
    username = app.config.get("MAIL_USERNAME", "").strip()
    password = app.config.get("MAIL_PASSWORD", "").strip()
    if not smtp_host or not sender or not username or not password:
        app.logger.error("Configuración SMTP incompleta; no se pudo enviar el correo a %s", recipient)
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("PEMU", sender))
    message["To"] = recipient
    message.set_content(body)
    try:
        smtp_port = int(app.config.get("MAIL_PORT", 587) or 587)
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            if app.config.get("MAIL_USE_TLS"):
                server.starttls()
        server.login(username, password)
        server.send_message(message)
        server.quit()
        return True
    except (smtplib.SMTPException, TimeoutError, OSError) as error:
        app.logger.exception("No se pudo enviar correo a %s: %s", recipient, error)
        return False


def issue_verification_code(database, email):
    user = database.execute(
        "SELECT id, name, email FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if not user:
        return False
    code = generate_email_code()
    expires_at = email_code_expiry(minutes=15)
    database.execute(
        "UPDATE users SET verification_code = ?, verification_code_expires_at = ? WHERE id = ?",
        (code, expires_at, user["id"]),
    )
    database.commit()
    body = (
        f"Hola {user['name']},\n\n"
        f"Tu código de verificación para XrteShop es: {code}\n\n"
        "Ingresa este código para confirmar tu correo y empezar a usar tu cuenta.\n\n"
        "Si no solicitaste esta verificación, ignora este mensaje."
    )
    return send_email_message(email, "Verifica tu cuenta de XrteShop", body)


def issue_password_reset_code(database, email):
    user = database.execute(
        "SELECT id, name, email FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if not user:
        return False
    code = generate_email_code()
    expires_at = email_code_expiry(minutes=20)
    database.execute(
        "UPDATE users SET password_reset_code = ?, password_reset_expires_at = ? WHERE id = ?",
        (code, expires_at, user["id"]),
    )
    database.commit()
    body = (
        f"Hola {user['name']},\n\n"
        f"Tu código para recuperar tu contraseña en XrteShop es: {code}\n\n"
        "Usa este código para crear una nueva contraseña. El código expira en 20 minutos.\n\n"
        "Si tú no pediste este cambio, ignora este mensaje."
    )
    return send_email_message(email, "Recupera tu contraseña en XrteShop", body)


def code_is_valid(value, expires_at):
    if not value or not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)
    except ValueError:
        return False


def product_json(product):
    keys = product.keys()
    result = {key: product[key] for key in ("id", "name", "category", "description", "image", "price", "original_price", "stock", "badge")}
    sale_active = False
    sale_percent = product["flash_sale_percent"] if "flash_sale_percent" in keys else 0
    sale_ends_at = product["flash_sale_ends_at"] if "flash_sale_ends_at" in keys else None
    if sale_percent and sale_ends_at:
        sale_active = code_is_valid("active", sale_ends_at)
    base_price = result["price"]
    if sale_active:
        result["price"] = max(1, base_price * (100 - sale_percent) // 100)
        result["original_price"] = base_price
    seller_id = product["seller_id"] if "seller_id" in keys else None
    result.update(
        seller_id=seller_id,
        seller_name=product["seller_name"] if "seller_name" in keys else None,
        external_catalog=product["external_catalog"] if "external_catalog" in keys else None,
        source_url=product["source_url"] if "source_url" in keys else None,
        active=product["active"] if "active" in keys else 1,
        base_price=base_price,
        flash_sale_percent=sale_percent if sale_active else 0,
        flash_sale_ends_at=sale_ends_at if sale_active else None,
        low_stock=0 < result["stock"] <= 5,
        currency="COP",
        purchase_available=True,
    )
    return result


def external_category(category):
    slug = str(category).strip().lower().replace("_", "-")
    if any(term in slug for term in ("smartphone", "laptop", "tablet", "mobile", "computer", "electronics")):
        return "Electrónica"
    if any(term in slug for term in ("beauty", "fragrance", "skin-care", "makeup", "nail")):
        return "Cuidado personal"
    if any(term in slug for term in ("furniture", "home", "kitchen", "lighting", "decoration")):
        return "Hogar"
    if any(term in slug for term in ("shirt", "top", "dress", "shoe", "bag", "jewellery", "jewelry", "fashion")):
        return "Moda"
    if any(term in slug for term in ("watch", "accessor")):
        return "Accesorios"
    return "Otros"


def fetch_usd_cop_rate():
    now = time.monotonic()
    with _exchange_rate_lock:
        if _exchange_rate_cache["expires_at"] > now:
            return _exchange_rate_cache["rate"], _exchange_rate_cache["updated_at"], False
        try:
            http_request = Request(
                EXCHANGE_RATE_URL,
                headers={"Accept": "application/json", "User-Agent": "PemuCatalog/1.0"},
            )
            with urlopen(http_request, timeout=5) as response:
                payload = response.read(256 * 1024 + 1)
            if len(payload) > 256 * 1024:
                raise ValueError("La respuesta de la tasa de cambio excedió el tamaño permitido.")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("El proveedor devolvió una respuesta de tasa inválida.")
            rates = data.get("rates")
            if not isinstance(rates, dict):
                raise ValueError("El proveedor no incluyó tasas de cambio.")
            rate = Decimal(str(rates.get("COP", "")))
            if data.get("result") != "success" or not rate.is_finite() or rate <= 0:
                raise ValueError("El proveedor devolvió una tasa USD/COP inválida.")
            updated_at = str(data.get("time_last_update_utc") or datetime.now(timezone.utc).isoformat())
            _exchange_rate_cache.update(
                rate=rate,
                updated_at=updated_at,
                expires_at=now + EXCHANGE_RATE_TTL_SECONDS,
            )
            return rate, updated_at, False
        except (OSError, ValueError, InvalidOperation, json.JSONDecodeError):
            if _exchange_rate_cache["rate"] is not None:
                return (
                    _exchange_rate_cache["rate"],
                    _exchange_rate_cache["updated_at"],
                    True,
                )
            raise


def translate_external_descriptions(products):
    if not products:
        return 0
    descriptions = {
        product["id"]: product.get("description_en", product["description"])
        for product in products
        if product.get("description_en", product.get("description"))
    }
    if not descriptions:
        return 0

    with closing(connect_db()) as database:
        placeholders = ",".join("?" for _ in descriptions)
        rows = database.execute(
            f"""SELECT product_id, source_hash, description_es
                FROM external_product_translations
                WHERE product_id IN ({placeholders})""",
            list(descriptions),
        ).fetchall()
        cached = {
            row["product_id"]: (row["source_hash"], row["description_es"])
            for row in rows
        }

    pending = {}
    for product in products:
        source = descriptions.get(product["id"])
        if source is None:
            continue
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        product["description_en"] = source
        cached_translation = cached.get(product["id"])
        if cached_translation and cached_translation[0] == source_hash:
            product["description_es"] = cached_translation[1]
        else:
            pending[product["id"]] = (source_hash, source)

    failed = 0
    translated = {}

    def translate_one(source):
        if len(source) > 4500:
            raise ValueError("La descripción supera el límite de traducción.")
        query = urlencode({"q": source, "langpair": "en|es"})
        http_request = Request(
            f"{TRANSLATION_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": "PemuCatalog/1.0"},
        )
        with urlopen(http_request, timeout=5) as response:
            payload = response.read(128 * 1024 + 1)
        if len(payload) > 128 * 1024:
            raise ValueError("La respuesta de traducción excedió el tamaño permitido.")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("El proveedor devolvió una respuesta de traducción inválida.")
        response_data = data.get("responseData")
        if not isinstance(response_data, dict):
            raise ValueError("El proveedor no incluyó el texto traducido.")
        result = response_data.get("translatedText", "")
        if data.get("responseStatus") != 200 or not isinstance(result, str) or not result.strip():
            raise ValueError("El proveedor no devolvió una traducción válida.")
        return result.strip()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(translate_one, source): product_id
            for product_id, (_, source) in pending.items()
        }
        for future in as_completed(futures):
            product_id = futures[future]
            try:
                translated[product_id] = future.result()
            except (OSError, ValueError, json.JSONDecodeError) as error:
                app.logger.warning(
                    "Could not translate external product %s: %s",
                    product_id,
                    error,
                )
                failed += 1

    if translated:
        with closing(connect_db()) as database:
            database.executemany(
                """INSERT INTO external_product_translations
                   (product_id, source_hash, description_es, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(product_id) DO UPDATE SET
                       source_hash = excluded.source_hash,
                       description_es = excluded.description_es,
                       updated_at = CURRENT_TIMESTAMP""",
                [
                    (product_id, pending[product_id][0], description)
                    for product_id, description in translated.items()
                ],
            )
            database.commit()

    for product in products:
        product_id = product["id"]
        if product_id in translated:
            product["description_es"] = translated[product_id]
        elif product_id in pending:
            product["description_es"] = product["description_en"]
    return failed


def fetch_external_catalog():
    now = time.monotonic()
    with _external_catalog_lock:
        if _external_catalog_cache["expires_at"] > now:
            return _external_catalog_cache["products"]
        exchange_rate, exchange_rate_updated_at, rate_is_stale = fetch_usd_cop_rate()
        http_request = Request(
            EXTERNAL_CATALOG_URL,
            headers={"Accept": "application/json", "User-Agent": "PemuCatalog/1.0"},
        )
        with urlopen(http_request, timeout=5) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("El catálogo externo excedió el tamaño permitido.")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("La respuesta externa no contiene un objeto de catálogo.")
        raw_products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(raw_products, list):
            raise ValueError("La respuesta externa no contiene una lista de productos.")
        products = []
        for raw in raw_products:
            if not isinstance(raw, dict):
                continue
            try:
                remote_id = int(raw["id"])
                price_usd = Decimal(str(raw["price"]))
                discount_percent = max(0.0, min(Decimal(str(raw.get("discountPercentage", 0))), Decimal("90")))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            image = str(raw.get("thumbnail", ""))
            if not image.startswith("https://") or not price_usd.is_finite() or price_usd < 0:
                continue
            original_price_usd = (
                price_usd / (Decimal("1") - discount_percent / Decimal("100"))
                if discount_percent
                else price_usd
            )
            price_cop = int((price_usd * exchange_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            original_price_cop = int(
                (original_price_usd * exchange_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            original_price_cop = max(price_cop, original_price_cop)
            name = str(raw.get("title", "")).strip()
            description = str(raw.get("description", "")).strip()
            if not name:
                continue
            category = external_category(raw.get("category", ""))
            source_url = f"https://dummyjson.com/products/{remote_id}"
            products.append({
                "id": f"external-dummyjson-{remote_id}",
                "name": name,
                "category": category,
                "description": description,
                "description_en": description,
                "image": image,
                "price": price_cop,
                "original_price": original_price_cop,
                "stock": 0,
                "badge": "Catálogo de referencia",
                "seller_id": None,
                "seller_name": "Catálogo externo · Solo referencia",
                "external_catalog": "DummyJSON",
                "source_url": source_url,
                "active": 1,
                "base_price": price_cop,
                "flash_sale_percent": 0,
                "flash_sale_ends_at": None,
                "low_stock": False,
                "currency": "COP",
                "price_usd": float(price_usd),
                "exchange_rate": float(exchange_rate),
                "exchange_rate_updated_at": exchange_rate_updated_at,
                "exchange_rate_stale": rate_is_stale,
                "purchase_available": False,
                "brand": str(raw.get("brand") or "").strip(),
            })
        _external_catalog_cache["products"] = products
        _external_catalog_cache["expires_at"] = now + EXTERNAL_CATALOG_TTL_SECONDS
        return products


def product_matches_search(product, search):
    search = search.casefold()
    return search in " ".join((
        str(product["name"]),
        str(product["description"]),
        str(product["category"]),
        str(product.get("brand", "")),
    )).casefold()


def catalog_order_key(product, sort):
    if sort in {"price-asc", "price-desc"}:
        return (product["price"], product["name"].casefold())
    if sort == "name":
        return (product["name"].casefold(), product["id"])
    return (product.get("seller_id") is None, product["id"])


def read_user_cart(database, user_id):
    return {
        row["product_id"]: row["quantity"]
        for row in database.execute(
            "SELECT product_id, quantity FROM user_cart_items WHERE user_id = ?",
            (user_id,),
        )
    }


def write_user_cart(database, user_id, cart):
    database.execute("DELETE FROM user_cart_items WHERE user_id = ?", (user_id,))
    database.executemany(
        """INSERT INTO user_cart_items (user_id, product_id, quantity)
           VALUES (?, ?, ?)""",
        [(user_id, product_id, quantity) for product_id, quantity in cart.items()],
    )


def ensure_user_cart(database, user_id, legacy_cart=None):
    initialized = database.execute(
        "SELECT cart_initialized FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()["cart_initialized"]
    cart = read_user_cart(database, user_id)
    if not initialized:
        for product_id, quantity in (legacy_cart or {}).items():
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            product = database.execute(
                "SELECT stock FROM products WHERE id = ? AND active = 1",
                (product_id,),
            ).fetchone()
            if not product or product["stock"] <= 0:
                continue
            cart[product_id] = min(20, product["stock"], cart.get(product_id, 0) + quantity)
        write_user_cart(database, user_id, cart)
        database.execute("UPDATE users SET cart_initialized = 1 WHERE id = ?", (user_id,))
        database.commit()
    return cart


def current_cart(database, user=None):
    user = user or signed_in_user(database)
    if not user:
        return session.get("cart", {})
    cart = ensure_user_cart(database, user["id"], session.get("cart", {}))
    session["cart"] = cart
    return cart


def cart_json(database):
    cart = current_cart(database)
    items = []
    for product_id, quantity in cart.items():
        product = database.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if product:
            item = product_json(product)
            item["quantity"] = quantity
            item["line_total"] = item["price"] * quantity
            items.append(item)
    return {"items": items, "count": sum(item["quantity"] for item in items), "total": sum(item["line_total"] for item in items)}


def seller_access_token(database, seller_id):
    if seller_id is None:
        token = os.environ.get("PEMU_MP_PLATFORM_ACCESS_TOKEN", "").strip()
        if not token:
            raise PaymentGatewayError("La tienda aún no tiene configurada su cuenta de cobro.")
        return token
    seller = database.execute(
        """SELECT mp_user_id, mp_access_token, mp_refresh_token, mp_token_expires_at
           FROM users WHERE id = ?""",
        (seller_id,),
    ).fetchone()
    if not seller or not seller["mp_access_token"]:
        raise PaymentGatewayError("El vendedor debe conectar su cuenta de Mercado Pago antes de comprar.")
    token_expiry = seller["mp_token_expires_at"]
    if token_expiry and datetime.fromisoformat(token_expiry) <= datetime.now(timezone.utc) + timedelta(minutes=5):
        if not seller["mp_refresh_token"]:
            raise PaymentGatewayError("La conexión de Mercado Pago del vendedor venció. Debe volver a conectarla.")
        from payment_gateway import refresh_seller_token

        refreshed = refresh_seller_token(decrypt_token(seller["mp_refresh_token"]))
        access_token = refreshed.get("access_token")
        refresh_token = refreshed.get("refresh_token") or decrypt_token(seller["mp_refresh_token"])
        if not access_token:
            raise PaymentGatewayError("Mercado Pago no renovó la conexión del vendedor.")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(refreshed.get("expires_in", 0)))
        database.execute(
            """UPDATE users SET mp_access_token = ?, mp_refresh_token = ?, mp_token_expires_at = ?
               WHERE id = ?""",
            (encrypt_token(access_token), encrypt_token(refresh_token), expires_at.isoformat(), seller_id),
        )
        database.commit()
        return access_token
    return decrypt_token(seller["mp_access_token"])


def make_checkout_preference(database, split, items, buyer, return_url, webhook_url):
    access_token = seller_access_token(database, split["seller_id"])
    preference_items = [
        {
            "id": item["id"],
            "title": item["name"][:256],
            "quantity": item["quantity"],
            "unit_price": item["price"],
            "currency_id": "COP",
        }
        for item in items
    ]
    payload = {
        "items": preference_items,
        "payer": {"name": buyer["name"], "email": buyer["email"]},
        "external_reference": split["id"],
        "notification_url": webhook_url,
        "back_urls": {
            "success": return_url,
            "pending": return_url,
            "failure": return_url,
        },
        "auto_return": "approved",
        "expires": True,
        "expiration_date_to": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    }
    payload["marketplace_fee"] = round(split["amount"] * MARKETPLACE_FEE_PERCENT / 100, 2)
    return create_preference(access_token, payload)


def order_payment_status(database, order_id):
    splits = database.execute(
        "SELECT status FROM payment_splits WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    statuses = [split["status"] for split in splits]
    approved = sum(status == "approved" for status in statuses)
    if statuses and approved == len(statuses):
        payment_status, order_status = "approved", "Recibido"
    elif approved:
        payment_status, order_status = "partial", "Pago parcial - requiere revisión"
    elif statuses and all(status in ("rejected", "cancelled", "refunded") for status in statuses):
        payment_status, order_status = "failed", "Pago no completado"
    else:
        payment_status, order_status = "pending", "Pendiente de pago"
    database.execute(
        "UPDATE orders SET payment_status = ?, status = ? WHERE id = ?",
        (payment_status, order_status, order_id),
    )
    if payment_status == "approved":
        database.execute(
            "UPDATE discount_coupons SET status = 'redeemed' WHERE reserved_order_id = ?",
            (order_id,),
        )
    elif payment_status == "failed":
        release_order_stock(database, order_id)
        database.execute(
            """UPDATE discount_coupons SET status = 'issued', reserved_order_id = NULL
               WHERE reserved_order_id = ?""",
            (order_id,),
        )
    return payment_status, order_status


def release_order_stock(database, order_id):
    order = database.execute(
        "SELECT stock_released FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if not order or order["stock_released"]:
        return
    items = database.execute(
        "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    for item in items:
        database.execute(
            "UPDATE products SET stock = stock + ? WHERE id = ?",
            (item["quantity"], item["product_id"]),
        )
    database.execute("UPDATE orders SET stock_released = 1 WHERE id = ?", (order_id,))


def verify_mp_payment(database, split_id, payment_id):
    split = database.execute(
        """SELECT payment_splits.*, orders.user_id, users.email AS buyer_email
           FROM payment_splits
           JOIN orders ON orders.id = payment_splits.order_id
           JOIN users ON users.id = orders.user_id
           WHERE payment_splits.id = ?""",
        (split_id,),
    ).fetchone()
    if not split:
        raise PaymentGatewayError("No encontramos el pago pendiente.")
    if split["status"] == "approved" and split["payment_id"] == str(payment_id):
        return split["order_id"]
    access_token = seller_access_token(database, split["seller_id"])
    payment = get_payment(access_token, payment_id)
    if (
        payment.get("external_reference") != split_id
        or payment.get("currency_id") != "COP"
        or payment.get("status") != "approved"
        or round(float(payment.get("transaction_amount", 0))) != split["amount"]
    ):
        status = payment.get("status")
        mapped_status = status if status in ("rejected", "cancelled", "refunded") else "pending"
        database.execute(
            "UPDATE payment_splits SET status = ?, payment_id = ? WHERE id = ? AND status != 'approved'",
            (mapped_status, str(payment_id), split_id),
        )
        order_payment_status(database, split["order_id"])
        database.commit()
        raise PaymentGatewayError("Mercado Pago aún no confirma este pago como aprobado.")
    account_id = payment.get("collector_id")
    if split["seller_id"] is not None:
        seller = database.execute("SELECT mp_user_id FROM users WHERE id = ?", (split["seller_id"],)).fetchone()
        if not seller or str(account_id) != str(seller["mp_user_id"]):
            raise PaymentGatewayError("El pago no pertenece a la cuenta conectada del vendedor.")
    database.execute(
        "UPDATE payment_splits SET status = 'approved', payment_id = ? WHERE id = ?",
        (str(payment_id), split_id),
    )
    order_payment_status(database, split["order_id"])
    database.commit()
    return split["order_id"]


def expire_unpaid_orders(database):
    now = datetime.now(timezone.utc).isoformat()
    expired = database.execute(
        """SELECT id FROM orders WHERE payment_status = 'pending' AND expires_at < ?""",
        (now,),
    ).fetchall()
    for order in expired:
        approved = database.execute(
            "SELECT COUNT(*) FROM payment_splits WHERE order_id = ? AND status = 'approved'",
            (order["id"],),
        ).fetchone()[0]
        if approved:
            database.execute(
                """UPDATE orders SET payment_status = 'partial',
                   status = 'Pago parcial - requiere revisión' WHERE id = ?""",
                (order["id"],),
            )
            continue
        release_order_stock(database, order["id"])
        database.execute(
            "UPDATE orders SET payment_status = 'expired', status = 'Pago vencido' WHERE id = ?",
            (order["id"],),
        )
        database.execute(
            """UPDATE discount_coupons SET status = 'issued', reserved_order_id = NULL
               WHERE reserved_order_id = ?""",
            (order["id"],),
        )
    if expired:
        database.commit()


def valid_mp_webhook_signature(payment_id):
    secret = os.environ.get("PEMU_MP_WEBHOOK_SECRET", "").strip()
    signature = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    if not secret or not signature or not request_id:
        return False
    fields = dict(
        part.split("=", 1)
        for part in signature.split(",")
        if "=" in part
    )
    timestamp = fields.get("ts", "")
    supplied_signature = fields.get("v1", "")
    if not timestamp or not supplied_signature:
        return False
    manifest = f"id:{str(payment_id).lower()};request-id:{request_id};ts:{timestamp};"
    expected = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied_signature)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/producto/<product_id>")
def product_detail_page(product_id):
    return render_template("producto.html")


@app.get("/cuenta")
@app.get("/cuenta.html")
def account_page():
    return render_template("cuenta.html")


@app.get("/emprendedores")
def entrepreneur_page():
    return render_template("emprendedores.html")


@app.get("/index.html")
def legacy_home():
    return redirect("/")


@app.get("/Ofertas.html")
def legacy_offers():
    return redirect("/?ofertas=1")


@app.get("/moda.html")
def legacy_fashion():
    return redirect("/?categoria=Moda")


@app.get("/catalogo-externo-ejemplo.csv")
def external_catalog_template():
    return send_from_directory(ROOT / "data", "catalogo-externo-ejemplo.csv", as_attachment=True)


@app.get("/api/session")
def get_session():
    session.permanent = True
    with closing(connect_db()) as database:
        user_id = session.get("user_id")
        user = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
        return jsonify(
            csrf_token=csrf_token(),
            user=account_profile_payload(user) if user else None,
        )


@app.route("/api/account/profile", methods=["GET", "PUT"])
def account_profile():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para consultar tu perfil."), 401
        if request.method == "GET":
            user = database.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            return jsonify(profile=account_profile_payload(user))
        error = csrf_error()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        fields = {
            "name": str(data.get("name", "")).strip(),
            "phone": str(data.get("phone", "")).strip(),
            "address": str(data.get("address", "")).strip(),
            "city": str(data.get("city", "")).strip(),
            "department": str(data.get("department", "")).strip(),
            "postal_code": str(data.get("postal_code", "")).strip(),
            "delivery_notes": str(data.get("delivery_notes", "")).strip(),
            "buyer_nequi_phone": str(data.get("buyer_nequi_phone", "")).strip(),
        }
        payment_method = str(data.get("preferred_payment_method", "auto")).strip()
        personalization_enabled = data.get("personalization_enabled") is True
        if len(fields["name"]) < 2 or len(fields["name"]) > 80:
            return jsonify(error="El nombre debe tener entre 2 y 80 caracteres."), 400
        if fields["phone"] and not re.fullmatch(r"[0-9+() .-]{7,24}", fields["phone"]):
            return jsonify(error="Escribe un teléfono de contacto válido."), 400
        if fields["buyer_nequi_phone"] and not re.fullmatch(r"[0-9+() .-]{7,24}", fields["buyer_nequi_phone"]):
            return jsonify(error="Escribe un teléfono Nequi válido."), 400
        if any(len(fields[key]) > maximum for key, maximum in (
            ("address", 160), ("city", 160), ("department", 160),
            ("postal_code", 20), ("delivery_notes", 300),
        )):
            return jsonify(error="Uno de los datos del perfil supera la longitud permitida."), 400
        if payment_method not in {"auto", "nequi", "mercadopago"}:
            return jsonify(error="Selecciona un método de pago válido."), 400
        database.execute(
            """UPDATE users SET name = ?, phone = ?, address = ?, city = ?, department = ?,
               postal_code = ?, delivery_notes = ?, buyer_nequi_phone = ?,
               preferred_payment_method = ?, personalization_enabled = ? WHERE id = ?""",
            (
                fields["name"], fields["phone"], fields["address"], fields["city"],
                fields["department"], fields["postal_code"], fields["delivery_notes"],
                fields["buyer_nequi_phone"], payment_method, int(personalization_enabled), user["id"],
            ),
        )
        if not personalization_enabled:
            database.execute("DELETE FROM product_interactions WHERE user_id = ?", (user["id"],))
        database.commit()
        updated_user = database.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        return jsonify(profile=account_profile_payload(updated_user))


@app.get("/api/products")
def list_products():
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    offers_only = request.args.get("offers") == "1"
    query = """SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.active = 1 AND products.stock > 0
               AND (products.seller_id IS NULL OR users.mp_access_token IS NOT NULL OR users.nequi_phone IS NOT NULL)"""
    parameters = []
    if category:
        query += " AND products.category = ?"
        parameters.append(category)
    if search:
        query += " AND (products.name LIKE ? OR products.description LIKE ? OR products.category LIKE ?)"
        parameters.extend([f"%{search}%"] * 3)
    if offers_only:
        query += """ AND (products.original_price > products.price
                      OR (products.flash_sale_percent > 0
                          AND products.flash_sale_ends_at > ?))"""
        parameters.append(datetime.now(timezone.utc).isoformat())
    query += " ORDER BY products.seller_id IS NOT NULL, products.id"
    with closing(connect_db()) as database:
        products = database.execute(query, parameters).fetchall()
        categories = database.execute("SELECT DISTINCT category FROM products WHERE active = 1 AND stock > 0 ORDER BY category").fetchall()
        return jsonify(products=[product_json(product) for product in products], categories=[row[0] for row in categories])


@app.get("/api/productos")
def paginated_catalog():
    try:
        page = int(request.args.get("page", "1"))
        limit = int(request.args.get("limit", "12"))
    except ValueError:
        return jsonify(error="La página y el límite deben ser números enteros."), 400
    if page < 1 or page > 1_000_000_000 or not 1 <= limit <= 48:
        return jsonify(error="La página debe ser positiva y el límite debe estar entre 1 y 48."), 400

    category = request.args.get("category", request.args.get("categoria", "")).strip()[:80]
    search = request.args.get("q", request.args.get("buscar", "")).strip()[:120]
    sort = request.args.get("sort", "featured").strip()
    language = request.args.get("lang", "es").strip().lower()
    if language not in {"es", "en"}:
        return jsonify(error="El idioma solicitado no es válido."), 400
    if sort not in {"featured", "price-asc", "price-desc", "name"}:
        return jsonify(error="El orden solicitado no es válido."), 400
    offers_only = request.args.get("offers") == "1"
    local_query = """SELECT products.*, users.name AS seller_name FROM products
                     LEFT JOIN users ON users.id = products.seller_id
                     WHERE products.active = 1 AND products.stock > 0
                     AND (products.seller_id IS NULL OR users.mp_access_token IS NOT NULL
                          OR users.nequi_phone IS NOT NULL)"""
    parameters = []
    if category:
        local_query += " AND products.category = ?"
        parameters.append(category)
    if search:
        local_query += """ AND (products.name LIKE ? OR products.description LIKE ?
                            OR products.category LIKE ?)"""
        parameters.extend([f"%{search}%"] * 3)
    if offers_only:
        local_query += """ AND (products.original_price > products.price
                            OR (products.flash_sale_percent > 0
                                AND products.flash_sale_ends_at > ?))"""
        parameters.append(datetime.now(timezone.utc).isoformat())

    order_by = {
        "featured": "products.seller_id IS NOT NULL DESC, products.id ASC",
        "price-asc": """CASE WHEN products.flash_sale_percent > 0
                              AND products.flash_sale_ends_at > ?
                         THEN products.price * (100 - products.flash_sale_percent) / 100
                         ELSE products.price END ASC, products.seller_id IS NOT NULL DESC,
                         products.id ASC""",
        "price-desc": """CASE WHEN products.flash_sale_percent > 0
                               AND products.flash_sale_ends_at > ?
                          THEN products.price * (100 - products.flash_sale_percent) / 100
                          ELSE products.price END DESC, products.seller_id IS NOT NULL DESC,
                          products.id ASC""",
        "name": "products.name COLLATE NOCASE ASC, products.id ASC",
    }[sort]

    warning = None
    exchange_rate = None
    exchange_rate_updated_at = None
    exchange_rate_stale = False
    try:
        all_external_products = fetch_external_catalog()
        if all_external_products:
            exchange_rate = all_external_products[0]["exchange_rate"]
            exchange_rate_updated_at = all_external_products[0]["exchange_rate_updated_at"]
            exchange_rate_stale = all_external_products[0]["exchange_rate_stale"]
    except (OSError, ValueError, json.JSONDecodeError) as error:
        app.logger.warning("Could not load the external catalog: %s", error)
        all_external_products = []
        warning = (
            "The external catalog is unavailable right now; showing local products."
            if language == "en"
            else "El catálogo externo no está disponible en este momento; mostramos los productos locales."
        )
    external_categories = {item["category"] for item in all_external_products}
    external_products = all_external_products
    if category:
        external_products = [item for item in external_products if item["category"] == category]
    if search:
        external_products = [item for item in external_products if product_matches_search(item, search)]
    if offers_only:
        external_products = [item for item in external_products if item["original_price"] > item["price"]]
    external_products.sort(key=lambda item: catalog_order_key(item, sort), reverse=sort == "price-desc")

    offset = (page - 1) * limit
    with closing(connect_db()) as database:
        local_count = database.execute(
            f"SELECT COUNT(*) FROM ({local_query}) AS matching_products",
            parameters,
        ).fetchone()[0]
        local_categories = database.execute(
            """SELECT DISTINCT products.category FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.active = 1 AND products.stock > 0
                 AND (products.seller_id IS NULL OR users.mp_access_token IS NOT NULL
                      OR users.nequi_phone IS NOT NULL)
               ORDER BY products.category"""
        ).fetchall()

        local_offset = min(offset, local_count)
        local_page_count = min(limit, max(0, local_count - local_offset))
        page_products = []
        if local_page_count:
            order_parameters = parameters.copy()
            if sort in {"price-asc", "price-desc"}:
                order_parameters.append(datetime.now(timezone.utc).isoformat())
            rows = database.execute(
                f"{local_query} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*order_parameters, local_page_count, local_offset],
            ).fetchall()
            page_products.extend(product_json(row) for row in rows)

    remote_offset = max(0, offset - local_count)
    remaining = limit - len(page_products)
    external_page = [
        dict(product)
        for product in external_products[remote_offset:remote_offset + remaining]
    ]
    translation_failures = 0
    if external_page:
        for product in external_page:
            product["description_en"] = product["description"]
        if language == "es":
            translation_failures = translate_external_descriptions(external_page)
            for product in external_page:
                product["description"] = product["description_es"]
        else:
            for product in external_page:
                product["description"] = product["description_en"]
        page_products.extend(external_page)
    total = local_count + len(external_products)
    categories = sorted(
        {row["category"] for row in local_categories}
        | external_categories,
        key=str.casefold,
    )
    for product in page_products:
        product["language"] = language
        if product.get("external_catalog"):
            product["category_name"] = {
                "Accesorios": "Accessories",
                "Cuidado personal": "Personal care",
                "Electrónica": "Electronics",
                "Hogar": "Home",
                "Moda": "Fashion",
                "Otros": "Other",
            }.get(product["category"], product["category"]) if language == "en" else product["category"]
        product.update(
            nombre=product["name"],
            precio=product["price"],
            descripcion=product["description"],
            imagen=product["image"],
            categoria=product["category"],
            vendedor=product["seller_name"] or "XrteShop",
        )
    warning_messages = []
    if warning:
        warning_messages.append(warning)
    if exchange_rate_stale:
        warning_messages.append(
            "Using the last available USD/COP rate."
            if language == "en"
            else "Usamos la última tasa USD/COP disponible."
        )
    if translation_failures:
        warning_messages.append(
            "Some descriptions could not be translated; the original text is shown."
            if language == "en"
            else "Algunas descripciones no se pudieron traducir; se muestra el texto original."
        )
    return jsonify(
        products=page_products,
        categories=categories,
        page=page,
        limit=limit,
        total=total,
        total_pages=(total + limit - 1) // limit,
        warning=" ".join(warning_messages) or None,
        exchange_rate=exchange_rate,
        exchange_rate_updated_at=exchange_rate_updated_at,
    )


@app.get("/api/products/recommendations")
def recommended_products():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        query = """SELECT products.*, users.name AS seller_name FROM products
                   LEFT JOIN users ON users.id = products.seller_id
                   WHERE products.active = 1 AND products.stock > 0
                   AND (products.seller_id IS NULL OR users.mp_access_token IS NOT NULL OR users.nequi_phone IS NOT NULL)"""
        products = database.execute(query).fetchall()
        if not products:
            return jsonify(products=[])
        category_scores = {}
        product_scores = {}
        if user:
            interactions = database.execute(
                """SELECT products.id, products.category, product_interactions.action,
                          COUNT(*) AS count
                   FROM product_interactions
                   JOIN products ON products.id = product_interactions.product_id
                   WHERE product_interactions.user_id = ?
                     AND product_interactions.created_at >= datetime('now', '-90 days')
                   GROUP BY products.id, products.category, product_interactions.action""",
                (user["id"],),
            ).fetchall()
            weights = {"view": 1, "search_result": 2, "cart_add": 4}
            for interaction in interactions:
                score = weights[interaction["action"]] * interaction["count"]
                category_scores[interaction["category"]] = category_scores.get(interaction["category"], 0) + score
                product_scores[interaction["id"]] = product_scores.get(interaction["id"], 0) + score
        if not category_scores:
            popular = database.execute(
                """SELECT product_id, SUM(CASE action WHEN 'cart_add' THEN 4
                          WHEN 'search_result' THEN 2 ELSE 1 END) AS score
                   FROM product_interactions
                   WHERE created_at >= datetime('now', '-30 days')
                   GROUP BY product_id"""
            ).fetchall()
            product_scores = {row["product_id"]: row["score"] for row in popular}
        products = sorted(
            products,
            key=lambda product: (
                -category_scores.get(product["category"], 0),
                -product_scores.get(product["id"], 0),
                product["id"],
            ),
        )
        return jsonify(products=[product_json(product) for product in products[:12]])


@app.post("/api/product-interactions")
def record_product_interaction():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids")
    if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 5:
        return jsonify(error="Selecciona hasta cinco productos para actualizar tus recomendaciones."), 400
    action = str(data.get("action", "")).strip()
    if action not in {"view", "search_result"}:
        return jsonify(error="La interacción de producto no es válida."), 400
    normalized_ids = list(dict.fromkeys(str(product_id)[:100] for product_id in product_ids))
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para guardar tus recomendaciones."), 401
        settings = database.execute(
            "SELECT personalization_enabled FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if not settings["personalization_enabled"]:
            return jsonify(error="Activa las recomendaciones personalizadas en Mi cuenta para guardar esta actividad."), 403
        for product_id in normalized_ids:
            product = database.execute(
                "SELECT id FROM products WHERE id = ? AND active = 1 AND stock > 0",
                (product_id,),
            ).fetchone()
            if product:
                database.execute(
                    """INSERT INTO product_interactions (user_id, product_id, action)
                       SELECT ?, ?, ? WHERE NOT EXISTS (
                           SELECT 1 FROM product_interactions
                           WHERE user_id = ? AND product_id = ? AND action = ?
                             AND created_at >= datetime('now', '-1 hour')
                       )""",
                    (user["id"], product_id, action, user["id"], product_id, action),
                )
        database.commit()
    return jsonify(ok=True)


@app.get("/api/rewards")
def list_rewards():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para ver tus cupones."), 401
        coupons = database.execute(
            """SELECT code, discount_percent, max_discount, status, expires_at
               FROM discount_coupons WHERE user_id = ? ORDER BY id DESC LIMIT 20""",
            (user["id"],),
        ).fetchall()
        available = [
            dict(coupon)
            for coupon in coupons
            if coupon["status"] == "issued" and code_is_valid("active", coupon["expires_at"])
        ]
        recent = database.execute(
            """SELECT created_at FROM discount_coupons
               WHERE user_id = ? AND created_at >= datetime('now', '-7 days')
               ORDER BY id DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        return jsonify(coupons=available, can_spin=recent is None)


@app.post("/api/rewards/spin")
def spin_reward():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para jugar por un cupón."), 401
        verified = database.execute(
            "SELECT email_verified FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if not verified["email_verified"]:
            return jsonify(error="Verifica tu correo para participar en la ruleta semanal."), 403
        database.execute("BEGIN IMMEDIATE")
        recent = database.execute(
            """SELECT created_at FROM discount_coupons
               WHERE user_id = ? AND created_at >= datetime('now', '-7 days')
               ORDER BY id DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if recent:
            return jsonify(error="Ya participaste esta semana. Podrás volver a jugar cuando se cumplan 7 días."), 429
        code = f"PEMU-{secrets.token_hex(5).upper()}"
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        database.execute(
            """INSERT INTO discount_coupons
               (user_id, code, discount_percent, max_discount, expires_at)
               VALUES (?, ?, 10, 20000, ?)""",
            (user["id"], code, expires_at),
        )
        database.commit()
        return jsonify(
            coupon={
                "code": code,
                "discount_percent": 10,
                "max_discount": 20000,
                "expires_at": expires_at,
            },
            message="¡Ganaste! Copia el código y aplícalo en tu bolsa.",
        ), 201


@app.get("/api/coupons/quote")
def quote_coupon():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para aplicar un cupón."), 401
        code = request.args.get("code", "").strip().upper()
        coupon = database.execute(
            """SELECT discount_percent, max_discount, status, expires_at
               FROM discount_coupons WHERE user_id = ? AND code = ?""",
            (user["id"], code),
        ).fetchone()
        if not coupon or coupon["status"] != "issued" or not code_is_valid("active", coupon["expires_at"]):
            return jsonify(error="El cupón no existe, ya se usó o venció."), 400
        items = cart_json(database)["items"]
        subtotal = sum(item["line_total"] for item in items)
        if subtotal <= 0:
            return jsonify(error="Añade productos a tu bolsa antes de aplicar el cupón."), 400
        discount = min(subtotal * coupon["discount_percent"] // 100, coupon["max_discount"])
        return jsonify(code=code, subtotal=subtotal, discount=discount, total=subtotal - discount)


@app.get("/api/products/<product_id>")
def product_detail(product_id):
    with closing(connect_db()) as database:
        product = database.execute(
            """SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.id = ? AND products.active = 1
               AND (products.seller_id IS NULL OR users.mp_access_token IS NOT NULL OR users.nequi_phone IS NOT NULL)""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="Este producto ya no está disponible."), 404
        return jsonify(product=product_json(product))


def safe_https_url(value):
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and len(str(value)) <= 2048


def parse_external_catalog(upload):
    filename = secure_filename(upload.filename or "")
    extension = Path(filename).suffix.lower()
    if extension not in (".csv", ".json"):
        raise ValueError("El catálogo debe estar en formato CSV o JSON.")
    content = upload.stream.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("El archivo no puede superar 2 MB.")
    try:
        if extension == ".json":
            document = json.loads(content.decode("utf-8-sig"))
            rows = document.get("products") if isinstance(document, dict) else document
        else:
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error) as error:
        raise ValueError("No se pudo leer el archivo. Revisa su formato y codificación UTF-8.") from error
    if not isinstance(rows, list) or not rows or len(rows) > 50:
        raise ValueError("El catálogo debe incluir entre 1 y 50 productos.")
    catalog_name = Path(filename).stem[:80] or "Catálogo externo"
    products = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"El producto de la fila {index} no tiene un formato válido.")
        name = str(row.get("name", "")).strip()
        description = str(row.get("description", "Producto importado desde un catálogo externo.")).strip()
        category = str(row.get("category", "Otros")).strip()
        raw_price = str(row.get("price", "")).strip()
        source_url = str(row.get("source_url", "")).strip()
        image_url = str(row.get("image_url", "")).strip()
        if not name or len(name) > 100 or len(description) > 500:
            raise ValueError(f"Revisa el nombre o la descripción de la fila {index}.")
        if not re.fullmatch(r"[0-9]{1,10}", raw_price) or int(raw_price) < 1:
            raise ValueError(f"El precio de referencia de la fila {index} debe ser un entero en pesos colombianos.")
        if not safe_https_url(source_url) or (image_url and not safe_https_url(image_url)):
            raise ValueError(f"La fila {index} necesita enlaces HTTPS válidos para el origen y la imagen.")
        products.append({
            "name": name,
            "description": description or "Producto importado desde un catálogo externo.",
            "category": category if category in CATEGORIES else "Otros",
            "reference_price": int(raw_price),
            "source_url": source_url,
            "image_url": image_url,
            "external_catalog": catalog_name,
        })
    return products


def validate_seller_listing(data):
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    category = str(data.get("category", "Otros")).strip() or "Otros"
    if len(name) < 3 or len(name) > 100:
        raise ValueError("El nombre debe tener entre 3 y 100 caracteres.")
    if len(description) > 500:
        raise ValueError("La descripción no puede superar los 500 caracteres.")
    if not description:
        description = "Producto disponible en XrteShop."
    if category not in CATEGORIES:
        raise ValueError("Selecciona una categoría válida.")
    try:
        price = int(data.get("price", ""))
        stock = int(data.get("stock", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("El precio y las existencias deben ser números enteros.") from error
    if not 1 <= price <= 10_000_000_000:
        raise ValueError("El precio debe ser mayor a cero y no superar $10.000.000.000.")
    if not 1 <= stock <= 1000:
        raise ValueError("Las existencias deben estar entre 1 y 1.000 unidades.")
    return name, category, description, price, stock


def current_seller(database):
    user = signed_in_user(database)
    if not user:
        return None, (jsonify(error="Inicia sesión para administrar tus publicaciones."), 401)
    return user, None


@app.get("/api/seller/payout-account")
def payout_account_status():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        account = database.execute(
            "SELECT mp_user_id, mp_access_token, nequi_phone FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        return jsonify(
            connected=bool(account["mp_access_token"]),
            account_id=account["mp_user_id"] if account["mp_access_token"] else None,
            nequi_phone=account["nequi_phone"],
        )


@app.put("/api/seller/nequi")
def update_seller_nequi():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    phone = re.sub(r"[\s().-]", "", str(data.get("phone", "")))
    if phone.startswith("+57"):
        phone = phone[3:]
    elif phone.startswith("57") and len(phone) == 12:
        phone = phone[2:]
    if phone and not re.fullmatch(r"3\d{9}", phone):
        return jsonify(error="Escribe un número celular colombiano de 10 dígitos registrado en Nequi."), 400
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        database.execute("UPDATE users SET nequi_phone = ? WHERE id = ?", (phone or None, user["id"]))
        database.commit()
        return jsonify(ok=True, nequi_phone=phone or None)


@app.get("/mercadopago/connect")
def connect_mercadopago():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return redirect("/cuenta?next=%2Femprendedores")
    state = secrets.token_urlsafe(32)
    session["mp_oauth_state"] = state
    session["mp_oauth_user_id"] = user["id"]
    try:
        return redirect(authorization_url(state))
    except PaymentGatewayError as error:
        app.logger.warning("Mercado Pago OAuth setup failed: %s", error)
        return redirect("/emprendedores?mp=configuration-error")


@app.get("/mercadopago/oauth/callback")
def mercadopago_oauth_callback():
    supplied_state = request.args.get("state", "")
    expected_state = session.pop("mp_oauth_state", "")
    user_id = session.pop("mp_oauth_user_id", None)
    if (
        not supplied_state
        or not expected_state
        or not secrets.compare_digest(supplied_state, expected_state)
        or not user_id
    ):
        return redirect("/emprendedores?mp=authorization-error")
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/emprendedores?mp=authorization-cancelled")
    try:
        tokens = exchange_authorization_code(request.args["code"])
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        account_id = tokens.get("user_id")
        expires_in = int(tokens.get("expires_in", 0))
        if not access_token or not refresh_token or not account_id or expires_in <= 0:
            raise PaymentGatewayError("Mercado Pago devolvió datos de autorización incompletos.")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        with closing(connect_db()) as database:
            database.execute(
                """UPDATE users SET mp_user_id = ?, mp_access_token = ?, mp_refresh_token = ?,
                   mp_token_expires_at = ? WHERE id = ?""",
                (
                    str(account_id),
                    encrypt_token(access_token),
                    encrypt_token(refresh_token),
                    expires_at.isoformat(),
                    user_id,
                ),
            )
            database.commit()
    except (PaymentGatewayError, ValueError) as error:
        app.logger.warning("Mercado Pago OAuth exchange failed: %s", error)
        return redirect("/emprendedores?mp=authorization-error")
    return redirect("/emprendedores?mp=connected")


@app.post("/api/seller/catalog/preview")
def preview_external_catalog():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        _, error = current_seller(database)
        if error:
            return error
    upload = request.files.get("catalog_file")
    if not upload or not upload.filename:
        return jsonify(error="Selecciona un archivo CSV o JSON."), 400
    try:
        products = parse_external_catalog(upload)
    except ValueError as exception:
        return jsonify(error=str(exception)), 400
    return jsonify(products=products)


@app.get("/api/seller/products")
def seller_products():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        products = database.execute(
            """SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.seller_id = ? ORDER BY products.id DESC""",
            (user["id"],),
        ).fetchall()
        return jsonify(products=[product_json(product) for product in products])


@app.post("/api/seller/products")
def create_seller_product():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        account = database.execute(
            "SELECT mp_access_token, nequi_phone FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if not account["mp_access_token"] and not account["nequi_phone"]:
            return jsonify(error="Registra tu número Nequi o conecta Mercado Pago para publicar y recibir pagos."), 409
        data = request.get_json(silent=True) if request.is_json else request.form
        data = data or {}
        try:
            name, category, description, price, stock = validate_seller_listing(data)
        except ValueError as exception:
            return jsonify(error=str(exception)), 400
        external_catalog = str(data.get("external_catalog", "")).strip()[:80] or None
        source_url = str(data.get("source_url", "")).strip() or None
        image_url = str(data.get("image_url", "")).strip()
        if source_url and not safe_https_url(source_url):
            return jsonify(error="El enlace del catálogo debe usar HTTPS."), 400
        if image_url and not safe_https_url(image_url):
            return jsonify(error="La imagen externa debe usar HTTPS."), 400
        if request.is_json:
            if not external_catalog or not source_url:
                return jsonify(error="La publicación importada necesita su catálogo y enlace de origen."), 400
            image = image_url or "imagenes/black-wireless-headphones-isolated-on-transparent-background-ai-generated-png.webp"
        else:
            upload = request.files.get("image")
            if not upload or not upload.filename:
                return jsonify(error="Añade una foto del producto en PNG, JPG o WEBP."), 400
            signature = upload.stream.read(12)
            if signature.startswith(b"\x89PNG\r\n\x1a\n"):
                extension = ".png"
            elif signature.startswith(b"\xff\xd8\xff"):
                extension = ".jpg"
            elif signature[:4] == b"RIFF" and signature[8:12] == b"WEBP":
                extension = ".webp"
            else:
                return jsonify(error="La imagen debe ser PNG, JPG o WEBP válido."), 400
            image_name = f"{uuid.uuid4().hex}{extension}"
            image_directory = ROOT / "static" / "images" / "vendedores"
            image_directory.mkdir(parents=True, exist_ok=True)
            upload.stream.seek(0)
            upload.save(image_directory / image_name)
            image = f"imagenes/vendedores/{image_name}"
        product_id = f"seller-{uuid.uuid4().hex}"
        database.execute(
            """INSERT INTO products
               (id, name, category, description, image, price, original_price, stock, badge, seller_id, external_catalog, source_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, name, category, description, image, price, price, stock,
             "Catálogo externo" if external_catalog else "Emprendedor", user["id"], external_catalog, source_url),
        )
        database.commit()
        product = database.execute(
            "SELECT products.*, users.name AS seller_name FROM products LEFT JOIN users ON users.id = products.seller_id WHERE products.id = ?",
            (product_id,),
        ).fetchone()
        return jsonify(product=product_json(product)), 201


@app.patch("/api/seller/products/<product_id>")
def update_seller_product(product_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    updates = []
    parameters = []
    for field, minimum, maximum in (("price", 1, 10_000_000_000), ("stock", 0, 1000)):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return jsonify(error="Precio y existencias deben ser números enteros."), 400
            if not minimum <= value <= maximum:
                return jsonify(error="El precio o las existencias están fuera del rango permitido."), 400
            updates.append(f"{field} = ?")
            parameters.append(value)
            if field == "price":
                updates.append("original_price = ?")
                parameters.append(value)
    if "sale_percent" in data:
        try:
            sale_percent = int(data["sale_percent"])
        except (TypeError, ValueError):
            return jsonify(error="El descuento de la oferta debe ser un número entero."), 400
        if sale_percent not in (0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70):
            return jsonify(error="El descuento debe ser una opción válida entre 0% y 70%."), 400
        updates.append("flash_sale_percent = ?")
        parameters.append(sale_percent)
        if sale_percent:
            try:
                sale_hours = int(data.get("sale_hours", 24))
            except (TypeError, ValueError):
                return jsonify(error="La duración de la oferta no es válida."), 400
            if sale_hours not in (1, 6, 12, 24, 48, 72):
                return jsonify(error="La duración debe ser de 1, 6, 12, 24, 48 o 72 horas."), 400
            updates.append("flash_sale_ends_at = ?")
            parameters.append((datetime.now(timezone.utc) + timedelta(hours=sale_hours)).isoformat())
        else:
            updates.append("flash_sale_ends_at = NULL")
    if not updates:
        return jsonify(error="Indica un precio o una cantidad para actualizar."), 400
    updates.append("active = 1")
    parameters.append(product_id)
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        parameters.append(user["id"])
        cursor = database.execute(
            f"UPDATE products SET {', '.join(updates)} WHERE id = ? AND seller_id = ?",
            parameters,
        )
        if not cursor.rowcount:
            return jsonify(error="No encontramos una publicación tuya con ese identificador."), 404
        database.commit()
        return jsonify(ok=True)


@app.delete("/api/seller/products/<product_id>")
def unlist_seller_product(product_id):
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        cursor = database.execute(
            "UPDATE products SET active = 0, stock = 0 WHERE id = ? AND seller_id = ?",
            (product_id, user["id"]),
        )
        if not cursor.rowcount:
            return jsonify(error="No encontramos una publicación tuya con ese identificador."), 404
        database.commit()
        return jsonify(ok=True)


@app.get("/api/cart")
def get_cart():
    with closing(connect_db()) as database:
        return jsonify(cart_json(database))


@app.post("/api/cart")
def update_cart():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id", ""))
    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        return jsonify(error="La cantidad no es válida."), 400
    if quantity < 0 or quantity > 20:
        return jsonify(error="Puedes elegir entre 1 y 20 unidades."), 400
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión o crea una cuenta para añadir productos a la bolsa."), 401
        product = database.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            return jsonify(error="El producto ya no está disponible."), 404
        cart = dict(current_cart(database, user))
        if quantity == 0:
            cart.pop(product_id, None)
        elif quantity > product["stock"]:
            return jsonify(error=f"Solo quedan {product['stock']} unidades."), 409
        else:
            cart[product_id] = quantity
            user = signed_in_user(database)
            if user:
                settings = database.execute(
                    "SELECT personalization_enabled FROM users WHERE id = ?",
                    (user["id"],),
                ).fetchone()
                if settings["personalization_enabled"]:
                    database.execute(
                        """INSERT INTO product_interactions (user_id, product_id, action)
                           SELECT ?, ?, 'cart_add' WHERE NOT EXISTS (
                               SELECT 1 FROM product_interactions
                               WHERE user_id = ? AND product_id = ? AND action = 'cart_add'
                                 AND created_at >= datetime('now', '-1 hour')
                           )""",
                        (user["id"], product_id, user["id"], product_id),
                    )
        write_user_cart(database, user["id"], cart)
        database.commit()
        session["cart"] = cart
        return jsonify(cart_json(database))


def allocate_amount(amount, weights):
    total_weight = sum(weights)
    if not amount or not total_weight:
        return [0] * len(weights)
    allocations = [amount * weight // total_weight for weight in weights]
    remainder = amount - sum(allocations)
    remainders = sorted(
        range(len(weights)),
        key=lambda index: (amount * weights[index] % total_weight, -index),
        reverse=True,
    )
    for index in remainders[:remainder]:
        allocations[index] += 1
    return allocations


def discounted_item_lines(items, discount):
    shares = allocate_amount(discount, [item["price"] * item["quantity"] for item in items])
    lines = []
    for item, share in zip(items, shares):
        per_unit, extra_units = divmod(share, item["quantity"])
        discounted_units = item["quantity"] - extra_units
        if discounted_units:
            lines.append({
                **item,
                "price": item["price"] - per_unit,
                "quantity": discounted_units,
            })
        if extra_units:
            lines.append({
                **item,
                "price": item["price"] - per_unit - 1,
                "quantity": extra_units,
            })
    return lines


@app.post("/api/cart/clear")
def clear_cart():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para administrar tu bolsa."), 401
        write_user_cart(database, user["id"], {})
        database.execute("UPDATE users SET cart_initialized = 1 WHERE id = ?", (user["id"],))
        database.commit()
        session["cart"] = {}
    return jsonify(items=[], count=0, total=0)



@app.post("/api/register")
def register():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    if len(name) < 2 or len(name) > 80:
        return jsonify(error="Escribe tu nombre (entre 2 y 80 caracteres)."), 400
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if len(password) < 4 or len(password) > 128:
        return jsonify(error="La contraseña debe tener entre 4 y 128 caracteres."), 400
    with closing(connect_db()) as database:
        existing = database.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if existing and existing["email_verified"] == 1:
            return jsonify(error="Ya existe una cuenta con ese correo."), 409
        if existing and existing["email_verified"] == 0:
            if not issue_verification_code(database, email):
                return jsonify(error="No pudimos enviar el código de verificación a tu correo. Inténtalo de nuevo."), 500
            return jsonify(message="Cuenta creada. Revisa tu correo para verificar tu cuenta.", email=email, verification_required=True), 201
        try:
            cursor = database.execute(
                "INSERT INTO users (name, email, password_hash, email_verified, verification_code, verification_code_expires_at) VALUES (?, ?, ?, 0, ?, ?)",
                (name, email, generate_password_hash(password), generate_email_code(), email_code_expiry(minutes=15)),
            )
            database.commit()
        except sqlite3.IntegrityError:
            return jsonify(error="Ya existe una cuenta con ese correo."), 409
        if not issue_verification_code(database, email):
            database.execute("DELETE FROM users WHERE id = ?", (cursor.lastrowid,))
            database.commit()
            return jsonify(error="No pudimos enviar el código de verificación a tu correo. Inténtalo de nuevo."), 500
        return jsonify(message="Cuenta creada. Revisa tu correo para verificar tu cuenta.", email=email, verification_required=True), 201


@app.post("/api/login")
def login():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    with closing(connect_db()) as database:
        user = database.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify(error="Correo o contraseña incorrectos."), 401
        if user["email_verified"] != 1:
            return jsonify(error="Verifica tu correo electrónico antes de iniciar sesión."), 401
        legacy_cart = session.get("cart", {}) if session.get("user_id") in (None, user["id"]) else {}
        saved_cart = ensure_user_cart(database, user["id"], legacy_cart)
        session.clear()
        session["user_id"] = user["id"]
        session["cart"] = saved_cart
        session.permanent = True
        csrf_token()
        return jsonify(user={"name": user["name"], "email": user["email"]})


@app.post("/api/resend-verification")
def resend_verification():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    with closing(connect_db()) as database:
        user = database.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            return jsonify(message="Si la dirección está registrada, te enviaremos un nuevo código."), 200
        if user["email_verified"] == 1:
            return jsonify(message="Este correo ya está verificado."), 200
        if not issue_verification_code(database, email):
            return jsonify(error="No pudimos enviar el código de verificación. Inténtalo de nuevo."), 500
        return jsonify(message="Te enviamos un nuevo código de verificación."), 200


@app.post("/api/verify-email")
def verify_email():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    code = str(data.get("code", "")).strip()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if len(code) != 6 or not code.isdigit():
        return jsonify(error="El código de verificación debe tener 6 dígitos."), 400
    with closing(connect_db()) as database:
        user = database.execute(
            "SELECT id, email_verified, verification_code, verification_code_expires_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if not user:
            return jsonify(error="No encontramos una cuenta con ese correo."), 404
        if user["email_verified"] == 1:
            return jsonify(message="Este correo ya está verificado."), 200
        if not code_is_valid(user["verification_code"], user["verification_code_expires_at"]):
            return jsonify(error="El código de verificación expiró o es incorrecto."), 400
        if not secrets.compare_digest(str(user["verification_code"]), code):
            return jsonify(error="El código de verificación es incorrecto."), 400
        database.execute(
            "UPDATE users SET email_verified = 1, verification_code = NULL, verification_code_expires_at = NULL WHERE id = ?",
            (user["id"],),
        )
        database.commit()
        return jsonify(message="Correo verificado correctamente."), 200


@app.post("/api/forgot-password")
def forgot_password():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    with closing(connect_db()) as database:
        user = database.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            return jsonify(message="Si la dirección está registrada, te enviaremos un código para recuperar la contraseña."), 200
        if not issue_password_reset_code(database, email):
            return jsonify(error="No pudimos enviar el código para recuperar la contraseña. Inténtalo de nuevo."), 500
    return jsonify(message="Si la dirección está registrada, te enviamos un código para recuperar la contraseña."), 200


@app.post("/api/reset-password")
def reset_password():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    code = str(data.get("code", "")).strip()
    password = str(data.get("password", data.get("new_password", "")))
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if len(code) != 6 or not code.isdigit():
        return jsonify(error="El código de recuperación debe tener 6 dígitos."), 400
    if len(password) < 4 or len(password) > 128:
        return jsonify(error="La nueva contraseña debe tener entre 4 y 128 caracteres."), 400
    with closing(connect_db()) as database:
        user = database.execute(
            "SELECT id, password_reset_code, password_reset_expires_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if not user:
            return jsonify(error="No encontramos una cuenta con ese correo."), 404
        if not code_is_valid(user["password_reset_code"], user["password_reset_expires_at"]):
            return jsonify(error="El código de recuperación expiró o es incorrecto."), 400
        if not secrets.compare_digest(str(user["password_reset_code"]), code):
            return jsonify(error="El código de recuperación es incorrecto."), 400
        database.execute(
            "UPDATE users SET password_hash = ?, password_reset_code = NULL, password_reset_expires_at = NULL WHERE id = ?",
            (generate_password_hash(password), user["id"]),
        )
        database.commit()
        return jsonify(message="Tu contraseña se actualizó correctamente."), 200


@app.post("/api/logout")
def logout():
    error = csrf_error()
    if error:
        return error
    session.clear()
    return jsonify(ok=True)


@app.get("/api/orders")
def list_orders():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para consultar tus pedidos."), 401
        orders = database.execute(
            """SELECT id, total, status, payment_status, created_at
               FROM orders WHERE user_id = ? ORDER BY id DESC""",
            (user["id"],),
        ).fetchall()
        result = []
        for order in orders:
            items = database.execute(
                """SELECT order_items.product_id, products.seller_id, order_items.product_name,
                          order_items.quantity, order_items.unit_price
                   FROM order_items JOIN products ON products.id = order_items.product_id
                   WHERE order_items.order_id = ? ORDER BY order_items.id""",
                (order["id"],),
            ).fetchall()
            shipments = database.execute(
                """SELECT id AS split_id, seller_id, shipping_status, shipping_carrier,
                          tracking_number, estimated_delivery_at, shipped_at, delivered_at
                   FROM payment_splits WHERE order_id = ? ORDER BY created_at""",
                (order["id"],),
            ).fetchall()
            returns = database.execute(
                """SELECT product_id, status, free_return, reason, created_at
                   FROM return_requests WHERE order_id = ? ORDER BY id""",
                (order["id"],),
            ).fetchall()
            result.append({
                "id": order["id"], "total": order["total"], "status": order["status"],
                "payment_status": order["payment_status"], "created_at": order["created_at"],
                "items": [dict(item) for item in items],
                "shipments": [dict(shipment) for shipment in shipments],
                "returns": [dict(return_request) for return_request in returns],
            })
        return jsonify(orders=result)


@app.post("/api/orders/<int:order_id>/returns")
def request_order_return(order_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id", "")).strip()
    reason = str(data.get("reason", "")).strip()
    details = str(data.get("details", "")).strip()
    allowed_reasons = {"damaged", "incomplete", "not_as_described", "other"}
    if reason not in allowed_reasons or len(details) > 1000:
        return jsonify(error="Selecciona un motivo válido y limita los detalles a 1.000 caracteres."), 400
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para solicitar una devolución."), 401
        order = database.execute(
            "SELECT id FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user["id"]),
        ).fetchone()
        if not order:
            return jsonify(error="No encontramos ese pedido en tu cuenta."), 404
        item = database.execute(
            """SELECT products.seller_id FROM order_items
               JOIN products ON products.id = order_items.product_id
               WHERE order_items.order_id = ? AND order_items.product_id = ?""",
            (order_id, product_id),
        ).fetchone()
        if not item:
            return jsonify(error="Ese producto no pertenece al pedido."), 400
        split = database.execute(
            """SELECT payment_splits.id, payment_splits.status, payment_splits.delivered_at
               FROM payment_splits WHERE payment_splits.order_id = ?
                 AND payment_splits.seller_id IS ?""",
            (order_id, item["seller_id"]),
        ).fetchone()
        if not split or split["status"] != "approved" or not split["delivered_at"]:
            return jsonify(error="Podrás solicitar la devolución cuando el pedido figure como entregado y pagado."), 409
        try:
            delivered_at = datetime.fromisoformat(split["delivered_at"])
        except ValueError:
            return jsonify(error="No se pudo verificar la fecha de entrega. Contacta al equipo de XrteShop."), 409
        if datetime.now(timezone.utc) - delivered_at > timedelta(days=90):
            return jsonify(error="El plazo de devolución de 90 días desde la entrega ya venció."), 409
        existing = database.execute(
            """SELECT id FROM return_requests
               WHERE order_id = ? AND product_id = ?""",
            (order_id, product_id),
        ).fetchone()
        if existing:
            return jsonify(error="Ya existe una solicitud para este producto de ese pedido."), 409
        free_return_used = database.execute(
            "SELECT 1 FROM return_requests WHERE order_id = ? AND free_return = 1 LIMIT 1",
            (order_id,),
        ).fetchone()
        database.execute(
            """INSERT INTO return_requests
               (order_id, split_id, user_id, product_id, reason, details, free_return)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, split["id"], user["id"], product_id, reason, details,
                int(free_return_used is None),
            ),
        )
        database.commit()
        return jsonify(
            ok=True,
            status="received",
            free_return=free_return_used is None,
            message="Recibimos tu solicitud. El equipo de XrteShop revisará el caso; esta solicitud no confirma que el reembolso ya se haya procesado.",
        ), 201


@app.get("/api/seller/orders")
def seller_orders():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        splits = database.execute(
            """SELECT payment_splits.id AS split_id, payment_splits.order_id,
                      payment_splits.amount, payment_splits.shipping_status,
                      payment_splits.shipping_carrier, payment_splits.tracking_number,
                      payment_splits.estimated_delivery_at, payment_splits.shipped_at,
                      payment_splits.delivered_at, orders.customer_name,
                      orders.phone, orders.address, orders.city, orders.department,
                      orders.created_at
               FROM payment_splits JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.seller_id = ? AND payment_splits.status = 'approved'
               ORDER BY orders.created_at DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
        orders = []
        for split in splits:
            items = database.execute(
                """SELECT product_id, product_name, quantity FROM order_items
                   JOIN products ON products.id = order_items.product_id
                   WHERE order_items.order_id = ? AND products.seller_id = ?
                   ORDER BY order_items.id""",
                (split["order_id"], user["id"]),
            ).fetchall()
            orders.append({**dict(split), "items": [dict(item) for item in items]})
        return jsonify(orders=orders)


@app.patch("/api/seller/orders/<split_id>/tracking")
def update_seller_tracking(split_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip()
    carrier = str(data.get("carrier", "")).strip()
    tracking_number = str(data.get("tracking_number", "")).strip()
    estimated_date = str(data.get("estimated_delivery_at", "")).strip()
    statuses = ("preparing", "shipped", "in_transit", "delivered")
    if status not in statuses or len(carrier) > 80 or len(tracking_number) > 120:
        return jsonify(error="Revisa el estado, la transportadora y el número de guía."), 400
    if status in {"shipped", "in_transit"} and (not carrier or not tracking_number):
        return jsonify(error="Indica la transportadora y el número de guía para actualizar el rastreo."), 400
    if estimated_date:
        try:
            datetime.strptime(estimated_date, "%Y-%m-%d")
        except ValueError:
            return jsonify(error="La fecha estimada no es válida."), 400
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        split = database.execute(
            """SELECT id, shipping_status, delivered_at FROM payment_splits
               WHERE id = ? AND seller_id = ? AND status = 'approved'""",
            (split_id, user["id"]),
        ).fetchone()
        if not split:
            return jsonify(error="No encontramos un envío pagado de tu tienda."), 404
        if split["delivered_at"]:
            return jsonify(error="El envío ya fue marcado como entregado y no se puede cambiar."), 409
        if statuses.index(status) < statuses.index(split["shipping_status"]):
            return jsonify(error="El estado del envío no puede retroceder."), 409
        now = datetime.now(timezone.utc).isoformat()
        database.execute(
            """UPDATE payment_splits SET shipping_status = ?, shipping_carrier = ?,
               tracking_number = ?, estimated_delivery_at = ?,
               shipped_at = CASE WHEN ? = 'preparing' THEN shipped_at
                                 ELSE COALESCE(shipped_at, ?) END,
               delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END
               WHERE id = ?""",
            (
                status, carrier or None, tracking_number or None, estimated_date or None,
                status, now, status, now, split_id,
            ),
        )
        database.commit()
        return jsonify(ok=True, shipping_status=status)


@app.get("/api/seller/returns")
def seller_return_requests():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        returns = database.execute(
            """SELECT return_requests.id, return_requests.order_id,
                      return_requests.product_id, return_requests.reason,
                      return_requests.details, return_requests.status,
                      return_requests.free_return, return_requests.created_at,
                      orders.customer_name, orders.phone, products.name AS product_name
               FROM return_requests
               JOIN payment_splits ON payment_splits.id = return_requests.split_id
               JOIN orders ON orders.id = return_requests.order_id
               JOIN products ON products.id = return_requests.product_id
               WHERE payment_splits.seller_id = ?
               ORDER BY return_requests.id DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
        return jsonify(requests=[dict(item) for item in returns])


@app.patch("/api/seller/returns/<int:return_id>")
def update_seller_return_request(return_id):
    error = csrf_error()
    if error:
        return error
    status = str((request.get_json(silent=True) or {}).get("status", "")).strip()
    if status not in {"reviewing", "accepted", "rejected"}:
        return jsonify(error="Elige un estado de revisión válido."), 400
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        cursor = database.execute(
            """UPDATE return_requests SET status = ?
               WHERE id = ? AND split_id IN (
                   SELECT id FROM payment_splits WHERE seller_id = ?
               ) AND status IN ('received', 'reviewing')""",
            (status, return_id, user["id"]),
        )
        if not cursor.rowcount:
            return jsonify(error="No encontramos una solicitud abierta de tu tienda."), 404
        database.commit()
        return jsonify(ok=True, status=status)


@app.get("/api/seller/nequi-orders")
def seller_nequi_orders():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        splits = database.execute(
            """SELECT payment_splits.id AS split_id, payment_splits.amount,
                      payment_splits.created_at, orders.id AS order_id,
                      orders.customer_name, orders.phone AS customer_phone,
                      orders.address, orders.city, orders.department
               FROM payment_splits
               JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.seller_id = ?
                 AND payment_splits.payment_method = 'nequi'
                 AND payment_splits.status = 'pending'
               ORDER BY orders.created_at DESC""",
            (user["id"],),
        ).fetchall()
        orders = []
        for split in splits:
            items = database.execute(
                """SELECT product_name, quantity FROM order_items
                   JOIN products ON products.id = order_items.product_id
                   WHERE order_items.order_id = ? AND products.seller_id = ?
                   ORDER BY order_items.id""",
                (split["order_id"], user["id"]),
            ).fetchall()
            orders.append({
                **dict(split),
                "commission_due": round(split["amount"] * MARKETPLACE_FEE_PERCENT / 100),
                "items": [dict(item) for item in items],
            })
        due = database.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM seller_commissions
               WHERE seller_id = ? AND status = 'pending'""",
            (user["id"],),
        ).fetchone()[0]
        return jsonify(orders=orders, commission_due=due)


@app.post("/api/seller/nequi-orders/<split_id>/confirm")
def confirm_nequi_payment(split_id):
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        split = database.execute(
            """SELECT id, order_id, amount FROM payment_splits
               WHERE id = ? AND seller_id = ? AND payment_method = 'nequi'
                 AND status = 'pending'""",
            (split_id, user["id"]),
        ).fetchone()
        if not split:
            return jsonify(error="No encontramos un pago Nequi pendiente para confirmar."), 404
        database.execute(
            "UPDATE payment_splits SET status = 'approved' WHERE id = ?",
            (split_id,),
        )
        commission_due = round(split["amount"] * MARKETPLACE_FEE_PERCENT / 100)
        database.execute(
            """INSERT INTO seller_commissions (seller_id, split_id, amount)
               VALUES (?, ?, ?)""",
            (user["id"], split_id, commission_due),
        )
        order_payment_status(database, split["order_id"])
        database.commit()
        return jsonify(ok=True, commission_due=commission_due)


@app.post("/api/checkout")
def checkout():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    coupon_code = str(data.get("coupon_code", "")).strip().upper()
    with closing(connect_db()) as database:
        user_id = session.get("user_id")
        user = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
        if not user:
            return jsonify(error="Inicia sesión para confirmar tu pedido."), 401
        cart = current_cart(database, user)
        if not cart:
            return jsonify(error="Tu carrito está vacío."), 400
        customer_name = str(data.get("name") or user["name"]).strip()
        address = str(data.get("address") or user["address"] or "").strip()
        city = str(data.get("city") or user["city"] or "").strip()
        department = str(data.get("department") or user["department"] or "").strip()
        phone = str(data.get("phone") or user["phone"] or "").strip()
        postal_code = str(data.get("postal_code") or user["postal_code"] or "").strip()
        delivery_notes = str(data.get("delivery_notes") or user["delivery_notes"] or "").strip()
        if (
            any(len(value) < 2 or len(value) > 160 for value in (customer_name, address, city, department))
            or not re.fullmatch(r"[0-9+() .-]{7,24}", phone)
            or len(postal_code) > 20
            or len(delivery_notes) > 300
        ):
            return jsonify(error="Revisa nombre, teléfono y dirección de entrega."), 400
        try:
            expire_unpaid_orders(database)
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """UPDATE users SET name = ?, phone = ?, address = ?, city = ?, department = ?,
                   postal_code = ?, delivery_notes = ? WHERE id = ?""",
                (
                    customer_name, phone, address, city, department,
                    postal_code, delivery_notes, user["id"],
                ),
            )
            rows = []
            grouped_items = {}
            total = 0
            for product_id, quantity in cart.items():
                product = database.execute(
                    "SELECT * FROM products WHERE id = ? AND active = 1",
                    (product_id,),
                ).fetchone()
                if not product or quantity > product["stock"]:
                    raise ValueError("El inventario cambió. Revisa las cantidades de tu carrito.")
                unit_price = product_json(product)["price"]
                line_total = unit_price * quantity
                rows.append((product, quantity, unit_price))
                total += line_total
                group = grouped_items.setdefault(
                    product["seller_id"],
                    {"amount": 0, "items": []},
                )
                group["amount"] += line_total
                group["items"].append(
                    {
                        "id": product["id"],
                        "name": product["name"],
                        "price": unit_price,
                        "quantity": quantity,
                    }
                )
            coupon = None
            discount = 0
            if coupon_code:
                coupon = database.execute(
                    """SELECT id, discount_percent, max_discount, status, expires_at
                       FROM discount_coupons WHERE user_id = ? AND code = ?""",
                    (user["id"], coupon_code),
                ).fetchone()
                if not coupon or coupon["status"] != "issued" or not code_is_valid("active", coupon["expires_at"]):
                    raise ValueError("El cupón no existe, ya se usó o venció.")
                discount = min(total * coupon["discount_percent"] // 100, coupon["max_discount"])
                group_ids = list(grouped_items)
                group_discounts = allocate_amount(
                    discount,
                    [grouped_items[seller_id]["amount"] for seller_id in group_ids],
                )
                for seller_id, group_discount in zip(group_ids, group_discounts):
                    group = grouped_items[seller_id]
                    group["items"] = discounted_item_lines(group["items"], group_discount)
                    group["amount"] -= group_discount
            total -= discount
            for seller_id, group in grouped_items.items():
                if seller_id is None:
                    seller_access_token(database, seller_id)
                    group["payment_method"] = "mercadopago"
                    group["nequi_phone"] = None
                    continue
                seller = database.execute(
                    "SELECT nequi_phone, mp_access_token FROM users WHERE id = ?",
                    (seller_id,),
                ).fetchone()
                buyer_preference = user["preferred_payment_method"]
                use_mercadopago = seller and seller["mp_access_token"] and (
                    buyer_preference == "mercadopago"
                    or not seller["nequi_phone"]
                )
                if use_mercadopago:
                    seller_access_token(database, seller_id)
                    group["payment_method"] = "mercadopago"
                    group["nequi_phone"] = None
                elif seller and seller["nequi_phone"]:
                    group["payment_method"] = "nequi"
                    group["nequi_phone"] = seller["nequi_phone"]
                else:
                    seller_access_token(database, seller_id)
                    group["payment_method"] = "mercadopago"
                    group["nequi_phone"] = None
            needs_mercadopago = any(
                group["payment_method"] == "mercadopago"
                for group in grouped_items.values()
            )
            if needs_mercadopago:
                payment_webhook_url = public_url(url_for("mercadopago_webhook"))
                return_url = public_url(url_for("mercadopago_return"))
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            cursor = database.execute(
                """INSERT INTO orders
                   (user_id, customer_name, address, city, total, status, payment_status,
                    phone, department, postal_code, delivery_notes, expires_at, stock_released)
                   VALUES (?, ?, ?, ?, ?, 'Pendiente de pago', 'pending', ?, ?, ?, ?, ?, 0)""",
                (
                    user["id"], customer_name, address, city, total, phone, department,
                    postal_code, delivery_notes, expires_at.isoformat(),
                ),
            )
            order_id = cursor.lastrowid
            for product, quantity, unit_price in rows:
                database.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (quantity, product["id"]))
            if coupon:
                database.execute(
                    """UPDATE discount_coupons SET status = 'reserved', reserved_order_id = ?
                       WHERE id = ? AND status = 'issued'""",
                    (order_id, coupon["id"]),
                )
                if database.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ValueError("El cupón ya se está usando en otro pedido.")
            split_records = []
            for seller_id, group in grouped_items.items():
                split_id = uuid.uuid4().hex
                database.execute(
                    """INSERT INTO payment_splits
                       (id, order_id, seller_id, amount, payment_method, nequi_phone)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        split_id,
                        order_id,
                        seller_id,
                        group["amount"],
                        group["payment_method"],
                        group["nequi_phone"],
                    ),
                )
                split_records.append(
                    {
                        "id": split_id,
                        "seller_id": seller_id,
                        "amount": group["amount"],
                        "items": group["items"],
                        "payment_method": group["payment_method"],
                    }
                )
                for item in group["items"]:
                    database.execute(
                        """INSERT INTO order_items
                           (order_id, product_id, product_name, unit_price, quantity)
                           VALUES (?, ?, ?, ?, ?)""",
                        (order_id, item["id"], item["name"], item["price"], item["quantity"]),
                    )
            database.commit()
        except ValueError as exception:
            database.rollback()
            return jsonify(error=str(exception)), 409
        except PaymentGatewayError as exception:
            database.rollback()
            return jsonify(error=str(exception)), 503
        except Exception:
            database.rollback()
            raise
        try:
            for split in split_records:
                if split["payment_method"] != "mercadopago":
                    continue
                preference_return_url = (
                    f"{return_url}?order_id={order_id}&split_id={split['id']}"
                )
                preference = make_checkout_preference(
                    database,
                    split,
                    split["items"],
                    user,
                    preference_return_url,
                    payment_webhook_url,
                )
                init_point = preference.get("init_point")
                preference_id = preference.get("id")
                if not init_point or not preference_id:
                    raise PaymentGatewayError("Mercado Pago no creó el enlace de pago.")
                database.execute(
                    """UPDATE payment_splits SET preference_id = ?, init_point = ?
                       WHERE id = ?""",
                    (str(preference_id), init_point, split["id"]),
                )
                split["init_point"] = init_point
            database.commit()
        except PaymentGatewayError as exception:
            database.rollback()
            database.execute("BEGIN IMMEDIATE")
            for item in rows:
                database.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ?",
                    (item[1], item[0]["id"]),
                )
            database.execute("DELETE FROM payment_splits WHERE order_id = ?", (order_id,))
            database.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
            database.execute(
                """UPDATE discount_coupons SET status = 'issued', reserved_order_id = NULL
                   WHERE reserved_order_id = ?""",
                (order_id,),
            )
            database.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            database.commit()
            return jsonify(error=str(exception)), 502
    return jsonify(
        order_id=order_id,
        total=total,
        status="Pendiente de pago",
        payment_url=url_for("nequi_payment_page", order_id=order_id),
    ), 201


@app.get("/pedido/<int:order_id>/pago")
def nequi_payment_page(order_id):
    with closing(connect_db()) as database:
        order = database.execute(
            "SELECT id, user_id, total, payment_status, status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order or order["user_id"] != session.get("user_id"):
            abort(404)
        splits = database.execute(
            """SELECT payment_splits.id, payment_splits.seller_id, payment_splits.amount,
                      payment_splits.payment_method, payment_splits.nequi_phone,
                      payment_splits.status, payment_splits.init_point,
                      COALESCE(users.name, 'XrteShop') AS seller_name
               FROM payment_splits
               LEFT JOIN users ON users.id = payment_splits.seller_id
               WHERE payment_splits.order_id = ?
               ORDER BY payment_splits.created_at, payment_splits.id""",
            (order_id,),
        ).fetchall()
        payment_details = []
        for split in splits:
            items = database.execute(
                """SELECT order_items.product_name, order_items.quantity
                   FROM order_items
                   JOIN products ON products.id = order_items.product_id
                   WHERE order_items.order_id = ?
                     AND (products.seller_id = ? OR (? IS NULL AND products.seller_id IS NULL))
                   ORDER BY order_items.id""",
                (order_id, split["seller_id"], split["seller_id"]),
            ).fetchall()
            payment_details.append({**dict(split), "items": [dict(item) for item in items]})
        if order["payment_status"] == "approved":
            write_user_cart(database, order["user_id"], {})
            database.commit()
            session["cart"] = {}
        return render_template(
            "nequi_payment.html",
            order_id=order_id,
            total=order["total"],
            payment_status=order["payment_status"],
            order_status=order["status"],
            splits=payment_details,
        )


@app.get("/mercadopago/return")
def mercadopago_return():
    try:
        order_id = int(request.args.get("order_id", ""))
    except ValueError:
        return redirect("/?pago=referencia-invalida")
    split_id = request.args.get("split_id", "")
    with closing(connect_db()) as database:
        order = database.execute(
            "SELECT user_id, status, payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order or order["user_id"] != session.get("user_id"):
            abort(404)
        payment_id = request.args.get("payment_id")
        if payment_id:
            try:
                verify_mp_payment(database, split_id, payment_id)
            except PaymentGatewayError as error:
                app.logger.info("Mercado Pago return not approved: %s", error)
        elif request.args.get("status") in ("failure", "rejected"):
            database.execute(
                "UPDATE payment_splits SET status = 'rejected' WHERE id = ? AND order_id = ? AND status = 'pending'",
                (split_id, order_id),
            )
            order_payment_status(database, order_id)
            database.commit()
        next_split = database.execute(
            """SELECT init_point FROM payment_splits
               WHERE order_id = ? AND payment_method = 'mercadopago'
                 AND status = 'pending' AND init_point IS NOT NULL
               ORDER BY created_at, id LIMIT 1""",
            (order_id,),
        ).fetchone()
        updated = database.execute(
            "SELECT payment_status, status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if updated["payment_status"] == "approved":
            write_user_cart(database, order["user_id"], {})
            database.commit()
            session["cart"] = {}
        elif request.args.get("status") == "approved" and next_split:
            return redirect(next_split["init_point"])
        elif updated["payment_status"] in ("pending", "partial"):
            return redirect(url_for("nequi_payment_page", order_id=order_id))
        return render_template(
            "payment_result.html",
            order_id=order_id,
            order_status=updated["status"],
            payment_status=updated["payment_status"],
        )


@app.post("/mercadopago/webhook")
def mercadopago_webhook():
    notification = request.get_json(silent=True) or {}
    if not isinstance(notification, dict):
        return jsonify(error="Notificación no válida."), 400
    payment_id = request.args.get("data.id") or (notification.get("data") or {}).get("id")
    if notification.get("type") not in (None, "payment") or not payment_id:
        return jsonify(ok=True)
    if not valid_mp_webhook_signature(payment_id):
        return jsonify(error="Firma de notificación no válida."), 401
    with closing(connect_db()) as database:
        expire_unpaid_orders(database)
        splits = database.execute(
            """SELECT id, seller_id FROM payment_splits
               WHERE status IN ('pending', 'approved') ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        for split in splits:
            try:
                token = seller_access_token(database, split["seller_id"])
                payment = get_payment(token, payment_id)
            except PaymentGatewayError as error:
                app.logger.warning("Could not verify Mercado Pago notification: %s", error)
                continue
            if payment.get("external_reference") == split["id"]:
                try:
                    verify_mp_payment(database, split["id"], payment_id)
                except PaymentGatewayError as error:
                    app.logger.info("Mercado Pago notification not approved: %s", error)
                return jsonify(ok=True)
    return jsonify(ok=True)


@app.get("/imagenes/<path:filename>")
def images(filename):
    return send_from_directory(ROOT / "static" / "images", filename)


@app.get("/<path:asset>")
def public_assets(asset):
    legacy_assets = {
        "styles.css": "css/styles.css",
        "script.js": "js/script.js",
        "cuenta.css": "css/cuenta.css",
        "cuenta.js": "js/cuenta.js",
        "emprendedores.css": "css/emprendedores.css",
        "emprendedores.js": "js/emprendedores.js",
        "producto.css": "css/producto.css",
        "producto.js": "js/producto.js",
    }
    resource = legacy_assets.get(asset)
    if resource is None:
        abort(404)
    return send_from_directory(ROOT / "static", resource)


initialize_database()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)