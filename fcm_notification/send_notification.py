import datetime
import inspect
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Union

import firebase_admin
import frappe
from firebase_admin import credentials, exceptions as firebase_exceptions, messaging
from frappe import enqueue
from frappe.utils import cint

DeviceInput = Union[str, Dict[str, Any]]
DeviceInputList = Optional[Union[DeviceInput, Iterable[DeviceInput]]]

SETTINGS_DOCTYPE = "FCM Notification Settings"

# Gap #3: ``send_each_for_multicast`` is NOT a batch call in firebase_admin 7.3.0 —
# it opens ``ThreadPoolExecutor(max_workers=len(messages))``, i.e. one thread and
# one HTTPS request per token against a 10-socket ``requests`` pool. 50 keeps a
# chunk's fan-out sane; FCM's own ceiling (500) is a limit, not a target.
MULTICAST_CHUNK_SIZE = 50

# Gap #9: the queue was hardcoded to ``notifications_queue``. A site that does not
# define it (the app is shared) would enqueue into a queue no worker listens on, so
# the fallback is the queue every bench has.
_DEFAULT_QUEUE = "notifications_queue"
_FALLBACK_QUEUE = "short"

# Raised by ``frappe.conf`` / ``frappe.get_cached_doc`` outside a bootstrapped site
# (pure-unit callers) — hoisted to a constant because ``ruff format`` on the pinned
# version rewrites an inline ``except (A, B):`` into Python-2 syntax.
_NO_SITE_ERRORS = (AttributeError, ImportError, KeyError, TypeError, ValueError)

_UNREGISTERED = "UNREGISTERED"
_SENDER_ID_MISMATCH = "SENDER_ID_MISMATCH"
_INVALID_ARGUMENT = "INVALID_ARGUMENT"
_NO_TOKEN = "NO_TOKEN"

# Which FCM error codes can cost a device its row, and the ``disabled_reason``
# Select value each one writes. ``INVALID_ARGUMENT`` is listed but applied only in
# a MIXED chunk — see ``FCMNotificationService.send_multicast_chunk``.
_DISABLE_REASON_BY_CODE = {
    _UNREGISTERED: "Unregistered",
    _SENDER_ID_MISMATCH: "Sender ID Mismatch",
    _INVALID_ARGUMENT: "Invalid Token",
}

# Everything FCM can answer that is about the moment rather than the token: 429s,
# 5xx, timeouts. A device is NEVER disabled for one of these; the caller retries.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        firebase_exceptions.RESOURCE_EXHAUSTED,
        firebase_exceptions.UNAVAILABLE,
        firebase_exceptions.INTERNAL,
        firebase_exceptions.DEADLINE_EXCEEDED,
        firebase_exceptions.ABORTED,
        firebase_exceptions.CANCELLED,
        firebase_exceptions.UNKNOWN,
        "QUOTA_EXCEEDED",
    }
)

# Cache namespaces. The legacy Notification Log lane and ``get_devices`` do NOT
# share one: they select different rows (the latter also requires a live token),
# so one key holding the other's answer would push to a cleared row.
_USER_CACHE_PREFIX = "user_devices"
_DEVICE_CACHE_PREFIX = "fcm_devices"
DEVICE_CACHE_TTL_SECONDS = 3600


def _setting(settings: Any, fieldname: str, default: Any = None) -> Any:
    """Read one settings field, tolerating a Single that predates it.

    A ``Single`` saved before a field existed has no ``tabSingles`` row for it, so
    Frappe reads it as missing — never as the DocField ``default``. Every caller
    therefore passes the documented fallback here instead of trusting the JSON.
    """
    getter = getattr(settings, "get", None)
    value = (
        getter(fieldname) if callable(getter) else getattr(settings, fieldname, None)
    )
    return default if value in (None, "") else value


def _get_settings():
    return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def notification_log_pushes_enabled(settings: Any = None) -> bool:
    """Gap #15: whether Desk ``Notification Log`` rows are pushed at all.

    Installing this app arms an ``after_insert`` hook for EVERY Desk notification,
    and an empty trigger-type allowlist means allow-all — so an operator testing a
    customer app with a Desk account would receive Desk alerts with an unroutable
    ``/alerts/alert/...`` payload. Unset reads as ON, so an existing site keeps its
    behaviour; a customer-facing site unchecks it.
    """
    settings = settings if settings is not None else _get_settings()
    return bool(cint(_setting(settings, "notification_log_pushes_enabled", 1)))


