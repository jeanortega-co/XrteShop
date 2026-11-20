import os
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PEMU_DATABASE"] = str(Path(_TEST_DIRECTORY.name) / "admin-security.sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["PEMU_DATABASE_URL"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ.pop("VERCEL", None)

import app as application


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.database_path = Path(_TEST_DIRECTORY.name) / f"{self._testMethodName}.sqlite3"
        application.app.config["DATABASE"] = str(self.database_path)
        application.app.config["TESTING"] = True
        application.DATABASE_URL = ""
        application.ADMIN_EMAIL = "owner@example.test"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "PEMU_DATABASE_URL": "",
                "POSTGRES_URL": "",
                "ADMIN_EMAIL": "owner@example.test",
                "ACCOUNTS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.mail_patch = patch.object(application, "send_email_message", return_value=True)
        self.mail_patch.start()
        self.addCleanup(self.mail_patch.stop)
        application.initialize_database()

        self.password = "OwnerPassphrase-Long-2026"
        with closing(application.connect_db()) as database:
            owner = database.execute(
                """INSERT INTO users
                   (name, email, password_hash, role)
                   VALUES ('Owner', ?, ?, 'admin')""",
                (
                    application.ADMIN_EMAIL,
                    application.hash_password(self.password),
                ),
            )
            self.owner_id = owner.lastrowid
            customer = database.execute(
                """INSERT INTO users (name, email, password_hash, email_verified)
                   VALUES ('Customer', 'customer@example.test', ?, 1)""",
                (application.hash_password("CustomerPass123"),),
            )
            self.customer_id = customer.lastrowid
            database.commit()
        self.client = application.app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def csrf_headers(self):
        csrf = self.client.get("/api/session").get_json()["csrf_token"]
        return {**self.headers, "X-CSRF-Token": csrf}

    def login_admin(self):
        headers = self.csrf_headers()
        response = self.client.post(
            "/api/auth/login",
            json={
                "email": application.ADMIN_EMAIL,
                "password": self.password,
            },
            headers=headers,
        )
        if response.status_code == 200:
            self.headers = self.csrf_headers()
        return response

    def login_customer(self):
        response = self.client.post(
            "/api/auth/login",
            json={
                "email": "customer@example.test",
                "password": "CustomerPass123",
            },
            headers=self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.headers = self.csrf_headers()
        return response

    def test_owner_login_requires_only_password_and_redirects_to_admin(self):
        response = self.login_admin()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["redirect_to"], "/admin")
        self.assertNotIn("is_admin", response.get_json()["user"])
        self.assertEqual(self.client.get("/admin").status_code, 200)
        panel_script = self.client.get("/static/js/admin.js")
        self.assertEqual(panel_script.status_code, 200)
        self.assertIn("no-store", panel_script.headers["Cache-Control"])
        self.assertIn("SameSite=Strict", response.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))
        panel_script.close()

    def test_customer_cannot_open_admin_html_api_or_panel_javascript(self):
        response = self.login_customer()
        self.assertNotIn("is_admin", response.get_json()["user"])
        self.assertEqual(self.client.get("/admin").status_code, 404)
        for path in (
            "/api/admin/session",
            "/api/admin/finance",
            "/api/admin/accounts",
            "/api/admin/dashboard",
            "/api/admin/audit",
            "/static/js/admin.js",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_registration_profile_and_recovery_cannot_escalate_or_reset_admin(self):
        owner_register = self.client.post(
            "/api/auth/register",
            json={
                "name": "Owner",
                "email": application.ADMIN_EMAIL,
                "password": "OwnerPassphrase-Long-2026",
                "confirm_password": "OwnerPassphrase-Long-2026",
                "role": "admin",
            },
            headers=self.csrf_headers(),
        )
        self.assertEqual(owner_register.status_code, 409)

        customer_register = self.client.post(
            "/api/auth/register",
            json={
                "name": "New Customer",
                "email": "new@example.test",
                "password": "ValidPass123",
                "confirm_password": "ValidPass123",
                "role": "admin",
            },
            headers=self.csrf_headers(),
        )
        self.assertEqual(customer_register.status_code, 201, customer_register.get_json())
        profile = self.client.put(
            "/api/account/profile",
            json={"name": "New Customer", "role": "admin", "email": application.ADMIN_EMAIL},
            headers=self.csrf_headers(),
        )
        self.assertEqual(profile.status_code, 200, profile.get_json())
        with closing(application.connect_db()) as database:
            new_user = database.execute(
                "SELECT role, email FROM users WHERE email = 'new@example.test'"
            ).fetchone()
            admin = database.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (self.owner_id,),
            ).fetchone()
        self.assertEqual(new_user["role"], "cliente")
        self.assertEqual(new_user["email"], "new@example.test")
        self.assertTrue(application.verify_password(admin["password_hash"], self.password))

        forgot = self.client.post(
            "/api/forgot-password",
            json={"email": application.ADMIN_EMAIL},
            headers=self.csrf_headers(),
        )
        self.assertEqual(forgot.status_code, 200)
        with closing(application.connect_db()) as database:
            admin = database.execute(
                "SELECT password_reset_code FROM users WHERE id = ?",
                (self.owner_id,),
            ).fetchone()
        self.assertIsNone(admin["password_reset_code"])

    def test_failed_attempts_temporarily_block_account(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"email": application.ADMIN_EMAIL, "password": "incorrect"},
                headers=self.csrf_headers(),
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["error"], "Correo o contraseña incorrectos.")
        blocked = self.client.post(
            "/api/auth/login",
            json={"email": application.ADMIN_EMAIL, "password": self.password},
            headers=self.csrf_headers(),
        )
        self.assertEqual(blocked.status_code, 429)
        with closing(application.connect_db()) as database:
            events = database.execute(
                "SELECT COUNT(*) FROM admin_security_events WHERE event = 'login_failed_credentials'"
            ).fetchone()[0]
        self.assertEqual(events, 5)

    def test_admin_login_failures_log_safe_diagnostic_reasons(self):
        headers = self.csrf_headers()
        with self.assertLogs(application.app.logger.name, level="WARNING") as captured:
            response = self.client.post(
                "/api/auth/login",
                json={
                    "email": application.ADMIN_EMAIL,
                    "password": "not-the-password",
                },
                headers=headers,
            )
        self.assertEqual(response.status_code, 401)
        self.assertIn("password_mismatch", "\n".join(captured.output))
        self.assertNotIn("not-the-password", "\n".join(captured.output))

        with closing(application.connect_db()) as database:
            database.execute(
                "UPDATE users SET role = 'cliente' WHERE id = ?",
                (self.owner_id,),
            )
            database.commit()
        with self.assertLogs(application.app.logger.name, level="WARNING") as captured:
            response = self.client.post(
                "/api/auth/login",
                json={
                    "email": application.ADMIN_EMAIL,
                    "password": self.password,
                },
                headers=self.csrf_headers(),
            )
        self.assertEqual(response.status_code, 401)
        self.assertIn("admin_role_mismatch", "\n".join(captured.output))

    def test_admin_password_change_requires_only_the_current_password(self):
        self.login_admin()
        new_password = "NewOwnerPassphrase-2026"
        changed = self.client.post(
            "/api/admin/security/change-password",
            json={
                "current_password": self.password,
                "new_password": new_password,
            },
            headers=self.headers,
        )
        self.assertEqual(changed.status_code, 200, changed.get_json())
        with closing(application.connect_db()) as database:
            row = database.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (self.owner_id,),
            ).fetchone()
        self.assertTrue(application.verify_password(row["password_hash"], new_password))
        self.assertEqual(self.client.get("/api/admin/session").status_code, 404)

    def test_password_policy_accepts_eight_characters_and_rejects_short_or_common(self):
        self.assertIsNotNone(application.password_error("Pass123", application.ADMIN_EMAIL))
        self.assertIsNone(application.password_error("Abcdefg8", application.ADMIN_EMAIL))
        self.assertIsNotNone(application.password_error("password123", application.ADMIN_EMAIL))
        self.assertIsNotNone(
            application.password_error(application.ADMIN_EMAIL, application.ADMIN_EMAIL)
        )
        long_passphrase = "A very long passphrase that goes beyond bcrypt's former byte limit 2026"
        self.assertIsNone(application.password_error(long_passphrase, application.ADMIN_EMAIL))
        self.assertTrue(application.verify_password(application.hash_password(long_passphrase), long_passphrase))

    def test_bank_account_is_encrypted_masked_and_revealed_after_password_reconfirmation(self):
        self.login_admin()
        account_number = "0123456789012345"
        created = self.client.post(
            "/api/admin/accounts",
            json={
                "bank_name": "Nequi",
                "account_type": "billetera",
                "account_holder": "Owner Name",
                "alias": "Cuenta principal",
                "account_number": account_number,
                "is_primary": True,
            },
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        self.assertNotIn(account_number, created.get_data(as_text=True))
        with closing(application.connect_db()) as database:
            account = database.execute(
                "SELECT encrypted_number, last_four FROM admin_bank_accounts"
            ).fetchone()
        self.assertNotIn(account_number, account["encrypted_number"])
        self.assertEqual(account["last_four"], "2345")
        self.assertEqual(
            application.admin_security.decrypt_secret(
                account["encrypted_number"],
                application.admin_security.accounts_encryption_key(),
            ),
            account_number,
        )

        listing = self.client.get("/api/admin/accounts")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(account_number, listing.get_data(as_text=True))
        account_id = listing.get_json()["accounts"][0]["id"]
        denied = self.client.post(
            f"/api/admin/accounts/{account_id}/reveal",
            json={"current_password": "wrong-password"},
            headers=self.headers,
        )
        self.assertEqual(denied.status_code, 401)
        revealed = self.client.post(
            f"/api/admin/accounts/{account_id}/reveal",
            json={"current_password": self.password},
            headers=self.headers,
        )
        self.assertEqual(revealed.status_code, 200, revealed.get_json())
        self.assertEqual(revealed.get_json()["account_number"], account_number)
        with closing(application.connect_db()) as database:
            audit = database.execute(
                """SELECT action, affected_resource FROM admin_audit_log
                   WHERE action = 'view_sensitive_account'"""
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertNotIn(account_number, audit["affected_resource"])

    def test_finance_totals_equal_confirmed_ledger_sales_less_refunds(self):
        self.login_admin()
        with closing(application.connect_db()) as database:
            order_cursor = database.execute(
                """INSERT INTO orders
                   (user_id, customer_name, address, city, total, payment_status,
                    payment_method, refunded_amount)
                   VALUES (?, 'Owner', 'Test address', 'Bogotá', 12000,
                           'partially_refunded', 'card_mastercard', 2000)""",
                (self.owner_id,),
            )
            order_id = order_cursor.lastrowid
            application.record_payment_ledger(
                database,
                entry_key=f"test-gross:{order_id}",
                order_id=order_id,
                entry_type="gross_sale",
                amount_cop=12000,
                status="payment_approved",
            )
            application.record_payment_ledger(
                database,
                entry_key=f"test-refund:{order_id}",
                order_id=order_id,
                entry_type="customer_refund",
                amount_cop=-2000,
            )
            database.commit()
        response = self.client.get("/api/admin/finance")
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["gross_collected"], 12000)
        self.assertEqual(payload["refunded"], 2000)
        self.assertEqual(payload["net_collected"], 10000)
        self.assertEqual(payload["net_collected"], 12000 - 2000)

    def test_admin_settings_persist_rates_without_enabling_production_payments(self):
        self.login_admin()
        updated = self.client.patch(
            "/api/admin/settings",
            json={"commission_rate": "0.12", "markup_rate": "0.20", "iva_rate": "0.19"},
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        settings = self.client.get("/api/admin/settings").get_json()
        self.assertEqual(settings["commission_rate"], "0.12")
        self.assertEqual(settings["markup_rate"], "0.2")
        self.assertEqual(settings["iva_rate"], "0.19")
        self.assertFalse(settings["payment_environment_editable"])
        forbidden = self.client.patch(
            "/api/admin/settings",
            json={"payment_environment": "production"},
            headers=self.headers,
        )
        self.assertEqual(forbidden.status_code, 400)
        report = self.client.get("/api/admin/finance/monthly?month=2026-03")
        self.assertEqual(report.status_code, 200, report.get_json())
        self.assertEqual(report.get_json()["report"]["iva_rate"], "0.19")

    def test_create_admin_script_prompts_without_echo_and_refuses_a_second_admin(self):
        from scripts import create_admin

        with closing(application.connect_db()) as database:
            database.execute("DELETE FROM users WHERE id = ?", (self.owner_id,))
            database.commit()
        script_environment = patch.dict(
            os.environ,
            {"SECRET_KEY": "script-secret", "PEMU_JWT_SECRET": "script-jwt-secret"},
            clear=False,
        )
        password_input = patch("getpass.getpass", side_effect=[self.password, self.password])
        output = StringIO()
        with script_environment, password_input, redirect_stdout(output):
            create_admin.main()
        self.assertNotIn(self.password, output.getvalue())
        self.assertNotIn("otpauth://", output.getvalue())
        with closing(application.connect_db()) as database:
            row = database.execute(
                "SELECT role, password_hash FROM users WHERE email = ?",
                (application.ADMIN_EMAIL,),
            ).fetchone()
            admin_count = database.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]
        self.assertEqual(row["role"], "admin")
        self.assertEqual(admin_count, 1)
        self.assertNotEqual(row["password_hash"], self.password)

        password_input = patch("getpass.getpass", side_effect=[self.password, self.password])
        with script_environment, password_input, redirect_stdout(StringIO()):
            with self.assertRaises(RuntimeError):
                create_admin.main()

    def test_reset_admin_password_updates_hash_and_revokes_sessions(self):
        from scripts import create_admin

        self.login_admin()
        new_password = "NewOwnerPassphrase-2026"
        script_environment = patch.dict(
            os.environ,
            {"SECRET_KEY": "script-secret", "PEMU_JWT_SECRET": "script-jwt-secret"},
            clear=False,
        )
        password_input = patch(
            "getpass.getpass",
            side_effect=[new_password, new_password],
        )
        output = StringIO()
        with script_environment, password_input, redirect_stdout(output):
            create_admin.main(reset_password=True)

        self.assertNotIn(new_password, output.getvalue())
        with closing(application.connect_db()) as database:
            row = database.execute(
                """SELECT password_hash, admin_totp_secret, admin_totp_enabled,
                          admin_totp_pending_secret, admin_recovery_hashes
                   FROM users WHERE id = ?""",
                (self.owner_id,),
            ).fetchone()
            active_sessions = database.execute(
                """SELECT COUNT(*) FROM auth_sessions
                   WHERE user_id = ? AND revoked_at IS NULL""",
                (self.owner_id,),
            ).fetchone()[0]
        self.assertTrue(application.verify_password(row["password_hash"], new_password))
        self.assertIsNone(row["admin_totp_secret"])
        self.assertEqual(row["admin_totp_enabled"], 0)
        self.assertIsNone(row["admin_totp_pending_secret"])
        self.assertEqual(row["admin_recovery_hashes"], "[]")
        self.assertEqual(active_sessions, 0)
        self.assertEqual(self.client.get("/api/admin/session").status_code, 404)

        login = self.client.post(
            "/api/auth/login",
            json={"email": application.ADMIN_EMAIL, "password": new_password},
            headers=self.csrf_headers(),
        )
        self.assertEqual(login.status_code, 200, login.get_json())
        self.assertEqual(login.get_json()["redirect_to"], "/admin")

    def test_reset_admin_password_promotes_existing_configured_customer(self):
        from scripts import create_admin

        with closing(application.connect_db()) as database:
            database.execute("DELETE FROM users WHERE id = ?", (self.owner_id,))
            database.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (application.ADMIN_EMAIL, self.customer_id),
            )
            database.commit()

        new_password = "NewOwnerPassphrase-2026"
        script_environment = patch.dict(
            os.environ,
            {"SECRET_KEY": "script-secret", "PEMU_JWT_SECRET": "script-jwt-secret"},
            clear=False,
        )
        password_input = patch(
            "getpass.getpass",
            side_effect=[new_password, new_password],
        )
        with script_environment, password_input, redirect_stdout(StringIO()):
            create_admin.main(reset_password=True)

        with closing(application.connect_db()) as database:
            row = database.execute(
                "SELECT role, email_verified, password_hash FROM users WHERE id = ?",
                (self.customer_id,),
            ).fetchone()
            admin_count = database.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["email_verified"], 1)
        self.assertTrue(application.verify_password(row["password_hash"], new_password))
        self.assertEqual(admin_count, 1)

        login = self.client.post(
            "/api/auth/login",
            json={"email": application.ADMIN_EMAIL, "password": new_password},
            headers=self.csrf_headers(),
        )
        self.assertEqual(login.status_code, 200, login.get_json())
        self.assertEqual(login.get_json()["redirect_to"], "/admin")


if __name__ == "__main__":
    unittest.main()
