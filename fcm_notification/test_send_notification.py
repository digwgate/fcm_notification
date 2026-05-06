from types import SimpleNamespace

import pytest

import fcm_notification.send_notification as send_module


def install_settings(monkeypatch, allowed_types=None):
    settings = SimpleNamespace(
        credentials="firebase.json",
        notifications_trigger_type=[
            SimpleNamespace(type=notification_type)
            for notification_type in (allowed_types or [])
        ],
        ttl=None,
        analytics_label=None,
        collapse_key=None,
        priority="high",
        restricted_package_name=None,
        channel_id="high_importance_channel",
        ios_sound_name=None,
        ios_sound_critical=0,
        ios_sound_volume=None,
    )
    monkeypatch.setattr(
        send_module.frappe,
        "get_cached_doc",
        lambda doctype: settings,
    )
    return settings


def install_throw(monkeypatch):
    def throw(message):
        raise ValueError(message)

    monkeypatch.setattr(send_module.frappe, "throw", throw)


def test_send_direct_notification_sends_to_users_and_devices_once(monkeypatch):
    install_settings(monkeypatch)
    sent = []

    monkeypatch.setattr(
        send_module,
        "get_user_devices",
        lambda user: [{"device_token": "user-token", "name": "device-1", "user": user}],
    )
    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "safe_send_to_device",
        lambda self, device, data, title, body, **kwargs: sent.append(
            {
                "device": device,
                "data": data,
                "title": title,
                "body": body,
                "kwargs": kwargs,
            }
        ),
    )

    send_module.send_direct_notification(
        title="<b>Title</b>",
        body="<p>Body</p>",
        users="user@example.com",
        devices=["user-token", {"device_token": "direct-token", "user": "manual"}],
        data={
            "custom": {"answer": 42},
            "title": "ignored",
            "message": "ignored",
            "doctype": "Ignored",
            "docname": "IGN-1",
            "type": "Ignored",
        },
        doctype="Task",
        docname="TASK-1",
        notification_type="Alert",
    )

    assert [row["device"]["device_token"] for row in sent] == [
        "user-token",
        "direct-token",
    ]
    assert sent[0]["title"] == "Title"
    assert sent[0]["body"] == "Body"
    assert sent[0]["data"] == {
        "custom": '{"answer": 42}',
        "title": "Title",
        "message": "Body",
        "doctype": "Task",
        "docname": "TASK-1",
        "type": "Alert",
    }
    assert sent[0]["kwargs"]["notification_type"] == "Alert"


def test_send_direct_notification_enqueues_when_requested(monkeypatch):
    install_settings(monkeypatch)
    enqueued = []

    monkeypatch.setattr(
        send_module,
        "get_user_devices",
        lambda user: [{"device_token": "user-token", "name": "device-1", "user": user}],
    )
    monkeypatch.setattr(
        send_module,
        "enqueue",
        lambda method, **kwargs: enqueued.append({"method": method, "kwargs": kwargs}),
    )

    send_module.send_direct_notification(
        title="Title",
        body="Body",
        users=["user@example.com"],
        notification_type="Alert",
        enqueue=True,
    )

    assert len(enqueued) == 1
    assert enqueued[0]["method"] is send_module._queue_send_device
    assert enqueued[0]["kwargs"]["queue"] == "notifications_queue"
    assert enqueued[0]["kwargs"]["device"]["device_token"] == "user-token"
    assert enqueued[0]["kwargs"]["data"]["title"] == "Title"
    assert enqueued[0]["kwargs"]["data"]["message"] == "Body"
    assert enqueued[0]["kwargs"]["notification_type"] == "Alert"
    assert enqueued[0]["kwargs"]["user"] == "user@example.com"


def test_send_direct_notification_validates_recipients(monkeypatch):
    install_settings(monkeypatch)
    install_throw(monkeypatch)

    with pytest.raises(ValueError, match="users or devices"):
        send_module.send_direct_notification(title="Title", body="Body")

    with pytest.raises(ValueError, match="device_token"):
        send_module.send_direct_notification(
            title="Title",
            body="Body",
            devices=[{"name": "device-without-token"}],
        )


def test_send_direct_notification_honors_notification_type_settings(monkeypatch):
    install_settings(monkeypatch, allowed_types=["Allowed"])
    sent = []

    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "safe_send_to_device",
        lambda self, device, data, title, body, **kwargs: sent.append(device),
    )

    send_module.send_direct_notification(
        title="Title",
        body="Body",
        devices="direct-token",
        notification_type="Blocked",
    )

    assert sent == []