def dispatch_queue(settings: Any = None) -> str:
    """The RQ queue asynchronous Notification Log sends are enqueued on.

    Settings win. With nothing configured the historical ``notifications_queue`` is
    kept whenever this bench actually defines it, and only a bench without it falls
    back to ``short`` — so a site that never provisioned the queue still delivers
    instead of enqueueing into a queue no worker listens on.
    """
    settings = settings if settings is not None else _get_settings()
    configured = _setting(settings, "queue")
    if configured:
        return str(configured)
    try:
        from frappe.utils.background_jobs import get_queues_timeout

        queues = get_queues_timeout() or {}
    except Exception:
        # No site / no Redis config in reach: keep the historical queue name.
        return _DEFAULT_QUEUE
    return _DEFAULT_QUEUE if _DEFAULT_QUEUE in queues else _FALLBACK_QUEUE


def enqueue_after_commit(settings: Any = None) -> bool:
    """Gap #2: whether an async Notification Log send waits for the commit.

    ``Notification Log.after_insert`` fires INSIDE the transaction that created
    the row, so a plain ``enqueue`` can push for a document that then rolls back.
    Default OFF (the historical behaviour, and the right one for a Desk alert that
    is committed immediately); a site whose notifications are written inside a
    longer transaction turns it on.
    """
    settings = settings if settings is not None else _get_settings()
    return bool(cint(_setting(settings, "enqueue_after_commit", 0)))


def supports_fid_targeting() -> bool:
    """Whether the installed ``firebase_admin`` can target a Firebase Installation ID.

    FCM is migrating from registration tokens to Installation IDs, and
    ``User Device.installation_id`` is already stored for the day both SDKs expose
    it. Probing the constructor (rather than pinning a version) makes the
    token -> FID switch a one-line change in the send path.
    """
    parameters = inspect.signature(messaging.Message.__init__).parameters
    return any(
        name in parameters for name in ("installation_id", "fid", "installation")
    )


@dataclass
class DeviceSendResult:
    """One token's outcome inside ``send_to_devices``.

    ``error_code`` is the FCM/Firebase code (``UNREGISTERED``, ``INVALID_ARGUMENT``,
    ``UNAVAILABLE``, ...) and is ``None`` on success. ``disabled`` says whether THIS
    call disabled the row — a transient failure (429/5xx/timeout) never does.
    """

    device: Any
    ok: bool
    error_code: Optional[str] = None
    disabled: bool = False


