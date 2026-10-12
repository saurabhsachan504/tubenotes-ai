import json
from pathlib import Path


RELEASE_DIR = Path(__file__).resolve().parents[1] / "extension-client" / "store-release"


def test_store_release_opens_google_welcome_tab_only_on_first_install():
    source = (RELEASE_DIR / "background.js").read_text(encoding="utf-8")

    assert 'details.reason === "install"' in source
    assert 'account.html?welcome=1' in source
    assert "TrialGuard.isSignedIn()" in source


def test_store_release_keeps_google_identity_permission_and_new_version():
    manifest = json.loads((RELEASE_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "2.3.19"
    assert "identity" in manifest["permissions"]


def test_welcome_page_explains_the_one_time_google_setup():
    source = (RELEASE_DIR / "account.js").read_text(encoding="utf-8")

    assert 'get("welcome") === "1"' in source
    assert "Continue with Google once" in source
