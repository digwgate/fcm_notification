"""Pure-unit tests for the multicast send path and the settings-backed switches.

Everything here runs under plain ``pytest`` — no site, no Firebase. The SDK call
(``messaging.send_each_for_multicast``) and ``frappe.db`` are faked, which is the
point: these tests pin the DECISIONS (chunking, which error disables a token,
which never does, which override reaches which config) rather than the network.

The DB-level halves of the same features live in ``test_device_registry.py`` as
``IntegrationTestCase``s.
"""

from types import SimpleNamespace

import pytest
from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import messaging

import fcm_notification.send_notification as send_module


# --- fakes ------------------------------------------------------------------


def install_settings(monkeypatch, **overrides):
    """A settings Single with this app's defaults, overridable per test."""
    settings = SimpleNamespace(
        credentials="firebase.json",
        notifications_trigger_type=[],
        ttl=None,
        analytics_label=None,
        collapse_key=None,
        priority="high",
        restricted_package_name=None,
        channel_id="high_importance_channel",
        ios_sound_name=None,
        ios_sound_critical=0,
        ios_sound_volume=None,
        queue=None,
        preserved_payload_keys=None,
        notification_log_pushes_enabled=1,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    monkeypatch.setattr(send_module.frappe, "get_cached_doc", lambda doctype: settings)
    return settings


def install_db(monkeypatch):
    """Record every ``set_value`` and fail loudly on a commit.

    ``send_to_devices`` runs inside the caller's transaction: a commit here would
    durably commit whatever else that caller had open.
    """
    set_value_calls = []

    def forbidden_commit():
        raise AssertionError("send_to_devices must not commit")

    monkeypatch.setattr(
        send_module.frappe,
        "db",
        SimpleNamespace(
            set_value=lambda *args, **kwargs: set_value_calls.append(args),
            commit=forbidden_commit,
        ),
    )
    monkeypatch.setattr(send_module, "invalidate_user_devices_cache", lambda user: None)
    monkeypatch.setattr(
        send_module, "invalidate_guest_devices_cache", lambda guest_id: None
    )
    monkeypatch.setattr(
        send_module.frappe, "logger", lambda: SimpleNamespace(info=lambda *a, **k: None)
    )
    return set_value_calls


def install_logs(monkeypatch):
    logged = []
    monkeypatch.setattr(
        send_module.frappe, "log_error", lambda **kwargs: logged.append(kwargs)
    )
    return logged


def install_multicast(monkeypatch, exceptions_for_chunk):
    """Fake the SDK. ``exceptions_for_chunk(index, tokens)`` returns one entry per
    token: ``None`` for success, an exception for a failure."""
    calls = []

    def fake_send(multicast_message, dry_run=False, app=None):
        index = len(calls)
        calls.append(multicast_message)
        errors = exceptions_for_chunk(index, multicast_message.tokens)
        return SimpleNamespace(
            responses=[
                SimpleNamespace(
                    success=error is None, exception=error, message_id="msg"
                )
                for error in errors
            ]
        )

    monkeypatch.setattr(
        send_module.FCMNotificationService, "ensure_initialized", lambda self: None
    )
    monkeypatch.setattr(send_module.messaging, "send_each_for_multicast", fake_send)
    return calls


def install_queues(monkeypatch, queues=None):
    """Pretend the bench provisions ``queues`` (a pure-unit run has no bench)."""
    import frappe.utils.background_jobs as background_jobs

    monkeypatch.setattr(
        background_jobs,
        "get_queues_timeout",
        lambda: queues or {"short": 300, "notifications_queue": 300},
    )


def install_enqueue(monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        send_module, "enqueue", lambda method, **kwargs: enqueued.append(kwargs)
    )
    return enqueued


def devices(count, start=0):
    return [
        {
            "name": f"DEV-{index}",
            "device_token": f"token-{index}",
            "user": "u@example.com",
        }
        for index in range(start, start + count)
    ]


def disabled_names(set_value_calls):
    return [args[1] for args in set_value_calls]


# --- chunking ---------------------------------------------------------------


def test_empty_device_list_returns_without_touching_the_sdk(monkeypatch):
    """``send_each([])`` raises, so an empty audience must never reach the SDK."""
    install_settings(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [])

    assert send_module.send_to_devices([], "T", "B") == []
    assert send_module.send_to_devices(None, "T", "B") == []
    assert calls == []


def test_devices_are_chunked_with_a_partial_last_chunk(monkeypatch):
    install_settings(monkeypatch)
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None] * len(tokens))

    results = send_module.send_to_devices(devices(120), "T", "B")

    assert [len(call.tokens) for call in calls] == [50, 50, 20]
    assert all(call.tokens for call in calls), "an empty chunk would raise in the SDK"
    assert len(results) == 120
    assert all(result.ok for result in results)


