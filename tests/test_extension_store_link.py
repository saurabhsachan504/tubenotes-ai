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
