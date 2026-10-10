def test_manifest_is_available_at_the_root(client):
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["id"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/?source=pwa"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}


def test_home_page_includes_mobile_pwa_install_ui(client):
    response = client.get("/")

    assert response.status_code == 200
    assert 'rel="manifest" href="/manifest.webmanifest"' in response.text
    assert 'id="addToHomeScreen"' in response.text
    assert 'id="addToHomeScreenHelp"' in response.text