def test_chunk_size_boundary_emits_no_empty_trailing_chunk(monkeypatch):
    install_settings(monkeypatch)
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None] * len(tokens))

    send_module.send_to_devices(devices(send_module.MULTICAST_CHUNK_SIZE), "T", "B")

    assert [len(call.tokens) for call in calls] == [send_module.MULTICAST_CHUNK_SIZE]


def test_tokenless_devices_are_reported_and_never_sent(monkeypatch):
    """A row whose token was cleared (rebind, logout) is not sendable, but the
    caller still gets one result per device it passed in."""
    install_settings(monkeypatch)
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None] * len(tokens))

    results = send_module.send_to_devices(
        [{"name": "DEV-CLEARED", "device_token": None}, *devices(1)], "T", "B"
    )

    assert len(calls) == 1 and len(calls[0].tokens) == 1
    assert len(results) == 2
    cleared = next(r for r in results if r.device.get("name") == "DEV-CLEARED")
    assert (cleared.ok, cleared.error_code, cleared.disabled) == (
        False,
        "NO_TOKEN",
        False,
    )


# --- outcome discrimination -------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_reason"),
    [
        (messaging.UnregisteredError("gone"), "UNREGISTERED", "Unregistered"),
        (
            messaging.SenderIdMismatchError("wrong sender"),
            "SENDER_ID_MISMATCH",
            "Sender ID Mismatch",
        ),
    ],
)
def test_dead_tokens_are_always_disabled(
    monkeypatch, error, expected_code, expected_reason
):
    """A dead token is dead whatever its neighbours did — including in a chunk
    where nothing succeeded."""
    install_settings(monkeypatch)
    set_value_calls = install_db(monkeypatch)
    install_logs(monkeypatch)
    install_multicast(monkeypatch, lambda index, tokens: [error])

    results = send_module.send_to_devices(devices(1), "T", "B")

    assert results[0].error_code == expected_code
    assert results[0].disabled is True
    assert set_value_calls == [
        ("User Device", "DEV-0", {"enabled": 0, "disabled_reason": expected_reason})
    ]


def test_invalid_argument_is_disabled_only_alongside_a_success(monkeypatch):
    """A MIXED chunk proves the payload was fine, so the 400 is about the token."""
    install_settings(monkeypatch)
    set_value_calls = install_db(monkeypatch)
    logged = install_logs(monkeypatch)
    install_multicast(
        monkeypatch,
        lambda index, tokens: [None, firebase_exceptions.InvalidArgumentError("bad")],
    )

    results = send_module.send_to_devices(devices(2), "T", "B")

    assert [result.ok for result in results] == [True, False]
    assert results[1].error_code == "INVALID_ARGUMENT"
    assert results[1].disabled is True
    assert set_value_calls == [
        ("User Device", "DEV-1", {"enabled": 0, "disabled_reason": "Invalid Token"})
    ]
    assert logged == [], "a mixed chunk is not a payload bug"


def test_all_invalid_chunk_logs_an_error_and_disables_nothing(monkeypatch):
    """Every token rejected with the same 400 means the PAYLOAD is wrong.
    Disabling those devices would delete a healthy audience over a code bug."""
    install_settings(monkeypatch)
    set_value_calls = install_db(monkeypatch)
    logged = install_logs(monkeypatch)
    install_multicast(
        monkeypatch,
        lambda index, tokens: [
            firebase_exceptions.InvalidArgumentError("bad") for _ in tokens
        ],
    )

    results = send_module.send_to_devices(devices(3), "T", "B")

    assert [result.disabled for result in results] == [False, False, False]
    assert set_value_calls == []
    assert len(logged) == 1
    assert "INVALID_ARGUMENT" in logged[0]["message"]


def test_dead_token_still_disabled_in_an_all_failed_chunk(monkeypatch):
    """The all-INVALID rule protects INVALID_ARGUMENT only."""
    install_settings(monkeypatch)
    set_value_calls = install_db(monkeypatch)
    install_logs(monkeypatch)
    install_multicast(
        monkeypatch,
        lambda index, tokens: [
            firebase_exceptions.InvalidArgumentError("bad"),
            messaging.UnregisteredError("gone"),
        ],
    )

    results = send_module.send_to_devices(devices(2), "T", "B")

    assert [result.disabled for result in results] == [False, True]
    assert disabled_names(set_value_calls) == ["DEV-1"]