class FCMNotificationService:
    """Encapsulates FCM initialization and Notification Log delivery."""

    _app = None

    def __init__(self):
        self.settings = frappe.get_cached_doc("FCM Notification Settings")

    def ensure_initialized(self):
        """Initialize Firebase once using the credentials from settings."""
        if self.__class__._app:
            return self.__class__._app

        try:
            self.__class__._app = firebase_admin.get_app()
            return self.__class__._app
        except ValueError:
            pass

        cred = credentials.Certificate(self.credentials_path())
        self.__class__._app = firebase_admin.initialize_app(cred)
        return self.__class__._app

    def credentials_path(self) -> str:
        """Resolve the service-account JSON path (gap #10).

        The ``credentials`` Attach field wins. With nothing attached the path falls
        back to ``frappe.conf.fcm_service_account_path``, so a secrets manager can
        inject the file at deploy time instead of it living in the site's files.
        """
        attached = _setting(self.settings, "credentials")
        if attached:
            return os.path.join(
                frappe.get_site_path(), str(attached).lstrip("/").lstrip("./")
            )

        try:
            configured = (frappe.conf or {}).get("fcm_service_account_path")
        except _NO_SITE_ERRORS:
            configured = None
        if configured:
            return str(configured)

        frappe.throw("FCM credentials are not configured in FCM Notification Settings.")

    def allowed_notification_type(self, notification_type: Optional[str]) -> bool:
        """Check if notification type is allowed per settings (empty list means allow all)."""
        allowed_types = {
            row.type
            for row in (self.settings.notifications_trigger_type or [])
            if row.type
        }
        if not allowed_types:
            return True
        return notification_type in allowed_types

    def build_data_payload(self, notification) -> Dict[str, str]:
        """Prepare data payload that will be delivered with the message."""
        payload = notification.payload or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        base = {
            "doctype": notification.document_type or "",
            "docname": notification.document_name or "",
            "type": notification.type or "",
        }

        if isinstance(payload, dict):
            for key, value in payload.items():
                base[key] = value

        if notification.subject:
            base.setdefault("title", convert_message(notification.subject))
        if notification.email_content:
            base.setdefault("message", convert_message(notification.email_content))

        return stringify_data(base)

    def build_direct_data_payload(
        self,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
        doctype: Optional[str] = None,
        docname: Optional[str] = None,
        notification_type: Optional[str] = None,
    ) -> Dict[str, str]:
        """Prepare direct-send data payload without a Notification Log document."""
        payload = dict(data or {})
        explicit_payload = {
            "title": title,
            "message": body,
            "doctype": doctype,
            "docname": docname,
            "type": notification_type,
        }
        for key, value in explicit_payload.items():
            if value:
                payload[key] = value

        return stringify_data(payload)

    @staticmethod
    def _override(opts: Optional[Dict[str, Any]], *keys: str) -> Any:
        """First per-message value the caller supplied under any of ``keys``."""
        for key in keys:
            value = (opts or {}).get(key)
            if value not in (None, ""):
                return value
        return None

    def _message_option(
        self, opts: Optional[Dict[str, Any]], setting_field: str, *keys: str
    ) -> Any:
        """Per-message override (gap #4), falling back to the settings default."""
        return self._override(opts, *keys) or _setting(self.settings, setting_field)

    def build_android_config(
        self, title: str, body: str, opts: Optional[Dict[str, Any]] = None
    ):
        ttl_seconds = self._message_option(opts, "ttl", "ttl", "ttl_seconds")
        ttl = datetime.timedelta(seconds=int(ttl_seconds)) if ttl_seconds else None

        analytics_label = self._message_option(
            opts, "analytics_label", "analytics_label"
        )
        android_options = (
            messaging.AndroidFCMOptions(analytics_label=analytics_label)
            if analytics_label
            else None
        )

        return messaging.AndroidConfig(
            collapse_key=self._message_option(
                opts, "collapse_key", "collapse", "collapse_key"
            ),
            priority=self._message_option(opts, "priority", "priority"),
            ttl=ttl,
            restricted_package_name=_setting(self.settings, "restricted_package_name"),
            notification=messaging.AndroidNotification(
                title=title or None,
                body=body or None,
                channel_id=self._message_option(
                    opts, "channel_id", "channel_id", "channel"
                ),
            ),
            fcm_options=android_options,
        )

    def build_apns_headers(self, opts: Optional[Dict[str, Any]] = None):
        """APNs equivalents of the Android priority/collapse overrides.

        Android's ``priority``/``collapse_key`` do nothing on iOS: APNs reads the
        ``apns-priority`` (10 = immediate, 5 = power-considerate) and
        ``apns-collapse-id`` headers instead, so one caller-supplied option has to
        be written twice.
        """
        headers = {}
        priority = self._message_option(opts, "priority", "priority")
        if priority:
            headers["apns-priority"] = "10" if str(priority).lower() == "high" else "5"
        collapse = self._message_option(
            opts, "collapse_key", "collapse", "collapse_key"
        )
        if collapse:
            headers["apns-collapse-id"] = str(collapse)
        return headers or None

    def build_apns_config(
        self,
        title: str,
        body: str,
        data: Dict[str, str],
        opts: Optional[Dict[str, Any]] = None,
    ):
        sound: Union[str, messaging.CriticalSound, None] = None
        if (
            self.settings.ios_sound_name
            or self.settings.ios_sound_critical
            or self.settings.ios_sound_volume
        ):
            if self.settings.ios_sound_critical:
                sound = messaging.CriticalSound(
                    name=self.settings.ios_sound_name or "default",
                    critical=bool(self.settings.ios_sound_critical),
                    volume=float(self.settings.ios_sound_volume or 1),
                )
            else:
                sound = self.settings.ios_sound_name or "default"

        analytics_label = self._message_option(
            opts, "analytics_label", "analytics_label"
        )
        apns_options = (
            messaging.APNSFCMOptions(analytics_label=analytics_label)
            if analytics_label
            else None
        )

        return messaging.APNSConfig(
            headers=self.build_apns_headers(opts),
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title or None, body=body or None),
                    sound=sound,
                    custom_data=data,
                )
            ),
            fcm_options=apns_options,
        )

    def build_common_fcm_options(self, opts: Optional[Dict[str, Any]] = None):
        analytics_label = self._message_option(
            opts, "analytics_label", "analytics_label"
        )
        if not analytics_label:
            return None
        return messaging.FCMOptions(analytics_label=analytics_label)

    def send_multicast_chunk(
        self,
        devices: List[Any],
        title: str,
        body: str,
        data: Dict[str, str],
        opts: Optional[Dict[str, Any]] = None,
    ) -> List[DeviceSendResult]:
        """Send ONE chunk of tokens and turn the batch response into outcomes.

        The chunk is the unit the ``INVALID_ARGUMENT`` rule is decided on: the same
        code means "this token is malformed" when its neighbours went through, and
        "this payload is malformed" when nothing in the chunk did. Only the first
        reading may disable a row; the second is a bug in the caller and is logged.

        Issues no commit — the caller owns the transaction (gap #13).
        """
        self.ensure_initialized()
        message = messaging.MulticastMessage(
            tokens=[self._device_token(device) for device in devices],
            data=data,
            notification=messaging.Notification(title=title or None, body=body or None),
            android=self.build_android_config(title, body, opts),
            apns=self.build_apns_config(title, body, data, opts),
            fcm_options=self.build_common_fcm_options(opts),
        )
        batch = messaging.send_each_for_multicast(message)

        outcomes = [
            (device, bool(response.success), error_code(response.exception))
            for device, response in zip(devices, batch.responses)
        ]
        chunk_had_success = any(ok for _, ok, _ in outcomes)
        invalid_tokens = [
            device
            for device, ok, code in outcomes
            if not ok and code == _INVALID_ARGUMENT
        ]
        if invalid_tokens and not chunk_had_success:
            frappe.log_error(
                title="FCM rejected every token in a chunk",
                message=(
                    f"{len(invalid_tokens)} of {len(devices)} tokens returned "
                    f"{_INVALID_ARGUMENT} and none succeeded — this is a payload bug, "
                    "not a token problem, so no device was disabled. "
                    f"Title: {title!r} | Data keys: {sorted(data or {})}"
                ),
            )

        results = []
        for device, ok, code in outcomes:
            reason = None
            if not ok and code in _DISABLE_REASON_BY_CODE:
                if code != _INVALID_ARGUMENT or chunk_had_success:
                    reason = _DISABLE_REASON_BY_CODE[code]
            disabled = bool(reason) and self.disable_device_row(device, reason, code)
            results.append(
                DeviceSendResult(
                    device=device, ok=ok, error_code=code, disabled=disabled
                )
            )
        return results

    def disable_device_row(self, device: Any, disabled_reason: str, code: str) -> bool:
        """Disable one ``User Device`` row without committing.

        The Notification Log lane's ``_disable_device`` commits on purpose (it runs
        one device per background job). This one must not: it runs inside the
        caller's transaction, mid-chunk, and a commit here would durably commit
        whatever else the caller had open.
        """
        name = self._device_name(device)
        if not name:
            return False

        frappe.db.set_value(
            "User Device",
            name,
            {"enabled": 0, "disabled_reason": disabled_reason},
        )
        # db.set_value fires no document hooks, so invalidation must be explicit.
        invalidate_user_devices_cache(self._device_user(device))
        invalidate_guest_devices_cache(self._device_guest(device))
        frappe.logger().info(
            f"FCM disabled User Device {name}: {code} -> {disabled_reason}"
        )
        return True

    def safe_send_to_device(
        self,
        device: Union[Dict[str, Any], Any],
        data: Dict[str, str],
        title: str,
        body: str,
        notification_name: Optional[str] = None,
        notification_type: Optional[str] = None,
        user: Optional[str] = None,
        opts: Optional[Dict[str, Any]] = None,
    ):
        """Send message to a single device, handling cleanup on token errors."""
        try:
            self.send_to_device(device, data, title, body, opts)
        except messaging.UnregisteredError as e:
            self._disable_device(
                device,
                user=user,
                reason=f"Unregistered Device: {e}",
                disabled_reason="Unregistered",
            )
            return self._device_token(device)
        except messaging.SenderIdMismatchError as e:
            self._disable_device(
                device,
                user=user,
                reason=f"Sender ID Mismatch: {e}",
                disabled_reason="Sender ID Mismatch",
            )
            return self._device_token(device)
        except Exception as e:
            frappe.log_error(
                title="Error sending FCM notification",
                message=f"Notification: {notification_name or ''} | Device: {self._device_token(device)} | Error: {e}",
            )
            return None

    def send_to_device(
        self,
        device: Union[Dict[str, Any], Any],
        data: Dict[str, str],
        title: str,
        body: str,
        opts: Optional[Dict[str, Any]] = None,
    ):
        self.ensure_initialized()
        message = messaging.Message(
            data=data,
            token=self._device_token(device),
            android=self.build_android_config(title, body, opts),
            apns=self.build_apns_config(title, body, data, opts),
            fcm_options=self.build_common_fcm_options(opts),
        )
        messaging.send(message)

    def dispatch(
        self,
        notification,
        event=None,
        send_async: Optional[bool] = None,
        devices: Optional[Iterable[Union[str, Dict[str, Any]]]] = None,
    ):
        """Send a Notification Log to user devices, honoring settings and triggers."""
        if not self.allowed_notification_type(getattr(notification, "type", None)):
            return

        device_records = self._prepare_devices(notification, devices)
        if not device_records:
            return

        data = self.build_data_payload(notification)
        title = convert_message(notification.subject or "")
        body = convert_message(notification.email_content or "")

        send_async = (
            send_async
            if send_async is not None
            else not bool(getattr(notification, "send_now", False))
        )

        for device in device_records:
            if send_async:
                enqueue(
                    _queue_send_device,
                    queue=dispatch_queue(self.settings),
                    enqueue_after_commit=enqueue_after_commit(self.settings),
                    device=device,
                    data=data,
                    title=title,
                    body=body,
                    notification_name=getattr(notification, "name", None),
                    notification_type=getattr(notification, "type", None),
                    user=getattr(notification, "for_user", None),
                )
            else:
                self.safe_send_to_device(
                    device,
                    data=data,
                    title=title,
                    body=body,
                    notification_name=getattr(notification, "name", None),
                    notification_type=getattr(notification, "type", None),
                    user=getattr(notification, "for_user", None),
                )

    def dispatch_direct(
        self,
        title: str,
        body: str,
        users: Optional[Union[str, Iterable[str]]] = None,
        devices: DeviceInputList = None,
        data: Optional[Dict[str, Any]] = None,
        doctype: Optional[str] = None,
        docname: Optional[str] = None,
        notification_type: Optional[str] = None,
        send_async: bool = False,
        opts: Optional[Dict[str, Any]] = None,
    ):
        """Send a direct FCM notification without a Notification Log document.

        ``opts`` carries the per-message overrides (``collapse_key``, ``priority``,
        ``ttl``, ``channel_id``, ``analytics_label``) — same contract as
        :func:`send_to_devices`; anything absent falls back to the settings default.
        """
        user_list = self._normalize_values(users)
        direct_devices = self._prepare_direct_devices(devices)

        if not user_list and not direct_devices:
            frappe.throw("Provide users or devices to send a direct FCM notification.")

        if not self.allowed_notification_type(notification_type):
            return

        device_records: List[Dict[str, Any]] = []
        for user in user_list:
            device_records.extend(get_user_devices(user) or [])
        device_records.extend(direct_devices)
        device_records = self._deduplicate_devices(device_records)

        if not device_records:
            return

        title = convert_message(title or "")
        body = convert_message(body or "")
        payload = self.build_direct_data_payload(
            title=title,
            body=body,
            data=data,
            doctype=doctype,
            docname=docname,
            notification_type=notification_type,
        )

        for device in device_records:
            user = self._device_user(device)
            if send_async:
                enqueue(
                    _queue_send_device,
                    queue=dispatch_queue(self.settings),
                    enqueue_after_commit=enqueue_after_commit(self.settings),
                    device=device,
                    data=payload,
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    user=user,
                    opts=opts,
                )
            else:
                self.safe_send_to_device(
                    device,
                    data=payload,
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    user=user,
                    opts=opts,
                )

    def _prepare_devices(
        self,
        notification,
        devices: Optional[Iterable[Union[str, Dict[str, Any]]]],
    ) -> List[Dict[str, Any]]:
        if devices is None:
            return get_user_devices(notification.for_user)

        prepared = []
        for device in devices:
            if isinstance(device, dict):
                prepared.append(device)
            else:
                prepared.append(
                    {
                        "device_token": device,
                        "user": getattr(notification, "for_user", None),
                    }
                )
        return prepared

    def _prepare_direct_devices(
        self,
        devices: DeviceInputList,
    ) -> List[Dict[str, Any]]:
        prepared = []
        for device in self._normalize_values(devices):
            if isinstance(device, dict):
                if not device.get("device_token"):
                    frappe.throw("Each direct device requires a device_token.")
                prepared.append(device)
            else:
                if not device:
                    frappe.throw("Each direct device requires a device_token.")
                prepared.append({"device_token": device})
        return prepared

    def _deduplicate_devices(
        self, devices: Iterable[Union[Dict[str, Any], Any]]
    ) -> List[Dict[str, Any]]:
        seen = set()
        deduplicated = []
        for device in devices:
            token = self._device_token(device)
            if not token or token in seen:
                continue
            seen.add(token)
            deduplicated.append(device)
        return deduplicated

    def _disable_device(
        self,
        device,
        user: Optional[str],
        reason: str = "",
        disabled_reason: str = "",
    ):
        """Soft-disable one device row.

        Two distinct "reasons", deliberately not merged:

        - ``disabled_reason`` is the ``User Device.disabled_reason`` Select
          value — a fixed, queryable category.
        - ``reason`` is free-text for the Error Log only, and carries the
          exception detail the caller saw. Overloading the Select with it
          would destroy that diagnostic.
        """
        device_name = self._device_name(device)
        device_token = self._device_token(device)
        if device_name:
            frappe.db.set_value(
                "User Device",
                device_name,
                {"enabled": 0, "disabled_reason": disabled_reason},
            )
            frappe.db.commit()
            invalidate_user_devices_cache(user or self._device_user(device))
        frappe.log_error(
            title="User Device disabled",
            message=f"Device {device_token} disabled. Reason: {reason}",
            reference_doctype="User Device",
            reference_name=device_name,
        )

    @staticmethod
    def _device_token(device: Union[Dict[str, Any], Any]) -> Optional[str]:
        if isinstance(device, dict):
            return device.get("device_token")
        return getattr(device, "device_token", None)

    @staticmethod
    def _device_name(device: Union[Dict[str, Any], Any]) -> Optional[str]:
        if isinstance(device, dict):
            return device.get("name")
        return getattr(device, "name", None)

    @staticmethod
    def _device_user(device: Union[Dict[str, Any], Any]) -> Optional[str]:
        if isinstance(device, dict):
            return device.get("user")
        return getattr(device, "user", None)

    @staticmethod
    def _device_guest(device: Union[Dict[str, Any], Any]) -> Optional[str]:
        if isinstance(device, dict):
            return device.get("guest_id")
        return getattr(device, "guest_id", None)

    @staticmethod
    def _normalize_values(value):
        if value is None:
            return []
        if isinstance(value, (str, bytes, dict)):
            return [value]
        try:
            return list(value)
        except TypeError:
            return [value]


