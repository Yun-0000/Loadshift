from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
REPO_ROOT = SRC_DIR.parent
ASSET_ROOT = PACKAGE_DIR / "assets" if (PACKAGE_DIR / "assets").is_dir() else REPO_ROOT
FIXTURES_DIR = ASSET_ROOT / "fixtures"
OPTIMIZER_DIR = PACKAGE_DIR / "optimizer"
WEB_DIR = ASSET_ROOT / "web"
