from __future__ import annotations

from app.config import settings
from tests.conftest import make_device, register

API = settings.API_PREFIX


def test_signup_returns_tokens_and_entitlement(client, device):
    body, headers, _ = register(client, device=device)

    assert body["user"]["email"] == "user@example.com"
    assert body["user"]["trials_used"] == 0
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["entitlement"]["allowed"] is True
    assert body["entitlement"]["trials_remaining"] == 5
    assert body["device_id"]

    me = client.get(f"{API}/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


def test_signup_sends_one_personalised_welcome_email(client, device, monkeypatch):
    from app.services import email as email_service

    sent = []
    monkeypatch.setattr(
        email_service,
        "send_welcome_email",
        lambda user, trial_limit: sent.append((user.email, user.full_name, trial_limit)),
    )

    res = client.post(
        f"{API}/auth/signup",
        json={
            "email": "welcome@example.com",
            "full_name": "Asha Sharma",
            "password": "Str0ngPass1",
            "device": device,
        },
    )
    assert res.status_code == 201, res.text
    assert sent == [("welcome@example.com", "Asha Sharma", 5)]


def test_signup_records_trusted_country_for_admin_analytics(client, db, device):
    res = client.post(
        f"{API}/auth/signup",
        json={"email": "india@example.com", "password": "Str0ngPass1", "device": device},
        headers={"CF-IPCountry": "IN"},
    )
    assert res.status_code == 201, res.text

    from app.models import User

    assert db.query(User).filter_by(email="india@example.com").one().billing_country == "IN"


def test_password_is_never_returned_or_stored_in_clear(client, db, device):
    register(client, device=device)
    from app.models import User

    user = db.query(User).one()
    assert user.password_hash != "Str0ngPass1"
    assert user.password_hash.startswith("$2")


def test_duplicate_email_is_rejected(client):
    register(client, email="dup@example.com", device=make_device())
    res = client.post(
        f"{API}/auth/signup",
        json={
            "email": "dup@example.com",
            "password": "Str0ngPass1",
            "device": make_device(),
        },
    )
    assert res.status_code == 409


def test_email_is_case_insensitive(client):
    register(client, email="Case@Example.com", device=make_device())
    res = client.post(
        f"{API}/auth/login",
        json={"email": "case@EXAMPLE.com", "password": "Str0ngPass1"},
    )
    assert res.status_code == 200


def test_weak_password_rejected(client):
    res = client.post(
        f"{API}/auth/signup",
        json={"email": "weak@example.com", "password": "short", "device": make_device()},
    )
    assert res.status_code == 422


def test_login_wrong_password_gives_generic_error(client):
    register(client, email="a@example.com", device=make_device())
    res = client.post(
        f"{API}/auth/login", json={"email": "a@example.com", "password": "Wr0ngPass1"}
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Incorrect email or password."

    # Unknown address gives exactly the same message - no user enumeration.
    res2 = client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "Wr0ngPass1"}
    )
    assert res2.status_code == 401
    assert res2.json()["detail"] == res.json()["detail"]