def error_code(exception) -> Optional[str]:
    """Stable code for one failed token, or ``None`` when it succeeded.

    ``UnregisteredError`` and ``SenderIdMismatchError`` carry the generic
    ``NOT_FOUND`` / ``PERMISSION_DENIED`` codes, which say nothing about the token,
    so both are named explicitly. Everything else reports the Firebase code as-is
    so the caller can tell a 429 from a 400 without re-deriving it.
    """
    if exception is None:
        return None
    if isinstance(exception, messaging.UnregisteredError):
        return _UNREGISTERED
    if isinstance(exception, messaging.SenderIdMismatchError):
        return _SENDER_ID_MISMATCH
    code = getattr(exception, "code", None)
    return str(code) if code else type(exception).__name__


def is_transient_error_code(code: Optional[str]) -> bool:
    """Whether the caller should retry this token later instead of giving up."""
    return bool(code) and code in _TRANSIENT_ERROR_CODES


def send_to_devices(
    devices: Iterable[Any],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    opts: Optional[Dict[str, Any]] = None,
) -> List[DeviceSendResult]:
    """Send one message to many devices and report per-token outcomes.

    ``devices`` are ``User Device`` rows (dicts or docs) — normally straight from
    ``get_devices``. ``data`` is passed through verbatim (after the 4 KB trim): the
    caller owns the routing keys, this app does not invent them. ``opts`` carries
    the per-message overrides (``priority``, ``ttl``, ``collapse``, ``channel_id``,
    ``analytics_label``); anything absent falls back to the settings default.

    Tokens are sent in chunks of ``MULTICAST_CHUNK_SIZE``; an empty audience
    returns ``[]`` without touching the SDK, because ``send_each([])`` raises.

    Issues NO commit (gap #13). Bad tokens are disabled in the caller's
    transaction, so the caller's own commit is what makes the disable durable.

    A per-token failure is reported, never raised. An SDK-level failure
    (credentials, DNS, the whole call timing out) propagates: that is a batch
    problem, and only the caller knows whether to retry it.
    """
    sendable = []
    tokenless = []
    for device in devices or []:
        target = sendable if FCMNotificationService._device_token(device) else tokenless
        target.append(device)

    results = [
        DeviceSendResult(device=device, ok=False, error_code=_NO_TOKEN)
        for device in tokenless
    ]
    if not sendable:
        return results

    service = FCMNotificationService()
    payload = stringify_data(dict(data or {}))
    title = convert_message(title or "")
    body = convert_message(body or "")

    for start in range(0, len(sendable), MULTICAST_CHUNK_SIZE):
        chunk = sendable[start : start + MULTICAST_CHUNK_SIZE]
        results.extend(service.send_multicast_chunk(chunk, title, body, payload, opts))
    return results


