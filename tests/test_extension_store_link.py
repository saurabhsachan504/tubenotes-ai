from app.config import settings


def test_meta_exposes_only_an_approved_chrome_web_store_link(client, monkeypatch):
    monkeypatch.setattr(
        settings,
        "CHROME_WEB_STORE_URL",
        "https://chromewebstore.google.com/detail/tubenotes/example-extension-id",
    )
    assert client.get(f"{settings.API_PREFIX}/meta").json()["chrome_web_store_url"].startswith(
        "https://chromewebstore.google.com/"
    )

    monkeypatch.setattr(settings, "CHROME_WEB_STORE_URL", "https://example.test/not-a-store")
    assert client.get(f"{settings.API_PREFIX}/meta").json()["chrome_web_store_url"] == ""


def test_extension_button_is_ready_for_the_live_store_listing(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Coming Soon" not in response.text
    assert 'title="Install TubeNotes from the Chrome Web Store"' in response.text
    assert ".nav-in{--extension-button-space:300px" in response.text
    assert ".extension-btn{position:absolute;top:10px;right:18px" in response.text