def test_protected_route_requires_token(client):
    assert client.get(f"{API}/auth/me").status_code == 401
    assert (
        client.get(f"{API}/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code
        == 401
    )


def test_refresh_rotates_and_old_token_is_dead(client, device):
    body, _, _ = register(client, device=device)
    old_refresh = body["tokens"]["refresh_token"]

    res = client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert res.status_code == 200
    new_tokens = res.json()
    assert new_tokens["refresh_token"] != old_refresh

    # Replaying the rotated token fails and revokes the family.
    replay = client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401

    reuse_new = client.post(
        f"{API}/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert reuse_new.status_code == 401


def test_logout_revokes_refresh_token(client, device):
    body, headers, _ = register(client, device=device)
    refresh = body["tokens"]["refresh_token"]

    assert (
        client.post(
            f"{API}/auth/logout", json={"refresh_token": refresh}, headers=headers
        ).status_code
        == 200
    )
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_password_reset_flow(client, db, device):
    register(client, email="reset@example.com", device=device)

    res = client.post(f"{API}/auth/password/forgot", json={"email": "reset@example.com"})
    assert res.status_code == 200

    # Unknown addresses get the identical response.
    res2 = client.post(f"{API}/auth/password/forgot", json={"email": "ghost@example.com"})
    assert res2.json() == res.json()

    from app.models import OneTimeToken

    assert db.query(OneTimeToken).count() >= 1


def test_change_password_invalidates_sessions(client, device):
    body, headers, _ = register(client, email="chg@example.com", device=device)
    res = client.post(
        f"{API}/auth/password/change",
        json={"current_password": "Str0ngPass1", "new_password": "N3wStrongPass"},
        headers=headers,
    )
    assert res.status_code == 200
    assert (
        client.post(
            f"{API}/auth/refresh", json={"refresh_token": body["tokens"]["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{API}/auth/login",
            json={"email": "chg@example.com", "password": "N3wStrongPass"},
        ).status_code
        == 200
    )


def test_device_list_and_revoke(client, device):
    body, headers, _ = register(client, device=device)
    devices = client.get(f"{API}/auth/devices", headers=headers).json()
    assert len(devices) == 1

    res = client.delete(f"{API}/auth/devices/{devices[0]['id']}", headers=headers)
    assert res.status_code == 200
    assert client.get(f"{API}/auth/devices", headers=headers).json() == []


def test_three_active_device_limit_and_slot_reuse(client, device):
    """An account may retain three devices; removal frees a reusable slot."""
    _, headers, _ = register(client, email="devices@example.com", device=device)
    devices = [
        make_device(label=f"Device {number}") for number in range(2, 5)
    ]

    for next_device in devices[:2]:
        response = client.post(
            f"{API}/auth/login",
            json={
                "email": "devices@example.com",
                "password": "Str0ngPass1",
                "device": next_device,
            },
        )
        assert response.status_code == 200, response.text

    fourth = client.post(
        f"{API}/auth/login",
        json={
            "email": "devices@example.com",
            "password": "Str0ngPass1",
            "device": devices[2],
        },
    )
    assert fourth.status_code == 409
    assert "3 devices" in fourth.json()["detail"]

    active = client.get(f"{API}/auth/devices", headers=headers).json()
    assert len(active) == 3
    device_two = next(item for item in active if item["label"] == "Device 2")
    assert client.delete(
        f"{API}/auth/devices/{device_two['id']}", headers=headers
    ).status_code == 200

    # A newly seen device can now take the freed slot.
    assert client.post(
        f"{API}/auth/login",
        json={
            "email": "devices@example.com",
            "password": "Str0ngPass1",
            "device": devices[2],
        },
    ).status_code == 200

    # The removed browser can later be reactivated after another slot is free.
    active = client.get(f"{API}/auth/devices", headers=headers).json()
    device_four = next(item for item in active if item["label"] == "Device 4")
    assert client.delete(
        f"{API}/auth/devices/{device_four['id']}", headers=headers
    ).status_code == 200
    assert client.post(
        f"{API}/auth/login",
        json={
            "email": "devices@example.com",
            "password": "Str0ngPass1",
            "device": devices[0],
        },
    ).status_code == 200
    assert len(client.get(f"{API}/auth/devices", headers=headers).json()) == 3


def test_device_recovery_replaces_all_old_devices_sessions_and_pushes(client, db, device):
    """A verified owner can make the current browser the sole active device."""
    from app.models import Device, RefreshToken, User, WebPushSubscription

    body, _, first = register(client, email="recover@example.com", device=device)
    old_refresh = body["tokens"]["refresh_token"]
    second, third, current = [
        make_device(label=f"Device {number}") for number in range(2, 5)
    ]
    for old_device in (second, third):
        assert client.post(
            f"{API}/auth/login",
            json={"email": "recover@example.com", "password": "Str0ngPass1", "device": old_device},
        ).status_code == 200

    user = db.query(User).filter_by(email="recover@example.com").one()
    db.add(WebPushSubscription(
        user_id=user.id,
        endpoint="https://push.example.test/old-browser-endpoint",
        p256dh="x" * 24,
        auth="y" * 16,
    ))
    db.commit()

    blocked = client.post(
        f"{API}/auth/login",
        json={"email": "recover@example.com", "password": "Str0ngPass1", "device": current},
    )
    assert blocked.status_code == 409

    recovered = client.post(
        f"{API}/auth/recover-device-access",
        json={
            "method": "password",
            "email": "recover@example.com",
            "password": "Str0ngPass1",
            "device": current,
        },
    )
    assert recovered.status_code == 200, recovered.text
    recovered_body = recovered.json()
    assert recovered_body["new_account"] is False
    headers = {"Authorization": f"Bearer {recovered_body['tokens']['access_token']}"}
    active = client.get(f"{API}/auth/devices", headers=headers).json()
    assert [row["label"] for row in active] == ["Device 4"]
    assert db.query(Device).filter_by(user_id=user.id, revoked=False).count() == 1
    assert db.query(WebPushSubscription).filter_by(user_id=user.id).count() == 0
    assert db.query(RefreshToken).filter_by(user_id=user.id, revoked_at=None).count() == 1
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401


def test_device_recovery_requires_valid_owner_password(client, device):
    register(client, email="recover-denied@example.com", device=device)
    denied = client.post(
        f"{API}/auth/recover-device-access",
        json={
            "method": "password",
            "email": "recover-denied@example.com",
            "password": "WrongPass1",
            "device": make_device(),
        },
    )
    assert denied.status_code == 401