@frappe.whitelist()
def send_notification(
    notification,
    event=None,
    send_async: Optional[bool] = None,
    devices=None,
    ignore_skip_flag: bool = False,
):
    """
    Public entrypoint to send a Notification Log.
    - `notification` can be a Notification Log doc or name.
    - `send_async` overrides the doc's send_now flag when provided.
    - `devices` can be a list of device dicts or tokens to target specific devices.

    Returns early when ``notification_log_pushes_enabled`` is off (gap #15) — the
    Desk lane is opt-out per site, and this hook fires for EVERY Notification Log.
    """
    if not notification_log_pushes_enabled():
        return

    should_skip = getattr(notification, "skip_fcm_send", False) or getattr(
        getattr(notification, "flags", None), "skip_fcm_send", False
    )
    if should_skip and not ignore_skip_flag:
        return

    notification_doc = (
        frappe.get_doc("Notification Log", notification)
        if isinstance(notification, str)
        else notification
    )
    service = FCMNotificationService()
    service.dispatch(
        notification_doc, event=event, send_async=send_async, devices=devices
    )


def send_direct_notification(
    title: str,
    body: str,
    users: Optional[Union[str, Iterable[str]]] = None,
    devices: DeviceInputList = None,
    data: Optional[Dict[str, Any]] = None,
    doctype: Optional[str] = None,
    docname: Optional[str] = None,
    notification_type: Optional[str] = None,
    enqueue: bool = False,
    opts: Optional[Dict[str, Any]] = None,
):
    """Python-only entrypoint to send an FCM notification directly."""
    service = FCMNotificationService()
    service.dispatch_direct(
        title=title,
        body=body,
        users=users,
        devices=devices,
        data=data,
        doctype=doctype,
        docname=docname,
        notification_type=notification_type,
        send_async=enqueue,
        opts=opts,
    )