@pytest.mark.parametrize(
    "error",
    [
        messaging.QuotaExceededError("429"),
        firebase_exceptions.UnavailableError("503"),
        firebase_exceptions.InternalError("500"),
        firebase_exceptions.DeadlineExceededError("timeout"),
    ],
)
def test_transient_errors_never_disable_a_device(monkeypatch, error):
    install_settings(monkeypatch)
    set_value_calls = install_db(monkeypatch)
    install_logs(monkeypatch)
    install_multicast(monkeypatch, lambda index, tokens: [error])

    results = send_module.send_to_devices(devices(1), "T", "B")

    assert results[0].ok is False
    assert results[0].disabled is False
    assert set_value_calls == []
    assert send_module.is_transient_error_code(results[0].error_code) is True


def test_dead_and_invalid_codes_are_not_transient():
    assert send_module.is_transient_error_code("UNREGISTERED") is False
    assert send_module.is_transient_error_code("INVALID_ARGUMENT") is False
    assert send_module.is_transient_error_code(None) is False


# --- per-message overrides --------------------------------------------------


def test_per_message_overrides_reach_android_and_apns(monkeypatch):
    install_settings(monkeypatch)
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None])

    send_module.send_to_devices(
        devices(1),
        "T",
        "B",
        data={"kind": "order", "ref": "SO-1"},
        opts={
            "priority": "normal",
            "ttl": 3600,
            "collapse": "order:SO-1",
            "channel_id": "orders",
        },
    )

    message = calls[0]
    assert message.android.priority == "normal"
    assert message.android.ttl.total_seconds() == 3600
    assert message.android.collapse_key == "order:SO-1"
    assert message.android.notification.channel_id == "orders"
    # Android's priority/collapse do nothing on iOS — APNs reads its own headers.
    assert message.apns.headers["apns-priority"] == "5"
    assert message.apns.headers["apns-collapse-id"] == "order:SO-1"
    # Both blocks are present: the OS renders `notification`, the client routes
    # on `data`.
    assert message.notification.title == "T"
    assert message.data == {"kind": "order", "ref": "SO-1"}


def test_settings_supply_the_defaults_when_no_opts(monkeypatch):
    install_settings(monkeypatch, collapse_key="global", ttl=60, channel_id="default")
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None])

    send_module.send_to_devices(devices(1), "T", "B")

    message = calls[0]
    assert message.android.priority == "high"
    assert message.android.collapse_key == "global"
    assert message.android.ttl.total_seconds() == 60
    assert message.android.notification.channel_id == "default"
    assert message.apns.headers["apns-priority"] == "10"


def test_payload_is_trimmed_to_the_fcm_budget(monkeypatch):
    """The 4 KB trim is reused, not re-implemented, and routing keys survive."""
    install_settings(monkeypatch)
    install_db(monkeypatch)
    calls = install_multicast(monkeypatch, lambda index, tokens: [None])

    send_module.send_to_devices(
        devices(1), "T", "B", data={"kind": "order", "ref": "SO-1", "blob": "x" * 9000}
    )

    payload = calls[0].data
    assert send_module._fcm_data_size(payload) <= send_module.FCM_DATA_BYTE_BUDGET
    assert payload["kind"] == "order"
    assert payload["ref"] == "SO-1"
    assert len(payload["blob"]) < 9000


def test_extra_preserved_keys_come_from_settings(monkeypatch):
    install_settings(monkeypatch, preserved_payload_keys="tracking_url, campaign")

    keys = send_module.preserved_payload_keys()

    assert {"tracking_url", "campaign"} <= keys
    assert {"kind", "ref", "nid"} <= keys, "the routing shape is always preserved"


def test_preserved_keys_fall_back_to_the_builtins_without_a_site(monkeypatch):
    def no_site(doctype):
        raise AttributeError("no site")

    monkeypatch.setattr(send_module.frappe, "get_cached_doc", no_site)

    assert send_module.preserved_payload_keys() == send_module._FCM_PRESERVED_KEYS


# --- settings-backed switches ----------------------------------------------


def test_notification_log_pushes_can_be_switched_off(monkeypatch):
    install_settings(monkeypatch, notification_log_pushes_enabled=0)
    dispatched = []
    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "dispatch",
        lambda self, notification, **kwargs: dispatched.append(notification),
    )

    send_module.send_notification(SimpleNamespace(name="LOG-1", flags=None))

    assert dispatched == []


