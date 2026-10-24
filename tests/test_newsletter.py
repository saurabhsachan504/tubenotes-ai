from datetime import datetime

from app.models import NewsletterRun, User
from app.services import newsletter
from app.services.trending import TrendingVideo


def test_newsletter_schedule_is_wednesday_and_sunday_at_9am_ist():
    assert newsletter.is_due(datetime(2026, 10, 25, 9, 0, tzinfo=newsletter.IST))  # Sunday
    assert newsletter.is_due(datetime(2026, 10, 28, 9, 0, tzinfo=newsletter.IST))  # Wednesday
    assert not newsletter.is_due(datetime(2026, 10, 28, 9, 1, tzinfo=newsletter.IST))
    assert not newsletter.is_due(datetime(2026, 10, 26, 9, 0, tzinfo=newsletter.IST))


def test_unsubscribe_link_turns_off_marketing_preference(client, db, device):
    result = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "updates@example.com",
            "password": "Str0ngPass1",
            "device": device,
            "newsletter_opt_in": True,
        },
    )
    assert result.status_code == 201, result.text
    user = db.query(User).filter_by(email="updates@example.com").one()
    assert user.newsletter_opt_in is True
    response = client.get(
        "/api/v1/newsletter/unsubscribe",
        params={"token": newsletter.unsubscribe_token(user.id)},
    )
    assert response.status_code == 200
    db.expire_all()
    user = db.query(User).filter_by(email="updates@example.com").one()
    assert user.newsletter_opt_in is False
    assert user.newsletter_unsubscribed_at is not None


def test_newsletter_preference_can_be_changed_after_signup(client, db, device):
    result = client.post(
        "/api/v1/auth/signup",
        json={"email": "preference@example.com", "password": "Str0ngPass1", "device": device},
    )
    token = result.json()["tokens"]["access_token"]
    response = client.put(
        "/api/v1/auth/newsletter-preference",
        headers={"Authorization": f"Bearer {token}"},
        json={"newsletter_opt_in": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["newsletter_opt_in"] is True
    db.expire_all()
    assert db.query(User).filter_by(email="preference@example.com").one().newsletter_opted_in_at is not None


def test_due_run_is_idempotent_and_targets_only_opted_in_users(db, monkeypatch):
    opted_in = User(email="yes@example.com", password_hash="x", newsletter_opt_in=True)
    opted_out = User(email="no@example.com", password_hash="x", newsletter_opt_in=False)
    db.add_all([opted_in, opted_out])
    db.commit()
    monkeypatch.setattr(newsletter.settings, "NEWSLETTER_ENABLED", True)
    monkeypatch.setattr(newsletter.settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(newsletter.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(newsletter.settings, "MARKETING_ADDRESS", "TubeNotes, India")
    monkeypatch.setattr(newsletter, "_latest_videos", lambda: ())
    sent = []
    monkeypatch.setattr(
        newsletter.email,
        "send_email",
        lambda to, *args, **kwargs: sent.append(to) or True,
    )
    at = datetime(2026, 10, 25, 9, 0, tzinfo=newsletter.IST)
    first = newsletter.run_due(db, at=at)
    second = newsletter.run_due(db, at=at)
    assert first == {"status": "completed", "recipients": 1, "delivered": 1, "failed": 0}
    assert second["status"] == "already_sent"
    assert sent == ["yes@example.com"]
    assert db.query(NewsletterRun).count() == 1


def test_newsletter_card_uses_full_width_image_and_decodes_youtube_title():
    user = User(id="a3bf0e0d-fc74-4971-b8aa-c674c0a9efbf", email="reader@example.com", password_hash="x")
    video = TrendingVideo(
        video_id="video-id",
        title="Daniel &quot;Podcast&quot;",
        channel="Tube &amp; Co",
        thumbnail="https://example.com/thumb.jpg",
        views=1300,
        published_at="2026-10-24T13:30:00Z",
        duration="PT48M21S",
        url="https://www.youtube.com/watch?v=video-id",
    )
    _, plain, rendered = newsletter.newsletter_content(user, (video,))
    assert 'width="100%"' in rendered
    assert 'Daniel "Podcast"' in plain
    assert "Daniel &amp;quot;" not in rendered
    assert "1.3K views" in rendered
    assert "Duration: 48m 21s" in rendered


def test_newsletter_groups_six_cards_into_two_three_column_rows():
    user = User(id="f8b94a75-6dac-4e55-b506-790165241a36", email="reader@example.com", password_hash="x")
    videos = tuple(
        TrendingVideo(
            video_id=f"video-{number}", title=f"Podcast {number}", channel="TubeNotes",
            thumbnail="https://example.com/thumb.jpg", views=100, published_at=None,
            duration="PT30M", url=f"https://www.youtube.com/watch?v=video-{number}",
        )
        for number in range(1, 7)
    )
    _, _, rendered = newsletter.newsletter_content(user, videos)
    assert rendered.count('class="podcast-column"') == 6
    assert rendered.count('width="33.33%"') == 6


def test_newsletter_preview_sends_only_requested_address(monkeypatch):
    monkeypatch.setattr(newsletter.settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(newsletter.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(newsletter.settings, "MARKETING_ADDRESS", "TubeNotes, India")
    monkeypatch.setattr(newsletter, "_latest_videos", lambda: ())
    sent = []
    monkeypatch.setattr(
        newsletter.email,
        "send_email",
        lambda to, subject, body, **kwargs: sent.append((to, subject, body, kwargs)) or True,
    )
    result = newsletter.send_test_email(" test@example.com ")
    assert result == {"status": "test_sent", "recipients": 1, "delivered": 1, "failed": 0}
    assert sent[0][0] == "test@example.com"
    assert sent[0][1].startswith("[Preview]")
    assert "no subscription preference was changed" in sent[0][2]
