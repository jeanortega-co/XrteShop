import getpass
import os
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import admin_security
import app as application


def main(reset_password=False):
    email = application.ADMIN_EMAIL
    if not email or not application.re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuntimeError("Define ADMIN_EMAIL en el entorno antes de crear la cuenta.")
    if not (os.environ.get("SECRET_KEY") or os.environ.get("PEMU_SECRET_KEY")):
        raise RuntimeError("Define SECRET_KEY (o PEMU_SECRET_KEY) antes de crear la cuenta.")
    if not os.environ.get("PEMU_JWT_SECRET"):
        raise RuntimeError("Define PEMU_JWT_SECRET antes de crear la cuenta.")
    application.initialize_database()

    password_prompt = (
        "Nueva contraseña del administrador: "
        if reset_password
        else "Contraseña inicial del administrador: "
    )
    password = getpass.getpass(password_prompt)
    confirmation = getpass.getpass("Confirma la contraseña: ")
    error = admin_security.validate_password(password, email)
    if error:
        raise ValueError(error)
    if password != confirmation:
        raise ValueError("Las contraseñas no coinciden.")

    with closing(application.connect_db()) as database:
        if reset_password:
            admins = database.execute(
                "SELECT id, email FROM users WHERE role = 'admin'"
            ).fetchall()
            if len(admins) > 1:
                raise RuntimeError(
                    "No hay un único administrador que coincida con ADMIN_EMAIL; "
                    "no se modificó ninguna cuenta."
                )
            admin = admins[0] if admins else None
            promote_customer = False
            if admin:
                if application.normalize_email(admin["email"]) != email:
                    raise RuntimeError(
                        "El administrador existente no coincide con ADMIN_EMAIL; "
                        "no se modificó ninguna cuenta."
                    )
            else:
                matching_users = database.execute(
                    """SELECT id, role, account_status FROM users
                       WHERE lower(email) = ?""",
                    (email,),
                ).fetchall()
                if (
                    len(matching_users) != 1
                    or matching_users[0]["role"] != "cliente"
                    or matching_users[0]["account_status"] != "active"
                ):
                    raise RuntimeError(
                        "No hay un administrador ni una cuenta cliente activa que coincida "
                        "con ADMIN_EMAIL; no se modificó ninguna cuenta."
                    )
                admin = matching_users[0]
                promote_customer = True
            now = application.auth_now().isoformat(timespec="seconds")
            database.execute(
                """UPDATE users
                   SET password_hash = ?, role = 'admin', email_verified = 1,
                       password_reset_code = NULL, password_reset_expires_at = NULL,
                       admin_totp_secret = NULL,
                       admin_totp_enabled = 0, admin_totp_pending_secret = NULL,
                       admin_recovery_hashes = '[]'
                   WHERE id = ?""",
                (admin_security.hash_password(password), admin["id"]),
            )
            database.execute(
                """UPDATE auth_sessions SET revoked_at = ?
                   WHERE user_id = ? AND revoked_at IS NULL""",
                (now, admin["id"]),
            )
            database.commit()
            if promote_customer:
                print(
                    "\nCuenta de cliente convertida en administrador; contraseña establecida "
                    "y sesiones activas cerradas."
                )
            else:
                print("\nContraseña restablecida; las sesiones activas se cerraron.")
            return

        existing_admin = database.execute(
            "SELECT id FROM users WHERE role = 'admin' OR lower(email) = ?",
            (email,),
        ).fetchone()
        if existing_admin:
            raise RuntimeError(
                "Ya existe una cuenta con ese correo o un administrador; no se modificó ninguna cuenta."
            )
        database.execute(
            """INSERT INTO users (name, email, password_hash, email_verified, role)
               VALUES (?, ?, ?, 1, 'admin')""",
            (
                "Administrador XrteShop",
                email,
                admin_security.hash_password(password),
            ),
        )
        database.commit()

    print("\nCuenta de administrador creada.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crear o restablecer la cuenta administradora.")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Restablece al administrador configurado o convierte su cuenta cliente activa.",
    )
    arguments = parser.parse_args()
    try:
        main(reset_password=arguments.reset_password)
    except (RuntimeError, ValueError) as error:
        action = "No se restableció la contraseña" if arguments.reset_password else "No se creó la cuenta"
        print(f"{action}: {error}", file=sys.stderr)
        sys.exit(1)