def _queue_send_device(
    device,
    data: Dict[str, str],
    title: str,
    body: str,
    notification_name: Optional[str] = None,
    notification_type: Optional[str] = None,
    user: Optional[str] = None,
    opts: Optional[Dict[str, Any]] = None,
):
    """Worker-safe wrapper to send messages from the enqueue queue."""
    service = FCMNotificationService()
    if not service.allowed_notification_type(notification_type):
        return
    service.safe_send_to_device(
        device,
        data=data,
        title=title,
        body=body,
        notification_name=notification_name,
        notification_type=notification_type,
        user=user,
        opts=opts,
    )


def convert_message(message):
    """Strip HTML tags from message/title before sending."""
    CLEANR = re.compile("<.*?>")
    cleanmessage = re.sub(CLEANR, "", message) if message else ""
    return cleanmessage


# Firebase rejects any FCM message whose data payload exceeds 4096 bytes
# ("Message is too large. The maximum is 4K (4096 bytes)."). Budget below that to
# leave room for the notification (title/body) envelope FCM adds on top of the data.
# ponytail: one flat budget; lower it if a notification's title/body still tips the
# total past 4 KB.
FCM_DATA_BYTE_BUDGET = 3500

# Small routing/metadata keys the client needs to act on a push — kept intact; only
# large free-text values (message body, injected blobs) get trimmed.
_FCM_PRESERVED_KEYS = frozenset(
    {
        "doctype",
        "docname",
        "type",
        "kind",
        "ref",
        "nid",
        "route",
        "spa_route",
        "ticket_id",
        "conversation_id",
    }
)
_FCM_ELLIPSIS = "…"
_PRESERVED_KEY_SEPARATORS = re.compile(r"[,\s]+")


