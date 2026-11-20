"""Back up, preview, and explicitly confirm cleanup of non-entrepreneur products."""

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app, initialize_database
from catalog_cleanup_service import (
    CONFIRMATION,
    apply_catalog_cleanup,
    backup_before_cleanup,
    catalog_cleanup_preview,
    connect_readonly,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply cleanup after backup and confirmation.")
    parser.add_argument("--confirm", default="", help=f"Must equal: {CONFIRMATION}")
    parser.add_argument("--expected-ids", default="", help="Comma-separated IDs reviewed in the dry-run.")
    arguments = parser.parse_args()

    initialize_database()
    backup_path, backup_manifest = backup_before_cleanup(
        app.config["DATABASE"],
        ROOT,
        ROOT / "static" / "images",
    )
    with closing(connect_readonly(app.config["DATABASE"])) as database:
        preview = catalog_cleanup_preview(database)
    report = {
        "mode": "apply" if arguments.apply else "dry-run",
        "backup_directory": str(backup_path.relative_to(ROOT)),
        "backup_manifest": backup_manifest,
        **preview,
    }

    if not arguments.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if arguments.confirm != CONFIRMATION:
        print("No se aplicaron cambios: falta la confirmación explícita.", file=sys.stderr)
        return 2
    expected_ids = [item for item in arguments.expected_ids.split(",") if item]
    actual_ids = [item["id"] for item in preview["targets"]]
    if expected_ids != actual_ids:
        print(
            "No se aplicaron cambios: --expected-ids no coincide con la simulación actual.",
            file=sys.stderr,
        )
        return 2

    result = apply_catalog_cleanup(app.config["DATABASE"], expected_ids)
    report.update(result)
    report["mode"] = "applied"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
