import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PEMU_DATABASE"] = str(Path(_TEST_DIRECTORY.name) / "cron-test.sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["PEMU_DATABASE_URL"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ.pop("VERCEL", None)

import app as application


class VercelCronTests(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()

    def test_payout_cron_requires_configured_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": ""}):
            response = self.client.get("/api/cron/process-payouts")

        self.assertEqual(response.status_code, 503)

    def test_payout_cron_rejects_invalid_authorization(self):
        with patch.dict(os.environ, {"CRON_SECRET": "test-secret"}):
            response = self.client.get(
                "/api/cron/process-payouts",
                headers={"Authorization": "Bearer wrong-secret"},
            )

        self.assertEqual(response.status_code, 401)

    def test_payout_cron_processes_with_valid_authorization(self):
        with (
            patch.dict(os.environ, {"CRON_SECRET": "test-secret"}),
            patch("app.process_due_payouts", return_value=3) as process_payouts,
        ):
            response = self.client.get(
                "/api/cron/process-payouts",
                headers={"Authorization": "Bearer test-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "processed": 3})
        process_payouts.assert_called_once_with()

    def test_mercadolibre_cron_requires_configured_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": ""}):
            response = self.client.get("/api/cron/mercadolibre-inventory")

        self.assertEqual(response.status_code, 503)

    def test_mercadolibre_cron_rejects_invalid_authorization(self):
        with patch.dict(os.environ, {"CRON_SECRET": "test-secret"}):
            response = self.client.get(
                "/api/cron/mercadolibre-inventory",
                headers={"Authorization": "Bearer wrong-secret"},
            )

        self.assertEqual(response.status_code, 401)

    def test_mercadolibre_cron_runs_a_bounded_batch(self):
        batch_result = {
            "products_seen": 2,
            "products_updated": 2,
            "results": [],
            "errors": [],
        }
        with (
            patch.dict(os.environ, {"CRON_SECRET": "test-secret"}),
            patch(
                "app._run_mercadolibre_inventory_batch",
                return_value=batch_result,
            ) as synchronize,
        ):
            response = self.client.get(
                "/api/cron/mercadolibre-inventory?limit=2",
                headers={"Authorization": "Bearer test-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, **batch_result})
        synchronize.assert_called_once_with(2)

    def test_mercadolibre_cron_rejects_oversized_batches(self):
        with (
            patch.dict(os.environ, {"CRON_SECRET": "test-secret"}),
            patch("app._run_mercadolibre_inventory_batch") as synchronize,
        ):
            response = self.client.get(
                "/api/cron/mercadolibre-inventory?limit=6",
                headers={"Authorization": "Bearer test-secret"},
            )

        self.assertEqual(response.status_code, 400)
        synchronize.assert_not_called()

    def test_vercel_schedules_bounded_daily_mercadolibre_sync(self):
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "vercel.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn(
            {
                "path": "/api/cron/mercadolibre-inventory?limit=5",
                "schedule": "0 9 * * *",
            },
            config["crons"],
        )


if __name__ == "__main__":
    unittest.main()
