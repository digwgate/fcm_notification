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


def test_send_notification_skips_documents_marked_for_manual_dispatch(monkeypatch):
    install_settings(monkeypatch)
    dispatched = []

    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "dispatch",
        lambda self, notification, **kwargs: dispatched.append(notification),
    )

    send_module.send_notification(
        SimpleNamespace(name="LOG-1", flags=SimpleNamespace(skip_fcm_send=True))
    )

    assert dispatched == []


def test_send_notification_can_force_manual_dispatch_for_skipped_documents(monkeypatch):
    install_settings(monkeypatch)
    dispatched = []

    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "dispatch",
        lambda self, notification, **kwargs: dispatched.append(
            {"notification": notification, "kwargs": kwargs}
        ),
    )

    notification = SimpleNamespace(
        name="LOG-1",
        flags=SimpleNamespace(skip_fcm_send=True),
    )
    devices = [{"device_token": "ios-token", "user": "target@example.com"}]

    send_module.send_notification(
        notification,
        send_async=False,
        devices=devices,
        ignore_skip_flag=True,
    )

    assert dispatched == [
        {
            "notification": notification,
            "kwargs": {
                "event": None,
                "send_async": False,
                "devices": devices,
            },
        }
    ]


def test_fit_data_to_fcm_limit_leaves_small_payload_untouched():
    data = {"doctype": "Qnina Ticket", "docname": "T-1", "message": "short body"}
    assert send_module.fit_data_to_fcm_limit(data) is data


def test_fit_data_to_fcm_limit_trims_oversized_payload_to_budget():
    big_body = "x" * 8000  # well over the 4 KB ceiling on its own
    data = {
        "doctype": "Qnina Ticket",
        "docname": "T-2",
        "kind": "ticket_updated",
        "spa_route": "/dashboard/tickets/T-2",
        "message": big_body,
    }

    fitted = send_module.fit_data_to_fcm_limit(data)

    # Whole payload now fits FCM's data budget...
    assert send_module._fcm_data_size(fitted) <= send_module.FCM_DATA_BYTE_BUDGET
    # ...routing keys survive intact...
    assert fitted["doctype"] == "Qnina Ticket"
    assert fitted["docname"] == "T-2"
    assert fitted["kind"] == "ticket_updated"
    assert fitted["spa_route"] == "/dashboard/tickets/T-2"
    # ...and the big body is the one that got truncated (with an ellipsis marker).
    assert fitted["message"].endswith(send_module._FCM_ELLIPSIS)
    assert len(fitted["message"]) < len(big_body)


def test_fit_data_to_fcm_limit_preserves_routing_keys_when_blob_injected():
    data = {
        "doctype": "Qnina Ticket",
        "docname": "T-3",
        "conversation_id": "group_t-3",
        "blob": "y" * 6000,
    }

    fitted = send_module.fit_data_to_fcm_limit(data)

    assert send_module._fcm_data_size(fitted) <= send_module.FCM_DATA_BYTE_BUDGET
    assert fitted["conversation_id"] == "group_t-3"
    assert send_module._utf8_len(fitted["blob"]) < 6000


def test_stringify_data_caps_payload_size():
    data = {"docname": "T-4", "message": "z" * 9000}

    result = send_module.stringify_data(data)

    assert send_module._fcm_data_size(result) <= send_module.FCM_DATA_BYTE_BUDGET
    assert result["docname"] == "T-4"
