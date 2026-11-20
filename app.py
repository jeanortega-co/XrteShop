import csv
import base64
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
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode, urlparse
from xml.sax.saxutils import escape
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, Response, abort, g, jsonify, make_response, redirect, render_template, request, send_from_directory, session, url_for
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
from payment_service import (
    PaymentOutcomeUnknown,
    PaymentServiceError,
    allowed_payment_methods,
    calculate_commission,
    payu_checkout_url,
    payu_payment_fields,
    payment_settings,
    require_payment_credentials,
    request_refund,
    request_payout,
    validate_checkout_payment_method,
    verify_payu_payment_notification,
)
from wompi_provider import (
    ALLOWED_PAYMENT_METHODS as WOMPI_ALLOWED_PAYMENT_METHODS,
    PaymentOutcomeUnknown as WompiPaymentOutcomeUnknown,
    PaymentProviderError as WompiPaymentProviderError,
    WompiPaymentProvider,
    validate_payment_method as validate_wompi_payment_method,
    wompi_environment,
    wompi_public_key,
)
from catalog_cleanup_service import (
    CONFIRMATION as CATALOG_CLEANUP_CONFIRMATION,
    apply_catalog_cleanup,
    backup_before_cleanup,
    catalog_cleanup_preview,
    connect_readonly as connect_catalog_readonly,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import bcrypt
import requests
import admin_security
from database_backend import (
    DATABASE_ERRORS,
    DATABASE_INTEGRITY_ERRORS,
    PostgresDatabase,
)
from mercadolibre_service import (
    MercadoLibreImportError,
    preview_mercadolibre_product,
    sanitize_mercadolibre_url,
)
from mercadolibre_catalog_service import (
    list_mercadolibre_products_for_sync,
    publish_mercadolibre_product,
    sync_mercadolibre_product,
)
import mercadolibre_oauth
from mercadolibre_oauth import MercadoLibreOAuthError


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SQLITE_DATABASE = os.environ.get("PEMU_DATABASE", "").strip()
SESSION_SECRET = os.environ.get("SECRET_KEY") or os.environ.get("PEMU_SECRET_KEY") or secrets.token_urlsafe(48)
JWT_SECRET = os.environ.get("PEMU_JWT_SECRET") or SESSION_SECRET
HTTPS_ENABLED = os.environ.get("PEMU_HTTPS", "0") == "1" or os.environ.get("VERCEL") == "1"
if HTTPS_ENABLED and (
    not (os.environ.get("SECRET_KEY") or os.environ.get("PEMU_SECRET_KEY"))
    or not os.environ.get("PEMU_JWT_SECRET")
):
    raise RuntimeError("Configura SECRET_KEY y PEMU_JWT_SECRET en producción HTTPS.")

app = Flask(
    __name__,
    static_folder=ROOT / "static",
    template_folder=ROOT / "templates",
)
app.config.update(
    SECRET_KEY=SESSION_SECRET,
    JWT_SECRET=JWT_SECRET,
    DATABASE=SQLITE_DATABASE or str(ROOT / "pemu.sqlite3"),
    SESSION_COOKIE_NAME="pemu_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=HTTPS_ENABLED,
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    AUTH_ACCESS_SECONDS=900,
    AUTH_REFRESH_DAYS=365,
    AUTH_REFRESH_SHORT_DAYS=30,
    AUTH_COOKIE_SECURE=HTTPS_ENABLED,
    AUTH_ACCESS_COOKIE="pemu_access",
    AUTH_REFRESH_COOKIE="pemu_refresh",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    MAIL_SERVER=os.environ.get("PEMU_SMTP_HOST", "").strip(),
    MAIL_PORT=int(os.environ.get("PEMU_SMTP_PORT", "587") or "587"),
    MAIL_USERNAME=os.environ.get("PEMU_SMTP_USERNAME", "").strip(),
    MAIL_PASSWORD=os.environ.get("PEMU_SMTP_PASSWORD", "").replace(" ", "").strip(),
    MAIL_DEFAULT_SENDER=os.environ.get("PEMU_SMTP_FROM", "").strip(),
    MAIL_USE_TLS=os.environ.get("PEMU_SMTP_USE_TLS", "1") == "1",
)


@app.context_processor
def inject_payment_environment():
    return {"payment_environment": wompi_environment()}


DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("PEMU_DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or ""
).strip()
if os.environ.get("VERCEL") == "1" and not DATABASE_URL:
    raise RuntimeError(
        "Configura DATABASE_URL con una base PostgreSQL persistente antes de desplegar en Vercel."
    )
if not DATABASE_URL and not SQLITE_DATABASE:
    raise RuntimeError(
        "Configura DATABASE_URL (Neon) o PEMU_DATABASE con una ruta SQLite explícita. "
        "No se seleccionará automáticamente un catálogo local distinto."
    )
if (
    os.environ.get("VERCEL") == "1"
    and os.environ.get("PEMU_PAYOUT_WORKER", "0") == "1"
):
    raise RuntimeError(
        "Vercel no admite el worker continuo; usa el Cron Job protegido por CRON_SECRET."
    )

CATEGORIES = ("Accesorios", "Cuidado personal", "Electrónica", "Hogar", "Moda", "Otros")
MARKETPLACE_FEE_PERCENT = 10
PAYMENT_PROVIDER = os.environ.get("PEMU_PAYMENT_PROVIDER", "wompi").strip().lower()
PAYMENT_RATE_LIMIT = 5
PAYMENT_RATE_WINDOW_SECONDS = 3600
ENTREPRENEUR_PRODUCT_VISIBILITY_SQL = (
    """(products.origin IN ('emprendedor', 'proveedor') OR products.seller_id IS NOT NULL)"""
)
SELLER_PAYMENT_VISIBILITY_SQL = (
    """(products.seller_id IS NULL OR EXISTS (
        SELECT 1 FROM seller_accounts
        WHERE seller_accounts.seller_id = products.seller_id
          AND seller_accounts.provider = 'payu'
          AND seller_accounts.verification_status = 'verified'
          AND seller_accounts.provider_account_id IS NOT NULL
    ))"""
    if PAYMENT_PROVIDER == "payu"
    else "1 = 1"
)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
EXCHANGE_RATE_TTL_SECONDS = 21600
_exchange_rate_lock = Lock()
_exchange_rate_cache = {"rate": None, "updated_at": None, "expires_at": 0.0}
LEGACY_SAMPLE_PRODUCT_IDS = (
    "airpods",
    "reloj-hombre",
    "set-reloj",
    "vgr-660",
    "maquina-vgr",
    "trimer-kemei",
    "pc-gamer",
    "chaqueta-cuero",
    "vestido",
    "nike-tn",
)


def connect_db():
    if DATABASE_URL:
        return PostgresDatabase.connect(DATABASE_URL)
    database = sqlite3.connect(app.config["DATABASE"], timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def initialize_database():
    if not DATABASE_URL:
        Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db()) as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'cliente',
                account_status TEXT NOT NULL DEFAULT 'active',
                admin_totp_secret TEXT,
                admin_totp_enabled INTEGER NOT NULL DEFAULT 0,
                admin_totp_pending_secret TEXT,
                admin_recovery_hashes TEXT NOT NULL DEFAULT '[]',
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
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                device TEXT NOT NULL DEFAULT '',
                remember_me INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                admin_2fa_verified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS auth_sessions_user_active
                ON auth_sessions(user_id, revoked_at, expires_at);
            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                key_hash TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_started_at TEXT NOT NULL,
                blocked_until TEXT
            );
            CREATE TABLE IF NOT EXISTS admin_known_devices (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                device_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (user_id, device_hash)
            );
            CREATE TABLE IF NOT EXISTS admin_security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                event TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                device TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS admin_bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                account_holder TEXT NOT NULL,
                alias TEXT NOT NULL,
                encrypted_number TEXT NOT NULL,
                last_four TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS admin_manual_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                movement_type TEXT NOT NULL,
                amount_cop INTEGER NOT NULL,
                movement_date TEXT NOT NULL,
                account_id INTEGER REFERENCES admin_bank_accounts(id),
                seller_id INTEGER REFERENCES users(id),
                reference TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                actor_user_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS admin_reconciliation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_reference TEXT NOT NULL UNIQUE,
                provider_amount_cop INTEGER NOT NULL,
                settlement_date TEXT,
                provider_status TEXT NOT NULL DEFAULT '',
                local_amount_cop INTEGER,
                difference_cop INTEGER,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor_user_id INTEGER REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS admin_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor_user_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                affected_resource TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}',
                ip_address TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS admin_audit_log_created
                ON admin_audit_log(created_at);
            CREATE TABLE IF NOT EXISTS admin_api_rate_limits (
                key_hash TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                window_started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mercadolibre_oauth_accounts (
                account_key TEXT PRIMARY KEY,
                external_user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                encrypted_access_token TEXT NOT NULL,
                encrypted_refresh_token TEXT NOT NULL,
                token_expires_at TEXT NOT NULL,
                connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS mercadolibre_oauth_states (
                state_hash TEXT PRIMARY KEY,
                actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS mercadolibre_oauth_states_expiry
                ON mercadolibre_oauth_states(expires_at);
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
                origin TEXT NOT NULL DEFAULT 'propio'
                    CHECK (origin IN ('propio', 'emprendedor', 'proveedor')),
                supplier_platform TEXT,
                supplier_id TEXT,
                supplier_product_id TEXT,
                supplier_cost INTEGER CHECK (supplier_cost IS NULL OR supplier_cost >= 0),
                supplier_original_price TEXT,
                supplier_currency TEXT,
                supplier_commission_percent INTEGER NOT NULL DEFAULT 10,
                supplier_url TEXT,
                supplier_shipping_estimate INTEGER NOT NULL DEFAULT 0
                    CHECK (supplier_shipping_estimate >= 0),
                supplier_updated_at TEXT,
                archived_at TEXT,
                external_catalog TEXT,
                source_url TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                shipping_cost INTEGER NOT NULL DEFAULT 0,
                shipping_min_days INTEGER NOT NULL DEFAULT 3,
                shipping_max_days INTEGER NOT NULL DEFAULT 7
            );
            CREATE TABLE IF NOT EXISTS product_images (
                image_path TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                image_data BLOB NOT NULL,
                content_type TEXT NOT NULL CHECK (
                    content_type IN ('image/png', 'image/jpeg', 'image/webp')
                ),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS supplier_product_images (
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                image_url TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (product_id, image_url)
            );
            CREATE TABLE IF NOT EXISTS suppliers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                connector TEXT NOT NULL,
                api_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (api_status IN ('pending', 'verified', 'restricted')),
                enabled INTEGER NOT NULL DEFAULT 0,
                payment_mode TEXT NOT NULL DEFAULT 'fallback_balance'
                    CHECK (payment_mode IN ('split', 'fallback_balance')),
                minimum_margin INTEGER NOT NULL DEFAULT 0,
                sync_interval_hours INTEGER NOT NULL DEFAULT 24,
                last_synced_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS catalog_cleanup_exclusions (
                product_id TEXT PRIMARY KEY,
                excluded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS supplier_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id TEXT NOT NULL REFERENCES suppliers(id),
                provider_product_id TEXT NOT NULL,
                product_id TEXT REFERENCES products(id),
                supplier_cost INTEGER NOT NULL CHECK (supplier_cost >= 0),
                supplier_stock INTEGER NOT NULL DEFAULT 0 CHECK (supplier_stock >= 0),
                currency TEXT NOT NULL DEFAULT 'COP',
                delivery_min_days INTEGER,
                delivery_max_days INTEGER,
                product_status TEXT NOT NULL DEFAULT 'pending',
                last_checked_at TEXT,
                UNIQUE (supplier_id, provider_product_id)
            );
            CREATE TABLE IF NOT EXISTS user_cart_items (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS product_variants (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                options_json TEXT NOT NULL,
                stock INTEGER NOT NULL CHECK (stock >= 0),
                price INTEGER CHECK (price IS NULL OR price > 0),
                supplier_variant_id TEXT,
                supplier_variant_sku TEXT,
                supplier_stock INTEGER NOT NULL DEFAULT 0 CHECK (supplier_stock >= 0),
                supplier_original_price TEXT,
                supplier_currency TEXT,
                UNIQUE (product_id, options_json)
            );
            CREATE TABLE IF NOT EXISTS user_cart_variant_items (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                variant_id TEXT NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, variant_id)
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
                payment_method TEXT,
                distribution_mode TEXT NOT NULL DEFAULT 'fallback',
                gateway_fee_payer TEXT NOT NULL DEFAULT 'split_proportional',
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
            CREATE TABLE IF NOT EXISTS supplier_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id TEXT NOT NULL REFERENCES suppliers(id),
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                external_order_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                supplier_cost INTEGER NOT NULL CHECK (supplier_cost >= 0),
                status TEXT NOT NULL DEFAULT 'pending',
                tracking_number TEXT,
                tracking_url TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS supplier_sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id TEXT NOT NULL REFERENCES suppliers(id),
                status TEXT NOT NULL,
                products_seen INTEGER NOT NULL DEFAULT 0,
                products_updated INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payment_splits (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                seller_id INTEGER REFERENCES users(id),
                amount INTEGER NOT NULL CHECK (amount > 0),
                shipping_amount INTEGER NOT NULL DEFAULT 0 CHECK (shipping_amount >= 0),
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
                redeemed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS loyalty_points_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                order_id INTEGER UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
                event TEXT NOT NULL CHECK (event IN ('welcome', 'purchase', 'redemption')),
                points INTEGER NOT NULL CHECK (points != 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS product_interactions (
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                action TEXT NOT NULL CHECK (action IN ('view', 'cart_add', 'search_result')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS product_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                review TEXT NOT NULL CHECK (length(review) BETWEEN 10 AND 1000),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                consented_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                active INTEGER NOT NULL DEFAULT 1
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
            CREATE TABLE IF NOT EXISTS seller_accounts (
                seller_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL DEFAULT 'payu',
                provider_account_id TEXT,
                verification_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (verification_status IN ('pending', 'verified', 'restricted')),
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                verified_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                processing_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payment_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_key TEXT NOT NULL UNIQUE,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                split_id TEXT REFERENCES payment_splits(id),
                order_item_id INTEGER REFERENCES order_items(id),
                seller_id INTEGER REFERENCES users(id),
                entry_type TEXT NOT NULL,
                amount_cop INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'registrado',
                payment_date TEXT,
                payment_reference TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payment_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER REFERENCES orders(id),
                split_id TEXT REFERENCES payment_splits(id),
                seller_id INTEGER REFERENCES users(id),
                actor_user_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                provider_reference TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payment_refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                provider_refund_id TEXT UNIQUE,
                amount INTEGER NOT NULL CHECK (amount > 0),
                commission_reversed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '',
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS seller_refund_liabilities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refund_id INTEGER NOT NULL REFERENCES payment_refunds(id),
                order_id INTEGER NOT NULL REFERENCES orders(id),
                seller_id INTEGER NOT NULL REFERENCES users(id),
                amount INTEGER NOT NULL CHECK (amount >= 0),
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TEXT,
                settled_by INTEGER REFERENCES users(id),
                settlement_reference TEXT,
                UNIQUE (refund_id, seller_id)
            );
            CREATE TABLE IF NOT EXISTS payment_disputes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_dispute_id TEXT NOT NULL UNIQUE,
                order_id INTEGER REFERENCES orders(id),
                payment_id TEXT,
                status TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payment_rate_limits (
                key_hash TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                window_started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pemu_transaction_lock (
                id INTEGER PRIMARY KEY CHECK (id = 1)
            );
            """
        )
        database.execute(
            "INSERT OR IGNORE INTO pemu_transaction_lock (id) VALUES (1)"
        )
        refund_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(payment_refunds)")
        }
        if "commission_reversed" not in refund_columns:
            database.execute(
                "ALTER TABLE payment_refunds ADD COLUMN commission_reversed INTEGER NOT NULL DEFAULT 0"
            )
        liability_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(seller_refund_liabilities)")
        }
        for column, definition in {
            "settled_by": "INTEGER REFERENCES users(id)",
            "settlement_reference": "TEXT",
        }.items():
            if column not in liability_columns:
                database.execute(
                    f"ALTER TABLE seller_refund_liabilities ADD COLUMN {column} {definition}"
                )
        user_columns = {row["name"] for row in database.execute("PRAGMA table_info(users)")}
        for column, definition in {
            "role": "TEXT NOT NULL DEFAULT 'cliente'",
            "account_status": "TEXT NOT NULL DEFAULT 'active'",
            "admin_totp_secret": "TEXT",
            "admin_totp_enabled": "INTEGER NOT NULL DEFAULT 0",
            "admin_totp_pending_secret": "TEXT",
            "admin_recovery_hashes": "TEXT NOT NULL DEFAULT '[]'",
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
        # Remove retired 2FA credentials left by older administrator accounts.
        database.execute(
            """UPDATE users
               SET admin_totp_secret = NULL, admin_totp_enabled = 0,
                   admin_totp_pending_secret = NULL, admin_recovery_hashes = '[]'
               WHERE role = 'admin'"""
        )
        auth_session_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(auth_sessions)")
        }
        if "admin_2fa_verified_at" not in auth_session_columns:
            database.execute("ALTER TABLE auth_sessions ADD COLUMN admin_2fa_verified_at TEXT")
        audit_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(admin_audit_log)")
        }
        if "details" not in audit_columns:
            database.execute(
                "ALTER TABLE admin_audit_log ADD COLUMN details TEXT NOT NULL DEFAULT '{}'"
            )
        order_columns = {row["name"] for row in database.execute("PRAGMA table_info(orders)")}
        order_migrations = {
            "payment_status": "TEXT NOT NULL DEFAULT 'pending'",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "department": "TEXT NOT NULL DEFAULT ''",
            "postal_code": "TEXT NOT NULL DEFAULT ''",
            "delivery_notes": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT",
            "stock_released": "INTEGER NOT NULL DEFAULT 1",
            "payment_provider": "TEXT NOT NULL DEFAULT 'legacy'",
            "payment_reference": "TEXT",
            "provider_payment_id": "TEXT",
            "refunded_amount": "INTEGER NOT NULL DEFAULT 0",
            "disputed": "INTEGER NOT NULL DEFAULT 0",
            "payment_method": "TEXT",
            "distribution_mode": "TEXT NOT NULL DEFAULT 'fallback'",
            "gateway_fee_payer": "TEXT NOT NULL DEFAULT 'split_proportional'",
        }
        for column, definition in order_migrations.items():
            if column not in order_columns:
                database.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
        split_columns = {row["name"] for row in database.execute("PRAGMA table_info(payment_splits)")}
        for column, definition in {
            "payment_method": "TEXT NOT NULL DEFAULT 'mercadopago'",
            "nequi_phone": "TEXT",
            "shipping_amount": "INTEGER NOT NULL DEFAULT 0",
            "shipping_status": "TEXT NOT NULL DEFAULT 'preparing'",
            "shipping_carrier": "TEXT",
            "tracking_number": "TEXT",
            "estimated_delivery_at": "TEXT",
            "shipped_at": "TEXT",
            "delivered_at": "TEXT",
        }.items():
            if column not in split_columns:
                database.execute(f"ALTER TABLE payment_splits ADD COLUMN {column} {definition}")
        split_columns = {row["name"] for row in database.execute("PRAGMA table_info(payment_splits)")}
        for column, definition in {
            "commission_amount": "INTEGER NOT NULL DEFAULT 0",
            "net_amount": "INTEGER NOT NULL DEFAULT 0",
            "transfer_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "provider_transfer_id": "TEXT",
            "payout_due_at": "TEXT",
            "transfer_attempts": "INTEGER NOT NULL DEFAULT 0",
            "refunded_amount": "INTEGER NOT NULL DEFAULT 0",
            "refunded_commission_amount": "INTEGER NOT NULL DEFAULT 0",
            "transfer_started_at": "TEXT",
            "transfer_completed_at": "TEXT",
            "distribution_mode": "TEXT NOT NULL DEFAULT 'fallback'",
        }.items():
            if column not in split_columns:
                database.execute(f"ALTER TABLE payment_splits ADD COLUMN {column} {definition}")
        order_item_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(order_items)")
        }
        if "variant_id" not in order_item_columns:
            database.execute(
                "ALTER TABLE order_items ADD COLUMN variant_id TEXT REFERENCES product_variants(id)"
            )
        order_item_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(order_items)")
        }
        for column, definition in {
            "seller_id": "INTEGER REFERENCES users(id)",
            "gross_amount": "INTEGER NOT NULL DEFAULT 0",
            "commission_xrte": "INTEGER NOT NULL DEFAULT 0",
            "net_seller_amount": "INTEGER NOT NULL DEFAULT 0",
            "transfer_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "transfer_id": "TEXT",
            "destination_id": "TEXT",
            "distribution_mode": "TEXT NOT NULL DEFAULT 'fallback'",
            "supplier_cost": "INTEGER NOT NULL DEFAULT 0",
            "supplier_platform": "TEXT",
            "supplier_original_price": "TEXT",
            "supplier_currency": "TEXT",
            "supplier_commission_percent": "INTEGER NOT NULL DEFAULT 0",
            "supplier_commission": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in order_item_columns:
                database.execute(f"ALTER TABLE order_items ADD COLUMN {column} {definition}")
        variant_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(product_variants)")
        }
        for column, definition in {
            "supplier_variant_id": "TEXT",
            "supplier_variant_sku": "TEXT",
            "supplier_stock": "INTEGER NOT NULL DEFAULT 0",
            "supplier_original_price": "TEXT",
            "supplier_currency": "TEXT",
        }.items():
            if column not in variant_columns:
                database.execute(
                    f"ALTER TABLE product_variants ADD COLUMN {column} {definition}"
                )
        database.execute(
            """UPDATE orders SET payment_status = 'approved'
               WHERE status = 'Recibido' AND payment_status = 'pending'"""
        )
        product_columns = {row["name"] for row in database.execute("PRAGMA table_info(products)")}
        migrations = {
            "seller_id": "INTEGER REFERENCES users(id)",
            "external_catalog": "TEXT",
            "source_url": "TEXT",
            "origin": "TEXT NOT NULL DEFAULT 'propio'",
            "supplier_platform": "TEXT",
            "supplier_id": "TEXT",
            "supplier_product_id": "TEXT",
            "supplier_cost": "INTEGER",
            "supplier_original_price": "TEXT",
            "supplier_currency": "TEXT",
            "supplier_commission_percent": "INTEGER NOT NULL DEFAULT 10",
            "supplier_url": "TEXT",
            "supplier_shipping_estimate": "INTEGER NOT NULL DEFAULT 0",
            "supplier_updated_at": "TEXT",
            "archived_at": "TEXT",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "flash_sale_percent": "INTEGER NOT NULL DEFAULT 0",
            "flash_sale_ends_at": "TEXT",
            "shipping_cost": "INTEGER NOT NULL DEFAULT 0",
            "shipping_min_days": "INTEGER NOT NULL DEFAULT 3",
            "shipping_max_days": "INTEGER NOT NULL DEFAULT 7",
        }
        for column, definition in migrations.items():
            if column not in product_columns:
                database.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")
        database.execute(
            """UPDATE products SET supplier_platform = 'mercadolibre'
               WHERE origin = 'proveedor' AND supplier_platform IS NULL
                 AND supplier_id = 'mercadolibre'"""
        )
        database.execute(
            """UPDATE order_items
               SET supplier_platform = (
                   SELECT products.supplier_platform FROM products
                   WHERE products.id = order_items.product_id
               )
               WHERE supplier_platform IS NULL
                 AND EXISTS (
                     SELECT 1 FROM products
                     WHERE products.id = order_items.product_id
                       AND products.supplier_platform IS NOT NULL
                 )"""
        )
        database.execute(
            """UPDATE products SET origin = CASE
                   WHEN seller_id IS NOT NULL THEN 'emprendedor'
                   WHEN COALESCE(external_catalog, '') != ''
                        OR COALESCE(source_url, '') != '' THEN 'proveedor'
                   ELSE 'propio' END
               WHERE origin IS NULL OR origin NOT IN ('propio', 'emprendedor', 'proveedor')
                  OR origin = 'propio' AND seller_id IS NOT NULL
                  OR origin = 'emprendedor' AND seller_id IS NULL
                     AND COALESCE(external_catalog, '') = ''
                     AND COALESCE(source_url, '') = ''"""
        )
        coupon_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(discount_coupons)")
        }
        if "redeemed_at" not in coupon_columns:
            database.execute("ALTER TABLE discount_coupons ADD COLUMN redeemed_at TEXT")
        database.execute("CREATE INDEX IF NOT EXISTS products_seller_id ON products(seller_id)")
        database.execute("CREATE INDEX IF NOT EXISTS product_interactions_user ON product_interactions(user_id, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS discount_coupons_user ON discount_coupons(user_id, status, expires_at)")
        database.execute("CREATE INDEX IF NOT EXISTS product_reviews_product ON product_reviews(product_id, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS loyalty_points_user ON loyalty_points_ledger(user_id, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS payment_events_processed ON payment_events(processed, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS payment_ledger_seller ON payment_ledger(seller_id, status, created_at)")
        database.execute("CREATE INDEX IF NOT EXISTS payment_splits_payout_due ON payment_splits(transfer_status, payout_due_at)")
        database.execute("CREATE INDEX IF NOT EXISTS payment_audit_order ON payment_audit_logs(order_id, created_at)")
        database.executemany(
            """INSERT OR IGNORE INTO suppliers (id, name, connector, api_status, enabled)
               VALUES (?, ?, ?, 'pending', 0)""",
            (
                ("aliexpress", "AliExpress Dropshipping API", "aliexpress"),
                ("mercadolibre", "MercadoLibre API", "mercadolibre"),
            ),
        )
        database.execute(
            """INSERT INTO loyalty_points_ledger (user_id, event, points)
               SELECT users.id, 'welcome', 100 FROM users
               WHERE users.email_verified = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM loyalty_points_ledger
                     WHERE loyalty_points_ledger.user_id = users.id
                       AND loyalty_points_ledger.event = 'welcome'
                 )"""
        )
        product_placeholders = ",".join("?" for _ in LEGACY_SAMPLE_PRODUCT_IDS)
        database.execute(
            f"""UPDATE products SET active = 0, stock = 0,
                       archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP)
                WHERE id IN ({product_placeholders})
                  AND origin = 'propio' AND seller_id IS NULL""",
            LEGACY_SAMPLE_PRODUCT_IDS,
        )
        database.execute(
            """UPDATE products SET active = 0, stock = 0,
                      archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP)
               WHERE supplier_id = 'dropi'
                 AND supplier_product_id LIKE ?""",
            ("mock-%",),
        )
        database.execute(
            """DELETE FROM suppliers
               WHERE id = 'dropi'
                 AND NOT EXISTS (
                     SELECT 1 FROM supplier_products WHERE supplier_id = 'dropi'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM supplier_orders WHERE supplier_id = 'dropi'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM supplier_sync_logs WHERE supplier_id = 'dropi'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM products WHERE supplier_id = 'dropi'
                 )"""
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


def auth_now():
    return datetime.now(timezone.utc)


def encode_jwt_access(user_id, session_id):
    # El JWT de acceso es de corta duración y no se expone al JavaScript.
    issued_at = int(auth_now().timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "sid": session_id,
        "iat": issued_at,
        "exp": issued_at + app.config["AUTH_ACCESS_SECONDS"],
    }
    encoded_header = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).rstrip(b"=")
    encoded_payload = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signing_input = encoded_header + b"." + encoded_payload
    signature = hmac.new(app.config["JWT_SECRET"].encode(), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return b".".join((encoded_header, encoded_payload, encoded_signature)).decode()


def decode_jwt_access(token):
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        signing_input = f"{encoded_header}.{encoded_payload}".encode()
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(app.config["JWT_SECRET"].encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        header = json.loads(base64.urlsafe_b64decode(encoded_header + "=" * (-len(encoded_header) % 4)))
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4)))
        if header != {"alg": "HS256", "typ": "JWT"}:
            return None
        if int(payload["exp"]) <= int(auth_now().timestamp()):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def create_auth_session(database, user_id, remember_me=True, admin_session=False):
    refresh_token = secrets.token_urlsafe(64)
    session_id = str(uuid.uuid4())
    now = auth_now()
    if admin_session:
        expires_at = now + timedelta(hours=12)
    else:
        days = app.config["AUTH_REFRESH_DAYS"] if remember_me else app.config["AUTH_REFRESH_SHORT_DAYS"]
        expires_at = now + timedelta(days=days)
    database.execute(
        """INSERT INTO auth_sessions
           (id, user_id, refresh_token_hash, device, remember_me, created_at, last_used_at,
            expires_at, admin_2fa_verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            user_id,
            hashlib.sha256(refresh_token.encode()).hexdigest(),
            request.user_agent.string[:300],
            int(remember_me),
            now.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            expires_at.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds") if admin_session else None,
        ),
    )
    # El valor original solo se entrega en la cookie HttpOnly; SQLite guarda su hash.
    return {
        "session_id": session_id,
        "access_token": encode_jwt_access(user_id, session_id),
        "refresh_token": refresh_token,
        "refresh_expires_at": expires_at,
        "refresh_max_age": int((expires_at - now).total_seconds()),
    }


def attach_auth_cookies(response, auth_tokens):
    response.set_cookie(
        app.config["AUTH_ACCESS_COOKIE"],
        auth_tokens["access_token"],
        max_age=app.config["AUTH_ACCESS_SECONDS"],
        httponly=True,
        secure=app.config["AUTH_COOKIE_SECURE"],
        samesite="Strict",
        path="/",
    )
    response.set_cookie(
        app.config["AUTH_REFRESH_COOKIE"],
        auth_tokens["refresh_token"],
        max_age=auth_tokens["refresh_max_age"],
        expires=auth_tokens["refresh_expires_at"],
        httponly=True,
        secure=app.config["AUTH_COOKIE_SECURE"],
        samesite="Strict",
        path="/",
    )
    return response


def clear_auth_cookies(response):
    for cookie_name in (app.config["AUTH_ACCESS_COOKIE"], app.config["AUTH_REFRESH_COOKIE"]):
        response.delete_cookie(
            cookie_name,
            path="/",
            secure=app.config["AUTH_COOKIE_SECURE"],
            httponly=True,
            samesite="Strict",
        )
    return response


def password_error(password, email=""):
    return admin_security.validate_password(password, email)


def hash_password(password):
    return admin_security.hash_password(password)


def verify_password(password_hash, password):
    if password_hash.startswith(admin_security.PASSWORD_HASH_PREFIX):
        return admin_security.verify_password(password_hash, password)
    if password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return False
    try:
        valid = check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False
    if valid:
        return True
    return False


def login_rate_keys(email):
    remote_address = request.remote_addr or "unknown"
    secret = app.config["JWT_SECRET"]
    ip_key = hashlib.sha256(f"{secret}:ip:{remote_address}".encode()).hexdigest()
    account_key = hashlib.sha256(f"{secret}:account:{remote_address}:{email}".encode()).hexdigest()
    return (ip_key, account_key)


def log_admin_security_event(database, event):
    database.execute(
        """INSERT INTO admin_security_events (event, ip_address, device)
           VALUES (?, ?, ?)""",
        (
            event,
            request.remote_addr or "unknown",
            request.user_agent.string[:300],
        ),
    )


def login_is_rate_limited(database, keys):
    now = auth_now().isoformat(timespec="seconds")
    for key in keys:
        attempt = database.execute(
            "SELECT blocked_until FROM auth_login_attempts WHERE key_hash = ?",
            (key,),
        ).fetchone()
        if attempt and attempt["blocked_until"] and attempt["blocked_until"] > now:
            return True
    return False


def record_failed_login(database, keys):
    now = auth_now()
    window_start = now - timedelta(minutes=15)
    blocked_until = now + timedelta(minutes=15)
    for key in keys:
        attempt = database.execute(
            "SELECT attempts, window_started_at FROM auth_login_attempts WHERE key_hash = ?",
            (key,),
        ).fetchone()
        if not attempt or attempt["window_started_at"] < window_start.isoformat(timespec="seconds"):
            attempts = 1
            started_at = now.isoformat(timespec="seconds")
        else:
            attempts = attempt["attempts"] + 1
            started_at = attempt["window_started_at"]
        database.execute(
            """INSERT INTO auth_login_attempts (key_hash, attempts, window_started_at, blocked_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key_hash) DO UPDATE SET attempts = excluded.attempts,
                   window_started_at = excluded.window_started_at, blocked_until = excluded.blocked_until""",
            (
                key,
                attempts,
                started_at,
                blocked_until.isoformat(timespec="seconds") if attempts >= 5 else None,
            ),
        )


def establish_user_session(database, user, remember_me=True):
    legacy_cart = session.get("cart", {}) if session.get("user_id") in (None, user["id"]) else {}
    ensure_user_cart(database, user["id"], legacy_cart)
    auth_tokens = create_auth_session(database, user["id"], remember_me)
    database.commit()
    session.clear()
    session["user_id"] = user["id"]
    session["auth_session_id"] = auth_tokens["session_id"]
    session.permanent = True
    session.modified = True
    g.pending_auth_tokens = None
    g.clear_auth_cookies = False
    csrf_token()
    return auth_tokens


def signed_in_user(database):
    user_id = session.get("user_id")
    if not user_id:
        return None
    return database.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()


def account_profile_payload(user):
    preferred_method = user["preferred_payment_method"] or "auto"
    if preferred_method not in WOMPI_ALLOWED_PAYMENT_METHODS and preferred_method != "auto":
        preferred_method = "auto"
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
        "preferred_payment_method": preferred_method,
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
    message["From"] = formataddr(("XrteShop", sender))
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


def notify_customer_order_event(database, order_id, event, split_id=None):
    order = database.execute(
        """SELECT orders.customer_name, orders.payment_status, users.email
           FROM orders JOIN users ON users.id = orders.user_id
           WHERE orders.id = ?""",
        (order_id,),
    ).fetchone()
    if not order:
        app.logger.error("No se pudo notificar el pedido %s: no existe.", order_id)
        return False

    shipping = None
    if split_id:
        shipping = database.execute(
            """SELECT shipping_status, shipping_carrier, tracking_number,
                      estimated_delivery_at
               FROM payment_splits WHERE id = ? AND order_id = ?""",
            (split_id, order_id),
        ).fetchone()
    details = ""
    if shipping:
        details = (
            f"\nEstado del envío: {shipping['shipping_status']}."
            f"\nTransportadora: {shipping['shipping_carrier'] or 'Pendiente'}."
            f"\nNúmero de guía: {shipping['tracking_number'] or 'Pendiente'}."
            f"\nEntrega estimada: {shipping['estimated_delivery_at'] or 'Pendiente'}."
        )

    messages = {
        "pending": (
            "Pedido recibido; pago pendiente",
            "Recibimos tu pedido. El pago todavía está pendiente. "
            "Si pagas por Nequi, el vendedor debe confirmar manualmente la transferencia.",
        ),
        "paid": (
            "Pago confirmado",
            "El pago de tu pedido fue confirmado. El vendedor empezará a preparar tu compra.",
        ),
        "partial": (
            "Pago parcial recibido",
            "Recibimos una parte del pago, pero aún hay pagos del pedido pendientes. "
            "No vuelvas a pagar una transacción aprobada; revisa Mi cuenta > Mis pedidos.",
        ),
        "preparing": (
            "Tu pedido está en preparación",
            "El pago está confirmado y el vendedor está preparando tu pedido.",
        ),
        "shipped": ("Tu pedido fue enviado", "El vendedor registró el despacho de tu pedido."),
        "in_transit": ("Tu pedido está en camino", "El vendedor actualizó el seguimiento de tu pedido."),
        "delivered": ("Tu pedido figura como entregado", "El vendedor marcó tu pedido como entregado."),
        "return_received": (
            "Recibimos tu solicitud de devolución",
            "El equipo revisará tu solicitud. Este aviso no confirma un reembolso.",
        ),
        "return_updated": (
            "Actualización de tu solicitud de devolución",
            "El estado de tu solicitud de devolución fue actualizado por el vendedor.",
        ),
    }
    message = messages.get(event)
    if not message:
        raise ValueError(f"Evento de correo de pedido desconocido: {event}")
    subject, description = message
    body = (
        f"Hola {order['customer_name']},\n\n"
        f"{description}\n\n"
        f"Pedido XrteShop #{order_id}.{details}\n\n"
        "Puedes revisar el estado desde Mi cuenta > Mis pedidos.\n"
        "Si tienes dudas sobre una demora, responde a este correo."
    )
    return send_email_message(order["email"], f"{subject} · XrteShop", body)


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
        origin=product["origin"] if "origin" in keys else (
            "emprendedor" if seller_id is not None else "propio"
        ),
        seller_name=product["seller_name"] if "seller_name" in keys else None,
        external_catalog=product["external_catalog"] if "external_catalog" in keys else None,
        source_url=product["source_url"] if "source_url" in keys else None,
        active=product["active"] if "active" in keys else 1,
        base_price=base_price,
        flash_sale_percent=sale_percent if sale_active else 0,
        flash_sale_ends_at=sale_ends_at if sale_active else None,
        shipping_cost=product["shipping_cost"] if "shipping_cost" in keys else 0,
        shipping_min_days=product["shipping_min_days"] if "shipping_min_days" in keys else 3,
        shipping_max_days=product["shipping_max_days"] if "shipping_max_days" in keys else 7,
        low_stock=0 < result["stock"] <= 5,
        currency="COP",
        purchase_available=True,
        has_variants=bool(product["has_variants"]) if "has_variants" in keys else False,
        review_average=product["review_average"] if "review_average" in keys else 0,
        review_count=product["review_count"] if "review_count" in keys else 0,
    )
    return result


def variant_options_label(options_json):
    options = json.loads(options_json)
    return ", ".join(f"{name}: {value}" for name, value in options.items())


def product_variants(database, product_id):
    variants = database.execute(
        """SELECT id, options_json, stock, price FROM product_variants
           WHERE product_id = ? ORDER BY options_json""",
        (product_id,),
    ).fetchall()
    return [
        {
            "id": variant["id"],
            "options": json.loads(variant["options_json"]),
            "label": variant_options_label(variant["options_json"]),
            "stock": variant["stock"],
            "price": variant["price"],
        }
        for variant in variants
    ]


def parse_product_variants(value):
    if isinstance(value, str):
        if not value.strip():
            return []
        rows = []
        for line_number, line in enumerate(value.splitlines(), start=1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) not in (2, 3):
                raise ValueError(
                    f"La variante de la línea {line_number} debe usar "
                    "'Opción=valor; Opción=valor | existencias | precio opcional'."
                )
            options = {}
            for selection in parts[0].split(";"):
                name, separator, option = selection.partition("=")
                name, option = name.strip(), option.strip()
                if not separator or not name or not option:
                    raise ValueError(f"Revisa las opciones de la línea {line_number}.")
                options[name] = option
            rows.append(
                {
                    "options": options,
                    "stock": parts[1],
                    "price": parts[2] if len(parts) == 3 else None,
                }
            )
        value = rows
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Indica hasta 100 variantes por producto.")

    variants = []
    seen = set()
    total_stock = 0
    for index, variant in enumerate(value, start=1):
        if not isinstance(variant, dict) or not isinstance(variant.get("options"), dict):
            raise ValueError(f"La variante {index} no tiene opciones válidas.")
        options = {
            str(name).strip()[:40]: str(option).strip()[:60]
            for name, option in variant["options"].items()
        }
        if not 1 <= len(options) <= 3 or any(not name or not option for name, option in options.items()):
            raise ValueError(f"La variante {index} debe tener entre 1 y 3 opciones completas.")
        if len(set(options)) != len(options):
            raise ValueError(f"La variante {index} repite una opción.")
        options_json = json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if options_json in seen:
            raise ValueError("No repitas la misma combinación de opciones.")
        seen.add(options_json)
        try:
            stock = int(variant.get("stock", ""))
            price = int(variant["price"]) if variant.get("price") not in (None, "") else None
        except (TypeError, ValueError) as error:
            raise ValueError(f"Las existencias o el precio de la variante {index} no son válidos.") from error
        if not 0 <= stock <= 1000 or (price is not None and not 1 <= price <= 10_000_000_000):
            raise ValueError(f"Revisa las existencias o el precio de la variante {index}.")
        total_stock += stock
        variants.append({"options_json": options_json, "stock": stock, "price": price})
    if variants and (total_stock < 1 or total_stock > 1000):
        raise ValueError("Las variantes deben sumar entre 1 y 1.000 unidades disponibles.")
    return variants


def save_product_variants(database, product_id, variants):
    existing = {
        row["options_json"]: row["id"]
        for row in database.execute(
            "SELECT id, options_json FROM product_variants WHERE product_id = ?",
            (product_id,),
        )
    }
    new_options = {variant["options_json"] for variant in variants}
    removed = set(existing) - new_options
    for options_json in removed:
        variant_id = existing[options_json]
        ordered = database.execute(
            "SELECT 1 FROM order_items WHERE variant_id = ? LIMIT 1",
            (variant_id,),
        ).fetchone()
        if ordered:
            raise ValueError("No puedes eliminar una variante que aparece en pedidos anteriores.")
        database.execute("DELETE FROM product_variants WHERE id = ?", (variant_id,))
    for variant in variants:
        variant_id = existing.get(variant["options_json"])
        if variant_id:
            database.execute(
                "UPDATE product_variants SET stock = ?, price = ? WHERE id = ?",
                (variant["stock"], variant["price"], variant_id),
            )
        else:
            database.execute(
                """INSERT INTO product_variants (id, product_id, options_json, stock, price)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    product_id,
                    variant["options_json"],
                    variant["stock"],
                    variant["price"],
                ),
            )
    if variants:
        database.execute(
            "UPDATE products SET stock = ? WHERE id = ?",
            (sum(variant["stock"] for variant in variants), product_id),
        )


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
    configured_rate = os.environ.get("USD_COP_RATE", "").strip()
    if configured_rate:
        try:
            rate = Decimal(configured_rate)
        except InvalidOperation as error:
            raise ValueError("USD_COP_RATE debe ser una tasa USD/COP válida.") from error
        if not rate.is_finite() or rate <= 0:
            raise ValueError("USD_COP_RATE debe ser una tasa USD/COP positiva.")
        return rate, "USD_COP_RATE", False

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


def convert_mercadolibre_prices_to_cop(product, usd_cop_rate):
    """Add server-calculated COP costs and 10% retail prices to an API preview."""
    currency_code = str(product.get("supplier_currency") or "").upper()
    if currency_code not in {"COP", "USD"}:
        return product
    rate = Decimal("1") if currency_code == "COP" else usd_cop_rate
    converted = dict(product)
    converted["variants"] = [dict(variant) for variant in product.get("variants", [])]
    original_price = product.get("supplier_original_price")
    if original_price is not None:
        cost = Decimal(str(original_price)) * rate
        cost_cop = int(cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        converted["supplier_cost_cop"] = cost_cop
        converted["suggested_price"] = int(
            (Decimal(cost_cop) * Decimal("1.10")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    for variant in converted["variants"]:
        original_variant_price = variant.get("supplier_original_price")
        if original_variant_price is None:
            continue
        cost = Decimal(str(original_variant_price)) * rate
        cost_cop = int(cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        variant["supplier_cost_cop"] = cost_cop
        variant["suggested_price"] = int(
            (Decimal(cost_cop) * Decimal("1.10")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    return converted


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
    cart = {
        row["product_id"]: row["quantity"]
        for row in database.execute(
            "SELECT product_id, quantity FROM user_cart_items WHERE user_id = ?",
            (user_id,),
        )
    }
    cart.update(
        {
            f"variant:{row['variant_id']}": row["quantity"]
            for row in database.execute(
                "SELECT variant_id, quantity FROM user_cart_variant_items WHERE user_id = ?",
                (user_id,),
            )
        }
    )
    return cart


def write_user_cart(database, user_id, cart):
    database.execute("DELETE FROM user_cart_items WHERE user_id = ?", (user_id,))
    database.execute("DELETE FROM user_cart_variant_items WHERE user_id = ?", (user_id,))
    database.executemany(
        """INSERT INTO user_cart_items (user_id, product_id, quantity)
           VALUES (?, ?, ?)""",
        [
            (user_id, cart_key, quantity)
            for cart_key, quantity in cart.items()
            if not cart_key.startswith("variant:")
        ],
    )
    database.executemany(
        """INSERT INTO user_cart_variant_items (user_id, variant_id, quantity)
           VALUES (?, ?, ?)""",
        [
            (user_id, cart_key.removeprefix("variant:"), quantity)
            for cart_key, quantity in cart.items()
            if cart_key.startswith("variant:")
        ],
    )


def ensure_user_cart(database, user_id, legacy_cart=None):
    initialized = database.execute(
        "SELECT cart_initialized FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()["cart_initialized"]
    cart = read_user_cart(database, user_id)
    guest_items = legacy_cart or {}
    # La fusión siempre vuelve a comprobar que el producto siga activo y tenga existencias.
    for cart_key, raw_quantity in guest_items.items():
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        if str(cart_key).startswith("variant:"):
            variant_id = str(cart_key)[8:]
            variant = database.execute(
                f"""SELECT product_variants.stock
                   FROM product_variants JOIN products ON products.id = product_variants.product_id
                   WHERE product_variants.id = ? AND products.active = 1
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
                (variant_id,),
            ).fetchone()
            stock = variant["stock"] if variant else 0
        else:
            product = database.execute(
                f"""SELECT products.stock FROM products
                   WHERE products.id = ? AND products.active = 1
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                     AND NOT EXISTS (
                         SELECT 1 FROM product_variants
                         WHERE product_variants.product_id = products.id
                     )""",
                (cart_key,),
            ).fetchone()
            stock = product["stock"] if product else 0
        if stock <= 0:
            continue
        cart[cart_key] = min(20, stock, cart.get(cart_key, 0) + quantity)
    if guest_items or not initialized:
        write_user_cart(database, user_id, cart)
        database.execute("UPDATE users SET cart_initialized = 1 WHERE id = ?", (user_id,))
        database.commit()
    return cart


def current_cart(database, user=None):
    user = user or signed_in_user(database)
    if not user:
        return session.get("cart", {})
    legacy_cart = session.pop("cart", {})
    return ensure_user_cart(database, user["id"], legacy_cart)


def cart_json(database):
    cart = current_cart(database)
    items = []
    for cart_key, quantity in cart.items():
        variant_id = cart_key.removeprefix("variant:") if cart_key.startswith("variant:") else None
        if variant_id:
            row = database.execute(
                f"""SELECT products.*, users.name AS seller_name,
                          product_variants.options_json, product_variants.stock AS variant_stock,
                          product_variants.price AS variant_price
                   FROM product_variants
                   JOIN products ON products.id = product_variants.product_id
                   LEFT JOIN users ON users.id = products.seller_id
                   WHERE product_variants.id = ? AND products.active = 1
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
                (variant_id,),
            ).fetchone()
            if not row:
                continue
            product_id = row["id"]
        else:
            product_id = cart_key
            row = database.execute(
                f"""SELECT products.*, users.name AS seller_name FROM products
                   LEFT JOIN users ON users.id = products.seller_id
                   WHERE products.id = ? AND products.active = 1
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
                (product_id,),
            ).fetchone()
        if row:
            item = product_json(row)
            if variant_id:
                label = variant_options_label(row["options_json"])
                item.update(
                    id=cart_key,
                    product_id=product_id,
                    variant_id=variant_id,
                    variant_label=label,
                    name=f"{item['name']} · {label}",
                    stock=row["variant_stock"],
                )
                if row["variant_price"]:
                    base_price = row["variant_price"]
                    item["base_price"] = base_price
                    item["price"] = (
                        max(1, base_price * (100 - item["flash_sale_percent"]) // 100)
                        if item["flash_sale_percent"]
                        else base_price
                    )
            item["quantity"] = quantity
            item["line_total"] = item["price"] * quantity
            item["shipping_total"] = item["shipping_cost"] * quantity
            items.append(item)
    subtotal = sum(item["line_total"] for item in items)
    shipping_total = sum(item["shipping_total"] for item in items)
    return {
        "items": items,
        "count": sum(item["quantity"] for item in items),
        "total": subtotal,
        "shipping_total": shipping_total,
        "grand_total": subtotal + shipping_total,
    }


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
    if split.get("shipping_amount", 0):
        preference_items.append(
            {
                "id": "shipping",
                "title": "Envío",
                "quantity": 1,
                "unit_price": split["shipping_amount"],
                "currency_id": "COP",
            }
        )
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
            """UPDATE discount_coupons
               SET status = 'redeemed', redeemed_at = CURRENT_TIMESTAMP
               WHERE reserved_order_id = ? AND status = 'reserved'""",
            (order_id,),
        )
        order = database.execute(
            "SELECT user_id, total FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if order:
            database.execute(
                """INSERT OR IGNORE INTO loyalty_points_ledger (user_id, order_id, event, points)
                   VALUES (?, ?, 'purchase', ?)""",
                (order["user_id"], order_id, max(1, order["total"] // 10_000)),
            )
    elif payment_status == "failed":
        release_order_stock(database, order_id)
        database.execute(
            """UPDATE discount_coupons SET status = 'issued', reserved_order_id = NULL
               WHERE reserved_order_id = ?""",
            (order_id,),
        )
    return payment_status, order_status


def record_payment_ledger(
    database,
    *,
    entry_key,
    order_id,
    entry_type,
    amount_cop,
    split_id=None,
    order_item_id=None,
    seller_id=None,
    status="registrado",
    details=None,
):
    cursor = database.execute(
        """INSERT OR IGNORE INTO payment_ledger
           (entry_key, order_id, split_id, order_item_id, seller_id,
            entry_type, amount_cop, status, details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry_key,
            order_id,
            split_id,
            order_item_id,
            seller_id,
            entry_type,
            int(amount_cop),
            status,
            json.dumps(details or {}, ensure_ascii=True, sort_keys=True),
        ),
    )
    if cursor.rowcount:
        payment_audit(
            database,
            "ledger_entry_recorded",
            order_id=order_id,
            split_id=split_id,
            seller_id=seller_id,
            amount=amount_cop,
            details={"entry_key": entry_key, "entry_type": entry_type},
        )


def record_entrepreneur_payment_ledger(database, order_id):
    splits = database.execute(
        """SELECT id, seller_id, amount, commission_amount, net_amount
           FROM payment_splits WHERE order_id = ? AND seller_id IS NOT NULL""",
        (order_id,),
    ).fetchall()
    for split in splits:
        commission_base = database.execute(
            """SELECT COALESCE(SUM(gross_amount), 0) AS gross,
                      COALESCE(SUM(commission_xrte), 0) AS commission
               FROM order_items WHERE order_id = ? AND seller_id = ?""",
            (order_id, split["seller_id"]),
        ).fetchone()
        commission_rate = (
            format(
                Decimal(commission_base["commission"]) / Decimal(commission_base["gross"]),
                "f",
            )
            if commission_base["gross"]
            else "0"
        )
        record_payment_ledger(
            database,
            entry_key=f"seller-payable:{split['id']}",
            order_id=order_id,
            split_id=split["id"],
            seller_id=split["seller_id"],
            entry_type="entrepreneur_payable",
            amount_cop=split["net_amount"],
            status="pendiente_de_pago_al_emprendedor",
            details={"gross_cop": split["amount"], "commission_rate": commission_rate},
        )
        record_payment_ledger(
            database,
            entry_key=f"xrteshop-commission:{split['id']}",
            order_id=order_id,
            split_id=split["id"],
            seller_id=split["seller_id"],
            entry_type="xrteshop_commission",
            amount_cop=split["commission_amount"],
            details={"commission_rate": commission_rate},
        )


def payment_audit(database, action, order_id=None, split_id=None, seller_id=None,
                  actor_user_id=None, amount=0, provider_reference=None, details=None):
    database.execute(
        """INSERT INTO payment_audit_logs
           (order_id, split_id, seller_id, actor_user_id, action, amount,
            provider_reference, details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order_id, split_id, seller_id, actor_user_id, action, int(amount),
            provider_reference, json.dumps(details or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def payment_rate_limit_exceeded(database, user_id):
    now = datetime.now(timezone.utc)
    identity = f"{user_id}:{request.remote_addr or 'unknown'}"
    key_hash = hmac.new(
        SESSION_SECRET.encode("utf-8"), identity.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    row = database.execute(
        "SELECT request_count, window_started_at FROM payment_rate_limits WHERE key_hash = ?",
        (key_hash,),
    ).fetchone()
    if row:
        try:
            elapsed = (now - datetime.fromisoformat(row["window_started_at"])).total_seconds()
        except ValueError:
            elapsed = PAYMENT_RATE_WINDOW_SECONDS + 1
        if elapsed < PAYMENT_RATE_WINDOW_SECONDS and row["request_count"] >= PAYMENT_RATE_LIMIT:
            return True
        if elapsed >= PAYMENT_RATE_WINDOW_SECONDS:
            database.execute(
                """UPDATE payment_rate_limits SET request_count = 1, window_started_at = ?
                   WHERE key_hash = ?""",
                (now.isoformat(), key_hash),
            )
        else:
            database.execute(
                "UPDATE payment_rate_limits SET request_count = request_count + 1 WHERE key_hash = ?",
                (key_hash,),
            )
    else:
        database.execute(
            """INSERT INTO payment_rate_limits (key_hash, request_count, window_started_at)
               VALUES (?, 1, ?)""",
            (key_hash, now.isoformat()),
        )
    database.commit()
    return False


def update_order_after_payu_payment(database, order, notification):
    state = str(notification.get("state_pol", ""))
    transaction_id = str(notification.get("transaction_id", "")).strip()
    if state == "4":
        if order["payment_status"] == "approved":
            return
        now = datetime.now(timezone.utc)
        settings = payment_settings()
        due_at = (now + timedelta(days=settings["hold_days"])).isoformat()
        database.execute(
            """UPDATE orders SET payment_status = 'approved', status = 'Recibido',
               provider_payment_id = ?, disputed = 0 WHERE id = ?""",
            (transaction_id, order["id"]),
        )
        database.execute(
            """UPDATE payment_splits SET status = 'approved', payment_id = ?,
               transfer_status = CASE WHEN seller_id IS NULL THEN 'not_applicable'
                                      ELSE 'scheduled' END,
               payout_due_at = CASE WHEN seller_id IS NULL THEN NULL ELSE ? END
               WHERE order_id = ?""",
            (transaction_id, due_at, order["id"]),
        )
        database.execute(
            """UPDATE order_items SET transfer_status = CASE WHEN seller_id IS NULL
                   THEN 'not_applicable' ELSE 'scheduled' END
               WHERE order_id = ?""",
            (order["id"],),
        )
        coupon = database.execute(
            "SELECT id FROM discount_coupons WHERE reserved_order_id = ? AND status = 'reserved'",
            (order["id"],),
        ).fetchone()
        if coupon:
            database.execute(
                """UPDATE discount_coupons SET status = 'redeemed',
                   redeemed_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (coupon["id"],),
            )
        database.execute(
            """INSERT OR IGNORE INTO loyalty_points_ledger (user_id, order_id, event, points)
               VALUES (?, ?, 'purchase', ?)""",
            (order["user_id"], order["id"], max(1, order["total"] // 10_000)),
        )
        payment_audit(
            database, "payment_approved", order_id=order["id"], amount=order["total"],
            provider_reference=transaction_id,
        )
    elif state in {"5", "6"}:
        database.execute(
            """UPDATE orders SET payment_status = 'failed', status = 'Pago no completado',
               provider_payment_id = ? WHERE id = ? AND payment_status = 'pending'""",
            (transaction_id, order["id"]),
        )
        database.execute(
            """UPDATE payment_splits SET status = 'rejected'
               WHERE order_id = ? AND status = 'pending'""",
            (order["id"],),
        )
        release_order_stock(database, order["id"])
        database.execute(
            """UPDATE discount_coupons SET status = 'issued', reserved_order_id = NULL
               WHERE reserved_order_id = ? AND status = 'reserved'""",
            (order["id"],),
        )
        payment_audit(
            database, "payment_failed", order_id=order["id"], amount=order["total"],
            provider_reference=transaction_id, details={"state_pol": state},
        )


def release_order_stock(database, order_id):
    order = database.execute(
        "SELECT stock_released FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if not order or order["stock_released"]:
        return
    items = database.execute(
        "SELECT product_id, variant_id, quantity FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    for item in items:
        if item["variant_id"]:
            database.execute(
                "UPDATE product_variants SET stock = stock + ? WHERE id = ?",
                (item["quantity"], item["variant_id"]),
            )
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
    payment_status, _ = order_payment_status(database, split["order_id"])
    database.commit()
    if payment_status == "approved":
        notify_customer_order_event(database, split["order_id"], "paid")
    elif payment_status == "partial":
        notify_customer_order_event(database, split["order_id"], "partial")
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
    return render_template(
        "index.html",
        seo_origin=public_origin(),
        payment_environment=wompi_environment(),
    )


@app.get("/api/public-config")
def public_config():
    return jsonify(
        ga4_id=os.environ.get("PEMU_GA4_MEASUREMENT_ID", "").strip(),
        meta_pixel_id=os.environ.get("PEMU_META_PIXEL_ID", "").strip(),
        payment_environment=wompi_environment(),
        wompi_public_key=wompi_public_key(),
        social_links={
            name: value
            for name, variable in {
                "instagram": "PEMU_INSTAGRAM_URL",
                "facebook": "PEMU_FACEBOOK_URL",
                "tiktok": "PEMU_TIKTOK_URL",
                "youtube": "PEMU_YOUTUBE_URL",
                "pinterest": "PEMU_PINTEREST_URL",
                "whatsapp": "PEMU_WHATSAPP_URL",
            }.items()
            if safe_https_url(value := os.environ.get(variable, "").strip())
        },
    )


@app.get("/api/wompi/acceptance")
def wompi_acceptance_info():
    if not session.get("user_id"):
        return jsonify(error="Inicia sesión para consultar la información de pago."), 401
    try:
        info = WompiPaymentProvider().acceptance_info()
    except WompiPaymentProviderError as error:
        app.logger.error("Wompi acceptance information unavailable: %s", error)
        return jsonify(error="Los documentos de aceptación de pago no están disponibles."), 503
    return jsonify(
        acceptance_token=info["acceptance_token"],
        acceptance_url=info["acceptance_url"],
        accept_personal_auth=info["accept_personal_auth"],
        personal_data_url=info["personal_data_url"],
    )


@app.get("/producto/<product_id>")
def product_detail_page(product_id):
    with closing(connect_db()) as database:
        product = database.execute(
            f"""SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.id = ? AND products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                 AND {SELLER_PAYMENT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        seo_product = None
        if product:
            payload = product_json(product)
            origin = public_origin()
            product_url = f"{origin}{url_for('product_detail_page', product_id=product_id)}"
            image = product["image"]
            image_url = image if safe_https_url(image) else f"{origin}/{quote(image.lstrip('/'), safe='/')}"
            seo_product = {
                "name": payload["name"],
                "description": payload["description"][:300],
                "canonical": product_url,
                "image": image_url,
                "price": payload["price"],
                "availability": "https://schema.org/InStock" if payload["stock"] > 0 else "https://schema.org/OutOfStock",
                "structured_data": {
                    "@context": "https://schema.org",
                    "@type": "Product",
                    "name": payload["name"],
                    "description": payload["description"][:300],
                    "image": image_url,
                    "sku": product_id,
                    "brand": {"@type": "Brand", "name": "XrteShop"},
                    "offers": {
                        "@type": "Offer",
                        "url": product_url,
                        "priceCurrency": "COP",
                        "price": payload["price"],
                        "availability": "https://schema.org/InStock" if payload["stock"] > 0 else "https://schema.org/OutOfStock",
                    },
                },
            }
    return render_template("producto.html", seo_product=seo_product, seo_origin=public_origin())


def public_origin():
    configured = os.environ.get("PEMU_PUBLIC_URL", "").strip().rstrip("/")
    origin = configured or request.url_root.rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PEMU_PUBLIC_URL debe ser una URL HTTP o HTTPS válida.")
    return origin


@app.get("/robots.txt")
def robots_txt():
    sitemap = f"{public_origin()}/sitemap.xml"
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin\n"
        f"Sitemap: {sitemap}\n",
        mimetype="text/plain",
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    origin = public_origin()
    static_paths = ("/", "/politicas", "/emprendedores")
    urls = [f"{origin}{path}" for path in static_paths]
    with closing(connect_db()) as database:
        products = database.execute(
            f"""SELECT products.id FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.active = 1 AND products.stock > 0
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                 AND {SELLER_PAYMENT_VISIBILITY_SQL}
               ORDER BY products.id LIMIT 5000"""
        ).fetchall()
    urls.extend(
        f"{origin}{url_for('product_detail_page', product_id=product['id'])}"
        for product in products
    )
    entries = "".join(f"<url><loc>{escape(url)}</loc></url>" for url in urls)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>",
        mimetype="application/xml",
    )


@app.get("/cuenta")
@app.get("/cuenta.html")
def account_page():
    return render_template("cuenta.html")


@app.get("/emprendedores")
def entrepreneur_page():
    return render_template("emprendedores.html")


@app.get("/admin")
def admin_page():
    return render_template("admin.html")


@app.get("/politicas")
def store_policies():
    return render_template("politicas.html")


@app.get("/index.html")
def legacy_home():
    return redirect("/")


@app.get("/Ofertas.html")
def legacy_offers():
    return redirect("/?ofertas=1")


@app.get("/moda.html")
def legacy_fashion():
    return redirect("/?categoria=Moda")


@app.before_request
def ensure_session_is_permanent():
    session.permanent = True


@app.before_request
def restore_authentication_from_cookies():
    g.auth_session_id = None
    g.pending_auth_tokens = None
    g.clear_auth_cookies = False
    if request.endpoint == "static" and request.path != "/static/js/admin.js":
        return None

    access_token = request.cookies.get(app.config["AUTH_ACCESS_COOKIE"], "")
    refresh_token = request.cookies.get(app.config["AUTH_REFRESH_COOKIE"], "")
    if not access_token and not refresh_token and not session.get("user_id"):
        return None

    now = auth_now().isoformat(timespec="seconds")
    access_claims = decode_jwt_access(access_token)
    with closing(connect_db()) as database:
        if access_claims:
            row = database.execute(
                """SELECT auth_sessions.id, auth_sessions.user_id, users.id AS active_user_id
                   FROM auth_sessions JOIN users ON users.id = auth_sessions.user_id
                   WHERE auth_sessions.id = ? AND auth_sessions.user_id = ?
                     AND auth_sessions.revoked_at IS NULL AND auth_sessions.expires_at > ?""",
                (access_claims.get("sid"), access_claims.get("sub"), now),
            ).fetchone()
            if row:
                g.auth_session_id = row["id"]
                session["user_id"] = row["active_user_id"]
                session["auth_session_id"] = row["id"]
                session.permanent = True
                return None

        if refresh_token:
            token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
            row = database.execute(
                """SELECT auth_sessions.id, auth_sessions.user_id, auth_sessions.remember_me,
                          auth_sessions.expires_at,
                          auth_sessions.admin_2fa_verified_at AS admin_session_created_at,
                          users.id AS active_user_id
                   FROM auth_sessions JOIN users ON users.id = auth_sessions.user_id
                   WHERE auth_sessions.refresh_token_hash = ? AND auth_sessions.revoked_at IS NULL
                     AND auth_sessions.expires_at > ?""",
                (token_hash, now),
            ).fetchone()
            if row:
                if row["admin_session_created_at"]:
                    expires_at = datetime.fromisoformat(row["expires_at"])
                    refresh_max_age = max(0, int((expires_at - auth_now()).total_seconds()))
                else:
                    # Renovación deslizante para las sesiones normales de la tienda.
                    refresh_days = (
                        app.config["AUTH_REFRESH_DAYS"]
                        if row["remember_me"]
                        else app.config["AUTH_REFRESH_SHORT_DAYS"]
                    )
                    expires_at = auth_now() + timedelta(days=refresh_days)
                    refresh_max_age = refresh_days * 24 * 60 * 60
                database.execute(
                    "UPDATE auth_sessions SET last_used_at = ?, expires_at = ? WHERE id = ?",
                    (
                        now,
                        expires_at.isoformat(timespec="seconds"),
                        row["id"],
                    ),
                )
                database.commit()
                g.auth_session_id = row["id"]
                g.pending_auth_tokens = {
                    "session_id": row["id"],
                    "access_token": encode_jwt_access(row["active_user_id"], row["id"]),
                    "refresh_token": refresh_token,
                    "refresh_expires_at": expires_at,
                    "refresh_max_age": refresh_max_age,
                }
                session["user_id"] = row["active_user_id"]
                session["auth_session_id"] = row["id"]
                session.permanent = True
                return None
            g.clear_auth_cookies = True

        if session.get("auth_session_id"):
            session.pop("user_id", None)
            session.pop("auth_session_id", None)

        legacy_user_id = session.get("user_id")
        if legacy_user_id:
            user = database.execute("SELECT id FROM users WHERE id = ?", (legacy_user_id,)).fetchone()
            if user:
                auth_tokens = create_auth_session(database, user["id"], True)
                database.commit()
                g.auth_session_id = auth_tokens["session_id"]
                g.pending_auth_tokens = auth_tokens
                g.clear_auth_cookies = False
                session["auth_session_id"] = auth_tokens["session_id"]
                session.permanent = True
            else:
                session.pop("user_id", None)


def require_admin():
    """Valida identidad y sesión administrativa persistida; nunca confía en datos del cliente."""
    user_id = session.get("user_id")
    session_id = getattr(g, "auth_session_id", None)
    if not user_id or not session_id or not ADMIN_EMAIL:
        abort(404)

    now = auth_now()
    now_text = now.isoformat(timespec="seconds")
    idle_cutoff = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    with closing(connect_db()) as database:
        auth = database.execute(
            """SELECT users.id, users.role, users.email,
                      auth_sessions.last_used_at, auth_sessions.expires_at
               FROM auth_sessions JOIN users ON users.id = auth_sessions.user_id
               WHERE auth_sessions.id = ? AND auth_sessions.user_id = ?
                 AND auth_sessions.revoked_at IS NULL
                 AND auth_sessions.expires_at > ?
                 AND auth_sessions.last_used_at > ?""",
            (session_id, user_id, now_text, idle_cutoff),
        ).fetchone()
        if (
            not auth
            or auth["role"] != "admin"
            or normalize_email(auth["email"]) != ADMIN_EMAIL
            or session.get("auth_session_id") != session_id
        ):
            abort(404)

        identity = f"{auth['id']}:{request.remote_addr or 'unknown'}"
        rate_key = hmac.new(
            SESSION_SECRET.encode("utf-8"), identity.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        rate = database.execute(
            "SELECT request_count, window_started_at FROM admin_api_rate_limits WHERE key_hash = ?",
            (rate_key,),
        ).fetchone()
        if rate:
            window_started = datetime.fromisoformat(rate["window_started_at"])
            if (now - window_started).total_seconds() < 60 and rate["request_count"] >= 120:
                return jsonify(error="Límite de solicitudes administrativas alcanzado."), 429
            if (now - window_started).total_seconds() >= 60:
                database.execute(
                    """UPDATE admin_api_rate_limits
                       SET request_count = 1, window_started_at = ? WHERE key_hash = ?""",
                    (now_text, rate_key),
                )
            else:
                database.execute(
                    """UPDATE admin_api_rate_limits SET request_count = request_count + 1
                       WHERE key_hash = ?""",
                    (rate_key,),
                )
        else:
            database.execute(
                """INSERT INTO admin_api_rate_limits (key_hash, request_count, window_started_at)
                   VALUES (?, 1, ?)""",
                (rate_key, now_text),
            )

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not token or not supplied or not secrets.compare_digest(token, supplied):
                return jsonify(error="La sesión expiró. Recarga la página e inténtalo de nuevo."), 403
            database.execute(
                """INSERT INTO admin_audit_log
                   (actor_user_id, action, affected_resource, ip_address)
                   VALUES (?, ?, ?, ?)""",
                (
                    auth["id"],
                    f"{request.method} {request.endpoint or 'admin'}",
                    request.path,
                    request.remote_addr or "unknown",
                ),
            )
        elif request.path.endswith("/reveal"):
            database.execute(
                """INSERT INTO admin_audit_log
                   (actor_user_id, action, affected_resource, ip_address)
                   VALUES (?, 'view_sensitive_account', ?, ?)""",
                (auth["id"], request.path, request.remote_addr or "unknown"),
            )
        database.execute(
            "UPDATE auth_sessions SET last_used_at = ? WHERE id = ?",
            (now_text, session_id),
        )
        database.commit()
    return None


@app.before_request
def enforce_admin_boundary():
    if request.path == "/admin" or request.path.startswith("/admin/"):
        return require_admin()
    if request.path == "/api/admin" or request.path.startswith("/api/admin/"):
        return require_admin()
    if request.path == "/static/js/admin.js":
        return require_admin()


@app.after_request
def set_security_headers_and_auth_cookies(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if (
        request.path == "/"
        or request.path == "/admin"
        or request.path.startswith("/admin/")
        or request.path == "/api/admin"
        or request.path.startswith("/api/admin/")
        or request.path == "/api/productos"
        or request.path == "/static/js/admin.js"
    ):
        response.headers["Cache-Control"] = "no-store"
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed_origins = {
        item.strip().rstrip("/")
        for item in os.environ.get("PEMU_CORS_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers.add("Vary", "Origin")
    if app.config["AUTH_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if getattr(g, "pending_auth_tokens", None):
        attach_auth_cookies(response, g.pending_auth_tokens)
    if getattr(g, "clear_auth_cookies", False):
        clear_auth_cookies(response)
    return response


@app.get("/api/session")
def get_session():
    with closing(connect_db()) as database:
        user_id = session.get("user_id")
        user = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
        if user:
            legacy_cart = session.pop("cart", {})
            ensure_user_cart(database, user["id"], legacy_cart)
        else:
            session.pop("user_id", None)
        return jsonify(
            csrf_token=csrf_token(),
            user=account_profile_payload(user) if user else None,
        )


@app.get("/api/auth/me")
def auth_me():
    with closing(connect_db()) as database:
        user = database.execute(
            "SELECT * FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone() if session.get("user_id") else None
        return jsonify(
            user=account_profile_payload(user) if user else None,
            csrf_token=csrf_token(),
        )


@app.post("/api/auth/refresh")
def refresh_authentication():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user = database.execute(
            "SELECT * FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone() if session.get("user_id") else None
        if not user or not g.auth_session_id:
            return jsonify(error="La sesión expiró. Inicia sesión de nuevo."), 401
        return jsonify(user=account_profile_payload(user))


@app.post("/api/contact")
def submit_contact_message():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if data.get("website"):
        return jsonify(ok=True), 202
    name = str(data.get("name", "")).strip()
    email = normalize_email(data.get("email", ""))
    subject = str(data.get("subject", "")).strip()
    message = str(data.get("message", "")).strip()
    if not 2 <= len(name) <= 80 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Revisa tu nombre y correo electrónico."), 400
    if not 3 <= len(subject) <= 120 or not 20 <= len(message) <= 3000:
        return jsonify(error="El asunto debe tener hasta 120 caracteres y el mensaje entre 20 y 3.000."), 400
    with closing(connect_db()) as database:
        cursor = database.execute(
            """INSERT INTO contact_messages (name, email, subject, message)
               VALUES (?, ?, ?, ?)""",
            (name, email, subject, message),
        )
        database.commit()
        message_id = cursor.lastrowid
    notified = False
    if ADMIN_EMAIL:
        notified = send_email_message(
            ADMIN_EMAIL,
            f"Nuevo mensaje de XrteShop: {subject}",
            f"De: {name} <{email}>\n\n{message}\n\nReferencia interna: {message_id}",
        )
    return jsonify(
        ok=True,
        email_sent=notified,
        message=(
            "Recibimos tu mensaje y enviamos una notificación."
            if notified
            else "Tu mensaje quedó guardado. La notificación por correo no está configurada."
        ),
    ), 201


@app.post("/api/newsletter")
def subscribe_newsletter():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if data.get("website"):
        return jsonify(ok=True), 202
    email = normalize_email(data.get("email", ""))
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if data.get("consent") is not True:
        return jsonify(error="Acepta el uso de tu correo para recibir novedades."), 400
    with closing(connect_db()) as database:
        database.execute(
            """INSERT INTO newsletter_subscribers (email, active)
               VALUES (?, 1)
               ON CONFLICT(email) DO UPDATE SET active = 1, consented_at = CURRENT_TIMESTAMP""",
            (email,),
        )
        database.commit()
    return jsonify(ok=True, message="Te suscribiste correctamente."), 201


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
        if payment_method not in WOMPI_ALLOWED_PAYMENT_METHODS | {"auto"}:
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
    query = f"""SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.active = 1 AND products.stock > 0
               AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
               AND {SELLER_PAYMENT_VISIBILITY_SQL}"""
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
    query += """ ORDER BY products.supplier_platform IS NOT NULL DESC,
                          products.supplier_updated_at DESC,
                          products.seller_id IS NOT NULL,
                          products.id"""
    with closing(connect_db()) as database:
        products = database.execute(query, parameters).fetchall()
        categories = database.execute(
            f"""SELECT DISTINCT products.category FROM products
               WHERE products.active = 1 AND products.stock > 0
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
               ORDER BY products.category"""
        ).fetchall()
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
    try:
        minimum_price = int(request.args["min_price"]) if request.args.get("min_price") else None
        maximum_price = int(request.args["max_price"]) if request.args.get("max_price") else None
    except ValueError:
        return jsonify(error="Los límites de precio deben ser números enteros."), 400
    if (
        (minimum_price is not None and not 0 <= minimum_price <= 10_000_000_000)
        or (maximum_price is not None and not 0 <= maximum_price <= 10_000_000_000)
        or (
            minimum_price is not None
            and maximum_price is not None
            and minimum_price > maximum_price
        )
    ):
        return jsonify(error="Revisa el rango de precio seleccionado."), 400
    availability = request.args.get("availability", "available").strip()
    if availability not in {"available", "all"}:
        return jsonify(error="El filtro de disponibilidad no es válido."), 400
    favorites_only = request.args.get("favorites_only") == "1"
    offers_only = request.args.get("offers") == "1"
    local_query = f"""SELECT products.*, users.name AS seller_name,
                     EXISTS(SELECT 1 FROM product_variants
                            WHERE product_variants.product_id = products.id) AS has_variants,
                     (SELECT ROUND(AVG(rating), 1) FROM product_reviews
                      WHERE product_reviews.product_id = products.id) AS review_average,
                     (SELECT COUNT(*) FROM product_reviews
                      WHERE product_reviews.product_id = products.id) AS review_count
                     FROM products
                     LEFT JOIN users ON users.id = products.seller_id
                     WHERE products.active = 1
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                     AND {SELLER_PAYMENT_VISIBILITY_SQL}"""
    if availability == "available":
        local_query += " AND products.stock > 0"
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
    current_price_sql = """CASE WHEN products.flash_sale_percent > 0
                                AND products.flash_sale_ends_at > ?
                           THEN MAX(1, products.price * (100 - products.flash_sale_percent) / 100)
                           ELSE products.price END"""
    if minimum_price is not None:
        local_query += f" AND {current_price_sql} >= ?"
        parameters.extend([datetime.now(timezone.utc).isoformat(), minimum_price])
    if maximum_price is not None:
        local_query += f" AND {current_price_sql} <= ?"
        parameters.extend([datetime.now(timezone.utc).isoformat(), maximum_price])
    if favorites_only:
        local_query += """ AND EXISTS (
            SELECT 1 FROM user_favorites
            WHERE user_favorites.product_id = products.id AND user_favorites.user_id = ?
        )"""

    order_by = {
        "featured": """products.supplier_platform IS NOT NULL DESC,
                       products.supplier_updated_at DESC,
                       products.seller_id IS NOT NULL DESC,
                       products.id ASC""",
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

    offset = (page - 1) * limit
    with closing(connect_db()) as database:
        if favorites_only:
            user = signed_in_user(database)
            if not user:
                return jsonify(error="Inicia sesión para ver tus productos favoritos."), 401
            parameters.append(user["id"])
        local_count = database.execute(
            f"SELECT COUNT(*) FROM ({local_query}) AS matching_products",
            parameters,
        ).fetchone()[0]
        local_categories = database.execute(
            f"""SELECT DISTINCT products.category FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                 AND {SELLER_PAYMENT_VISIBILITY_SQL}
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

    total = local_count
    categories = sorted({row["category"] for row in local_categories}, key=str.casefold)
    for product in page_products:
        product["language"] = language
        product.update(
            nombre=product["name"],
            precio=product["price"],
            descripcion=product["description"],
            imagen=product["image"],
            categoria=product["category"],
            vendedor=product["seller_name"] or "XrteShop",
        )
    return jsonify(
        products=page_products,
        categories=categories,
        page=page,
        limit=limit,
        total=total,
        total_pages=(total + limit - 1) // limit,
        warning=None,
        exchange_rate=None,
        exchange_rate_updated_at=None,
    )


@app.get("/api/products/recommendations")
def recommended_products():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        query = f"""SELECT products.*, users.name AS seller_name FROM products
                   LEFT JOIN users ON users.id = products.seller_id
                   WHERE products.active = 1 AND products.stock > 0
                       AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
                       AND {SELLER_PAYMENT_VISIBILITY_SQL}"""
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
                f"""SELECT products.id FROM products
                   WHERE products.id = ? AND products.active = 1 AND products.stock > 0
                     AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
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
        balance = database.execute(
            """SELECT COALESCE(SUM(points), 0) AS balance,
                      COALESCE(SUM(CASE WHEN points > 0 THEN points ELSE 0 END), 0) AS earned
               FROM loyalty_points_ledger WHERE user_id = ?""",
            (user["id"],),
        ).fetchone()
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
        total_earned = balance["earned"]
        tier = "oro" if total_earned >= 1500 else "plata" if total_earned >= 500 else "bronce"
        return jsonify(
            coupons=available,
            can_spin=recent is None,
            points_balance=balance["balance"],
            points_earned=total_earned,
            loyalty_tier=tier,
            points_until_reward=max(0, 100 - balance["balance"] % 100),
        )


@app.post("/api/rewards/redeem")
def redeem_loyalty_points():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        points = int(data.get("points", 0))
    except (TypeError, ValueError):
        return jsonify(error="Indica una cantidad válida de puntos."), 400
    if points < 100 or points > 5000 or points % 100:
        return jsonify(error="Canjea entre 100 y 5.000 puntos, en múltiplos de 100."), 400
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para canjear tus puntos."), 401
        verified = database.execute(
            "SELECT email_verified FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if not verified["email_verified"]:
            return jsonify(error="Verifica tu correo antes de canjear puntos."), 403
        database.execute("BEGIN IMMEDIATE")
        balance = database.execute(
            "SELECT COALESCE(SUM(points), 0) FROM loyalty_points_ledger WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0]
        if points > balance:
            return jsonify(error="No tienes suficientes puntos para este canje."), 409
        code = f"PEMU-PUNTOS-{secrets.token_hex(4).upper()}"
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        database.execute(
            """INSERT INTO discount_coupons
               (user_id, code, discount_percent, max_discount, expires_at)
               VALUES (?, ?, 100, ?, ?)""",
            (user["id"], code, points * 10, expires_at),
        )
        database.execute(
            """INSERT INTO loyalty_points_ledger (user_id, event, points)
               VALUES (?, 'redemption', ?)""",
            (user["id"], -points),
        )
        database.commit()
        return jsonify(
            coupon={"code": code, "discount_percent": 100, "max_discount": points * 10, "expires_at": expires_at},
            points_balance=balance - points,
            message="Canje realizado. Tu cupón es de un solo uso y vence en 30 días.",
        ), 201


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
        notify_customer_order_event(database, order_id, "return_received", split["id"])
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
            f"""SELECT products.*, users.name AS seller_name FROM products
               LEFT JOIN users ON users.id = products.seller_id
               WHERE products.id = ? AND products.active = 1
               AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}
               AND {SELLER_PAYMENT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="Este producto ya no está disponible."), 404
        payload = product_json(product)
        payload["variants"] = product_variants(database, product_id)
        payload["has_variants"] = bool(payload["variants"])
        payload["image_urls"] = [
            row["image_url"]
            for row in database.execute(
                """SELECT image_url FROM supplier_product_images
                   WHERE product_id = ? ORDER BY position, created_at""",
                (product_id,),
            )
        ]
        if not payload["image_urls"]:
            payload["image_urls"] = [payload["image"]]
        summary = database.execute(
            """SELECT COUNT(*) AS count, COALESCE(AVG(rating), 0) AS average
               FROM product_reviews WHERE product_id = ?""",
            (product_id,),
        ).fetchone()
        payload["review_count"] = summary["count"]
        payload["review_average"] = round(summary["average"], 1)
        return jsonify(product=payload)


@app.get("/api/products/<product_id>/reviews")
def list_product_reviews(product_id):
    with closing(connect_db()) as database:
        product = database.execute(
            f"""SELECT products.id FROM products
               WHERE products.id = ? AND products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="Este producto ya no está disponible."), 404
        reviews = database.execute(
            """SELECT product_reviews.rating, product_reviews.review,
                      product_reviews.created_at, users.name AS reviewer
               FROM product_reviews JOIN users ON users.id = product_reviews.user_id
               WHERE product_reviews.product_id = ?
               ORDER BY product_reviews.created_at DESC LIMIT 50""",
            (product_id,),
        ).fetchall()
        summary = database.execute(
            """SELECT COUNT(*) AS count, COALESCE(AVG(rating), 0) AS average
               FROM product_reviews WHERE product_id = ?""",
            (product_id,),
        ).fetchone()
        user = signed_in_user(database)
        can_review = False
        existing_review = None
        if user:
            can_review = bool(database.execute(
                """SELECT 1 FROM order_items
                   JOIN orders ON orders.id = order_items.order_id
                   WHERE order_items.product_id = ? AND orders.user_id = ?
                     AND orders.payment_status = 'approved' LIMIT 1""",
                (product_id, user["id"]),
            ).fetchone())
            existing_review = database.execute(
                """SELECT rating, review FROM product_reviews
                   WHERE product_id = ? AND user_id = ?""",
                (product_id, user["id"]),
            ).fetchone()
        return jsonify(
            reviews=[dict(review) for review in reviews],
            count=summary["count"],
            average=round(summary["average"], 1),
            can_review=can_review,
            existing_review=dict(existing_review) if existing_review else None,
        )


@app.post("/api/products/<product_id>/reviews")
def create_product_review(product_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        rating = int(data.get("rating", ""))
    except (TypeError, ValueError):
        return jsonify(error="Selecciona una calificación de 1 a 5 estrellas."), 400
    review = str(data.get("review", "")).strip()
    if not 1 <= rating <= 5 or not 10 <= len(review) <= 1000:
        return jsonify(error="La reseña debe tener entre 10 y 1.000 caracteres y una calificación válida."), 400
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para dejar una reseña."), 401
        product = database.execute(
            f"""SELECT products.id FROM products
               WHERE products.id = ? AND products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="Este producto ya no está disponible."), 404
        purchased = database.execute(
            """SELECT 1 FROM order_items
               JOIN orders ON orders.id = order_items.order_id
               WHERE order_items.product_id = ? AND orders.user_id = ?
                 AND orders.payment_status = 'approved' LIMIT 1""",
            (product_id, user["id"]),
        ).fetchone()
        if not purchased:
            return jsonify(error="Solo puedes reseñar productos de pedidos pagados."), 403
        try:
            database.execute(
                """INSERT INTO product_reviews (user_id, product_id, rating, review)
                   VALUES (?, ?, ?, ?)""",
                (user["id"], product_id, rating, review),
            )
        except DATABASE_INTEGRITY_ERRORS:
            return jsonify(error="Ya publicaste una reseña para este producto."), 409
        database.commit()
        return jsonify(ok=True), 201


@app.get("/api/favorites")
def list_favorites():
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para ver tus favoritos."), 401
        rows = database.execute(
            """SELECT products.id FROM user_favorites
               JOIN products ON products.id = user_favorites.product_id
               WHERE user_favorites.user_id = ? AND products.active = 1
               ORDER BY user_favorites.created_at DESC""",
            (user["id"],),
        ).fetchall()
        return jsonify(product_ids=[row["id"] for row in rows])


@app.route("/api/favorites/<product_id>", methods=["POST", "DELETE"])
def update_favorite(product_id):
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        if not user:
            return jsonify(error="Inicia sesión para guardar tus favoritos."), 401
        product = database.execute(
            f"""SELECT products.id FROM products
               WHERE products.id = ? AND products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="Este producto ya no está disponible."), 404
        if request.method == "POST":
            database.execute(
                "INSERT OR IGNORE INTO user_favorites (user_id, product_id) VALUES (?, ?)",
                (user["id"], product_id),
            )
        else:
            database.execute(
                "DELETE FROM user_favorites WHERE user_id = ? AND product_id = ?",
                (user["id"], product_id),
            )
        database.commit()
        return jsonify(favorite=request.method == "POST")


def safe_https_url(value):
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and len(str(value)) <= 2048


def carrier_tracking_url(carrier, tracking_number):
    template = os.environ.get("PEMU_TRACKING_URL_TEMPLATE", "").strip()
    if not template or not tracking_number:
        return None
    try:
        target = template.format(
            carrier=quote(str(carrier or ""), safe=""),
            tracking_number=quote(str(tracking_number), safe=""),
        )
    except (KeyError, ValueError) as error:
        app.logger.error("PEMU_TRACKING_URL_TEMPLATE is invalid: %s", error)
        return None
    if not safe_https_url(target):
        app.logger.error("PEMU_TRACKING_URL_TEMPLATE must produce an HTTPS URL.")
        return None
    return target


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
    price, stock = validate_listing_price_stock(data.get("price"), data.get("stock"))
    return name, category, description, price, stock


def validate_listing_price_stock(price_value, stock_value):
    if any(
        isinstance(value, bool) or not isinstance(value, (int, str))
        for value in (price_value, stock_value)
    ):
        raise ValueError("El precio y las existencias deben ser números enteros.")
    try:
        price = int(price_value)
        stock = int(stock_value)
    except (TypeError, ValueError) as error:
        raise ValueError("El precio y las existencias deben ser números enteros.") from error
    if not 1 <= price <= 10_000_000_000:
        raise ValueError("El precio debe ser mayor a cero y no superar $10.000.000.000.")
    if not 1 <= stock <= 1000:
        raise ValueError("Las existencias deben estar entre 1 y 1.000 unidades.")
    return price, stock


def validate_shipping_options(data, require_explicit=False):
    required_fields = ("shipping_cost", "shipping_min_days", "shipping_max_days")
    if require_explicit and any(
        field not in data or data[field] in (None, "")
        for field in required_fields
    ):
        raise ValueError(
            "Verifica con el proveedor el costo y el plazo de envío antes de publicar."
        )
    raw_values = (
        data.get("shipping_cost", 0),
        data.get("shipping_min_days", 3),
        data.get("shipping_max_days", 7),
    )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, str))
        for value in raw_values
    ):
        raise ValueError("El costo y el plazo de envío deben ser números enteros.")
    try:
        cost, minimum_days, maximum_days = (int(value) for value in raw_values)
    except (TypeError, ValueError) as error:
        raise ValueError("El costo y el plazo de envío deben ser números enteros.") from error
    if not 0 <= cost <= 1_000_000:
        raise ValueError("El costo de envío debe estar entre $0 y $1.000.000 COP por unidad.")
    if not 1 <= minimum_days <= maximum_days <= 90:
        raise ValueError("El plazo debe estar entre 1 y 90 días, y el máximo no puede ser menor al mínimo.")
    return cost, minimum_days, maximum_days


def current_seller(database):
    user = signed_in_user(database)
    if not user:
        return None, (jsonify(error="Inicia sesión para administrar tus publicaciones."), 401)
    status = database.execute(
        "SELECT account_status FROM users WHERE id = ?",
        (user["id"],),
    ).fetchone()
    if not status or status["account_status"] != "active":
        return None, (jsonify(error="La cuenta de emprendedor está suspendida. Contacta al equipo de XrteShop."), 403)
    return user, None


def current_admin(database):
    user = database.execute(
        "SELECT id, name, email, role FROM users WHERE id = ?",
        (session.get("user_id"),),
    ).fetchone() if session.get("user_id") else None
    if (
        not user
        or user["role"] != "admin"
        or not ADMIN_EMAIL
        or normalize_email(user["email"]) != ADMIN_EMAIL
    ):
        abort(404)
    return user, None


def read_admin_setting(database, key, fallback):
    row = database.execute(
        "SELECT setting_value FROM admin_settings WHERE setting_key = ?",
        (key,),
    ).fetchone()
    return row["setting_value"] if row else str(fallback)


@app.get("/api/seller/payout-account")
def payout_account_status():
    return jsonify(error="La configuración de beneficiarios externos está deshabilitada; XrteShop registra pagos manuales a emprendedores."), 410
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        payout_account = database.execute(
            """SELECT provider, provider_account_id, verification_status,
                      requested_at, verified_at
               FROM seller_accounts WHERE seller_id = ? AND provider = 'payu'""",
            (user["id"],),
        ).fetchone()
        return jsonify(
            provider="payu",
            verification_status=payout_account["verification_status"] if payout_account else "pending",
            provider_account_id=payout_account["provider_account_id"] if payout_account else None,
            connected=bool(payout_account and payout_account["verification_status"] == "verified"),
            nequi_phone=None,
        )


@app.post("/api/seller/payout-account/request")
def request_seller_payout_account():
    return jsonify(error="La configuración de beneficiarios externos está deshabilitada."), 410
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        database.execute(
            """INSERT INTO seller_accounts (seller_id, provider, verification_status)
               VALUES (?, 'payu', 'pending')
               ON CONFLICT(seller_id) DO UPDATE SET
                   verification_status = CASE
                       WHEN seller_accounts.verification_status IN ('verified', 'restricted')
                       THEN seller_accounts.verification_status
                       ELSE 'pending' END,
                   requested_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP""",
            (user["id"],),
        )
        payment_audit(
            database, "seller_payout_onboarding_requested", seller_id=user["id"],
            actor_user_id=user["id"],
        )
        current_status = database.execute(
            """SELECT verification_status FROM seller_accounts
               WHERE seller_id = ? AND provider = 'payu'""",
            (user["id"],),
        ).fetchone()["verification_status"]
        database.commit()
        return jsonify(
            ok=True,
            verification_status=current_status,
            message="Solicitud registrada. Completa la verificación de beneficiario directamente con PayU y comparte solo su ID de beneficiario con XrteShop.",
        ), 202


@app.put("/api/seller/payout-account")
def submit_seller_payu_beneficiary():
    return jsonify(error="La configuración de beneficiarios externos está deshabilitada."), 410
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    beneficiary_id = str(data.get("provider_account_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,36}", beneficiary_id):
        return jsonify(error="Escribe el ID de beneficiario que PayU asignó. No envíes datos bancarios."), 400
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        database.execute(
            """INSERT INTO seller_accounts
               (seller_id, provider, provider_account_id, verification_status, requested_at, updated_at)
               VALUES (?, 'payu', ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(seller_id) DO UPDATE SET
                   provider_account_id = excluded.provider_account_id,
                   verification_status = CASE
                       WHEN seller_accounts.verification_status = 'restricted' THEN 'restricted'
                       WHEN seller_accounts.provider_account_id = excluded.provider_account_id
                            AND seller_accounts.verification_status = 'verified'
                       THEN 'verified' ELSE 'pending' END,
                   requested_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP""",
            (user["id"], beneficiary_id),
        )
        payment_audit(
            database, "seller_payout_beneficiary_submitted", seller_id=user["id"],
            actor_user_id=user["id"], provider_reference=beneficiary_id,
        )
        current_status = database.execute(
            """SELECT verification_status FROM seller_accounts
               WHERE seller_id = ? AND provider = 'payu'""",
            (user["id"],),
        ).fetchone()["verification_status"]
        database.commit()
        return jsonify(
            ok=True,
            verification_status=current_status,
            message="ID recibido. No publiques hasta que XrteShop confirme la verificación del beneficiario en PayU.",
        ), 202


@app.patch("/api/admin/sellers/<int:seller_id>/payout-account")
def admin_update_seller_payout_account(seller_id):
    return jsonify(error="La configuración de beneficiarios externos está deshabilitada."), 410
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = str(data.get("verification_status", "")).strip()
    provider_account_id = str(data.get("provider_account_id", "")).strip()
    if status not in {"pending", "verified", "restricted"}:
        return jsonify(error="El estado de verificación no es válido."), 400
    if status == "verified" and not re.fullmatch(r"[A-Za-z0-9-]{1,36}", provider_account_id):
        return jsonify(error="Para verificar la cuenta se requiere un ID de beneficiario válido de PayU."), 400
    with closing(connect_db()) as database:
        admin, error = current_admin(database)
        if error:
            return error
        seller = database.execute("SELECT id FROM users WHERE id = ?", (seller_id,)).fetchone()
        if not seller:
            return jsonify(error="No encontramos ese vendedor."), 404
        database.execute(
            """INSERT INTO seller_accounts
               (seller_id, provider, provider_account_id, verification_status,
                requested_at, verified_at, updated_at)
               VALUES (?, 'payu', ?, ?, CURRENT_TIMESTAMP,
                       CASE WHEN ? = 'verified' THEN CURRENT_TIMESTAMP ELSE NULL END,
                       CURRENT_TIMESTAMP)
               ON CONFLICT(seller_id) DO UPDATE SET
                   provider_account_id = excluded.provider_account_id,
                   verification_status = excluded.verification_status,
                   verified_at = excluded.verified_at,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                seller_id, provider_account_id or None, status, status,
            ),
        )
        payment_audit(
            database, "seller_payout_account_status_changed", seller_id=seller_id,
            actor_user_id=admin["id"], provider_reference=provider_account_id or None,
            details={"verification_status": status},
        )
        database.commit()
        return jsonify(ok=True, verification_status=status)


@app.put("/api/seller/nequi")
def update_seller_nequi():
    error = csrf_error()
    if error:
        return error
    if PAYMENT_PROVIDER == "payu":
        return jsonify(error="Los pagos directos por Nequi están deshabilitados; configura tu beneficiario de payouts PayU."), 409
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
    abort(410)
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
    abort(410)
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
               WHERE products.seller_id = ? AND products.active = 1
               ORDER BY products.id DESC""",
            (user["id"],),
        ).fetchall()
        payload = []
        for product in products:
            item = product_json(product)
            item["variants"] = product_variants(database, product["id"])
            item["has_variants"] = bool(item["variants"])
            payload.append(item)
        return jsonify(products=payload)


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
            """SELECT provider_account_id, verification_status FROM seller_accounts
               WHERE seller_id = ? AND provider = 'payu'""",
            (user["id"],),
        ).fetchone()
        if (
            PAYMENT_PROVIDER == "payu"
            and (
                not account
                or account["verification_status"] != "verified"
                or not account["provider_account_id"]
            )
        ):
            return jsonify(error="Verifica tu beneficiario de payouts PayU antes de publicar productos."), 409
        data = request.get_json(silent=True) if request.is_json else request.form
        data = data or {}
        try:
            name, category, description, price, stock = validate_seller_listing(data)
            shipping_cost, shipping_min_days, shipping_max_days = validate_shipping_options(data)
            variants = parse_product_variants(data.get("variants"))
            if variants:
                stock = sum(variant["stock"] for variant in variants)
        except ValueError as exception:
            return jsonify(error=str(exception)), 400
        external_catalog = str(data.get("external_catalog", "")).strip()[:80] or None
        source_url = str(data.get("source_url", "")).strip() or None
        image_url = str(data.get("image_url", "")).strip()
        image_blob = None
        image_content_type = None
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
                image_content_type = "image/png"
            elif signature.startswith(b"\xff\xd8\xff"):
                extension = ".jpg"
                image_content_type = "image/jpeg"
            elif signature[:4] == b"RIFF" and signature[8:12] == b"WEBP":
                extension = ".webp"
                image_content_type = "image/webp"
            else:
                return jsonify(error="La imagen debe ser PNG, JPG o WEBP válido."), 400
            upload.stream.seek(0)
            image_blob = upload.stream.read(4 * 1024 * 1024 + 1)
            if len(image_blob) > 4 * 1024 * 1024:
                return jsonify(error="La imagen no puede superar 4 MB."), 413
            image_name = f"{uuid.uuid4().hex}{extension}"
            image = f"imagenes/vendedores/{image_name}"
        product_id = f"seller-{uuid.uuid4().hex}"
        database.execute(
            """INSERT INTO products
               (id, name, category, description, image, price, original_price, stock, badge,
                seller_id, origin, external_catalog, source_url, shipping_cost, shipping_min_days,
                shipping_max_days)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, name, category, description, image, price, price, stock,
             "Catálogo externo" if external_catalog else "Emprendedor", user["id"], "emprendedor", external_catalog,
             source_url, shipping_cost, shipping_min_days, shipping_max_days),
        )
        if image_blob is not None:
            database.execute(
                """INSERT INTO product_images
                   (image_path, product_id, image_data, content_type)
                   VALUES (?, ?, ?, ?)""",
                (image, product_id, image_blob, image_content_type),
            )
        if variants:
            save_product_variants(database, product_id, variants)
        database.commit()
        product = database.execute(
            "SELECT products.*, users.name AS seller_name FROM products LEFT JOIN users ON users.id = products.seller_id WHERE products.id = ?",
            (product_id,),
        ).fetchone()
        payload = product_json(product)
        payload["variants"] = product_variants(database, product_id)
        payload["has_variants"] = bool(payload["variants"])
        return jsonify(product=payload), 201


@app.patch("/api/seller/products/<product_id>")
def update_seller_product(product_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Los datos de la publicación no son válidos."), 400
    updates = []
    parameters = []
    if "name" in data:
        if not isinstance(data["name"], str):
            return jsonify(error="El nombre debe ser texto."), 400
        name = data["name"].strip()
        if not 3 <= len(name) <= 100:
            return jsonify(error="El nombre debe tener entre 3 y 100 caracteres."), 400
        updates.append("name = ?")
        parameters.append(name)
    if "description" in data:
        if not isinstance(data["description"], str):
            return jsonify(error="La descripción debe ser texto."), 400
        description = data["description"].strip()
        if len(description) > 500:
            return jsonify(error="La descripción no puede superar los 500 caracteres."), 400
        updates.append("description = ?")
        parameters.append(description)
    try:
        submitted_variants = parse_product_variants(data.get("variants")) if "variants" in data else None
    except ValueError as exception:
        return jsonify(error=str(exception)), 400
    for field, minimum, maximum in (("price", 1, 10_000_000_000), ("stock", 0, 1000)):
        if field == "stock" and submitted_variants:
            continue
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
    shipping_fields = {
        "shipping_cost": (0, 1_000_000),
        "shipping_min_days": (1, 90),
        "shipping_max_days": (1, 90),
    }
    for field, (minimum, maximum) in shipping_fields.items():
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return jsonify(error="El costo y el plazo de envío deben ser números enteros."), 400
            if not minimum <= value <= maximum:
                return jsonify(error="El costo o plazo de envío está fuera del rango permitido."), 400
            updates.append(f"{field} = ?")
            parameters.append(value)
    if ("shipping_min_days" in data) != ("shipping_max_days" in data):
        return jsonify(error="Indica juntos el plazo mínimo y máximo de envío."), 400
    if "shipping_min_days" in data or "shipping_max_days" in data:
        minimum_days = int(data.get("shipping_min_days", 1))
        maximum_days = int(data.get("shipping_max_days", 90))
        if maximum_days < minimum_days:
            return jsonify(error="El máximo del plazo de envío no puede ser menor al mínimo."), 400
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
    if not updates and submitted_variants is None:
        return jsonify(error="Indica al menos un dato para actualizar."), 400
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        owned_product = database.execute(
            "SELECT id, supplier_id FROM products WHERE id = ? AND seller_id = ? AND active = 1",
            (product_id, user["id"]),
        ).fetchone()
        if not owned_product:
            return jsonify(error="No encontramos una publicación tuya con ese identificador."), 404
        existing_variants = database.execute(
            "SELECT 1 FROM product_variants WHERE product_id = ? LIMIT 1",
            (product_id,),
        ).fetchone()
        if "stock" in data and existing_variants and not submitted_variants:
            return jsonify(error="Actualiza las existencias en las variantes, no en el total del producto."), 400
        if submitted_variants:
            total_stock = sum(variant["stock"] for variant in submitted_variants)
            if not 1 <= total_stock <= 1000:
                return jsonify(error="Las variantes deben sumar entre 1 y 1.000 unidades disponibles."), 400
        try:
            if updates:
                parameters.extend((product_id, user["id"]))
                database.execute(
                    f"UPDATE products SET {', '.join(updates)} WHERE id = ? AND seller_id = ? AND active = 1",
                    parameters,
                )
            if submitted_variants is not None and (submitted_variants or existing_variants):
                save_product_variants(database, product_id, submitted_variants)
        except ValueError as exception:
            database.rollback()
            return jsonify(error=str(exception)), 409
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
    variant_id = str(data.get("variant_id", "")).strip() or None
    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        return jsonify(error="La cantidad no es válida."), 400
    if quantity < 0 or quantity > 20:
        return jsonify(error="Puedes elegir entre 1 y 20 unidades."), 400
    with closing(connect_db()) as database:
        user = signed_in_user(database)
        product = database.execute(
            f"""SELECT products.stock FROM products
               WHERE products.id = ? AND products.active = 1
                 AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="El producto ya no está disponible."), 404
        if variant_id:
            variant = database.execute(
                """SELECT stock FROM product_variants
                   WHERE id = ? AND product_id = ?""",
                (variant_id, product_id),
            ).fetchone()
            if not variant:
                return jsonify(error="La variante seleccionada ya no está disponible."), 404
            stock = variant["stock"]
            cart_key = f"variant:{variant_id}"
        else:
            has_variants = database.execute(
                "SELECT 1 FROM product_variants WHERE product_id = ? LIMIT 1",
                (product_id,),
            ).fetchone()
            if has_variants:
                return jsonify(error="Elige una variante antes de añadir este producto."), 400
            stock = product["stock"]
            cart_key = product_id
        cart = dict(current_cart(database, user))
        if quantity == 0:
            cart.pop(cart_key, None)
        elif quantity > stock:
            return jsonify(error=f"Solo quedan {stock} unidades de esta opción."), 409
        else:
            cart[cart_key] = quantity
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
        if user:
            write_user_cart(database, user["id"], cart)
            database.commit()
            session.pop("cart", None)
        else:
            session["cart"] = cart
            session.modified = True
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
        if user:
            write_user_cart(database, user["id"], {})
            database.execute("UPDATE users SET cart_initialized = 1 WHERE id = ?", (user["id"],))
            database.commit()
            session.pop("cart", None)
        else:
            session["cart"] = {}
            session.modified = True
    return jsonify(items=[], count=0, total=0)



@app.post("/api/auth/register")
@app.post("/api/register")
def register():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    password_confirmation = str(data.get("confirm_password", data.get("password_confirmation", "")))
    if len(name) < 2 or len(name) > 80:
        return jsonify(error="Escribe tu nombre (entre 2 y 80 caracteres)."), 400
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        return jsonify(error="Ya existe una cuenta con ese correo."), 409
    error_message = password_error(password, email)
    if error_message:
        return jsonify(error=error_message), 400
    if password != password_confirmation:
        return jsonify(error="Las contraseñas no coinciden."), 400
    with closing(connect_db()) as database:
        existing = database.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            return jsonify(error="Ya existe una cuenta con ese correo."), 409
        try:
            cursor = database.execute(
                """INSERT INTO users
                   (name, email, password_hash, email_verified)
                   VALUES (?, ?, ?, 1)""",
                (name, email, hash_password(password)),
            )
        except DATABASE_INTEGRITY_ERRORS:
            return jsonify(error="Ya existe una cuenta con ese correo."), 409
        database.execute(
            """INSERT INTO loyalty_points_ledger (user_id, event, points)
               VALUES (?, 'welcome', 100)""",
            (cursor.lastrowid,),
        )
        user = database.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        auth_tokens = establish_user_session(database, user)
        response = jsonify(user=account_profile_payload(user))
        return attach_auth_cookies(response, auth_tokens), 201


@app.post("/api/auth/login")
@app.post("/api/login")
def login():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    remember_me = data.get("remember_me", True) not in (False, 0, "0", "false", "off", "")
    keys = login_rate_keys(email)
    with closing(connect_db()) as database:
        if login_is_rate_limited(database, keys):
            return jsonify(error="Demasiados intentos. Espera 15 minutos antes de volver a intentarlo."), 429
        user = database.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        password_matches = bool(user and verify_password(user["password_hash"], password))
        is_admin_identity = bool(ADMIN_EMAIL and email == ADMIN_EMAIL)
        admin_role_matches = bool(
            user and user["role"] == "admin" and is_admin_identity
        )
        if not password_matches or (is_admin_identity and not admin_role_matches):
            if is_admin_identity:
                if user is None:
                    failure_reason = "account_missing"
                elif not password_matches:
                    failure_reason = "password_mismatch"
                else:
                    failure_reason = "admin_role_mismatch"
                app.logger.warning(
                    "Administrator login rejected (%s).",
                    failure_reason,
                )
                log_admin_security_event(database, "login_failed_credentials")
            elif user and user["role"] == "admin":
                app.logger.warning(
                    "Administrator login rejected because ADMIN_EMAIL does not match the account."
                )
            record_failed_login(database, keys)
            database.commit()
            if is_admin_identity:
                send_email_message(
                    ADMIN_EMAIL,
                    "Intento fallido de acceso a XrteShop",
                    f"Se rechazó un intento de acceso administrativo desde {request.remote_addr or 'IP desconocida'} "
                    f"({request.user_agent.string[:120]}).",
                )
            return jsonify(error="Correo o contraseña incorrectos."), 401

        if is_admin_identity:
            database.execute("DELETE FROM auth_login_attempts WHERE key_hash IN (?, ?)", keys)
            device_identity = f"{request.remote_addr or 'unknown'}:{request.user_agent.string[:300]}"
            device_hash = hmac.new(
                SESSION_SECRET.encode("utf-8"), device_identity.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            known_device = database.execute(
                "SELECT 1 FROM admin_known_devices WHERE user_id = ? AND device_hash = ?",
                (user["id"], device_hash),
            ).fetchone()
            if not known_device:
                database.execute(
                    """INSERT INTO admin_known_devices (user_id, device_hash, first_seen_at)
                       VALUES (?, ?, ?)""",
                    (user["id"], device_hash, auth_now().isoformat(timespec="seconds")),
                )
            log_admin_security_event(
                database, "login_success_new_device" if not known_device else "login_success"
            )
            legacy_cart = session.get("cart", {}) if session.get("user_id") in (None, user["id"]) else {}
            ensure_user_cart(database, user["id"], legacy_cart)
            auth_tokens = create_auth_session(
                database, user["id"], remember_me=False, admin_session=True
            )
            database.commit()
            session.clear()
            session["user_id"] = user["id"]
            session["auth_session_id"] = auth_tokens["session_id"]
            session.permanent = True
            session.modified = True
            g.pending_auth_tokens = None
            g.clear_auth_cookies = False
            csrf_token()
            if not known_device:
                send_email_message(
                    ADMIN_EMAIL,
                    "Nuevo inicio de sesión administrativo",
                    f"Se inició sesión correctamente desde un dispositivo nuevo, IP "
                    f"{request.remote_addr or 'desconocida'} ({request.user_agent.string[:120]}).",
                )
            response = jsonify(
                user=account_profile_payload(user),
                redirect_to="/admin",
            )
            return attach_auth_cookies(response, auth_tokens)

        database.execute("DELETE FROM auth_login_attempts WHERE key_hash IN (?, ?)", keys)
        if not user["password_hash"].startswith(admin_security.PASSWORD_HASH_PREFIX):
            database.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user["id"]),
            )
        auth_tokens = establish_user_session(database, user, remember_me)
        response = jsonify(user=account_profile_payload(user))
        return attach_auth_cookies(response, auth_tokens)


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
        database.execute(
            """INSERT INTO loyalty_points_ledger (user_id, event, points)
               VALUES (?, 'welcome', 100)""",
            (user["id"],),
        )
        database.commit()
        return jsonify(message="Correo verificado correctamente. Recibiste 100 puntos de bienvenida."), 200


@app.post("/api/forgot-password")
def forgot_password():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        return jsonify(
            message="Si la dirección está registrada, te enviaremos un código para recuperar la contraseña."
        ), 200
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
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        return jsonify(
            message="Si la dirección está registrada, te enviaremos un código para recuperar la contraseña."
        ), 200
    error_message = password_error(password, email)
    if error_message:
        return jsonify(error=error_message), 400
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
            (hash_password(password), user["id"]),
        )
        database.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (auth_now().isoformat(timespec="seconds"), user["id"]),
        )
        database.commit()
        if session.get("user_id") == user["id"]:
            session.clear()
            g.pending_auth_tokens = None
            g.clear_auth_cookies = True
        return jsonify(message="Tu contraseña se actualizó correctamente."), 200


@app.post("/api/auth/logout")
@app.post("/api/logout")
def logout():
    error = csrf_error()
    if error:
        return error
    if g.auth_session_id:
        with closing(connect_db()) as database:
            database.execute(
                """UPDATE auth_sessions SET revoked_at = ?
                   WHERE id = ? AND revoked_at IS NULL""",
                (auth_now().isoformat(timespec="seconds"), g.auth_session_id),
            )
            database.commit()
    session.clear()
    g.pending_auth_tokens = None
    g.clear_auth_cookies = True
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
            shipment_payloads = []
            for shipment in shipments:
                details = dict(shipment)
                details["tracking_url"] = carrier_tracking_url(
                    shipment["shipping_carrier"],
                    shipment["tracking_number"],
                )
                shipment_payloads.append(details)
            returns = database.execute(
                """SELECT product_id, status, free_return, reason, created_at
                   FROM return_requests WHERE order_id = ? ORDER BY id""",
                (order["id"],),
            ).fetchall()
            result.append({
                "id": order["id"], "total": order["total"], "status": order["status"],
                "payment_status": order["payment_status"], "created_at": order["created_at"],
                "items": [dict(item) for item in items],
                "shipments": shipment_payloads,
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


@app.get("/api/seller/payments")
def seller_payment_history():
    with closing(connect_db()) as database:
        user, error = current_seller(database)
        if error:
            return error
        rows = database.execute(
            """SELECT payment_splits.id AS split_id, payment_splits.order_id,
                      payment_splits.amount,
                      payment_splits.commission_amount - payment_splits.refunded_commission_amount
                          AS commission_amount,
                      payment_splits.net_amount, payment_splits.transfer_status,
                      payment_splits.payout_due_at, payment_splits.provider_transfer_id,
                      payment_splits.transfer_completed_at,
                      orders.payment_status, orders.created_at,
                      COALESCE((SELECT SUM(seller_refund_liabilities.amount)
                                FROM seller_refund_liabilities
                                WHERE seller_refund_liabilities.order_id = orders.id
                                  AND seller_refund_liabilities.seller_id = payment_splits.seller_id
                                  AND seller_refund_liabilities.status = 'open'), 0) AS recovery_owed
               FROM payment_splits
               JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.seller_id = ?
                 AND orders.payment_provider = 'wompi'
               ORDER BY orders.created_at DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
        balance = database.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN payment_splits.transfer_status IN
                       ('pending', 'scheduled') THEN payment_splits.net_amount ELSE 0 END), 0)
                       AS pending_cop,
                   COALESCE(SUM(CASE WHEN payment_splits.transfer_status = 'paid'
                       THEN payment_splits.net_amount ELSE 0 END), 0) AS paid_cop,
                   COALESCE((SELECT SUM(amount) FROM seller_refund_liabilities
                       WHERE seller_id = ? AND status = 'open'), 0) AS recovery_due_cop
               FROM payment_splits JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.seller_id = ?
                 AND orders.payment_provider = 'wompi'
                 AND orders.payment_status IN ('approved', 'partially_refunded', 'refunded')""",
            (user["id"], user["id"]),
        ).fetchone()
        return jsonify(payments=[dict(row) for row in rows], balance=dict(balance))


@app.get("/api/admin/session")
def admin_session():
    return jsonify(ok=True, csrf_token=csrf_token())


@app.get("/api/admin/settings")
def admin_get_settings():
    with closing(connect_db()) as database:
        return jsonify(
            markup_rate=read_admin_setting(
                database, "markup_rate", os.environ.get("MARKUP_RATE", "0.10")
            ),
            iva_rate=read_admin_setting(database, "iva_rate", os.environ.get("IVA_RATE", "0")),
            commission_rate=read_admin_setting(
                database, "commission_rate", os.environ.get("COMMISSION_RATE", "0.10")
            ),
            allowed_payment_methods=sorted(WOMPI_ALLOWED_PAYMENT_METHODS),
            payment_environment=os.environ.get("WOMPI_ENV", "sandbox").strip().lower(),
            payment_environment_editable=False,
        )


@app.patch("/api/admin/settings")
def admin_update_settings():
    data = request.get_json(silent=True) or {}
    if set(data) - {"markup_rate", "iva_rate", "commission_rate"}:
        return jsonify(error="Solo se pueden editar el margen, la comisión y la tasa IVA contable."), 400
    settings = {}
    for key in ("markup_rate", "iva_rate", "commission_rate"):
        if key in data:
            try:
                value = Decimal(str(data[key]))
            except (InvalidOperation, ValueError):
                return jsonify(error=f"El valor de {key} no es válido."), 400
            maximum = Decimal("5") if key == "markup_rate" else Decimal("1")
            if not value.is_finite() or value < 0 or value > maximum:
                return jsonify(error=f"El valor de {key} está fuera del rango permitido."), 400
            settings[key] = format(value.normalize(), "f")
    if not settings:
        return jsonify(error="No hay cambios para guardar."), 400
    with closing(connect_db()) as database:
        for key, value in settings.items():
            database.execute(
                """INSERT INTO admin_settings (setting_key, setting_value, updated_by)
                   VALUES (?, ?, ?)
                   ON CONFLICT(setting_key) DO UPDATE SET
                     setting_value = excluded.setting_value,
                     updated_at = CURRENT_TIMESTAMP,
                     updated_by = excluded.updated_by""",
                (key, value, session["user_id"]),
            )
        database.commit()
        return jsonify(
            message="Configuración guardada.",
            markup_rate=read_admin_setting(
                database, "markup_rate", os.environ.get("MARKUP_RATE", "0.10")
            ),
            iva_rate=read_admin_setting(database, "iva_rate", os.environ.get("IVA_RATE", "0")),
            commission_rate=read_admin_setting(
                database, "commission_rate", os.environ.get("COMMISSION_RATE", "0.10")
            ),
        )


def admin_reauthentication_is_valid(database, user_id, password):
    user = database.execute(
        "SELECT password_hash FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return bool(user and verify_password(user["password_hash"], password))


@app.get("/api/admin/accounts")
def admin_list_bank_accounts():
    with closing(connect_db()) as database:
        rows = database.execute(
            """SELECT id, bank_name, account_type, account_holder, alias,
                      last_four, is_primary, active, created_at, updated_at
               FROM admin_bank_accounts ORDER BY is_primary DESC, id DESC"""
        ).fetchall()
    return jsonify(accounts=[dict(row) for row in rows])


@app.post("/api/admin/accounts")
def admin_create_bank_account():
    data = request.get_json(silent=True) or {}
    bank_name = str(data.get("bank_name", "")).strip()
    account_type = str(data.get("account_type", "")).strip()
    holder = str(data.get("account_holder", "")).strip()
    alias = str(data.get("alias", "")).strip()
    number = re.sub(r"[\s-]", "", str(data.get("account_number", "")))
    primary = data.get("is_primary", False)
    if (
        not 2 <= len(bank_name) <= 80
        or account_type not in {"ahorros", "corriente", "billetera"}
        or not 2 <= len(holder) <= 100
        or not 2 <= len(alias) <= 50
        or not re.fullmatch(r"[0-9]{5,30}", number)
        or not isinstance(primary, bool)
    ):
        return jsonify(error="Revisa los datos de la cuenta y el número ingresado."), 400
    encrypted_number = admin_security.encrypt_secret(
        number, admin_security.accounts_encryption_key()
    )
    with closing(connect_db()) as database:
        if primary:
            database.execute("UPDATE admin_bank_accounts SET is_primary = 0")
        database.execute(
            """INSERT INTO admin_bank_accounts
               (bank_name, account_type, account_holder, alias, encrypted_number,
                last_four, is_primary, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                bank_name,
                account_type,
                holder,
                alias,
                encrypted_number,
                number[-4:],
                int(primary),
                auth_now().isoformat(timespec="seconds"),
            ),
        )
        database.commit()
    return jsonify(message="Cuenta registrada de forma cifrada."), 201


@app.patch("/api/admin/accounts/<int:account_id>")
def admin_update_bank_account(account_id):
    data = request.get_json(silent=True) or {}
    allowed = {"bank_name", "account_type", "account_holder", "alias", "account_number", "is_primary", "active"}
    if set(data) - allowed:
        return jsonify(error="La solicitud contiene campos no permitidos."), 400
    with closing(connect_db()) as database:
        current = database.execute(
            "SELECT id, active, is_primary FROM admin_bank_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not current:
            abort(404)
        values = {}
        for field, minimum, maximum in (
            ("bank_name", 2, 80),
            ("account_holder", 2, 100),
            ("alias", 2, 50),
        ):
            if field in data:
                value = str(data[field]).strip()
                if not minimum <= len(value) <= maximum:
                    return jsonify(error="Revisa los datos de la cuenta."), 400
                values[field] = value
        if "account_type" in data:
            if data["account_type"] not in {"ahorros", "corriente", "billetera"}:
                return jsonify(error="El tipo de cuenta no está permitido."), 400
            values["account_type"] = data["account_type"]
        if "account_number" in data:
            number = re.sub(r"[\s-]", "", str(data["account_number"]))
            if not re.fullmatch(r"[0-9]{5,30}", number):
                return jsonify(error="El número de cuenta no es válido."), 400
            values["encrypted_number"] = admin_security.encrypt_secret(
                number, admin_security.accounts_encryption_key()
            )
            values["last_four"] = number[-4:]
        for field in ("active", "is_primary"):
            if field in data:
                if not isinstance(data[field], bool):
                    return jsonify(error="Los indicadores de estado deben ser booleanos."), 400
                values[field] = int(data[field])
        target_active = values.get("active", current["active"])
        target_primary = values.get("is_primary", current["is_primary"])
        if target_primary and not target_active:
            return jsonify(error="Una cuenta inactiva no puede ser la principal."), 400
        if values.get("is_primary"):
            database.execute("UPDATE admin_bank_accounts SET is_primary = 0")
        if "active" in values and not target_active:
            values["is_primary"] = 0
        if values:
            assignments = ", ".join(f"{field} = ?" for field in values)
            parameters = list(values.values())
            parameters.extend([auth_now().isoformat(timespec="seconds"), account_id])
            database.execute(
                f"UPDATE admin_bank_accounts SET {assignments}, updated_at = ? WHERE id = ?",
                parameters,
            )
        database.commit()
    return jsonify(message="Cuenta actualizada.")


@app.post("/api/admin/accounts/<int:account_id>/reveal")
def admin_reveal_bank_account(account_id):
    data = request.get_json(silent=True) or {}
    with closing(connect_db()) as database:
        if not admin_reauthentication_is_valid(
            database,
            session["user_id"],
            str(data.get("current_password", "")),
        ):
            log_admin_security_event(database, "bank_account_reveal_rejected")
            database.commit()
            return jsonify(error="No se pudo verificar la identidad."), 401
        row = database.execute(
            "SELECT encrypted_number FROM admin_bank_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not row:
            abort(404)
        number = admin_security.decrypt_secret(
            row["encrypted_number"], admin_security.accounts_encryption_key()
        )
        database.execute(
            """INSERT INTO admin_audit_log
               (actor_user_id, action, affected_resource, ip_address)
               VALUES (?, 'view_sensitive_account', ?, ?)""",
            (session["user_id"], f"bank-account:{account_id}", request.remote_addr or "unknown"),
        )
        database.commit()
    return jsonify(account_number=number)


@app.post("/api/admin/security/change-password")
def admin_change_password():
    data = request.get_json(silent=True) or {}
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))
    error_message = password_error(new_password, ADMIN_EMAIL)
    if error_message:
        return jsonify(error=error_message), 400

    with closing(connect_db()) as database:
        user = database.execute(
            "SELECT id, password_hash FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
        if not verify_password(user["password_hash"], current_password):
            log_admin_security_event(database, "password_change_rejected")
            database.commit()
            return jsonify(error="No se pudo verificar la identidad."), 401

        database.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user["id"]),
        )
        database.execute(
            """UPDATE auth_sessions SET revoked_at = ?
               WHERE user_id = ? AND revoked_at IS NULL""",
            (auth_now().isoformat(timespec="seconds"), user["id"]),
        )
        database.commit()
    session.clear()
    g.clear_auth_cookies = True
    return jsonify(message="Contraseña actualizada. Inicia sesión de nuevo.")


@app.get("/api/admin/audit")
def admin_audit():
    with closing(connect_db()) as database:
        actions = database.execute(
            """SELECT admin_audit_log.id, admin_audit_log.created_at,
                      admin_audit_log.action, admin_audit_log.affected_resource,
                      admin_audit_log.ip_address, users.email AS actor_email
               FROM admin_audit_log LEFT JOIN users
                 ON users.id = admin_audit_log.actor_user_id
               ORDER BY admin_audit_log.id DESC LIMIT 200"""
        ).fetchall()
        attempts = database.execute(
            """SELECT id, created_at, event, ip_address, device
               FROM admin_security_events ORDER BY id DESC LIMIT 200"""
        ).fetchall()
        return jsonify(actions=[dict(row) for row in actions], access_events=[dict(row) for row in attempts])


@app.get("/api/admin/customers")
def admin_customers():
    with closing(connect_db()) as database:
        rows = database.execute(
            """SELECT users.id, users.name, users.email, users.phone, users.created_at,
                      users.account_status,
                      COUNT(orders.id) AS order_count
               FROM users LEFT JOIN orders ON orders.user_id = users.id
               WHERE users.role != 'admin'
               GROUP BY users.id ORDER BY users.created_at DESC LIMIT 500"""
        ).fetchall()
    return jsonify(customers=[dict(row) for row in rows])


@app.get("/api/admin/sellers")
def admin_sellers():
    with closing(connect_db()) as database:
        rows = database.execute(
            """SELECT users.id, users.name, users.email, users.phone, users.created_at,
                      users.account_status,
                      seller_accounts.verification_status,
                      COUNT(DISTINCT products.id) AS product_count,
                      COALESCE((SELECT SUM(ps.amount) FROM payment_splits ps
                                JOIN orders paid_orders ON paid_orders.id = ps.order_id
                                WHERE ps.seller_id = users.id
                                  AND paid_orders.payment_status IN
                                    ('approved', 'partially_refunded', 'refunded')), 0) AS sales_cop
               FROM users
               LEFT JOIN seller_accounts ON seller_accounts.seller_id = users.id
               LEFT JOIN products ON products.seller_id = users.id
               WHERE users.role != 'admin'
                 AND (products.id IS NOT NULL OR seller_accounts.seller_id IS NOT NULL
                      OR EXISTS (SELECT 1 FROM payment_splits ps WHERE ps.seller_id = users.id))
               GROUP BY users.id, seller_accounts.verification_status
               ORDER BY users.created_at DESC LIMIT 500"""
        ).fetchall()
        balances = database.execute(
            """SELECT seller_id, COALESCE(SUM(amount_cop), 0) AS pending_cop
               FROM payment_ledger WHERE entry_type = 'entrepreneur_payable'
                 AND status = 'pendiente_de_pago_al_emprendedor'
               GROUP BY seller_id"""
        ).fetchall()
    pending_by_seller = {row["seller_id"]: row["pending_cop"] for row in balances}
    sellers = [
        {**dict(row), "pending_cop": pending_by_seller.get(row["id"], 0)}
        for row in rows
    ]
    return jsonify(sellers=sellers)


@app.patch("/api/admin/sellers/<int:seller_id>/status")
def admin_update_seller_status(seller_id):
    status = str((request.get_json(silent=True) or {}).get("status", "")).strip()
    if status not in {"active", "suspended"}:
        return jsonify(error="Selecciona activar o suspender al emprendedor."), 400
    with closing(connect_db()) as database:
        seller = database.execute(
            """SELECT users.id FROM users
               WHERE users.id = ? AND users.role != 'admin'
                 AND (EXISTS (SELECT 1 FROM products WHERE products.seller_id = users.id)
                   OR EXISTS (SELECT 1 FROM seller_accounts WHERE seller_accounts.seller_id = users.id)
                   OR EXISTS (SELECT 1 FROM payment_splits WHERE payment_splits.seller_id = users.id))""",
            (seller_id,),
        ).fetchone()
        if not seller:
            abort(404)
        database.execute(
            "UPDATE users SET account_status = ? WHERE id = ?",
            (status, seller_id),
        )
        database.commit()
    return jsonify(message="Estado del emprendedor actualizado.", status=status)


@app.get("/api/admin/products")
def admin_products():
    with closing(connect_db()) as database:
        current_admin(database)
        rows = database.execute(
            """SELECT products.id, products.name, products.category, products.price,
                      products.original_price, products.stock, products.active,
                      products.origin, products.supplier_cost,
                      products.supplier_shipping_estimate, products.supplier_url,
                      users.name AS seller_name
               FROM products LEFT JOIN users ON users.id = products.seller_id
               ORDER BY products.id DESC LIMIT 500"""
        ).fetchall()
    return jsonify(products=[dict(row) for row in rows])


@app.get("/api/admin/orders")
def admin_orders():
    payment_status = request.args.get("payment_status", "").strip()
    shipping_status = request.args.get("shipping_status", "").strip()
    allowed_payment_statuses = {
        "pending", "approved", "partially_refunded", "refunded", "failed", "expired", "partial"
    }
    allowed_shipping_statuses = {"preparing", "shipped", "in_transit", "delivered"}
    if payment_status and payment_status not in allowed_payment_statuses:
        return jsonify(error="El filtro de pago no es válido."), 400
    if shipping_status and shipping_status not in allowed_shipping_statuses:
        return jsonify(error="El filtro de envío no es válido."), 400
    clauses = []
    parameters = []
    if payment_status:
        clauses.append("orders.payment_status = ?")
        parameters.append(payment_status)
    if shipping_status:
        clauses.append(
            "EXISTS (SELECT 1 FROM payment_splits ps WHERE ps.order_id = orders.id AND ps.shipping_status = ?)"
        )
        parameters.append(shipping_status)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_db()) as database:
        orders = database.execute(
            f"""SELECT orders.id, orders.user_id, orders.customer_name, users.email,
                       orders.phone, orders.address, orders.city, orders.department,
                       orders.postal_code, orders.delivery_notes, orders.total,
                       orders.payment_status, orders.payment_method, orders.status,
                       orders.payment_provider, orders.provider_payment_id,
                       orders.created_at
                FROM orders JOIN users ON users.id = orders.user_id
                {where_clause}
                ORDER BY orders.id DESC LIMIT 200""",
            parameters,
        ).fetchall()
        result = []
        for order_row in orders:
            order = dict(order_row)
            order["items"] = [
                dict(item)
                for item in database.execute(
                    """SELECT order_items.product_id, order_items.product_name,
                              order_items.unit_price, order_items.quantity,
                              order_items.seller_id, products.origin,
                              products.supplier_cost, products.supplier_shipping_estimate
                       FROM order_items LEFT JOIN products ON products.id = order_items.product_id
                       WHERE order_items.order_id = ? ORDER BY order_items.id""",
                    (order["id"],),
                ).fetchall()
            ]
            order["shipments"] = [
                dict(shipment)
                for shipment in database.execute(
                    """SELECT id AS split_id, seller_id, shipping_status, shipping_carrier,
                              tracking_number, tracking_url, estimated_delivery_at,
                              shipped_at, delivered_at
                       FROM payment_splits WHERE order_id = ? ORDER BY created_at""",
                    (order["id"],),
                ).fetchall()
            ]
            result.append(order)
    return jsonify(orders=result)


@app.patch("/api/admin/orders/<int:order_id>/shipping")
def admin_update_order_shipping(order_id):
    data = request.get_json(silent=True) or {}
    allowed_fields = {"split_id", "shipping_status", "shipping_carrier", "tracking_number"}
    if set(data) - allowed_fields:
        return jsonify(error="La solicitud contiene campos no permitidos."), 400
    split_id = str(data.get("split_id", "")).strip()
    shipping_status = str(data.get("shipping_status", "")).strip()
    carrier = str(data.get("shipping_carrier", "")).strip()
    tracking = str(data.get("tracking_number", "")).strip()
    statuses = {"preparing", "shipped", "in_transit", "delivered"}
    if (
        not split_id
        or shipping_status not in statuses
        or len(carrier) > 80
        or len(tracking) > 100
    ):
        return jsonify(error="Revisa el estado, la guía y la transportadora."), 400
    with closing(connect_db()) as database:
        order = database.execute(
            "SELECT id, payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order:
            abort(404)
        if order["payment_status"] not in {"approved", "partially_refunded", "refunded"}:
            return jsonify(error="Solo se pueden actualizar envíos de pedidos con pago confirmado."), 409
        split = database.execute(
            "SELECT id, shipping_status FROM payment_splits WHERE id = ? AND order_id = ?",
            (split_id, order_id),
        ).fetchone()
        if not split:
            abort(404)
        now = auth_now().isoformat(timespec="seconds")
        shipped_at = now if shipping_status in {"shipped", "in_transit", "delivered"} else None
        delivered_at = now if shipping_status == "delivered" else None
        database.execute(
            """UPDATE payment_splits SET shipping_status = ?, shipping_carrier = ?,
                      tracking_number = ?, tracking_url = ?, shipped_at = COALESCE(?, shipped_at),
                      delivered_at = COALESCE(?, delivered_at)
               WHERE id = ?""",
            (
                shipping_status,
                carrier or None,
                tracking or None,
                carrier_tracking_url(carrier, tracking) if carrier and tracking else None,
                shipped_at,
                delivered_at,
                split_id,
            ),
        )
        shipment_state = database.execute(
            """SELECT COUNT(*) AS count,
                      SUM(CASE WHEN shipping_status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                      SUM(CASE WHEN shipping_status = 'in_transit' THEN 1 ELSE 0 END) AS in_transit,
                      SUM(CASE WHEN shipping_status = 'shipped' THEN 1 ELSE 0 END) AS shipped
               FROM payment_splits WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if shipment_state["count"] and shipment_state["delivered"] == shipment_state["count"]:
            order_status = "Entregado"
        elif shipment_state["in_transit"]:
            order_status = "En tránsito"
        elif shipment_state["shipped"]:
            order_status = "Enviado"
        else:
            order_status = "Preparando"
        database.execute("UPDATE orders SET status = ? WHERE id = ?", (order_status, order_id))
        database.commit()
    return jsonify(message="Estado de envío actualizado.", shipping_status=shipping_status)


@app.get("/api/admin/finance")
def admin_finance():
    now = auth_now()
    today = now.date().isoformat()
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    month_start = now.date().replace(day=1).isoformat()
    with closing(connect_db()) as database:
        def period_sales(start_date):
            return database.execute(
                """SELECT COALESCE(SUM(amount_cop), 0) FROM payment_ledger
                   WHERE entry_type = 'gross_sale' AND substr(created_at, 1, 10) >= ?""",
                (start_date,),
            ).fetchone()[0]

        collected = database.execute(
            """SELECT COALESCE(SUM(amount_cop), 0) AS net_collected,
                      COALESCE(SUM(CASE WHEN entry_type = 'gross_sale'
                                        THEN amount_cop ELSE 0 END), 0) AS gross_collected,
                      COALESCE(SUM(CASE WHEN entry_type = 'customer_refund'
                                        THEN -amount_cop ELSE 0 END), 0) AS refunded,
                      COALESCE(SUM(CASE WHEN entry_type = 'xrteshop_commission'
                                        THEN amount_cop ELSE 0 END), 0) AS commission,
                      COALESCE(SUM(CASE WHEN entry_type = 'supplier_profit'
                                        THEN amount_cop ELSE 0 END), 0) AS supplier_profit,
                      COALESCE(SUM(CASE WHEN entry_type = 'gateway_fee_estimate'
                                        THEN -amount_cop ELSE 0 END), 0) AS gateway_fee_estimate
               FROM payment_ledger"""
        ).fetchone()
        pending_sellers = database.execute(
            """SELECT COALESCE(SUM(amount_cop), 0) FROM payment_ledger
               WHERE entry_type = 'entrepreneur_payable'
                 AND status = 'pendiente_de_pago_al_emprendedor'"""
        ).fetchone()[0]
        manual_movements = database.execute(
            """SELECT admin_manual_movements.id, admin_manual_movements.movement_type,
                      admin_manual_movements.amount_cop, admin_manual_movements.movement_date,
                      admin_manual_movements.reference, admin_manual_movements.note,
                      admin_bank_accounts.alias AS account_alias,
                      users.name AS seller_name
               FROM admin_manual_movements
               LEFT JOIN admin_bank_accounts ON admin_bank_accounts.id = admin_manual_movements.account_id
               LEFT JOIN users ON users.id = admin_manual_movements.seller_id
               ORDER BY admin_manual_movements.id DESC LIMIT 100"""
        ).fetchall()
        reconciliations = database.execute(
            """SELECT id, provider_reference, provider_amount_cop, settlement_date,
                      provider_status, local_amount_cop, difference_cop, imported_at
               FROM admin_reconciliation_items ORDER BY id DESC LIMIT 200"""
        ).fetchall()
        methods = database.execute(
            """SELECT orders.payment_method,
                      COALESCE(SUM(payment_ledger.amount_cop), 0) AS amount_cop
               FROM payment_ledger JOIN orders ON orders.id = payment_ledger.order_id
               WHERE payment_ledger.entry_type = 'gross_sale'
               GROUP BY orders.payment_method ORDER BY orders.payment_method"""
        ).fetchall()
        settlement = database.execute(
            """SELECT COUNT(*) FROM payment_ledger
               WHERE status IN ('settled', 'liquidado')"""
        ).fetchone()[0]
        return jsonify(
            approved_today=period_sales(today),
            approved_week=period_sales(week_start),
            approved_month=period_sales(month_start),
            net_collected=collected["net_collected"],
            gross_collected=collected["gross_collected"],
            refunded=collected["refunded"],
            marketplace_commission=collected["commission"],
            imported_product_profit=collected["supplier_profit"],
            gateway_fee_estimate=collected["gateway_fee_estimate"],
            net_business_profit=(
                collected["commission"]
                + collected["supplier_profit"]
                - collected["gateway_fee_estimate"]
            ),
            entrepreneur_payables_pending=pending_sellers,
            manual_movements=[dict(row) for row in manual_movements],
            reconciliations=[dict(row) for row in reconciliations],
            collected_by_method=[dict(row) for row in methods],
            settlement_records=settlement,
            settlement_note=(
                "La liquidación de Wompi no está conectada; estos valores representan pagos aprobados. "
                "El saldo bancario real se consulta directamente en cada banco."
            ),
            gateway_fee_note=(
                "La comisión de pasarela se muestra como estimada solo cuando su tarifa está configurada."
            ),
        )


def monthly_ledger_summary(database, month):
    rows = database.execute(
        """SELECT entry_type, COALESCE(SUM(amount_cop), 0) AS total
           FROM payment_ledger WHERE substr(created_at, 1, 7) = ?
           GROUP BY entry_type""",
        (month,),
    ).fetchall()
    totals = {row["entry_type"]: int(row["total"]) for row in rows}
    gross = totals.get("gross_sale", 0)
    refunds = -totals.get("customer_refund", 0)
    rate = Decimal(
        read_admin_setting(database, "iva_rate", os.environ.get("IVA_RATE", "0"))
    )
    net_sales = gross - refunds
    return {
        "month": month,
        "gross_sales_cop": gross,
        "refunds_cop": refunds,
        "net_sales_cop": net_sales,
        "commission_cop": totals.get("xrteshop_commission", 0),
        "imported_product_profit_cop": totals.get("supplier_profit", 0),
        "gateway_fee_estimate_cop": -totals.get("gateway_fee_estimate", 0),
        "iva_rate": str(rate),
        "iva_estimate_cop": int(
            (Decimal(max(0, net_sales)) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        ),
        "iva_note": "Estimación contable; confirma base gravable y tratamiento con tu contador.",
    }


@app.get("/api/admin/finance/monthly")
def admin_monthly_finance():
    month = request.args.get("month", auth_now().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify(error="Usa el formato de mes AAAA-MM."), 400
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        return jsonify(error="El mes indicado no es válido."), 400
    with closing(connect_db()) as database:
        return jsonify(report=monthly_ledger_summary(database, month))


@app.get("/api/admin/finance/monthly.csv")
def admin_monthly_finance_csv():
    month = request.args.get("month", auth_now().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify(error="Usa el formato de mes AAAA-MM."), 400
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        return jsonify(error="El mes indicado no es válido."), 400
    with closing(connect_db()) as database:
        report = monthly_ledger_summary(database, month)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["campo", "valor"])
    writer.writerows((key, value) for key, value in report.items())
    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename=xrteshop-{month}-contabilidad.csv"
    return response


@app.post("/api/admin/finance/manual-movements")
def admin_record_manual_movement():
    data = request.get_json(silent=True) or {}
    movement_type = str(data.get("movement_type", "")).strip()
    amount = data.get("amount_cop")
    movement_date = str(data.get("movement_date", "")).strip()
    reference = str(data.get("reference", "")).strip()
    note = str(data.get("note", "")).strip()
    account_id = data.get("account_id")
    seller_id = data.get("seller_id")
    if (
        movement_type not in {"seller_payout", "owner_withdrawal"}
        or isinstance(amount, bool)
        or not isinstance(amount, (int, str))
        or not str(amount).isdigit()
        or int(amount) <= 0
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", movement_date)
        or not 1 <= len(reference) <= 120
        or len(note) > 500
        or isinstance(account_id, bool)
        or not str(account_id or "").isdigit()
    ):
        return jsonify(error="Revisa el tipo, monto, fecha, cuenta y referencia del movimiento."), 400
    try:
        datetime.strptime(movement_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="La fecha del movimiento no es válida."), 400
    amount = int(amount)
    account_id = int(account_id)
    seller_id = int(seller_id) if str(seller_id or "").isdigit() else None
    if movement_type == "seller_payout" and not seller_id:
        return jsonify(error="Selecciona el emprendedor al que se realizó el pago."), 400
    if movement_type == "owner_withdrawal" and seller_id:
        return jsonify(error="Un retiro del dueño no debe incluir un emprendedor."), 400

    with closing(connect_db()) as database:
        account = database.execute(
            "SELECT id, encrypted_number FROM admin_bank_accounts WHERE id = ? AND active = 1",
            (account_id,),
        ).fetchone()
        if not account:
            return jsonify(error="Selecciona una cuenta bancaria activa."), 400
        account_number = admin_security.decrypt_secret(
            account["encrypted_number"], admin_security.accounts_encryption_key()
        )
        if account_number in re.sub(r"\D", "", reference):
            return jsonify(error="La referencia no puede contener un número de cuenta completo."), 400
        if seller_id:
            seller = database.execute(
                """SELECT users.id FROM users
                   WHERE users.id = ? AND users.role != 'admin'
                     AND (EXISTS (SELECT 1 FROM products WHERE products.seller_id = users.id)
                       OR EXISTS (SELECT 1 FROM payment_splits WHERE payment_splits.seller_id = users.id)
                       OR EXISTS (SELECT 1 FROM seller_accounts WHERE seller_accounts.seller_id = users.id))""",
                (seller_id,),
            ).fetchone()
            if not seller:
                return jsonify(error="El emprendedor no es válido."), 400
            payable = database.execute(
                """SELECT split_id FROM payment_ledger
                   WHERE seller_id = ? AND entry_type = 'entrepreneur_payable'
                     AND status = 'pendiente_de_pago_al_emprendedor'
                     AND amount_cop = ? ORDER BY id LIMIT 1""",
                (seller_id, amount),
            ).fetchone()
            if not payable:
                return jsonify(
                    error="El pago manual debe corresponder al monto exacto de un saldo pendiente del emprendedor."
                ), 409
            database.execute(
                """UPDATE payment_ledger SET status = 'paid', payment_date = ?,
                          payment_reference = ? WHERE split_id = ?
                    AND entry_type = 'entrepreneur_payable'
                    AND status = 'pendiente_de_pago_al_emprendedor'""",
                (movement_date, reference, payable["split_id"]),
            )
            database.execute(
                """UPDATE payment_splits SET transfer_status = 'paid',
                          transfer_completed_at = ?, provider_transfer_id = ?
                   WHERE id = ? AND seller_id = ?""",
                (movement_date, reference, payable["split_id"], seller_id),
            )
        cursor = database.execute(
            """INSERT INTO admin_manual_movements
               (movement_type, amount_cop, movement_date, account_id, seller_id,
                reference, note, actor_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                movement_type,
                -amount,
                movement_date,
                account_id,
                seller_id,
                reference,
                note,
                session["user_id"],
            ),
        )
        database.execute(
            """INSERT INTO admin_audit_log
               (actor_user_id, action, affected_resource, details, ip_address)
               VALUES (?, 'manual_ledger_movement', ?, ?, ?)""",
            (
                session["user_id"],
                f"manual-movement:{cursor.lastrowid}",
                json.dumps(
                    {
                        "movement_type": movement_type,
                        "amount_cop": amount,
                        "seller_id": seller_id,
                        "account_id": account_id,
                        "reference": reference,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                request.remote_addr or "unknown",
            ),
        )
        database.commit()
    return jsonify(message="Movimiento manual registrado en el libro interno."), 201


@app.get("/api/admin/finance/reconciliation/template.csv")
def admin_reconciliation_template():
    response = Response(
        "provider_reference,amount_cop,settlement_date,status\r\n",
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = "attachment; filename=wompi-reconciliation-template.csv"
    return response


@app.post("/api/admin/finance/reconciliation/import")
def admin_import_reconciliation():
    upload = request.files.get("file")
    if not upload:
        return jsonify(error="Selecciona un archivo CSV para importar."), 400
    raw = upload.stream.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        return jsonify(error="El archivo CSV supera el tamaño máximo de 5 MB."), 413
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(error="El archivo debe estar codificado en UTF-8."), 400
    reader = csv.DictReader(io.StringIO(content))
    required = {"provider_reference", "amount_cop", "settlement_date", "status"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        return jsonify(
            error="El CSV debe incluir provider_reference, amount_cop, settlement_date y status."
        ), 400
    records = []
    for line_number, row in enumerate(reader, start=2):
        if line_number > 1002:
            return jsonify(error="El archivo puede contener como máximo 1.000 filas."), 400
        reference = str(row.get("provider_reference") or "").strip()
        amount_value = str(row.get("amount_cop") or "").strip()
        settlement_date = str(row.get("settlement_date") or "").strip()
        provider_status = str(row.get("status") or "").strip()
        if (
            not 1 <= len(reference) <= 120
            or not amount_value.isdigit()
            or len(provider_status) > 40
            or (settlement_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", settlement_date))
        ):
            return jsonify(error=f"La fila {line_number} tiene datos inválidos."), 400
        if settlement_date:
            try:
                datetime.strptime(settlement_date, "%Y-%m-%d")
            except ValueError:
                return jsonify(error=f"La fecha de la fila {line_number} no es válida."), 400
        records.append((reference, int(amount_value), settlement_date or None, provider_status))
    if not records:
        return jsonify(error="El CSV no contiene movimientos."), 400

    with closing(connect_db()) as database:
        for reference, amount, settlement_date, provider_status in records:
            order = database.execute(
                """SELECT id, total, refunded_amount FROM orders
                   WHERE provider_payment_id = ? AND payment_status IN
                     ('approved', 'partially_refunded', 'refunded')
                   ORDER BY id DESC LIMIT 1""",
                (reference,),
            ).fetchone()
            local_amount = max(0, order["total"] - order["refunded_amount"]) if order else None
            difference = amount - local_amount if local_amount is not None else None
            database.execute(
                """INSERT INTO admin_reconciliation_items
                   (provider_reference, provider_amount_cop, settlement_date, provider_status,
                    local_amount_cop, difference_cop, actor_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider_reference) DO UPDATE SET
                     provider_amount_cop = excluded.provider_amount_cop,
                     settlement_date = excluded.settlement_date,
                     provider_status = excluded.provider_status,
                     local_amount_cop = excluded.local_amount_cop,
                     difference_cop = excluded.difference_cop,
                     imported_at = CURRENT_TIMESTAMP,
                     actor_user_id = excluded.actor_user_id""",
                (
                    reference, amount, settlement_date, provider_status,
                    local_amount, difference, session["user_id"],
                ),
            )
        database.commit()
    return jsonify(imported=len(records), message="Archivo importado y conciliado contra el libro interno.")


@app.get("/api/admin/finance/export.csv")
def admin_finance_export():
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    if any(value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (date_from, date_to)):
        return jsonify(error="Usa fechas con formato AAAA-MM-DD."), 400
    clauses = []
    parameters = []
    if date_from:
        clauses.append("substr(payment_ledger.created_at, 1, 10) >= ?")
        parameters.append(date_from)
    if date_to:
        clauses.append("substr(payment_ledger.created_at, 1, 10) <= ?")
        parameters.append(date_to)
    method = request.args.get("method", "").strip()
    if method:
        if method not in WOMPI_ALLOWED_PAYMENT_METHODS:
            return jsonify(error="El método de pago no está permitido."), 400
        clauses.append("orders.payment_method = ?")
        parameters.append(method)
    seller_value = request.args.get("seller_id", "").strip()
    if seller_value:
        if not seller_value.isdigit():
            return jsonify(error="El emprendedor no es válido."), 400
        clauses.append("payment_ledger.seller_id = ?")
        parameters.append(int(seller_value))
    origin = request.args.get("product_type", "").strip()
    if origin:
        if origin not in {"propio", "emprendedor", "proveedor"}:
            return jsonify(error="El tipo de producto no es válido."), 400
        clauses.append(
            """EXISTS (SELECT 1 FROM order_items oi JOIN products p ON p.id = oi.product_id
                       WHERE oi.order_id = payment_ledger.order_id AND p.origin = ?)"""
        )
        parameters.append(origin)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_db()) as database:
        rows = database.execute(
            f"""SELECT payment_ledger.created_at, payment_ledger.order_id,
                       payment_ledger.entry_type, payment_ledger.amount_cop,
                       payment_ledger.status, payment_ledger.seller_id,
                       payment_ledger.payment_date, payment_ledger.payment_reference,
                       orders.payment_method
                FROM payment_ledger JOIN orders ON orders.id = payment_ledger.order_id
                {where_clause}
                ORDER BY payment_ledger.created_at, payment_ledger.id LIMIT 10000""",
            parameters,
        ).fetchall()
        manual_rows = []
        if not method and not origin:
            manual_clauses = []
            manual_parameters = []
            if date_from:
                manual_clauses.append("substr(movement_date, 1, 10) >= ?")
                manual_parameters.append(date_from)
            if date_to:
                manual_clauses.append("substr(movement_date, 1, 10) <= ?")
                manual_parameters.append(date_to)
            if seller_value:
                manual_clauses.append("seller_id = ?")
                manual_parameters.append(int(seller_value))
            manual_where = (
                f"WHERE {' AND '.join(manual_clauses)}" if manual_clauses else ""
            )
            manual_rows = database.execute(
                f"""SELECT movement_date, movement_type, amount_cop, seller_id, reference
                    FROM admin_manual_movements {manual_where}
                    ORDER BY movement_date, id LIMIT 10000""",
                manual_parameters,
            ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["fecha", "pedido", "tipo", "monto_cop", "estado", "emprendedor_id",
         "método", "fecha_pago", "referencia"]
    )
    for row in rows:
        writer.writerow(
            [
                row["created_at"], row["order_id"], row["entry_type"], row["amount_cop"],
                row["status"], row["seller_id"], row["payment_method"],
                row["payment_date"], row["payment_reference"],
            ]
        )
    for row in manual_rows:
        writer.writerow(
            [
                row["movement_date"], "", f"manual_{row['movement_type']}",
                row["amount_cop"], "manual", row["seller_id"], "",
                row["movement_date"], row["reference"],
            ]
        )
    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=xrteshop-ledger.csv"
    return response


@app.get("/api/admin/dashboard")
def admin_dashboard():
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
        sellers = database.execute(
            """SELECT users.id, users.name, users.email,
                      COUNT(products.id) AS listing_count,
                      SUM(CASE WHEN products.active = 1 THEN 1 ELSE 0 END) AS active_count
               FROM users JOIN products ON products.seller_id = users.id
               GROUP BY users.id ORDER BY users.created_at DESC LIMIT 100"""
        ).fetchall()
        products = database.execute(
            """SELECT products.id, products.name, products.category, products.price,
                      products.stock, products.active,
                      users.name AS seller_name, users.email AS seller_email
               FROM products JOIN users ON users.id = products.seller_id
               ORDER BY products.id DESC LIMIT 150"""
        ).fetchall()
        orders = database.execute(
            """SELECT orders.id, orders.customer_name, orders.total,
                      orders.status, orders.payment_status, orders.created_at,
                      users.email
               FROM orders JOIN users ON users.id = orders.user_id
               WHERE orders.payment_status IN ('pending', 'partial', 'failed')
               ORDER BY orders.id DESC LIMIT 100"""
        ).fetchall()
        returns = database.execute(
            """SELECT return_requests.id, return_requests.order_id,
                      return_requests.product_id, return_requests.reason,
                      return_requests.details, return_requests.status,
                      return_requests.created_at, orders.customer_name,
                      users.email AS customer_email, products.name AS product_name,
                      sellers.name AS seller_name
               FROM return_requests
               JOIN orders ON orders.id = return_requests.order_id
               JOIN users ON users.id = orders.user_id
               JOIN products ON products.id = return_requests.product_id
               LEFT JOIN users AS sellers ON sellers.id = products.seller_id
               WHERE return_requests.status IN ('received', 'reviewing')
               ORDER BY return_requests.id DESC LIMIT 100"""
        ).fetchall()
        messages = database.execute(
            """SELECT id, name, email, subject, message, created_at
               FROM contact_messages ORDER BY id DESC LIMIT 100"""
        ).fetchall()
        newsletter_count = database.execute(
            "SELECT COUNT(*) FROM newsletter_subscribers WHERE active = 1"
        ).fetchone()[0]
        sales_summary = database.execute(
            """SELECT COUNT(*) AS paid_orders, COALESCE(SUM(total), 0) AS gross_sales
               FROM orders WHERE payment_status = 'approved'
                 AND created_at >= datetime('now', '-30 days')"""
        ).fetchone()
        interaction_summary = database.execute(
            """SELECT action, COUNT(*) AS count FROM product_interactions
               WHERE created_at >= datetime('now', '-30 days') GROUP BY action"""
        ).fetchall()
        coupon_summary = database.execute(
            """SELECT COUNT(*) AS issued,
                      SUM(CASE WHEN status = 'redeemed'
                               AND redeemed_at >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS redeemed
               FROM discount_coupons WHERE created_at >= datetime('now', '-30 days')"""
        ).fetchone()
        loyalty_points_outstanding = database.execute(
            """SELECT COALESCE(SUM(points), 0) FROM loyalty_points_ledger"""
        ).fetchone()[0]
        top_interactions = database.execute(
            """SELECT products.id, products.name, COUNT(*) AS count
               FROM product_interactions
               JOIN products ON products.id = product_interactions.product_id
               WHERE product_interactions.created_at >= datetime('now', '-30 days')
               GROUP BY products.id, products.name ORDER BY count DESC LIMIT 10"""
        ).fetchall()
        payout_rows = database.execute(
               """SELECT payment_splits.id AS split_id, payment_splits.order_id,
                         payment_splits.seller_id, users.name AS seller_name,
                         payment_splits.amount, payment_splits.commission_amount,
                         payment_splits.net_amount, payment_splits.transfer_status,
                         payment_splits.payout_due_at, payment_splits.provider_transfer_id,
                         payment_splits.transfer_attempts, payment_splits.transfer_completed_at
                  FROM payment_splits
                  JOIN orders ON orders.id = payment_splits.order_id
                  LEFT JOIN users ON users.id = payment_splits.seller_id
                  WHERE payment_splits.transfer_status IN
                        ('scheduled', 'pending', 'processing', 'submitted', 'unknown', 'failed')
                    AND orders.payment_provider = 'wompi'
                  ORDER BY payment_splits.payout_due_at, payment_splits.created_at LIMIT 200"""
        ).fetchall()
        refunds = database.execute(
               """SELECT payment_refunds.id, payment_refunds.order_id, payment_refunds.amount,
                         payment_refunds.commission_reversed, payment_refunds.status,
                         payment_refunds.reason, payment_refunds.provider_refund_id,
                         payment_refunds.created_at
                  FROM payment_refunds ORDER BY payment_refunds.created_at DESC LIMIT 100"""
        ).fetchall()
        refund_liabilities = database.execute(
            """SELECT seller_refund_liabilities.id, seller_refund_liabilities.order_id,
                      seller_refund_liabilities.seller_id, users.name AS seller_name,
                      seller_refund_liabilities.amount, seller_refund_liabilities.status,
                      seller_refund_liabilities.settlement_reference,
                      seller_refund_liabilities.created_at
               FROM seller_refund_liabilities
               JOIN users ON users.id = seller_refund_liabilities.seller_id
               ORDER BY seller_refund_liabilities.created_at DESC LIMIT 100"""
        ).fetchall()
        disputes = database.execute(
               """SELECT payment_disputes.id, payment_disputes.provider_dispute_id,
                         payment_disputes.order_id, payment_disputes.payment_id,
                         payment_disputes.status, payment_disputes.amount,
                         payment_disputes.created_at
                  FROM payment_disputes ORDER BY payment_disputes.created_at DESC LIMIT 100"""
        ).fetchall()
        payment_totals = database.execute(
               """SELECT
                      COALESCE(SUM(payment_splits.commission_amount -
                                   payment_splits.refunded_commission_amount), 0) AS commissions,
                      COALESCE(SUM(CASE WHEN payment_splits.transfer_status IN
                                        ('scheduled', 'pending', 'processing', 'submitted', 'unknown', 'failed')
                                       THEN payment_splits.net_amount ELSE 0 END), 0) AS pending_payouts
                  FROM payment_splits JOIN orders ON orders.id = payment_splits.order_id
                  WHERE orders.payment_status IN ('approved', 'partially_refunded', 'refunded')"""
        ).fetchone()
        refund_total = database.execute(
               """SELECT COALESCE(SUM(amount), 0) FROM payment_refunds
                  WHERE status = 'completed'"""
        ).fetchone()[0]
        open_disputes = database.execute(
               "SELECT COUNT(*) FROM payment_disputes WHERE status IN ('open', 'under_review', 'lost')"
        ).fetchone()[0]
        open_liabilities = database.execute(
               """SELECT COALESCE(SUM(amount), 0) FROM seller_refund_liabilities
                  WHERE status = 'open'"""
        ).fetchone()[0]
        return jsonify(
               sellers=[dict(row) for row in sellers],
               products=[dict(row) for row in products],
               orders=[dict(row) for row in orders],
               returns=[dict(row) for row in returns],
               messages=[dict(row) for row in messages],
               payment_payouts=[dict(row) for row in payout_rows],
               payment_refunds=[dict(row) for row in refunds],
               payment_refund_liabilities=[dict(row) for row in refund_liabilities],
               payment_disputes=[dict(row) for row in disputes],
               newsletter_subscribers=newsletter_count,
               analytics={
                "paid_orders_30d": sales_summary["paid_orders"],
                "gross_sales_30d": sales_summary["gross_sales"],
                "coupons_issued_30d": coupon_summary["issued"] or 0,
                "coupons_redeemed_30d": coupon_summary["redeemed"] or 0,
                "coupon_redemption_rate_30d": round(
                    (coupon_summary["redeemed"] or 0) * 100 / coupon_summary["issued"], 1
                ) if coupon_summary["issued"] else 0,
                "loyalty_points_outstanding": loyalty_points_outstanding,
                "commissions_accumulated": payment_totals["commissions"],
                "payouts_pending": payment_totals["pending_payouts"],
                "refunds_total": refund_total,
                "open_disputes": open_disputes,
                "seller_recoveries_open": open_liabilities,
                "product_interactions_30d": {
                    row["action"]: row["count"] for row in interaction_summary
                },
                "top_interacted_products": [dict(row) for row in top_interactions],
            },
        )


@app.post("/api/admin/catalog-cleanup/preview")
def admin_catalog_cleanup_preview():
    error = csrf_error()
    if error:
        return error
    if DATABASE_URL:
        return jsonify(error="La limpieza de catálogo solo está disponible con SQLite local."), 409
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
    try:
        backup_directory, backup_manifest = backup_before_cleanup(
            app.config["DATABASE"],
            ROOT,
            ROOT / "static" / "images",
        )
        with closing(connect_catalog_readonly(app.config["DATABASE"])) as database:
            preview = catalog_cleanup_preview(database)
        return jsonify(
            **preview,
            backup_directory=str(backup_directory.relative_to(ROOT)),
            backup_manifest=backup_manifest,
            confirmation_phrase=CATALOG_CLEANUP_CONFIRMATION,
        )
    except (OSError,) + DATABASE_ERRORS as error:
        app.logger.exception("No se pudo crear la copia previa a la limpieza del catálogo.")
        return jsonify(error="No se pudo crear y verificar la copia de seguridad."), 500


def estimate_gateway_fee(amount):
    raw_rate = os.environ.get("PAYMENT_GATEWAY_FEE_RATE", "").strip()
    raw_fixed_fee = os.environ.get("PAYMENT_GATEWAY_FIXED_FEE_COP", "0").strip()
    if not raw_rate:
        return None
    try:
        rate = Decimal(raw_rate)
        fixed_fee = int(raw_fixed_fee)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PaymentServiceError(
            "Configura PAYMENT_GATEWAY_FEE_RATE y PAYMENT_GATEWAY_FIXED_FEE_COP con valores válidos."
        ) from error
    if not Decimal("0") <= rate <= Decimal("1") or not 0 <= fixed_fee <= 10_000_000:
        raise PaymentServiceError(
            "La comisión de pasarela debe estar entre 0 y 1 y su valor fijo entre 0 y 10.000.000 COP."
        )
    return calculate_commission(amount, rate) + fixed_fee


@app.get("/api/admin/suppliers")
def admin_supplier_overview():
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
        suppliers = database.execute(
            """SELECT suppliers.id, suppliers.name, suppliers.connector,
                      suppliers.api_status, suppliers.enabled, suppliers.payment_mode,
                      suppliers.minimum_margin, suppliers.sync_interval_hours,
                      suppliers.last_synced_at, suppliers.updated_at,
                      (SELECT COUNT(*) FROM supplier_products
                       WHERE supplier_products.supplier_id = suppliers.id) AS linked_products,
                      (SELECT COUNT(*) FROM supplier_orders
                       WHERE supplier_orders.supplier_id = suppliers.id
                         AND supplier_orders.status NOT IN ('completed', 'cancelled')) AS open_orders,
                      (SELECT COALESCE(SUM(supplier_orders.supplier_cost), 0)
                       FROM supplier_orders
                       WHERE supplier_orders.supplier_id = suppliers.id
                         AND supplier_orders.status NOT IN ('completed', 'cancelled')) AS open_order_cost
               FROM suppliers
               WHERE suppliers.id IN ('aliexpress', 'mercadolibre')
               ORDER BY suppliers.name"""
        ).fetchall()
        products = database.execute(
            """SELECT products.id, products.name, products.price, products.stock,
                      products.active, products.supplier_id,
                      products.supplier_product_id,
                      products.supplier_url,
                      products.supplier_shipping_estimate,
                      products.shipping_cost, products.shipping_min_days,
                      products.shipping_max_days,
                      supplier_products.supplier_cost, supplier_products.supplier_stock,
                      supplier_products.product_status, suppliers.name AS supplier_name,
                      suppliers.minimum_margin
               FROM supplier_products
               JOIN products ON products.id = supplier_products.product_id
               JOIN suppliers ON suppliers.id = supplier_products.supplier_id
               WHERE products.origin = 'proveedor'
                 AND products.supplier_id = 'mercadolibre'
               ORDER BY products.id DESC LIMIT 100"""
        ).fetchall()
        try:
            product_margins = []
            for row in products:
                product = dict(row)
                product["variants"] = [
                    {
                        "id": variant["id"],
                        "label": variant_options_label(variant["options_json"]),
                        "price": variant["price"],
                        "stock": variant["stock"],
                        "supplier_stock": variant["supplier_stock"],
                    }
                    for variant in database.execute(
                        """SELECT id, options_json, price, stock, supplier_stock
                           FROM product_variants WHERE product_id = ? ORDER BY options_json""",
                        (product["id"],),
                    )
                ]
                product["margin_before_gateway_fee"] = (
                    product["price"]
                    - product["supplier_cost"]
                    - product["supplier_shipping_estimate"]
                )
                product["gateway_fee_estimate"] = estimate_gateway_fee(product["price"])
                product["net_profit_estimate"] = (
                    product["margin_before_gateway_fee"]
                    - product["gateway_fee_estimate"]
                    if product["gateway_fee_estimate"] is not None
                    else None
                )
                product_margins.append(product)
        except PaymentServiceError as error:
            return jsonify(error=str(error)), 503
        orders = database.execute(
            """SELECT supplier_orders.id, supplier_orders.order_id,
                      supplier_orders.product_id, supplier_orders.external_order_id,
                      supplier_orders.status, supplier_orders.supplier_cost,
                      supplier_orders.tracking_number, supplier_orders.tracking_url,
                      supplier_orders.attempts, supplier_orders.updated_at,
                      suppliers.name AS supplier_name
               FROM supplier_orders JOIN suppliers ON suppliers.id = supplier_orders.supplier_id
               WHERE supplier_orders.status NOT IN ('completed', 'cancelled')
               ORDER BY supplier_orders.updated_at DESC LIMIT 100"""
        ).fetchall()
        sync_logs = database.execute(
            """SELECT supplier_sync_logs.id, supplier_sync_logs.supplier_id,
                      supplier_sync_logs.status, supplier_sync_logs.products_seen,
                      supplier_sync_logs.products_updated, supplier_sync_logs.error,
                      supplier_sync_logs.started_at, supplier_sync_logs.completed_at,
                      suppliers.name AS supplier_name
               FROM supplier_sync_logs JOIN suppliers ON suppliers.id = supplier_sync_logs.supplier_id
               WHERE supplier_sync_logs.supplier_id IN ('aliexpress', 'mercadolibre')
               ORDER BY supplier_sync_logs.started_at DESC LIMIT 100"""
        ).fetchall()
        return jsonify(
            suppliers=[dict(row) for row in suppliers],
            products=product_margins,
            open_orders=[dict(row) for row in orders],
            sync_logs=[dict(row) for row in sync_logs],
            markup_rate=os.environ.get("MARKUP_RATE", "0.10"),
            gateway_fee_configured=bool(
                os.environ.get("PAYMENT_GATEWAY_FEE_RATE", "").strip()
            ),
        )


@app.get("/admin/supplier-import")
@app.get("/admin/ml-import")
def admin_supplier_import_page():
    with closing(connect_db()) as database:
        current_admin(database)
    return render_template("supplier_import.html", csrf_token=csrf_token())


def _consume_mercadolibre_oauth_state(database, state):
    if not isinstance(state, str) or not 32 <= len(state) <= 256:
        return None
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    database.execute("BEGIN IMMEDIATE")
    row = database.execute(
        """SELECT actor_user_id FROM mercadolibre_oauth_states
           WHERE state_hash = ? AND expires_at > ?""",
        (state_hash, now),
    ).fetchone()
    if not row:
        database.rollback()
        return None
    database.execute(
        "DELETE FROM mercadolibre_oauth_states WHERE state_hash = ?",
        (state_hash,),
    )
    database.commit()
    return row["actor_user_id"]


def _refresh_mercadolibre_account_if_needed(database):
    account = database.execute(
        """SELECT external_user_id, nickname, encrypted_access_token,
                  encrypted_refresh_token, token_expires_at, connected_at
           FROM mercadolibre_oauth_accounts WHERE account_key = 'primary'"""
    ).fetchone()
    if not account:
        return None
    try:
        expires_at = datetime.fromisoformat(account["token_expires_at"])
    except ValueError as error:
        raise MercadoLibreOAuthError(
            "La fecha de expiración del token guardado no es válida."
        ) from error
    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        return account

    refresh_token = mercadolibre_oauth.decrypt_token(
        account["encrypted_refresh_token"]
    )
    tokens = mercadolibre_oauth.refresh_access_token(refresh_token)
    profile = mercadolibre_oauth.account_profile(tokens["access_token"])
    if (
        profile["user_id"] != account["external_user_id"]
        or tokens["user_id"] and tokens["user_id"] != account["external_user_id"]
    ):
        raise MercadoLibreOAuthError(
            "La renovación no corresponde a la cuenta Mercado Libre conectada."
        )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    database.execute(
        """UPDATE mercadolibre_oauth_accounts
           SET encrypted_access_token = ?, encrypted_refresh_token = ?,
               token_expires_at = ?, nickname = ?, updated_at = CURRENT_TIMESTAMP
           WHERE account_key = 'primary'""",
        (
            mercadolibre_oauth.encrypt_token(tokens["access_token"]),
            mercadolibre_oauth.encrypt_token(tokens["refresh_token"]),
            expires_at.isoformat(),
            profile["nickname"],
        ),
    )
    database.commit()
    return {
        "external_user_id": profile["user_id"],
        "nickname": profile["nickname"],
        "token_expires_at": expires_at.isoformat(),
        "connected_at": account["connected_at"],
    }


@app.get("/admin/mercadolibre/connect")
def mercadolibre_oauth_connect():
    with closing(connect_db()) as database:
        admin, error = current_admin(database)
        if error:
            return error
        state = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        database.execute(
            "DELETE FROM mercadolibre_oauth_states WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        database.execute(
            """INSERT INTO mercadolibre_oauth_states
               (state_hash, actor_user_id, expires_at)
               VALUES (?, ?, ?)""",
            (
                hashlib.sha256(state.encode("utf-8")).hexdigest(),
                admin["id"],
                (now + timedelta(minutes=10)).isoformat(),
            ),
        )
        database.commit()
    try:
        return redirect(mercadolibre_oauth.authorization_url(state))
    except MercadoLibreOAuthError as error:
        app.logger.warning("Mercado Libre OAuth is not configured: %s", error)
        return redirect(url_for("admin_supplier_import_page", oauth="not_configured"))


@app.get("/admin/mercadolibre/oauth/callback")
def mercadolibre_oauth_callback():
    state = request.args.get("state", "")
    try:
        with closing(connect_db()) as database:
            actor_id = _consume_mercadolibre_oauth_state(database, state)
            if actor_id is None:
                return redirect(url_for("admin_supplier_import_page", oauth="invalid_state"))
            admin = database.execute(
                "SELECT id, email, role FROM users WHERE id = ?",
                (actor_id,),
            ).fetchone()
            if (
                not admin
                or admin["role"] != "admin"
                or not ADMIN_EMAIL
                or normalize_email(admin["email"]) != ADMIN_EMAIL
            ):
                return redirect(url_for("admin_supplier_import_page", oauth="not_authorized"))
            database.commit()
            if request.args.get("error"):
                return redirect(url_for("admin_supplier_import_page", oauth="cancelled"))
            code = request.args.get("code", "")
            tokens = mercadolibre_oauth.exchange_authorization_code(code)
            profile = mercadolibre_oauth.account_profile(tokens["access_token"])
            if tokens["user_id"] and tokens["user_id"] != profile["user_id"]:
                raise MercadoLibreOAuthError(
                    "La cuenta autorizada no coincide con la cuenta confirmada por la API."
                )
            now = datetime.now(timezone.utc)
            database.execute(
                """INSERT INTO mercadolibre_oauth_accounts
                   (account_key, external_user_id, nickname, encrypted_access_token,
                    encrypted_refresh_token, token_expires_at, connected_at, updated_at)
                   VALUES ('primary', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT(account_key) DO UPDATE SET
                       external_user_id = excluded.external_user_id,
                       nickname = excluded.nickname,
                       encrypted_access_token = excluded.encrypted_access_token,
                       encrypted_refresh_token = excluded.encrypted_refresh_token,
                       token_expires_at = excluded.token_expires_at,
                       connected_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    profile["user_id"],
                    profile["nickname"],
                    mercadolibre_oauth.encrypt_token(tokens["access_token"]),
                    mercadolibre_oauth.encrypt_token(tokens["refresh_token"]),
                    (now + timedelta(seconds=tokens["expires_in"])).isoformat(),
                ),
            )
            database.execute(
                """INSERT INTO admin_audit_log
                   (actor_user_id, action, affected_resource, details, ip_address)
                   VALUES (?, 'mercadolibre_account_connected', 'mercadolibre:primary',
                           ?, ?)""",
                (
                    actor_id,
                    json.dumps({"external_user_id": profile["user_id"]}),
                    request.remote_addr or "unknown",
                ),
            )
            database.commit()
    except MercadoLibreOAuthError as error:
        app.logger.warning("Mercado Libre OAuth callback failed: %s", error)
        return redirect(url_for("admin_supplier_import_page", oauth="error"))
    except DATABASE_ERRORS:
        app.logger.exception("Could not save Mercado Libre OAuth account.")
        return redirect(url_for("admin_supplier_import_page", oauth="storage_error"))
    return redirect(url_for("admin_supplier_import_page", oauth="connected"))


@app.get("/api/admin/mercadolibre/account")
def admin_mercadolibre_account():
    try:
        with closing(connect_db()) as database:
            _, error = current_admin(database)
            if error:
                return error
            account = _refresh_mercadolibre_account_if_needed(database)
    except MercadoLibreOAuthError as error:
        app.logger.warning("Mercado Libre account token could not be refreshed: %s", error)
        return jsonify(error=str(error)), 503
    if not account:
        return jsonify(connected=False)
    return jsonify(connected=True, **dict(account))


@app.post("/api/admin/mercadolibre/disconnect")
def admin_mercadolibre_disconnect():
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        admin, error = current_admin(database)
        if error:
            return error
        database.execute(
            "DELETE FROM mercadolibre_oauth_accounts WHERE account_key = 'primary'"
        )
        database.execute(
            "DELETE FROM mercadolibre_oauth_states WHERE actor_user_id = ?",
            (admin["id"],),
        )
        database.execute(
            """INSERT INTO admin_audit_log
               (actor_user_id, action, affected_resource, ip_address)
               VALUES (?, 'mercadolibre_account_disconnected', 'mercadolibre:primary', ?)""",
            (admin["id"], request.remote_addr or "unknown"),
        )
        database.commit()
    return jsonify(ok=True, connected=False)






@app.get("/api/admin/mercadolibre/preview")
def admin_mercadolibre_preview(product_url=None):
    with closing(connect_db()) as database:
        current_admin(database)
    product_url = product_url if product_url is not None else request.args.get("url", "")
    try:
        product = preview_mercadolibre_product(product_url)
        exchange_rate = Decimal("1")
        rate_updated_at = "COP"
        rate_is_stale = False
        if product.get("supplier_currency") == "USD":
            exchange_rate, rate_updated_at, rate_is_stale = fetch_usd_cop_rate()
        if product.get("supplier_currency") in {"COP", "USD"}:
            product = convert_mercadolibre_prices_to_cop(product, exchange_rate)
        return jsonify(
            product=product,
            exchange_rate=str(exchange_rate),
            exchange_rate_updated_at=rate_updated_at,
            exchange_rate_stale=rate_is_stale,
        )
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except MercadoLibreImportError as error:
        app.logger.warning("Mercado Libre administrator preview failed: %s", error)
        return jsonify(error=str(error)), 502
    except (OSError, InvalidOperation):
        app.logger.exception("Mercado Libre preview could not retrieve the USD/COP rate.")
        return jsonify(error="No se pudo consultar la tasa USD/COP."), 503


@app.get("/api/admin/supplier/preview")
def admin_supplier_preview():
    with closing(connect_db()) as database:
        current_admin(database)
    supplier_url = request.args.get("url", "").strip()
    try:
        sanitize_mercadolibre_url(supplier_url)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return admin_mercadolibre_preview(supplier_url)


@app.post("/api/admin/supplier/publish")
def admin_supplier_publish():
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Indica el enlace del producto y los cambios revisados."), 400
    supplier_url = data.get("url")
    overrides = data.get("overrides", {})
    if not isinstance(supplier_url, str) or not supplier_url.strip() or len(supplier_url) > 1000:
        return jsonify(error="Ingresa un ID o enlace de producto válido."), 400

    with closing(connect_db()) as database:
        current_admin(database)
        try:
            safe_url = sanitize_mercadolibre_url(supplier_url)
            product = preview_mercadolibre_product(safe_url)
            exchange_rate = Decimal("1")
            if product.get("supplier_currency") == "USD":
                exchange_rate, _updated_at, _stale = fetch_usd_cop_rate()
            database.execute("BEGIN IMMEDIATE")
            result = publish_mercadolibre_product(
                database,
                product,
                overrides,
                exchange_rate,
            )
            database.commit()
        except MercadoLibreImportError as error:
            database.rollback()
            app.logger.warning("Mercado Libre supplier publication failed: %s", error)
            return jsonify(error=str(error)), 502
        except (ValueError, InvalidOperation) as error:
            database.rollback()
            return jsonify(error=str(error)), 409
        except (OSError, requests.RequestException):
            database.rollback()
            app.logger.exception("Supplier product could not be revalidated for publication.")
            return jsonify(error="No se pudo revalidar el producto con el proveedor."), 503
        except DATABASE_ERRORS:
            database.rollback()
            app.logger.exception("Could not save an imported supplier product.")
            return jsonify(error="No se pudo guardar el producto del proveedor."), 500
    return jsonify(ok=True, **result), 201


def _run_mercadolibre_inventory_batch(limit):
    with closing(connect_db()) as database:
        rows = list_mercadolibre_products_for_sync(database, limit)
        cursor = database.execute(
            """INSERT INTO supplier_sync_logs (supplier_id, status)
               VALUES ('mercadolibre', 'running')"""
        )
        sync_log_id = cursor.lastrowid
        database.commit()

    updated = []
    errors = []
    for row in rows:
        try:
            product = preview_mercadolibre_product(
                row["provider_product_id"],
                include_description=False,
            )
            exchange_rate = Decimal("1")
            if product.get("supplier_currency") == "USD":
                exchange_rate, _, _ = fetch_usd_cop_rate()
            with closing(connect_db()) as database:
                database.execute("BEGIN IMMEDIATE")
                result = sync_mercadolibre_product(
                    database,
                    row,
                    product,
                    exchange_rate,
                )
                database.commit()
            updated.append(result)
        except (
            MercadoLibreImportError,
            OSError,
            requests.RequestException,
            ValueError,
            InvalidOperation,
        ) as error:
            errors.append(f"{row['provider_product_id']}: {str(error)[:200]}")
            with closing(connect_db()) as database:
                database.execute(
                    """UPDATE supplier_products SET last_checked_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND supplier_id = 'mercadolibre'""",
                    (row["id"],),
                )
                database.commit()
        except DATABASE_ERRORS:
            errors.append(f"{row['provider_product_id']}: Error de base de datos.")
            app.logger.exception(
                "Could not synchronize Mercado Libre product %s.",
                row["provider_product_id"],
            )

    with closing(connect_db()) as database:
        database.execute(
            """UPDATE supplier_sync_logs
               SET status = ?, products_seen = ?, products_updated = ?, error = ?,
                   completed_at = CURRENT_TIMESTAMP
               WHERE id = ? AND supplier_id = 'mercadolibre'""",
            (
                "failed" if errors else "completed",
                len(rows),
                len(updated),
                "; ".join(errors)[:500] if errors else None,
                sync_log_id,
            ),
        )
        database.execute(
            """UPDATE suppliers SET last_synced_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
               WHERE id = 'mercadolibre'"""
        )
        database.commit()
    return {
        "products_seen": len(rows),
        "products_updated": len(updated),
        "results": updated,
        "errors": errors,
    }


@app.get("/api/cron/mercadolibre-inventory")
def cron_sync_mercadolibre_inventory():
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    if not cron_secret:
        app.logger.error("CRON_SECRET is required before syncing Mercado Libre inventory.")
        return jsonify(error="La sincronización programada no está configurada."), 503
    if not hmac.compare_digest(
        request.headers.get("Authorization", ""),
        f"Bearer {cron_secret}",
    ):
        return jsonify(error="No autorizado."), 401
    try:
        limit = int(request.args.get("limit", "1"))
        if not 1 <= limit <= 5:
            raise ValueError
        return jsonify(ok=True, **_run_mercadolibre_inventory_batch(limit))
    except ValueError:
        return jsonify(error="El lote debe contener entre 1 y 5 productos."), 400
    except DATABASE_ERRORS:
        app.logger.exception("Scheduled Mercado Libre inventory synchronization failed.")
        return jsonify(error="No se pudo completar la sincronización de Mercado Libre."), 500












@app.post("/api/admin/catalog-cleanup/apply")
def admin_apply_catalog_cleanup():
    error = csrf_error()
    if error:
        return error
    if DATABASE_URL:
        return jsonify(error="La limpieza de catálogo solo está disponible con SQLite local."), 409
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != CATALOG_CLEANUP_CONFIRMATION:
        return jsonify(error="La frase de confirmación no coincide."), 400
    expected_ids = data.get("expected_ids")
    if (
        not isinstance(expected_ids, list)
        or any(not isinstance(product_id, str) or not product_id for product_id in expected_ids)
        or len(expected_ids) != len(set(expected_ids))
    ):
        return jsonify(error="La simulación de productos no es válida; vuelve a ejecutarla."), 400
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
    try:
        with closing(connect_catalog_readonly(app.config["DATABASE"])) as database:
            preview = catalog_cleanup_preview(database)
        actual_ids = [item["id"] for item in preview["targets"]]
        if actual_ids != expected_ids:
            return jsonify(
                error="El catálogo cambió después de la simulación. No se borró nada; vuelve a previsualizar."
            ), 409
        backup_directory, backup_manifest = backup_before_cleanup(
            app.config["DATABASE"],
            ROOT,
            ROOT / "static" / "images",
        )
        result = apply_catalog_cleanup(app.config["DATABASE"], expected_ids)
        return jsonify(
            **result,
            backup_directory=str(backup_directory.relative_to(ROOT)),
            backup_manifest=backup_manifest,
        )
    except (OSError,) + DATABASE_ERRORS + (RuntimeError,) as error:
        app.logger.exception("No se pudo completar la limpieza confirmada del catálogo.")
        return jsonify(error=str(error)), 409 if isinstance(error, RuntimeError) else 500


@app.patch("/api/admin/products/<product_id>")
def admin_update_product(product_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if set(data) - {"active", "price"} or not data:
        return jsonify(error="Solo se pueden modificar el precio y el estado de publicación."), 400
    active = data.get("active")
    if "active" in data and not isinstance(active, bool):
        return jsonify(error="Indica si la publicación debe estar activa o pausada."), 400
    price = data.get("price")
    if "price" in data and (
        isinstance(price, bool)
        or not isinstance(price, (int, str))
        or not str(price).isdigit()
        or int(price) <= 0
        or int(price) > 100_000_000
    ):
        return jsonify(error="El precio debe ser un monto entero en COP dentro del rango permitido."), 400
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
        product = database.execute(
            "SELECT stock, price, original_price FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if not product:
            return jsonify(error="No encontramos ese producto."), 404
        if active and product["stock"] < 1:
            return jsonify(error="No se puede reactivar una publicación sin existencias."), 409
        assignments = []
        values = []
        if "active" in data:
            assignments.append("active = ?")
            values.append(int(active))
        if "price" in data:
            price = int(price)
            assignments.extend(["price = ?", "original_price = ?"])
            values.extend([price, max(price, product["original_price"])])
        values.append(product_id)
        database.execute(
            f"UPDATE products SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        database.commit()
        return jsonify(
            ok=True,
            active=active if "active" in data else None,
            price=int(price) if "price" in data else product["price"],
        )


@app.delete("/api/admin/products/<product_id>")
def admin_delete_product(product_id):
    error = csrf_error()
    if error:
        return error
    with closing(connect_db()) as database:
        admin, error = current_admin(database)
        if error:
            return error
        database.execute("BEGIN IMMEDIATE")
        product = database.execute(
            "SELECT id, name FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if not product:
            database.rollback()
            return jsonify(error="No encontramos ese producto."), 404

        references = database.execute(
            """SELECT
                 EXISTS(SELECT 1 FROM order_items WHERE product_id = ?) AS has_order_items,
                 EXISTS(SELECT 1 FROM supplier_orders WHERE product_id = ?) AS has_supplier_orders,
                 EXISTS(SELECT 1 FROM return_requests WHERE product_id = ?) AS has_returns,
                 EXISTS(SELECT 1 FROM product_reviews WHERE product_id = ?) AS has_reviews""",
            (product_id, product_id, product_id, product_id),
        ).fetchone()
        has_history = any(bool(references[key]) for key in references.keys())
        if has_history:
            action = "archived"
            database.execute(
                """UPDATE products SET active = 0, stock = 0,
                           archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP)
                   WHERE id = ?""",
                (product_id,),
            )
            message = (
                "El producto tenía pedidos u otras referencias históricas; "
                "se ocultó de la tienda para conservarlas."
            )
        else:
            action = "deleted"
            database.execute(
                "DELETE FROM supplier_products WHERE product_id = ?",
                (product_id,),
            )
            database.execute(
                "DELETE FROM product_interactions WHERE product_id = ?",
                (product_id,),
            )
            database.execute(
                "DELETE FROM catalog_cleanup_exclusions WHERE product_id = ?",
                (product_id,),
            )
            database.execute("DELETE FROM products WHERE id = ?", (product_id,))
            message = "El producto se eliminó del catálogo."

        database.execute(
            """INSERT INTO admin_audit_log
               (actor_user_id, action, affected_resource, details, ip_address)
               VALUES (?, 'delete_product', ?, ?, ?)""",
            (
                admin["id"],
                f"product:{product_id}",
                json.dumps({"result": action}, ensure_ascii=True, sort_keys=True),
                request.remote_addr or "unknown",
            ),
        )
        database.commit()
        return jsonify(ok=True, action=action, message=message)


@app.patch("/api/admin/returns/<int:return_id>")
def admin_update_return(return_id):
    error = csrf_error()
    if error:
        return error
    status = str((request.get_json(silent=True) or {}).get("status", "")).strip()
    if status not in {"reviewing", "accepted", "rejected"}:
        return jsonify(error="Elige un estado de revisión válido."), 400
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
        return_request = database.execute(
            """SELECT order_id, split_id FROM return_requests
               WHERE id = ? AND status IN ('received', 'reviewing')""",
            (return_id,),
        ).fetchone()
        if not return_request:
            return jsonify(error="No encontramos una solicitud de devolución abierta."), 404
        database.execute(
            "UPDATE return_requests SET status = ? WHERE id = ?",
            (status, return_id),
        )
        database.commit()
        notify_customer_order_event(
            database,
            return_request["order_id"],
            "return_updated",
            return_request["split_id"],
        )
        return jsonify(ok=True, status=status)


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
            """SELECT id, order_id, shipping_status, delivered_at FROM payment_splits
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
        if status != split["shipping_status"]:
            notify_customer_order_event(database, split["order_id"], status, split_id)
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
        if return_request:
            notify_customer_order_event(
                database,
                return_request["order_id"],
                "return_updated",
                return_request["split_id"],
            )
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
        commission_rate = Decimal(
            read_admin_setting(
                database, "commission_rate", os.environ.get("COMMISSION_RATE", "0.10")
            )
        )
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
                "commission_due": calculate_commission(split["amount"], commission_rate),
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
    app.logger.warning("Rejected manual Nequi confirmation for split %s.", split_id)
    return jsonify(error="Nequi solo se confirma mediante el webhook firmado de Wompi."), 410


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
            return jsonify(error="Inicia sesión o regístrate para continuar con tu compra."), 401
        if PAYMENT_PROVIDER != "wompi":
            return jsonify(error="El proveedor de pagos configurado no está habilitado."), 503
        if payment_rate_limit_exceeded(database, user["id"]):
            return jsonify(error="Alcanzaste el límite de intentos de pago. Inténtalo más tarde."), 429
        try:
            requested_method = validate_wompi_payment_method(data.get("payment_method"))
        except WompiPaymentProviderError as exception:
            return jsonify(error=str(exception)), 400
        try:
            wompi = WompiPaymentProvider()
            acceptance = wompi.acceptance_info()
            if not (
                isinstance(data.get("acceptance_token"), str)
                and hmac.compare_digest(
                    data["acceptance_token"], acceptance["acceptance_token"]
                )
                and isinstance(data.get("accept_personal_auth"), str)
                and hmac.compare_digest(
                    data["accept_personal_auth"], acceptance["accept_personal_auth"]
                )
            ):
                return jsonify(error="Lee y acepta las condiciones de pago y tratamiento de datos."), 400
            public_url("/")
            if requested_method in {"card_visa", "card_mastercard"}:
                token = data.get("card_token")
                brand = str(data.get("card_brand", "")).strip().upper()
                expected_brand = "VISA" if requested_method == "card_visa" else "MASTERCARD"
                expected_token_prefix = (
                    "tok_test_" if wompi.settings["environment"] == "sandbox" else "tok_prod_"
                )
                if (
                    not isinstance(token, str)
                    or not token.startswith(expected_token_prefix)
                    or not re.fullmatch(r"tok_(?:test|prod)_[A-Za-z0-9_-]{1,160}", token)
                    or brand != expected_brand
                ):
                    return jsonify(error="No se pudo validar una tarjeta Visa o Mastercard permitida."), 400
            else:
                nequi_phone = re.sub(r"\D", "", str(data.get("nequi_phone", "")))
                if not re.fullmatch(r"3\d{9}", nequi_phone):
                    return jsonify(error="Ingresa un número colombiano de 10 dígitos registrado en Nequi."), 400
        except WompiPaymentProviderError as exception:
            app.logger.error("Wompi checkout is not ready: %s", exception)
            return jsonify(error=str(exception)), 503
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
            for cart_key, quantity in cart.items():
                variant_id = cart_key[8:] if cart_key.startswith("variant:") else None
                variant = None
                if variant_id:
                    variant = database.execute(
                        """SELECT * FROM product_variants WHERE id = ?""",
                        (variant_id,),
                    ).fetchone()
                    product_id = variant["product_id"] if variant else None
                else:
                    product_id = cart_key
                product = database.execute(
                    f"""SELECT products.*, users.name AS seller_name,
                              EXISTS(SELECT 1 FROM product_variants
                                     WHERE product_variants.product_id = products.id) AS has_variants
                       FROM products LEFT JOIN users ON users.id = products.seller_id
                       WHERE products.id = ? AND products.active = 1
                         AND {ENTREPRENEUR_PRODUCT_VISIBILITY_SQL}""",
                    (product_id,),
                ).fetchone()
                if (
                    not product
                    or (variant_id and (not variant or quantity > variant["stock"]))
                    or (not variant_id and (product["has_variants"] or quantity > product["stock"]))
                ):
                    raise ValueError("El inventario cambió. Revisa las cantidades de tu carrito.")
                unit_price = product_json(product)["price"]
                variant_label = None
                if variant:
                    variant_label = variant_options_label(variant["options_json"])
                    if variant["price"]:
                        sale_percent = product_json(product)["flash_sale_percent"]
                        unit_price = (
                            max(1, variant["price"] * (100 - sale_percent) // 100)
                            if sale_percent
                            else variant["price"]
                        )
                line_total = unit_price * quantity
                item_shipping = product["shipping_cost"] * quantity
                rows.append((product, quantity, unit_price, variant_id))
                total += line_total
                group = grouped_items.setdefault(
                    product["seller_id"],
                    {"amount": 0, "shipping_amount": 0, "items": []},
                )
                group["amount"] += line_total
                group["shipping_amount"] += item_shipping
                group["items"].append(
                    {
                        "id": product["id"],
                        "variant_id": variant_id,
                        "name": f"{product['name']} · {variant_label}" if variant_label else product["name"],
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
            total = total - discount + sum(
                group["shipping_amount"] for group in grouped_items.values()
            )
            commission_rate = Decimal(
                read_admin_setting(
                    database,
                    "commission_rate",
                    os.environ.get("COMMISSION_RATE", "0.10"),
                )
            )
            for seller_id, group in grouped_items.items():
                group["amount"] += group["shipping_amount"]
                if PAYMENT_PROVIDER == "wompi":
                    group["payment_method"] = "wompi"
                    group["nequi_phone"] = None
                    group["commission_amount"] = (
                        sum(
                            calculate_commission(
                                item["price"] * item["quantity"],
                                commission_rate,
                            )
                            for item in group["items"]
                        )
                        if seller_id is not None
                        else 0
                    )
                    group["net_amount"] = (
                        group["amount"] - group["commission_amount"]
                        if seller_id is not None
                        else 0
                    )
                    continue
                if PAYMENT_PROVIDER == "payu":
                    if seller_id is not None:
                        seller_account = database.execute(
                            """SELECT provider_account_id, verification_status
                               FROM seller_accounts WHERE seller_id = ? AND provider = 'payu'""",
                            (seller_id,),
                        ).fetchone()
                        if (
                            not seller_account
                            or seller_account["verification_status"] != "verified"
                            or not seller_account["provider_account_id"]
                        ):
                            raise ValueError(
                                "Un vendedor de tu bolsa aún no tiene su cuenta PayU verificada."
                            )
                    group["payment_method"] = "payu"
                    group["nequi_phone"] = None
                    group["commission_amount"] = (
                        sum(
                            calculate_commission(
                                item["price"] * item["quantity"],
                                payment_settings()["commission_rate"],
                            )
                            for item in group["items"]
                        )
                        if seller_id is not None
                        else 0
                    )
                    group["net_amount"] = (
                        group["amount"] - group["commission_amount"]
                        if seller_id is not None
                        else 0
                    )
                    continue
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
            if PAYMENT_PROVIDER == "payu":
                payment_webhook_url = public_url("/api/webhooks/payments")
                return_url = public_url("/payu/return")
            if needs_mercadopago:
                payment_webhook_url = public_url(url_for("mercadopago_webhook"))
                return_url = public_url(url_for("mercadopago_return"))
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            cursor = database.execute(
                """INSERT INTO orders
                   (user_id, customer_name, address, city, total, status, payment_status,
                    phone, department, postal_code, delivery_notes, payment_method, expires_at,
                    stock_released, payment_provider)
                   VALUES (?, ?, ?, ?, ?, 'Pendiente de pago', 'pending', ?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    user["id"], customer_name, address, city, total, phone, department,
                    postal_code, delivery_notes, requested_method, expires_at.isoformat(),
                    PAYMENT_PROVIDER,
                ),
            )
            order_id = cursor.lastrowid
            payment_reference = f"XRTE-{order_id}"
            database.execute(
                "UPDATE orders SET payment_reference = ? WHERE id = ?",
                (payment_reference, order_id),
            )
            for product, quantity, unit_price, variant_id in rows:
                if variant_id:
                    database.execute(
                        "UPDATE product_variants SET stock = stock - ? WHERE id = ?",
                        (quantity, variant_id),
                    )
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
                       (id, order_id, seller_id, amount, shipping_amount, payment_method,
                        nequi_phone, commission_amount, net_amount, transfer_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        split_id,
                        order_id,
                        seller_id,
                        group["amount"],
                        group["shipping_amount"],
                        group["payment_method"],
                        group["nequi_phone"],
                        group.get("commission_amount", 0),
                        group.get("net_amount", 0),
                        "pending" if seller_id is not None and PAYMENT_PROVIDER == "wompi"
                        else "scheduled" if seller_id is not None and PAYMENT_PROVIDER == "payu"
                        else "not_applicable",
                    ),
                )
                split_records.append(
                    {
                        "id": split_id,
                        "seller_id": seller_id,
                        "amount": group["amount"],
                        "shipping_amount": group["shipping_amount"],
                        "items": group["items"],
                        "payment_method": group["payment_method"],
                        "commission_amount": group.get("commission_amount", 0),
                        "net_amount": group.get("net_amount", 0),
                    }
                )
                for item in group["items"]:
                    gross_amount = item["price"] * item["quantity"]
                    commission_amount = (
                        calculate_commission(
                            gross_amount,
                            commission_rate if PAYMENT_PROVIDER == "wompi"
                            else payment_settings()["commission_rate"],
                        )
                        if PAYMENT_PROVIDER in {"payu", "wompi"} and seller_id is not None
                        else 0
                    )
                    database.execute(
                        """INSERT INTO order_items
                           (order_id, product_id, product_name, unit_price, quantity, variant_id,
                            seller_id, gross_amount, commission_xrte, net_seller_amount,
                            transfer_status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            order_id,
                            item["id"],
                            item["name"],
                            item["price"],
                            item["quantity"],
                            item["variant_id"],
                            seller_id,
                            gross_amount,
                            commission_amount,
                            gross_amount - commission_amount if seller_id is not None else 0,
                            "pending" if seller_id is not None and PAYMENT_PROVIDER == "wompi"
                            else "scheduled" if seller_id is not None and PAYMENT_PROVIDER == "payu"
                            else "not_applicable",
                        ),
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
            for product, quantity, _unit_price, variant_id in rows:
                if variant_id:
                    database.execute(
                        "UPDATE product_variants SET stock = stock + ? WHERE id = ?",
                        (quantity, variant_id),
                    )
                database.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ?",
                    (quantity, product["id"]),
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
        try:
            transaction = wompi.create_transaction(
                reference=payment_reference,
                amount_in_cents=total * 100,
                customer_email=user["email"],
                payment_method=requested_method,
                acceptance_token=acceptance["acceptance_token"],
                accept_personal_auth=acceptance["accept_personal_auth"],
                redirect_url=public_url(f"/pedido/{order_id}/pago"),
                card_token=data.get("card_token"),
                card_brand=str(data.get("card_brand", "")).strip().upper(),
                nequi_phone=(
                    re.sub(r"\D", "", str(data.get("nequi_phone", "")))
                    if requested_method == "nequi"
                    else None
                ),
            )
        except WompiPaymentOutcomeUnknown as exception:
            with closing(connect_db()) as audit_database:
                payment_audit(
                    audit_database,
                    "wompi_transaction_outcome_unknown",
                    order_id=order_id,
                    amount=total,
                    details={"reference": payment_reference, "error": str(exception)},
                )
                audit_database.commit()
            app.logger.error("Wompi transaction outcome unknown for order %s.", order_id)
            return jsonify(
                error="No se pudo confirmar la respuesta de Wompi. No repitas el pago; revisa el estado del pedido.",
                order_id=order_id,
                payment_url=url_for("nequi_payment_page", order_id=order_id),
            ), 202
        except WompiPaymentProviderError as exception:
            with closing(connect_db()) as failure_database:
                failure_database.execute("BEGIN IMMEDIATE")
                failure_database.execute(
                    """UPDATE orders SET payment_status = 'failed',
                       status = 'Pago no completado' WHERE id = ?
                         AND payment_status = 'pending'""",
                    (order_id,),
                )
                failure_database.execute(
                    """UPDATE payment_splits SET status = 'rejected'
                       WHERE order_id = ? AND status = 'pending'""",
                    (order_id,),
                )
                release_order_stock(failure_database, order_id)
                failure_database.execute(
                    """UPDATE discount_coupons SET status = 'issued',
                       reserved_order_id = NULL WHERE reserved_order_id = ?""",
                    (order_id,),
                )
                payment_audit(
                    failure_database,
                    "wompi_transaction_creation_failed",
                    order_id=order_id,
                    amount=total,
                    details={"error": str(exception)},
                )
                failure_database.commit()
            app.logger.warning("Wompi rejected transaction creation for order %s.", order_id)
            return jsonify(error=str(exception)), 502
        transaction_id = str(transaction["id"])
        with closing(connect_db()) as transaction_database:
            transaction_database.execute("BEGIN IMMEDIATE")
            transaction_database.execute(
                """UPDATE orders SET provider_payment_id = ?
                   WHERE id = ? AND payment_status = 'pending'""",
                (transaction_id, order_id),
            )
            transaction_database.execute(
                """UPDATE payment_splits SET payment_id = ?
                   WHERE order_id = ?""",
                (transaction_id, order_id),
            )
            payment_audit(
                transaction_database,
                "wompi_transaction_created",
                order_id=order_id,
                amount=total,
                provider_reference=transaction_id,
                details={"status": str(transaction.get("status", "PENDING"))[:30]},
            )
            transaction_database.commit()
        notify_customer_order_event(database, order_id, "pending")
    return jsonify(
        order_id=order_id,
        total=total,
        shipping_total=sum(group["shipping_amount"] for group in grouped_items.values()),
        status="Pendiente de pago",
        payment_url=url_for("nequi_payment_page", order_id=order_id),
    ), 201


@app.get("/payu/checkout/<int:order_id>")
def payu_checkout_page(order_id):
    abort(410)
    with closing(connect_db()) as database:
        order = database.execute(
            """SELECT orders.id, orders.user_id, orders.total, orders.payment_status,
                      orders.payment_provider, orders.payment_reference, users.email
               FROM orders JOIN users ON users.id = orders.user_id
               WHERE orders.id = ?""",
            (order_id,),
        ).fetchone()
        if not order or order["user_id"] != session.get("user_id"):
            abort(404)
        if order["payment_provider"] != "payu" or order["payment_status"] != "pending":
            abort(409)
        try:
            response_url = public_url(f"/payu/return?order_id={order_id}")
            confirmation_url = public_url("/api/webhooks/payments")
            fields = payu_payment_fields(
                order_id, order["total"], order["email"], response_url, confirmation_url
            )
        except (PaymentServiceError, PaymentGatewayError) as error:
            app.logger.error("Could not build hosted PayU checkout: %s", error)
            abort(503)
        return render_template(
            "payu_checkout.html",
            checkout_url=payu_checkout_url(),
            fields=fields,
            order_id=order_id,
        )


@app.get("/payu/return")
def payu_payment_return():
    abort(410)
    try:
        order_id = int(request.args.get("order_id", ""))
    except ValueError:
        abort(404)
    with closing(connect_db()) as database:
        order = database.execute(
            "SELECT id, user_id, status, payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order or order["user_id"] != session.get("user_id"):
            abort(404)
        return render_template(
            "payment_result.html",
            order_id=order_id,
            order_status=order["status"],
            payment_status=order["payment_status"],
        )


@app.get("/pedido/<int:order_id>/pago")
def nequi_payment_page(order_id):
    with closing(connect_db()) as database:
        order = database.execute(
            """SELECT id, user_id, total, payment_status, payment_method,
                      payment_provider, status FROM orders WHERE id = ?""",
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
            session.pop("cart", None)
        return render_template(
            "nequi_payment.html",
            order_id=order_id,
            total=order["total"],
            payment_status=order["payment_status"],
            order_status=order["status"],
            splits=payment_details,
            payment_method=order["payment_method"],
            payment_provider=order["payment_provider"],
            payment_environment=wompi_environment(),
        )


@app.get("/mercadopago/return")
def mercadopago_return():
    abort(410)
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
            session.pop("cart", None)
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
    return jsonify(error="Mercado Pago no es el proveedor de pagos activo."), 410
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


def notify_payment_admin(subject, detail):
    if ADMIN_EMAIL:
        send_email_message(ADMIN_EMAIL, f"{subject} · XrteShop", detail)


@app.post("/api/webhooks/payments")
def payu_payment_webhook():
    return jsonify(error="PayU no es el proveedor de pagos activo."), 410
    notification = request.form.to_dict()
    try:
        valid_signature = verify_payu_payment_notification(notification)
    except PaymentServiceError as error:
        app.logger.error("PayU payment webhook is not configured: %s", error)
        return jsonify(error="La recepción de pagos no está configurada."), 503
    if not valid_signature:
        return jsonify(error="Firma de notificación de pago no válida."), 401
    transaction_id = str(notification.get("transaction_id", "")).strip()
    reference = str(notification.get("reference_sale", "")).strip()
    if not transaction_id or not re.fullmatch(r"XRTE-[1-9]\d{0,17}", reference):
        return jsonify(error="La referencia o transacción de PayU no es válida."), 400
    if str(notification.get("state_pol", "")) == "10":
        return jsonify(
            error="PayU no documenta autenticación para el webhook de disputas. Registra el caso desde el panel administrativo después de verificarlo en PayU."
        ), 503
    safe_payload = {
        key: str(notification.get(key, ""))[:200]
        for key in (
            "transaction_id", "reference_sale", "value", "new_value", "currency",
            "state_pol", "response_code_pol", "merchant_id",
        )
    }
    event_id = f"payu-payment:{transaction_id}:{notification.get('state_pol', '')}"
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        order = database.execute(
            """SELECT id, user_id, total, payment_status FROM orders
               WHERE payment_reference = ? AND payment_provider = 'payu'""",
            (reference,),
        ).fetchone()
        if not order:
            database.rollback()
            return jsonify(error="No encontramos el pedido de esta notificación."), 404
        try:
            value = Decimal(str(notification.get("new_value", ""))).quantize(Decimal("0.01"))
        except InvalidOperation:
            database.rollback()
            return jsonify(error="El monto de la notificación no es válido."), 400
        if value != Decimal(order["total"]).quantize(Decimal("0.01")):
            database.rollback()
            return jsonify(error="El monto de PayU no coincide con el total del pedido."), 409
        existing = database.execute(
            "SELECT processed FROM payment_events WHERE provider_event_id = ?",
            (event_id,),
        ).fetchone()
        if existing and existing["processed"]:
            database.rollback()
            return jsonify(ok=True, duplicate=True)
        database.execute(
            """INSERT INTO payment_events
               (provider_event_id, event_type, payload, processed)
               VALUES (?, 'payment_status', ?, 0)
               ON CONFLICT(provider_event_id) DO UPDATE SET
                   processing_error = NULL""",
            (event_id, json.dumps(safe_payload, sort_keys=True)),
        )
        try:
            state = str(notification.get("state_pol", ""))
            update_order_after_payu_payment(database, order, notification)
            payment_status = database.execute(
                "SELECT payment_status FROM orders WHERE id = ?",
                (order["id"],),
            ).fetchone()["payment_status"]
            database.execute(
                """UPDATE payment_events SET processed = 1, processed_at = CURRENT_TIMESTAMP
                   WHERE provider_event_id = ?""",
                (event_id,),
            )
            database.commit()
        except Exception as error:
            database.execute(
                """UPDATE payment_events SET processing_error = ?
                   WHERE provider_event_id = ?""",
                (str(error)[:300], event_id),
            )
            database.commit()
            app.logger.exception("Could not process signed PayU payment event %s", event_id)
            return jsonify(error="No se pudo procesar la notificación; PayU puede reintentar."), 500
        payment_status = database.execute(
            "SELECT payment_status FROM orders WHERE id = ?", (order["id"],)
        ).fetchone()["payment_status"]
        if payment_status == "approved":
            write_user_cart(database, order["user_id"], {})
            database.commit()
            notify_customer_order_event(database, order["id"], "paid")
    return jsonify(ok=True)


def _wompi_transaction_method_matches(order_method, transaction):
    payment_method = transaction.get("payment_method")
    if not isinstance(payment_method, dict):
        return False
    payment_type = str(
        transaction.get("payment_method_type") or payment_method.get("type") or ""
    ).upper()
    if order_method == "nequi":
        return payment_type == "NEQUI"
    if order_method not in {"card_visa", "card_mastercard"} or payment_type != "CARD":
        return False
    extra = payment_method.get("extra")
    brand = (
        extra.get("brand")
        if isinstance(extra, dict)
        else payment_method.get("brand")
    )
    expected_brand = "VISA" if order_method == "card_visa" else "MASTERCARD"
    return str(brand or "").upper() == expected_brand


def _apply_wompi_refund_status(database, refund, provider_refund):
    provider_status = str(provider_refund.get("status", "")).upper()
    if provider_status == "PENDING":
        database.execute(
            "UPDATE payment_refunds SET status = 'pending' WHERE id = ?",
            (refund["id"],),
        )
        return
    if provider_status in {"DECLINED", "CANCELLED", "ERROR"}:
        database.execute(
            "UPDATE payment_refunds SET status = 'failed' WHERE id = ?",
            (refund["id"],),
        )
        payment_audit(
            database,
            "refund_failed",
            order_id=refund["order_id"],
            amount=refund["amount"],
            provider_reference=refund["provider_refund_id"],
            details={"provider_status": provider_status},
        )
        return
    if provider_status != "APPROVED":
        raise ValueError("Wompi devolvió un estado de reembolso no reconocido.")

    order = database.execute(
        """SELECT id, total, refunded_amount, payment_status
           FROM orders WHERE id = ?""",
        (refund["order_id"],),
    ).fetchone()
    if not order or refund["amount"] > order["total"] - order["refunded_amount"]:
        raise ValueError("El monto confirmado por Wompi excede el saldo reembolsable.")
    split_rows = database.execute(
        """SELECT id, seller_id, amount, commission_amount, net_amount,
                  transfer_status, refunded_amount, refunded_commission_amount
           FROM payment_splits WHERE order_id = ? AND seller_id IS NOT NULL""",
        (refund["order_id"],),
    ).fetchall()
    commission_reversed_total = 0
    for split in split_rows:
        seller_refund = min(
            split["amount"] - split["refunded_amount"],
            (refund["amount"] * split["amount"] + order["total"] // 2) // order["total"],
        )
        if seller_refund <= 0 or split["amount"] <= 0:
            continue
        commission_reversed = min(
            split["commission_amount"],
            (split["commission_amount"] * seller_refund + split["amount"] // 2) // split["amount"],
        )
        seller_recovery = min(
            split["net_amount"],
            (split["net_amount"] * seller_refund + split["amount"] // 2) // split["amount"],
        )
        commission_reversed_total += commission_reversed
        adjusted_net = max(0, split["net_amount"] - seller_recovery)
        database.execute(
            """UPDATE payment_splits SET net_amount = ?,
               refunded_amount = refunded_amount + ?,
               refunded_commission_amount = refunded_commission_amount + ?,
               transfer_status = CASE
                   WHEN transfer_status IN ('pending', 'scheduled') AND ? = 0
                   THEN 'not_applicable' ELSE transfer_status END
               WHERE id = ?""",
            (
                adjusted_net,
                seller_refund,
                commission_reversed,
                adjusted_net,
                split["id"],
            ),
        )
        if split["transfer_status"] in {"pending", "scheduled"}:
            database.execute(
                """UPDATE payment_ledger SET amount_cop = ?,
                   status = CASE WHEN ? = 0 THEN 'ajustado_por_reembolso'
                                 ELSE status END
                   WHERE entry_key = ?""",
                (
                    adjusted_net,
                    adjusted_net,
                    f"seller-payable:{split['id']}",
                ),
            )
            if adjusted_net == 0:
                database.execute(
                    """UPDATE order_items SET transfer_status = 'not_applicable'
                       WHERE order_id = ? AND seller_id = ?""",
                    (refund["order_id"], split["seller_id"]),
                )
        elif seller_recovery:
            database.execute(
                """INSERT INTO seller_refund_liabilities
                   (refund_id, order_id, seller_id, amount)
                   VALUES (?, ?, ?, ?)""",
                (refund["id"], refund["order_id"], split["seller_id"], seller_recovery),
            )
        record_payment_ledger(
            database,
            entry_key=f"seller-refund-adjustment:{refund['id']}:{split['id']}",
            order_id=refund["order_id"],
            split_id=split["id"],
            seller_id=split["seller_id"],
            entry_type="seller_refund_adjustment",
            amount_cop=-seller_recovery,
            status=(
                "ajustado_por_reembolso"
                if split["transfer_status"] in {"pending", "scheduled"}
                else "por_recuperar_al_emprendedor"
            ),
            details={"refund_id": refund["id"]},
        )
        record_payment_ledger(
            database,
            entry_key=f"commission-reversal:{refund['id']}:{split['id']}",
            order_id=refund["order_id"],
            split_id=split["id"],
            seller_id=split["seller_id"],
            entry_type="xrteshop_commission_reversal",
            amount_cop=-commission_reversed,
            details={"refund_id": refund["id"]},
        )
        payment_audit(
            database,
            "seller_refund_allocated",
            order_id=refund["order_id"],
            split_id=split["id"],
            seller_id=split["seller_id"],
            amount=seller_refund,
            details={
                "refund_id": refund["id"],
                "commission_reversed": commission_reversed,
                "seller_recovery": seller_recovery,
            },
        )
    refunded_total = order["refunded_amount"] + refund["amount"]
    payment_status = (
        "refunded" if refunded_total == order["total"] else "partially_refunded"
    )
    database.execute(
        "UPDATE orders SET refunded_amount = ?, payment_status = ? WHERE id = ?",
        (refunded_total, payment_status, refund["order_id"]),
    )
    database.execute(
        """UPDATE payment_refunds SET commission_reversed = ?, status = 'completed',
           completed_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (commission_reversed_total, refund["id"]),
    )
    record_payment_ledger(
        database,
        entry_key=f"refund-outflow:{refund['id']}",
        order_id=refund["order_id"],
        entry_type="customer_refund",
        amount_cop=-refund["amount"],
        status="registrado",
        details={"provider": "wompi", "refund_id": refund["id"]},
    )
    payment_audit(
        database,
        "refund_completed",
        order_id=refund["order_id"],
        amount=refund["amount"],
        provider_reference=refund["provider_refund_id"],
        details={"refund_id": refund["id"], "payment_status": payment_status},
    )


@app.post("/api/webhooks/wompi")
def wompi_webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Notificación Wompi no válida."), 400
    try:
        wompi = WompiPaymentProvider()
    except WompiPaymentProviderError as error:
        app.logger.error("Wompi webhook cannot start: %s", error)
        return jsonify(error="La recepción de pagos no está configurada."), 503
    if not wompi.verify_webhook(payload, request.headers.get("X-Event-Checksum")):
        return jsonify(error="Firma de notificación Wompi no válida."), 401

    event_type = str(payload.get("event", "")).strip()
    data = payload.get("data")
    if not isinstance(data, dict):
        return jsonify(error="Notificación Wompi sin datos válidos."), 400
    event_object = data.get("transaction") if event_type == "transaction.updated" else (
        data.get("refund") if event_type == "refund.updated" else None
    )
    if event_type in {"transaction.updated", "refund.updated"} and not isinstance(event_object, dict):
        return jsonify(error="El objeto de pago de Wompi no es válido."), 400
    provider_object_id = (
        str(event_object.get("id") or event_object.get("refund_id") or "").strip()
        if isinstance(event_object, dict)
        else ""
    )
    if event_type in {"transaction.updated", "refund.updated"} and not provider_object_id:
        return jsonify(error="La notificación Wompi no contiene un identificador."), 400
    try:
        verified_object = None
        if event_type == "transaction.updated":
            verified_object = wompi.get_transaction(provider_object_id)
        elif event_type == "refund.updated":
            verified_object = wompi.get_refund(provider_object_id)
    except WompiPaymentProviderError as error:
        app.logger.warning("Wompi event verification failed for object %s: %s", provider_object_id, error)
        return jsonify(error="No fue posible verificar el evento con Wompi."), 503
    if verified_object and str(
        verified_object.get("id") or verified_object.get("refund_id") or ""
    ) != provider_object_id:
        return jsonify(error="El identificador del evento no coincide con Wompi."), 400

    verified_status = str((verified_object or {}).get("status", "")).upper()
    event_id = (
        f"wompi:{event_type}:{provider_object_id}:{verified_status}"
        if event_type in {"transaction.updated", "refund.updated"}
        else f"wompi:{hashlib.sha256(request.get_data(cache=True)).hexdigest()}"
    )
    signature = payload.get("signature") if isinstance(payload.get("signature"), dict) else {}
    safe_payload = json.dumps(
        {
            "event": event_type[:80],
            "object_id": provider_object_id,
            "status": verified_status[:30],
            "timestamp": payload.get("timestamp"),
            "checksum": str(signature.get("checksum", ""))[:64],
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    payment_status = None
    order_id = None
    try:
        with closing(connect_db()) as database:
            database.execute("BEGIN IMMEDIATE")
            prior_event = database.execute(
                """SELECT processed FROM payment_events
                   WHERE provider_event_id = ?""",
                (event_id,),
            ).fetchone()
            if prior_event and prior_event["processed"]:
                database.rollback()
                return jsonify(ok=True, duplicate=True)
            database.execute(
                """INSERT INTO payment_events
                   (provider_event_id, event_type, payload, processed)
                   VALUES (?, ?, ?, 0)
                   ON CONFLICT(provider_event_id) DO UPDATE SET
                       processing_error = NULL""",
                (event_id, event_type or "unknown", safe_payload),
            )

            if event_type == "transaction.updated":
                transaction = verified_object
                reference = str(transaction.get("reference", ""))
                order = database.execute(
                    """SELECT id, user_id, total, payment_status, payment_method,
                              payment_reference, provider_payment_id
                       FROM orders WHERE payment_reference = ?
                         AND payment_provider = 'wompi'""",
                    (reference,),
                ).fetchone()
                if not order:
                    database.rollback()
                    return jsonify(error="No encontramos el pedido de esta notificación."), 404
                order_id = order["id"]
                status = str(transaction.get("status", "")).upper()
                transaction_matches = (
                    str(transaction.get("id")) == provider_object_id
                    and transaction.get("currency") == "COP"
                    and transaction.get("amount_in_cents") == order["total"] * 100
                    and (
                        status != "APPROVED"
                        or _wompi_transaction_method_matches(order["payment_method"], transaction)
                    )
                )
                if (
                    not transaction_matches
                ):
                    payment_audit(
                        database,
                        "wompi_transaction_validation_failed",
                        order_id=order_id,
                        amount=order["total"],
                        provider_reference=provider_object_id,
                        details={"reference": reference},
                    )
                    database.execute(
                        """UPDATE payment_events SET processed = 1,
                           processed_at = CURRENT_TIMESTAMP WHERE provider_event_id = ?""",
                        (event_id,),
                    )
                    database.commit()
                    notify_payment_admin(
                        "Pago Wompi requiere revisión",
                        f"La transacción del pedido #{order_id} no coincide con el método o total esperado.",
                    )
                    return jsonify(ok=True, requires_review=True)

                if status == "APPROVED" and order["payment_status"] == "pending":
                    database.execute(
                        """UPDATE orders SET provider_payment_id = ?,
                           payment_status = 'approved', status = 'Recibido'
                           WHERE id = ? AND payment_status = 'pending'""",
                        (provider_object_id, order_id),
                    )
                    database.execute(
                        """UPDATE payment_splits SET status = 'approved',
                           payment_id = ?, transfer_status = CASE
                               WHEN seller_id IS NULL THEN 'not_applicable'
                               ELSE 'pending' END
                           WHERE order_id = ?""",
                        (provider_object_id, order_id),
                    )
                    database.execute(
                        """UPDATE order_items SET transfer_status = CASE
                               WHEN seller_id IS NULL THEN 'not_applicable'
                               ELSE 'pending' END WHERE order_id = ?""",
                        (order_id,),
                    )
                    order_payment_status(database, order_id)
                    record_payment_ledger(
                        database,
                        entry_key=f"gross-sale:{order_id}",
                        order_id=order_id,
                        entry_type="gross_sale",
                        amount_cop=order["total"],
                        status="payment_approved",
                        details={
                            "provider": "wompi",
                            "payment_method": order["payment_method"],
                            "provider_reference": provider_object_id,
                        },
                    )
                    gateway_fee_estimate = estimate_gateway_fee(order["total"])
                    if gateway_fee_estimate is not None:
                        record_payment_ledger(
                            database,
                            entry_key=f"gateway-fee-estimate:{order_id}",
                            order_id=order_id,
                            entry_type="gateway_fee_estimate",
                            amount_cop=-gateway_fee_estimate,
                            status="estimado",
                            details={"provider": "wompi", "estimate": True},
                        )
                    record_entrepreneur_payment_ledger(database, order_id)
                    payment_audit(
                        database,
                        "payment_approved",
                        order_id=order_id,
                        amount=order["total"],
                        provider_reference=provider_object_id,
                    )
                    write_user_cart(database, order["user_id"], {})
                elif status in {"DECLINED", "ERROR", "VOIDED"} and order["payment_status"] == "pending":
                    database.execute(
                        """UPDATE orders SET provider_payment_id = ?,
                           payment_status = 'failed', status = 'Pago no completado'
                           WHERE id = ? AND payment_status = 'pending'""",
                        (provider_object_id, order_id),
                    )
                    database.execute(
                        """UPDATE payment_splits SET status = 'rejected'
                           WHERE order_id = ? AND status = 'pending'""",
                        (order_id,),
                    )
                    release_order_stock(database, order_id)
                    database.execute(
                        """UPDATE discount_coupons SET status = 'issued',
                           reserved_order_id = NULL WHERE reserved_order_id = ?
                             AND status = 'reserved'""",
                        (order_id,),
                    )
                    payment_audit(
                        database,
                        "payment_failed",
                        order_id=order_id,
                        amount=order["total"],
                        provider_reference=provider_object_id,
                        details={"status": status},
                    )
                payment_status = database.execute(
                    "SELECT payment_status FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()["payment_status"]
            elif event_type == "refund.updated":
                refund_id = str(verified_object.get("refund_id") or provider_object_id)
                refund = database.execute(
                    """SELECT payment_refunds.id, payment_refunds.order_id,
                              payment_refunds.amount, payment_refunds.provider_refund_id,
                              orders.total, orders.provider_payment_id
                       FROM payment_refunds JOIN orders
                         ON orders.id = payment_refunds.order_id
                       WHERE payment_refunds.provider_refund_id = ?
                         AND orders.payment_provider = 'wompi'""",
                    (refund_id,),
                ).fetchone()
                if not refund:
                    database.rollback()
                    return jsonify(error="No encontramos el reembolso de esta notificación."), 404
                order_id = refund["order_id"]
                if (
                    str(verified_object.get("transaction_id", "")) != str(refund["provider_payment_id"])
                    or verified_object.get("amount_in_cents") != refund["amount"] * 100
                ):
                    database.rollback()
                    return jsonify(error="El reembolso no coincide con el pedido registrado."), 409
                _apply_wompi_refund_status(database, refund, verified_object)
                payment_status = database.execute(
                    "SELECT payment_status FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()["payment_status"]

            database.execute(
                """UPDATE payment_events SET processed = 1,
                   processed_at = CURRENT_TIMESTAMP WHERE provider_event_id = ?""",
                (event_id,),
            )
            database.commit()
    except DATABASE_ERRORS as error:
        app.logger.exception("Could not process signed Wompi event %s.", event_id)
        with closing(connect_db()) as error_database:
            error_database.execute(
                """INSERT INTO payment_events
                   (provider_event_id, event_type, payload, processed, processing_error)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(provider_event_id) DO UPDATE SET
                       processing_error = excluded.processing_error""",
                (event_id, event_type or "unknown", safe_payload, str(error)[:300]),
            )
            error_database.commit()
        return jsonify(error="No se pudo procesar la notificación; Wompi puede reintentar."), 500
    if payment_status == "approved" and order_id:
        with closing(connect_db()) as database:
            notify_customer_order_event(database, order_id, "paid")
    return jsonify(ok=True)


@app.post("/api/webhooks/payouts")
def payu_payout_webhook():
    return jsonify(error="PayU no es el proveedor de pagos activo."), 410
    return jsonify(
        error="PayU no documenta firma ni autenticación para notificaciones de payouts. "
              "Por seguridad, concilia ese estado desde el panel de XrteShop.",
    ), 503


@app.get("/api/admin/payouts/reconciliation")
def admin_payout_reconciliation():
    with closing(connect_db()) as database:
        _, error = current_admin(database)
        if error:
            return error
        rows = database.execute(
            """SELECT payment_splits.id AS split_id, payment_splits.order_id,
                      payment_splits.seller_id, users.name AS seller_name,
                      users.email AS seller_email, payment_splits.amount,
                      payment_splits.commission_amount, payment_splits.net_amount,
                      payment_splits.transfer_status, payment_splits.payout_due_at,
                      payment_splits.provider_transfer_id, payment_splits.transfer_attempts,
                      payment_splits.transfer_completed_at
               FROM payment_splits
               JOIN orders ON orders.id = payment_splits.order_id
               LEFT JOIN users ON users.id = payment_splits.seller_id
               WHERE payment_splits.transfer_status IN
                     ('scheduled', 'pending', 'processing', 'submitted', 'unknown', 'failed')
                 AND orders.payment_provider = 'wompi'
               ORDER BY payment_splits.payout_due_at, payment_splits.created_at LIMIT 200"""
        ).fetchall()
        return jsonify(payouts=[dict(row) for row in rows])


@app.post("/api/admin/orders/<int:order_id>/disputes")
def admin_record_dispute(order_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    provider_id = str(data.get("provider_dispute_id", "")).strip()
    payment_id = str(data.get("payment_id", "")).strip()
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="El monto de la disputa no es válido."), 400
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", provider_id) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,100}", payment_id
    ):
        return jsonify(error="Registra los IDs de disputa y pago que verificaste en PayU."), 400
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        admin, error = current_admin(database)
        if error:
            database.rollback()
            return error
        order = database.execute(
            "SELECT total, payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order or order["payment_status"] not in {"approved", "partially_refunded"}:
            database.rollback()
            return jsonify(error="El pedido no está en un estado elegible para registrar una disputa."), 409
        dispute_amount = amount or order["total"]
        if not 0 < dispute_amount <= order["total"]:
            database.rollback()
            return jsonify(error="El monto de la disputa debe estar entre 1 y el total del pedido."), 400
        try:
            cursor = database.execute(
                """INSERT INTO payment_disputes
                   (provider_dispute_id, order_id, payment_id, status, amount, payload)
                   VALUES (?, ?, ?, 'open', ?, '{}')""",
                (provider_id, order_id, payment_id, dispute_amount),
            )
        except DATABASE_INTEGRITY_ERRORS:
            database.rollback()
            return jsonify(error="Ese ID de disputa ya está registrado."), 409
        database.execute("UPDATE orders SET disputed = 1 WHERE id = ?", (order_id,))
        payment_audit(
            database, "payment_dispute_manually_recorded", order_id=order_id,
            actor_user_id=admin["id"], amount=dispute_amount,
            provider_reference=provider_id, details={"payment_id": payment_id},
        )
        database.commit()
        return jsonify(ok=True, dispute_id=cursor.lastrowid, status="open"), 201


@app.patch("/api/admin/disputes/<int:dispute_id>")
def admin_update_dispute(dispute_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in {"open", "under_review", "won", "lost"}:
        return jsonify(error="El estado de disputa no es válido."), 400
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        admin, error = current_admin(database)
        if error:
            database.rollback()
            return error
        dispute = database.execute(
            "SELECT order_id, amount, provider_dispute_id FROM payment_disputes WHERE id = ?",
            (dispute_id,),
        ).fetchone()
        if not dispute:
            database.rollback()
            return jsonify(error="No encontramos esa disputa."), 404
        database.execute(
            """UPDATE payment_disputes SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, dispute_id),
        )
        remaining_disputes = database.execute(
            """SELECT COUNT(*) FROM payment_disputes
               WHERE order_id = ? AND id != ?
                 AND status IN ('open', 'under_review', 'lost')""",
            (dispute["order_id"], dispute_id),
        ).fetchone()[0]
        database.execute(
            "UPDATE orders SET disputed = ? WHERE id = ?",
            (int(status != "won" or remaining_disputes > 0), dispute["order_id"]),
        )
        if status == "lost":
            database.execute(
                "UPDATE orders SET payment_status = 'chargeback' WHERE id = ?",
                (dispute["order_id"],),
            )
        payment_audit(
            database, "payment_dispute_manually_updated", order_id=dispute["order_id"],
            actor_user_id=admin["id"], amount=dispute["amount"],
            provider_reference=dispute["provider_dispute_id"], details={"status": status},
        )
        database.commit()
        return jsonify(ok=True, status=status)


@app.patch("/api/admin/refund-liabilities/<int:liability_id>")
def admin_settle_seller_refund_liability(liability_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    reference = str(data.get("settlement_reference", "")).strip()[:100]
    if not reference:
        return jsonify(error="Registra la referencia comprobable del reintegro del vendedor."), 400
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        admin, error = current_admin(database)
        if error:
            database.rollback()
            return error
        liability = database.execute(
            """SELECT order_id, seller_id, amount, status FROM seller_refund_liabilities
               WHERE id = ?""",
            (liability_id,),
        ).fetchone()
        if not liability:
            database.rollback()
            return jsonify(error="No encontramos esa recuperación."), 404
        if liability["status"] != "open":
            database.rollback()
            return jsonify(error="Esta recuperación ya fue conciliada."), 409
        database.execute(
            """UPDATE seller_refund_liabilities SET status = 'settled',
               settled_at = CURRENT_TIMESTAMP, settled_by = ?, settlement_reference = ?
               WHERE id = ?""",
            (admin["id"], reference, liability_id),
        )
        payment_audit(
            database, "seller_refund_recovery_settled", order_id=liability["order_id"],
            seller_id=liability["seller_id"], actor_user_id=admin["id"],
            amount=liability["amount"], provider_reference=reference,
            details={"liability_id": liability_id},
        )
        database.commit()
        return jsonify(ok=True, status="settled")


@app.post("/api/admin/payouts/<split_id>/mark-paid")
def admin_mark_seller_payout_paid(split_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    payment_reference = str(data.get("payment_reference", "")).strip()
    payment_date = str(data.get("payment_date", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", payment_reference):
        return jsonify(error="Registra una referencia verificable del pago al emprendedor."), 400
    try:
        datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Registra la fecha del pago en formato AAAA-MM-DD."), 400
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        admin, error = current_admin(database)
        if error:
            database.rollback()
            return error
        if payment_rate_limit_exceeded(database, admin["id"]):
            database.rollback()
            return jsonify(error="Límite de registros de pago alcanzado. Inténtalo más tarde."), 429
        split = database.execute(
            """SELECT payment_splits.id, payment_splits.order_id,
                      payment_splits.seller_id, payment_splits.net_amount,
                      payment_splits.transfer_status, orders.payment_status,
                      orders.disputed, orders.payment_provider
               FROM payment_splits JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.id = ?""",
            (split_id,),
        ).fetchone()
        if not split or split["seller_id"] is None:
            database.rollback()
            return jsonify(error="No encontramos una cuenta por pagar a un emprendedor."), 404
        if (
            split["transfer_status"] != "pending"
            or split["payment_provider"] != "wompi"
            or split["payment_status"] not in {"approved", "partially_refunded"}
            or split["disputed"]
            or split["net_amount"] <= 0
        ):
            database.rollback()
            return jsonify(error="El saldo no está listo para marcarse como pagado."), 409
        payable = database.execute(
            """UPDATE payment_ledger SET status = 'pagado_al_emprendedor',
               amount_cop = ?, payment_date = ?, payment_reference = ?
               WHERE entry_key = ? AND status = 'pendiente_de_pago_al_emprendedor'""",
            (
                split["net_amount"],
                payment_date,
                payment_reference,
                f"seller-payable:{split_id}",
            ),
        )
        if not payable.rowcount:
            database.rollback()
            return jsonify(error="Falta el asiento contable pendiente; no se registró el pago."), 409
        database.execute(
            """UPDATE payment_splits SET transfer_status = 'paid',
               provider_transfer_id = ?, transfer_completed_at = ?
               WHERE id = ? AND transfer_status = 'pending'""",
            (payment_reference, payment_date, split_id),
        )
        database.execute(
            """UPDATE order_items SET transfer_status = 'paid', transfer_id = ?
               WHERE order_id = ? AND seller_id = ?""",
            (payment_reference, split["order_id"], split["seller_id"]),
        )
        payment_audit(
            database,
            "seller_payout_manually_marked_paid",
            order_id=split["order_id"],
            split_id=split_id,
            seller_id=split["seller_id"],
            actor_user_id=admin["id"],
            amount=split["net_amount"],
            provider_reference=payment_reference,
            details={"payment_date": payment_date},
        )
        database.commit()
        return jsonify(
            ok=True,
            transfer_status="paid",
            payment_date=payment_date,
            payment_reference=payment_reference,
        )


@app.patch("/api/admin/payouts/<split_id>/reconcile")
def admin_reconcile_payout(split_id):
    return jsonify(error="Usa el flujo manual auditado de fecha y referencia del pago."), 410
    if provider_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", provider_id):
        return jsonify(error="El ID de payout no es válido."), 400
    if status in {"submitted", "paid"} and not provider_id:
        return jsonify(error="Registra el ID de payout verificado en el panel de PayU."), 400
    with closing(connect_db()) as database:
        database.execute("BEGIN IMMEDIATE")
        admin, error = current_admin(database)
        if error:
            database.rollback()
            return error
        split = database.execute(
            """SELECT payment_splits.id, payment_splits.order_id,
                      payment_splits.seller_id, payment_splits.net_amount,
                      payment_splits.transfer_status, payment_splits.transfer_started_at,
                      orders.payment_status,
                      orders.disputed
               FROM payment_splits JOIN orders ON orders.id = payment_splits.order_id
               WHERE payment_splits.id = ?""",
            (split_id,),
        ).fetchone()
        if not split:
            database.rollback()
            return jsonify(error="No encontramos ese payout."), 404
        if (
            split["transfer_status"] == "processing"
            and split["transfer_started_at"]
            and database.execute(
                "SELECT julianday(?) < julianday('now', '-5 minutes')",
                (split["transfer_started_at"],),
            ).fetchone()[0] != 1
        ):
            database.rollback()
            return jsonify(error="El payout está en curso; vuelve a conciliar cuando termine."), 409
        if status == "scheduled":
            if split["transfer_status"] not in {"unknown", "failed", "submitted"}:
                database.rollback()
                return jsonify(error="Solo se puede reprogramar un payout fallido o incierto."), 409
            if data.get("confirmation") != "PAYU_CONFIRMED_NO_TRANSFER":
                database.rollback()
                return jsonify(
                    error="Verifica primero en PayU que no se haya enviado dinero; no se reintentan resultados inciertos a ciegas."
                ), 409
            if split["payment_status"] != "approved" or split["disputed"]:
                database.rollback()
                return jsonify(error="El pedido no puede pagarse mientras no esté aprobado y libre de disputas."), 409
            account = database.execute(
                """SELECT verification_status, provider_account_id FROM seller_accounts
                   WHERE seller_id = ? AND provider = 'payu'""",
                (split["seller_id"],),
            ).fetchone()
            if not account or account["verification_status"] != "verified" or not account["provider_account_id"]:
                database.rollback()
                return jsonify(error="El beneficiario PayU del vendedor no está verificado."), 409
        if status == "paid" and split["payment_status"] != "approved":
            database.rollback()
            return jsonify(error="No se puede conciliar un payout si el pago del comprador no está aprobado."), 409
        due_at = datetime.now(timezone.utc).isoformat() if status == "scheduled" else None
        database.execute(
            """UPDATE payment_splits SET transfer_status = ?,
               provider_transfer_id = COALESCE(?, provider_transfer_id),
               transfer_started_at = NULL,
               payout_due_at = COALESCE(?, payout_due_at),
               transfer_attempts = CASE WHEN ? = 'scheduled' THEN 0 ELSE transfer_attempts END
               WHERE id = ?""",
            (status, provider_id or None, due_at, status, split_id),
        )
        database.execute(
            """UPDATE order_items SET transfer_status = ?, transfer_id = COALESCE(?, transfer_id)
               WHERE order_id = ? AND seller_id = ?""",
            (status, provider_id or None, split["order_id"], split["seller_id"]),
        )
        payment_audit(
            database,
            "seller_payout_manually_reconciled",
            order_id=split["order_id"],
            split_id=split_id,
            seller_id=split["seller_id"],
            actor_user_id=admin["id"],
            amount=split["net_amount"],
            provider_reference=provider_id or None,
            details={"previous_status": split["transfer_status"], "status": status, "note": note},
        )
        database.commit()
        return jsonify(ok=True, transfer_status=status)


def process_due_payouts(limit=20):
    app.logger.info("Automatic seller payouts are disabled; use the audited manual payout flow.")
    return 0
    settings = require_payment_credentials()
    if not settings["payouts_approved"]:
        raise PaymentServiceError("PayU Payouts no está aprobado para esta cuenta.")
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect_db()) as database:
        due = database.execute(
            """SELECT payment_splits.id, payment_splits.order_id,
                      payment_splits.seller_id, payment_splits.net_amount,
                      seller_accounts.provider_account_id
               FROM payment_splits
               JOIN orders ON orders.id = payment_splits.order_id
               JOIN seller_accounts ON seller_accounts.seller_id = payment_splits.seller_id
               WHERE payment_splits.transfer_status IN ('scheduled', 'pending')
                 AND payment_splits.payout_due_at <= ?
                 AND payment_splits.status = 'approved'
                 AND orders.payment_status = 'approved'
                 AND orders.disputed = 0
                 AND seller_accounts.provider = 'payu'
                 AND seller_accounts.verification_status = 'verified'
                 AND seller_accounts.provider_account_id IS NOT NULL
                 AND payment_splits.transfer_attempts < 5
               ORDER BY payment_splits.payout_due_at, payment_splits.id LIMIT ?""",
            (now, limit),
        ).fetchall()
    processed = 0
    for row in due:
        with closing(connect_db()) as database:
            database.execute("BEGIN IMMEDIATE")
            claimed = database.execute(
                """UPDATE payment_splits SET transfer_status = 'processing',
                   transfer_started_at = CURRENT_TIMESTAMP,
                   transfer_attempts = transfer_attempts + 1
                   WHERE id = ? AND transfer_status IN ('scheduled', 'pending')
                     AND status = 'approved'""",
                (row["id"],),
            )
            if not claimed.rowcount:
                database.rollback()
                continue
            attempt = database.execute(
                "SELECT transfer_attempts FROM payment_splits WHERE id = ?",
                (row["id"],),
            ).fetchone()["transfer_attempts"]
            payment_audit(
                database, "seller_payout_requested", order_id=row["order_id"],
                split_id=row["id"], seller_id=row["seller_id"],
                amount=row["net_amount"], details={"attempt": attempt},
            )
            database.commit()
        try:
            result = request_payout(
                row["provider_account_id"], row["net_amount"],
                f"XRTE-{row['order_id']}-{row['id']}",
            )
            if not result["provider_transfer_id"]:
                raise PaymentOutcomeUnknown(
                    "PayU no devolvió un identificador de payout; requiere conciliación."
                )
        except PaymentOutcomeUnknown as error:
            with closing(connect_db()) as database:
                database.execute(
                    """UPDATE payment_splits SET transfer_status = 'unknown',
                       transfer_started_at = NULL WHERE id = ?""",
                    (row["id"],),
                )
                database.execute(
                    "UPDATE order_items SET transfer_status = 'unknown' WHERE order_id = ? AND seller_id = ?",
                    (row["order_id"], row["seller_id"]),
                )
                payment_audit(
                    database, "seller_payout_outcome_unknown", order_id=row["order_id"],
                    split_id=row["id"], seller_id=row["seller_id"],
                    amount=row["net_amount"], details={"error": str(error)[:200]},
                )
                database.commit()
            notify_payment_admin(
                "Payout PayU requiere conciliación",
                f"El resultado del payout del pedido #{row['order_id']} es incierto. No reintentes hasta verificarlo en PayU.",
            )
        except PaymentServiceError as error:
            with closing(connect_db()) as database:
                status = "pending" if attempt < 5 else "failed"
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * (2 ** attempt)))
                ).isoformat() if status == "pending" else None
                database.execute(
                    """UPDATE payment_splits SET transfer_status = ?,
                       transfer_started_at = NULL,
                       payout_due_at = COALESCE(?, payout_due_at) WHERE id = ?""",
                    (status, retry_at, row["id"]),
                )
                database.execute(
                    "UPDATE order_items SET transfer_status = ? WHERE order_id = ? AND seller_id = ?",
                    (status, row["order_id"], row["seller_id"]),
                )
                payment_audit(
                    database, "seller_payout_retryable_failure", order_id=row["order_id"],
                    split_id=row["id"], seller_id=row["seller_id"],
                    amount=row["net_amount"], details={"attempt": attempt, "error": str(error)[:200]},
                )
                database.commit()
            notify_payment_admin(
                "Payout PayU pendiente",
                f"Intento {attempt}/5 fallido para el pedido #{row['order_id']}: {error}",
            )
        else:
            with closing(connect_db()) as database:
                database.execute(
                    """UPDATE payment_splits SET transfer_status = 'submitted',
                       provider_transfer_id = ?, transfer_started_at = NULL
                       WHERE id = ? AND transfer_status = 'processing'""",
                    (result["provider_transfer_id"], row["id"]),
                )
                database.execute(
                    """UPDATE order_items SET transfer_status = 'submitted',
                       transfer_id = ? WHERE order_id = ? AND seller_id = ?""",
                    (result["provider_transfer_id"], row["order_id"], row["seller_id"]),
                )
                if result["provider_beneficiary_id"] != row["provider_account_id"]:
                    database.execute(
                        """UPDATE seller_accounts SET provider_account_id = ?, updated_at = CURRENT_TIMESTAMP
                           WHERE seller_id = ? AND provider = 'payu'""",
                        (result["provider_beneficiary_id"], row["seller_id"]),
                    )
                payment_audit(
                    database, "seller_payout_submitted", order_id=row["order_id"],
                    split_id=row["id"], seller_id=row["seller_id"],
                    amount=row["net_amount"], provider_reference=result["provider_transfer_id"],
                )
                database.commit()
        processed += 1
    return processed


@app.get("/api/cron/process-payouts")
def cron_process_due_payouts():
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    if not cron_secret:
        app.logger.error("CRON_SECRET is required before processing scheduled payouts.")
        return jsonify(error="El procesamiento programado no está configurado."), 503
    expected_authorization = f"Bearer {cron_secret}"
    authorization = request.headers.get("Authorization", "")
    if not hmac.compare_digest(authorization, expected_authorization):
        return jsonify(error="No autorizado."), 401
    try:
        processed = process_due_payouts()
    except PaymentServiceError as error:
        app.logger.error("Scheduled payout processing failed: %s", error)
        return jsonify(error="No se pudieron procesar las transferencias pendientes."), 503
    return jsonify(ok=True, processed=processed)


@app.post("/api/admin/orders/<int:order_id>/refunds")
def admin_refund_order(order_id):
    error = csrf_error()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    raw_amount = data.get("amount", 0)
    if isinstance(raw_amount, bool):
        return jsonify(error="El reembolso debe ser un monto entero en COP."), 400
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError):
        return jsonify(error="El reembolso debe ser un monto entero en COP."), 400
    reason = str(data.get("reason", "")).strip()[:300]
    try:
        wompi = WompiPaymentProvider()
    except WompiPaymentProviderError as error:
        return jsonify(error=str(error)), 503
    with closing(connect_db()) as database:
        admin, error = current_admin(database)
        if error:
            return error
        if payment_rate_limit_exceeded(database, admin["id"]):
            return jsonify(error="Límite de solicitudes de pago alcanzado. Inténtalo más tarde."), 429
        database.execute("BEGIN IMMEDIATE")
        order = database.execute(
            """SELECT id, total, payment_status, payment_provider, payment_reference,
                      provider_payment_id,
                      refunded_amount, disputed FROM orders WHERE id = ?""",
            (order_id,),
        ).fetchone()
        if (
            not order
            or order["payment_status"] not in {"approved", "partially_refunded"}
            or order["payment_provider"] != "wompi"
        ):
            database.rollback()
            return jsonify(error="Solo se pueden reembolsar pedidos Wompi pagados."), 409
        if order["disputed"]:
            database.rollback()
            return jsonify(error="No reembolses un pedido con una disputa abierta."), 409
        remaining = order["total"] - order["refunded_amount"]
        if not 1 <= amount <= remaining:
            database.rollback()
            return jsonify(error=f"El reembolso debe estar entre 1 y {remaining} COP."), 400
        blocking_refund = database.execute(
            """SELECT id FROM payment_refunds WHERE order_id = ?
               AND status IN ('pending', 'unknown') LIMIT 1""",
            (order_id,),
        ).fetchone()
        if blocking_refund:
            database.rollback()
            return jsonify(
                error="Este pedido ya tiene un reembolso pendiente o incierto. Concilia ese movimiento antes de solicitar otro."
            ), 409
        if not order["payment_reference"] or not order["provider_payment_id"]:
            database.rollback()
            return jsonify(error="Falta la referencia de la transacción original de Wompi."), 409
        processing_payout = database.execute(
            """SELECT id FROM payment_splits WHERE order_id = ?
               AND transfer_status IN ('processing', 'submitted', 'unknown') LIMIT 1""",
            (order_id,),
        ).fetchone()
        if processing_payout:
            database.rollback()
            return jsonify(error="Hay una liquidación en curso o incierta. Concíliala antes de reembolsar."), 409
        cursor = database.execute(
            """INSERT INTO payment_refunds
               (order_id, amount, status, reason, created_by)
               VALUES (?, ?, 'pending', ?, ?)""",
            (order_id, amount, reason, admin["id"]),
        )
        refund_id = cursor.lastrowid
        payment_audit(
            database, "refund_requested", order_id=order_id, actor_user_id=admin["id"],
            amount=amount, details={"refund_id": refund_id, "reason": reason},
        )
        database.commit()

    try:
        result = wompi.request_refund(
            transaction_id=order["provider_payment_id"],
            amount_in_cents=amount * 100,
            original_amount_in_cents=order["total"] * 100,
            idempotency_key=f"XRTE-REF-{order_id}-{refund_id}",
            reference=order["payment_reference"],
        )
    except WompiPaymentOutcomeUnknown as error:
        with closing(connect_db()) as database:
            database.execute(
                "UPDATE payment_refunds SET status = 'unknown' WHERE id = ?",
                (refund_id,),
            )
            payment_audit(
                database, "refund_outcome_unknown", order_id=order_id,
                actor_user_id=admin["id"], amount=amount,
                details={"refund_id": refund_id, "error": str(error)[:200]},
            )
            database.commit()
        notify_payment_admin(
            "Reembolso Wompi requiere conciliación",
            f"El reembolso del pedido #{order_id} tiene resultado incierto. Verifica Wompi antes de iniciar otro.",
        )
        return jsonify(error=str(error), refund_id=refund_id, status="unknown"), 202
    except WompiPaymentProviderError as error:
        with closing(connect_db()) as database:
            database.execute(
                "UPDATE payment_refunds SET status = 'failed' WHERE id = ?",
                (refund_id,),
            )
            payment_audit(
                database, "refund_failed", order_id=order_id,
                actor_user_id=admin["id"], amount=amount,
                details={"refund_id": refund_id, "error": str(error)[:200]},
            )
            database.commit()
        return jsonify(error=str(error), refund_id=refund_id, status="failed"), 502

    with closing(connect_db()) as database:
        provider_refund_id = str(result.get("refund_id") or result.get("id") or "")
        if not provider_refund_id:
            database.execute(
                "UPDATE payment_refunds SET status = 'unknown' WHERE id = ?",
                (refund_id,),
            )
            database.commit()
            return jsonify(error="Wompi no devolvió el identificador del reembolso.", refund_id=refund_id, status="unknown"), 202
        database.execute(
            """UPDATE payment_refunds SET provider_refund_id = ?, status = 'pending'
               WHERE id = ?""",
            (provider_refund_id, refund_id),
        )
        payment_audit(
            database, "refund_submitted_to_wompi", order_id=order_id,
            actor_user_id=admin["id"], amount=amount,
            provider_reference=provider_refund_id,
            details={"refund_id": refund_id, "reason": reason},
        )
        database.commit()
    return jsonify(
        ok=True,
        refund_id=refund_id,
        provider_refund_id=provider_refund_id,
        status="pending",
        refunded_amount=amount,
        message="El reembolso se completará únicamente cuando Wompi lo confirme mediante su webhook firmado.",
    ), 202


@app.get("/imagenes/<path:filename>")
def images(filename):
    with closing(connect_db()) as database:
        image = database.execute(
            "SELECT image_data, content_type FROM product_images WHERE image_path = ?",
            (f"imagenes/{filename}",),
        ).fetchone()
        if image:
            response = Response(image["image_data"], mimetype=image["content_type"])
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
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


if os.environ.get("PEMU_SKIP_DATABASE_INIT") != "1" and (
    os.environ.get("VERCEL") != "1"
    or os.environ.get("PEMU_AUTO_INIT_DATABASE") == "1"
):
    initialize_database()


if __name__ == "__main__":
    if os.environ.get("PEMU_PAYOUT_WORKER", "0") == "1":
        app.logger.info("Starting the PayU payout reconciliation worker.")
        while True:
            try:
                processed = process_due_payouts()
                if processed:
                    app.logger.info("Payout worker processed %s payout(s).", processed)
            except PaymentServiceError as error:
                app.logger.error("Payout worker is not configured: %s", error)
            except Exception:
                app.logger.exception("Payout worker iteration failed.")
            time.sleep(60)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)