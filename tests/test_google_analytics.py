import pytest


MEASUREMENT_ID = "G-QYGNEG3KYG"
TAG_SRC = f"https://www.googletagmanager.com/gtag/js?id={MEASUREMENT_ID}"


@pytest.mark.parametrize("path", ["/", "/about", "/terms", "/privacy", "/refund-policy", "/contact"])
def test_google_analytics_tag_is_on_each_public_page(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert TAG_SRC in response.text
    assert f"gtag('config', '{MEASUREMENT_ID}');" in response.text


def test_google_analytics_tag_is_not_on_admin_dashboard(client):
    response = client.get("/admin")

    assert response.status_code == 200
    assert TAG_SRC not in response.text
