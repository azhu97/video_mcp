import shutil
import subprocess
from pathlib import Path

import pytest

from clipper import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def fixtures_built() -> Path:
    if not (FIXTURES / "events.mp4").exists() or not (FIXTURES / "vfr.mp4").exists():
        subprocess.run([str(Path(__file__).parent / "make_fixtures.sh")], check=True)
    return FIXTURES


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    """Each test gets its own cache and a media dir as the only allowed dir."""
    media = tmp_path_factory.mktemp("media")
    cache = tmp_path_factory.mktemp("cache")
    monkeypatch.setenv("ALLOWED_DIRS", str(media))
    monkeypatch.setenv("CLIPPER_CACHE", str(cache))
    monkeypatch.setenv("CLIPPER_CONFIG", str(cache / "none.toml"))
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    config.get_settings.cache_clear()
    yield media
    config.get_settings.cache_clear()


@pytest.fixture
def media(isolated_settings) -> Path:
    return isolated_settings


@pytest.fixture
def copy_fixture(media):
    def _copy(name: str) -> Path:
        dst = media / name
        shutil.copy(FIXTURES / name, dst)
        return dst

    return _copy