def test_notification_log_pushes_are_on_when_the_field_is_unset(monkeypatch):
    """An existing site has no ``tabSingles`` row for a field that did not exist
    when it was last saved, and Frappe reads that as 0 — never the DocField
    default. Unset therefore has to mean ON here, or an upgrade silently stops
    pushing."""
    settings = install_settings(monkeypatch)
    del settings.notification_log_pushes_enabled
    dispatched = []
    monkeypatch.setattr(
        send_module.FCMNotificationService,
        "dispatch",
        lambda self, notification, **kwargs: dispatched.append(notification),
    )

    send_module.send_notification(SimpleNamespace(name="LOG-1", flags=None))

    assert len(dispatched) == 1
    assert send_module.notification_log_pushes_enabled(settings) is True


def test_dispatch_queue_prefers_the_configured_value(monkeypatch):
    settings = install_settings(monkeypatch, queue="notifications_queue")
    assert send_module.dispatch_queue(settings) == "notifications_queue"


def test_dispatch_queue_keeps_the_bench_queue_when_it_exists(monkeypatch):
    import frappe.utils.background_jobs as background_jobs

    settings = install_settings(monkeypatch)
    monkeypatch.setattr(
        background_jobs,
        "get_queues_timeout",
        lambda: {"short": 300, "notifications_queue": 300},
    )

    assert send_module.dispatch_queue(settings) == "notifications_queue"


def test_dispatch_queue_falls_back_to_short_when_the_bench_lacks_it(monkeypatch):
    import frappe.utils.background_jobs as background_jobs

    settings = install_settings(monkeypatch)
    monkeypatch.setattr(
        background_jobs, "get_queues_timeout", lambda: {"short": 300, "default": 300}
    )

    assert send_module.dispatch_queue(settings) == "short"


def test_notification_log_sends_do_not_wait_for_a_commit_by_default(monkeypatch):
    install_settings(monkeypatch)
    install_queues(monkeypatch)
    enqueued = install_enqueue(monkeypatch)

    send_module.send_direct_notification(
        title="T", body="B", devices="direct-token", enqueue=True
    )

    assert enqueued[0]["enqueue_after_commit"] is False


def test_notification_log_sends_can_wait_for_the_commit(monkeypatch):
    """``after_insert`` fires inside the caller's transaction, so a site whose
    notifications are written mid-transaction needs the wake to wait for it."""
    install_settings(monkeypatch, enqueue_after_commit=1)
    install_queues(monkeypatch)
    enqueued = install_enqueue(monkeypatch)

    send_module.send_direct_notification(
        title="T", body="B", devices="direct-token", enqueue=True
    )

    assert enqueued[0]["enqueue_after_commit"] is True


def test_credentials_come_from_the_attachment_first(monkeypatch):
    install_settings(monkeypatch, credentials="/private/files/firebase.json")
    monkeypatch.setattr(send_module.frappe, "get_site_path", lambda: "/sites/x")

    path = send_module.FCMNotificationService().credentials_path()

    assert path == "/sites/x/private/files/firebase.json"


def test_credentials_fall_back_to_the_site_config_path(monkeypatch):
    install_settings(monkeypatch, credentials=None)
    monkeypatch.setattr(
        send_module.frappe,
        "conf",
        {"fcm_service_account_path": "/run/secrets/fcm.json"},
    )

    assert (
        send_module.FCMNotificationService().credentials_path()
        == "/run/secrets/fcm.json"
    )


def test_credentials_missing_everywhere_is_an_error(monkeypatch):
    install_settings(monkeypatch, credentials=None)
    monkeypatch.setattr(send_module.frappe, "conf", {})

    def throw(message):
        raise ValueError(message)

    monkeypatch.setattr(send_module.frappe, "throw", throw)

    with pytest.raises(ValueError, match="credentials"):
        send_module.FCMNotificationService().credentials_path()


# --- FID readiness ----------------------------------------------------------


def test_fid_targeting_is_unavailable_on_this_sdk():
    """firebase_admin 7.3.0 targets token/topic/condition only. When that changes
    the probe flips and the send path can switch targets."""
    assert send_module.supports_fid_targeting() is False


def test_fid_targeting_probe_reads_the_sdk_signature(monkeypatch):
    class FutureMessage:
        def __init__(self, data=None, token=None, installation_id=None):
            pass

    monkeypatch.setattr(send_module.messaging, "Message", FutureMessage)

    assert send_module.supports_fid_targeting() is True
