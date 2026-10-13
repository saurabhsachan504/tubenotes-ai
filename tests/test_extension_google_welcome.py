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

    assert manifest["version"] == "2.3.23"
    assert "identity" in manifest["permissions"]


def test_store_release_uses_only_tubenotes_and_youtube_hosts():
    manifest = json.loads((RELEASE_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["host_permissions"] == [
        "https://tubenotes.ai/*",
        "*://*.youtube.com/*",
    ]


def test_translation_uses_the_authenticated_tubenotes_api():
    panel = (RELEASE_DIR / "sidepanel.js").read_text(encoding="utf-8")
    guard = (RELEASE_DIR / "trialguard.js").read_text(encoding="utf-8")

    assert "TrialGuard.translateText" in panel
    assert 'request("/translate"' in guard
    assert "translate.googleapis.com" not in panel


def test_injected_summarize_button_uses_tubenotes_branding():
    content = (RELEASE_DIR / "content.js").read_text(encoding="utf-8")
    styles = (RELEASE_DIR / "content.css").read_text(encoding="utf-8")

    assert "ons-brand-icon" in content
    assert "Summarize with TubeNotes" in content
    assert "#7c3aed" in styles
    assert "#ec4899" in styles


def test_welcome_page_explains_the_one_time_google_setup():
    source = (RELEASE_DIR / "account.js").read_text(encoding="utf-8")
    markup = (RELEASE_DIR / "account.html").read_text(encoding="utf-8")

    assert 'get("welcome") === "1"' in source
    assert 'classList.add("welcome-install")' in source
    assert "Maybe later" in source
    assert "welcome-benefits" in markup
    assert "Works with 40+ languages" in markup
    assert "place-items:center" in markup
    assert "google-signin-mark" in markup
    assert "#4285f4" not in markup
    assert "max-height:760px" in markup
