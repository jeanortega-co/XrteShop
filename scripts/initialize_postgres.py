import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

if not (
    os.environ.get("DATABASE_URL")
    or os.environ.get("PEMU_DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
):
    raise SystemExit(
        "Configura DATABASE_URL en el entorno o en .env; no pases secretos como argumentos."
    )

os.environ["PEMU_SKIP_DATABASE_INIT"] = "1"
os.environ["PEMU_HTTPS"] = "0"

try:
    import app as application

    application.initialize_database()
except (OSError, RuntimeError, psycopg.Error) as error:
    print(f"No se pudo inicializar PostgreSQL: {error}", file=sys.stderr)
    raise SystemExit(1)

print("Esquema de XrteShop inicializado en PostgreSQL.")
