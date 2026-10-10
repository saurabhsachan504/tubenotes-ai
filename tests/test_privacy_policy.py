def test_privacy_policy_discloses_chrome_extension_data_use(client):
    response = client.get("/privacy")

    assert response.status_code == 200
    assert "Chrome Extension Privacy" in response.text
    assert "current video URL, title and available transcript" in response.text
    assert "Ask a question" in response.text
    assert "Chrome Web Store User Data Policy" in response.text
