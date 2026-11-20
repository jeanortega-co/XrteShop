import tomllib
import tempfile
import unittest
from pathlib import Path

from scripts.build_vercel import build_static_assets

ROOT = Path(__file__).resolve().parents[1]


class VercelBuildTests(unittest.TestCase):
    def test_pyproject_dependencies_match_requirements(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertCountEqual(project["project"]["dependencies"], requirements)

    def test_build_copies_static_assets_and_product_images_to_public_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            build_static_assets(output)

            self.assertTrue((output / "static" / "css" / "styles.css").is_file())
            self.assertTrue((output / "static" / "js" / "script.js").is_file())
            self.assertTrue((output / "imagenes" / "xrteshop-logo.png").is_file())


if __name__ == "__main__":
    unittest.main()
