import pytest


PIXEL_ID = "969832678881906"
PIXEL_SCRIPT = "https://connect.facebook.net/en_US/fbevents.js"
PIXEL_FALLBACK = f"https://www.facebook.com/tr?id={PIXEL_ID}&amp;ev=PageView&amp;noscript=1"


@pytest.mark.parametrize("path", ["/", "/about", "/terms", "/privacy", "/refund-policy", "/contact"])
def test_meta_pixel_page_view_is_on_each_public_page(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert PIXEL_SCRIPT in response.text
    assert f"fbq('init', '{PIXEL_ID}');" in response.text
    assert "fbq('track', 'PageView');" in response.text
    assert PIXEL_FALLBACK in response.text


def test_meta_pixel_is_not_on_admin_dashboard(client):
    response = client.get("/admin")

    assert response.status_code == 200
    assert PIXEL_SCRIPT not in response.text