def preserved_payload_keys(settings: Any = None) -> frozenset:
    """Routing keys the 4 KB trim must never touch (gap #6).

    The built-in set covers this app's own Desk payload and the ``{kind, ref, nid}``
    shape customer pushes route on; a site adds its own keys in
    ``FCM Notification Settings.preserved_payload_keys`` instead of editing code.
    Falls back to the built-ins for pure-unit callers with no site in reach.
    """
    try:
        settings = settings if settings is not None else _get_settings()
        configured = _setting(settings, "preserved_payload_keys", "")
    except _NO_SITE_ERRORS:
        return _FCM_PRESERVED_KEYS
    extra = {key for key in _PRESERVED_KEY_SEPARATORS.split(str(configured)) if key}
    return _FCM_PRESERVED_KEYS | extra


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _fcm_data_size(data: Dict[str, str]) -> int:
    """FCM counts the data payload as the sum of every key and value byte length."""
    return sum(_utf8_len(key) + _utf8_len(value) for key, value in data.items())


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate to <= max_bytes UTF-8 bytes without splitting a character.

    Appends an ellipsis when there is room for it; drops the value to empty when the
    budget is smaller than the ellipsis itself.
    """
    if _utf8_len(value) <= max_bytes:
        return value
    ellipsis_len = _utf8_len(_FCM_ELLIPSIS)
    if max_bytes <= ellipsis_len:
        return ""
    keep = max_bytes - ellipsis_len
    return value.encode("utf-8")[:keep].decode("utf-8", "ignore") + _FCM_ELLIPSIS


def fit_data_to_fcm_limit(
    data: Dict[str, str],
    budget: int = FCM_DATA_BYTE_BUDGET,
    preserved: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Shrink an over-budget FCM data payload so Firebase accepts it.

    Truncates the largest non-routing value first (a long message body or an injected
    blob), preserving the small routing keys so the client can still act on the push.
    Best-effort: if only routing keys remain and they still exceed the budget
    (pathological), returns what it has rather than looping forever.
    """
    if _fcm_data_size(data) <= budget:
        return data

    preserved_keys = (
        frozenset(preserved) if preserved is not None else _FCM_PRESERVED_KEYS
    )
    fitted = dict(data)
    while _fcm_data_size(fitted) > budget:
        candidates = [
            (key, value)
            for key, value in fitted.items()
            if key not in preserved_keys and value
        ]
        if not candidates:
            break  # ponytail: only routing keys left — nothing safe to trim
        key, value = max(candidates, key=lambda item: _utf8_len(item[1]))
        over = _fcm_data_size(fitted) - budget
        fitted[key] = _truncate_utf8(value, max(0, _utf8_len(value) - over))
    return fitted


