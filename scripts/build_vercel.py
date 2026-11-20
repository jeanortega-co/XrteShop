import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_static_assets(output_directory):
    static_directory = ROOT / "static"
    output_directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        static_directory,
        output_directory / "static",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        static_directory / "images",
        output_directory / "imagenes",
        dirs_exist_ok=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Prepara los archivos estáticos públicos para Vercel."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "public",
        help="Directorio de salida (por defecto: public).",
    )
    args = parser.parse_args()
    build_static_assets(args.output.resolve())


if __name__ == "__main__":
    main()
