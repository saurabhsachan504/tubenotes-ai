from app.config import settings
from app.services import pricing


def test_city_is_taken_only_from_trusted_cloudflare_headers(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_CLOUDFLARE_COUNTRY_HEADER", True)
    assert pricing.city_for_headers({"cf-ipcity": "New%20Delhi"}) == "New Delhi"

    monkeypatch.setattr(settings, "TRUST_CLOUDFLARE_COUNTRY_HEADER", False)
    assert pricing.city_for_headers({"cf-ipcity": "New%20Delhi"}) is None


def test_city_header_is_normalized_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_CLOUDFLARE_COUNTRY_HEADER", True)
    assert pricing.city_for_headers({"cf-ipcity": "  Los%20%20Angeles  "}) == "Los Angeles"
    assert pricing.city_for_headers({"cf-ipcity": "City\x00Name"}) is None
    assert pricing.city_for_headers({"cf-ipcity": ""}) is None