def stringify_data(data: Dict[str, Any]) -> Dict[str, str]:
    """Coerce payload values into strings for FCM data payloads, size-capped to 4 KB."""
    result = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            result[key] = json.dumps(value)
        else:
            result[key] = frappe.as_unicode(value)
    return fit_data_to_fcm_limit(result, preserved=preserved_payload_keys())


def get_user_devices(user):
    """Get all enabled devices for a user, with caching."""
    cache_key = f"user_devices:{user}"
    cached_devices = frappe.cache().get_value(cache_key)

    if cached_devices is not None:
        return cached_devices

    devices = frappe.get_all(
        "User Device",
        filters={"user": user, "enabled": True},
        fields=["device_token", "name", "user"],
        order_by="creation desc",
        limit=5,
        distinct=True,
    )

    frappe.cache().set_value(cache_key, devices, expires_in_sec=3600)
    return devices


def populate_payload_data(doc, event):
    """Populate Notification Log payload using Notification Payload mapping."""
    if not (doc.document_type and doc.document_name):
        return
    payload = {
        "doctype": doc.document_type,
        "docname": doc.document_name,
        "route": f"/alerts/alert/{doc.name}",
    }
    doc.payload = payload
    payload_doc_name = frappe.db.get_value(
        "Notification Payload",
        filters={"for_doctype": doc.document_type, "disabled": 0},
    )
    if not payload_doc_name:
        return

    mapping_doc = frappe.get_doc("Notification Payload", payload_doc_name)
    if not mapping_doc.fields_mapper:
        return

    source_doc = frappe.get_cached_doc(doc.document_type, doc.document_name)

    for row in mapping_doc.fields_mapper:
        if not (row.key and row.doc_field):
            continue
        payload[row.key] = source_doc.get(row.doc_field)
    doc.payload = payload


def invalidate_user_devices_cache_hooks(doc, method):
    """Invalidate both subject caches when a User Device row changes.

    A row can be reached by ``user`` OR by ``guest_id``; invalidating only the
    former would leave a guest's cached list pointing at a row that has just been
    disabled, rebound or deleted.
    """
    invalidate_user_devices_cache(doc.get("user"))
    invalidate_guest_devices_cache(doc.get("guest_id"))


def device_cache_key(user: Optional[str] = None, guest_id: Optional[str] = None) -> str:
    """Cache key ``get_devices`` stores one subject's device list under."""
    subject = f"user:{user}" if user else f"guest:{guest_id}"
    return f"{_DEVICE_CACHE_PREFIX}:{subject}"


def invalidate_user_devices_cache(user):
    """Invalidate every cached device list for ``user``."""
    if not user:
        return
    frappe.cache().delete_value(f"{_USER_CACHE_PREFIX}:{user}")
    frappe.cache().delete_value(device_cache_key(user=user))


def invalidate_guest_devices_cache(guest_id):
    """Invalidate the cached device list for a pre-login subject."""
    if not guest_id:
        return
    frappe.cache().delete_value(device_cache_key(guest_id=guest_id))


def disable_user_devices(user: str, reason: str) -> int:
    """Soft-disable every enabled device of ``user``, stamping ``reason``.

    ``reason`` is a ``User Device.disabled_reason`` Select value. Rows are
    never deleted here — they stay subject to ``token_sweep``'s retention
    window like any other disabled row.

    Returns the number of rows disabled. The caller owns the transaction;
    this issues no commit.
    """
    names = frappe.get_all(
        "User Device", filters={"user": user, "enabled": 1}, pluck="name"
    )
    if not names:
        return 0

    frappe.db.set_value(
        "User Device",
        {"name": ["in", names]},
        {"enabled": 0, "disabled_reason": reason},
    )
    # db.set_value fires no document hooks, so invalidation must be explicit.
    invalidate_user_devices_cache(user)
    return len(names)


def enable_user_devices(user: str, reason: str) -> int:
    """Re-enable only the devices of ``user`` that were disabled *for* ``reason``.

    Matching on ``disabled_reason`` is what makes the restore symmetric
    rather than blanket: a row disabled for a dead token
    (``Unregistered``/``Sender ID Mismatch``/``Stale``) is not resurrected,
    and a legacy row disabled before the field existed carries an empty
    reason, so it is left alone too.

    Clears ``disabled_reason`` on the rows it re-enables, so an enabled row
    never carries a stale disable category.

    Returns the number of rows re-enabled. The caller owns the transaction;
    this issues no commit.
    """
    names = frappe.get_all(
        "User Device",
        filters={"user": user, "enabled": 0, "disabled_reason": reason},
        pluck="name",
    )
    if not names:
        return 0

    frappe.db.set_value(
        "User Device",
        {"name": ["in", names]},
        {"enabled": 1, "disabled_reason": ""},
    )
    invalidate_user_devices_cache(user)
    return len(names)
