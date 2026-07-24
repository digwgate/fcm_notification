import datetime
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Union

import firebase_admin
import frappe
from firebase_admin import credentials, messaging
from frappe import enqueue

DeviceInput = Union[str, Dict[str, Any]]
DeviceInputList = Optional[Union[DeviceInput, Iterable[DeviceInput]]]


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

        if not self.settings.credentials:
            frappe.throw(
                "FCM credentials are not configured in FCM Notification Settings."
            )

        credentials_path = os.path.join(
            frappe.get_site_path(), self.settings.credentials.lstrip("/").lstrip("./")
        )
        cred = credentials.Certificate(credentials_path)
        self.__class__._app = firebase_admin.initialize_app(cred)
        return self.__class__._app

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

    def build_android_config(self, title: str, body: str):
        ttl = (
            datetime.timedelta(seconds=int(self.settings.ttl))
            if self.settings.ttl
            else None
        )

        android_options = (
            messaging.AndroidFCMOptions(analytics_label=self.settings.analytics_label)
            if self.settings.analytics_label
            else None
        )

        return messaging.AndroidConfig(
            collapse_key=self.settings.collapse_key or None,
            priority=self.settings.priority or None,
            ttl=ttl,
            restricted_package_name=self.settings.restricted_package_name or None,
            notification=messaging.AndroidNotification(
                title=title or None,
                body=body or None,
                channel_id=self.settings.channel_id or None,
            ),
            fcm_options=android_options,
        )

    def build_apns_config(self, title: str, body: str, data: Dict[str, str]):
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

        apns_options = (
            messaging.APNSFCMOptions(analytics_label=self.settings.analytics_label)
            if self.settings.analytics_label
            else None
        )

        return messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title or None, body=body or None),
                    sound=sound,
                    custom_data=data,
                )
            ),
            fcm_options=apns_options,
        )

    def build_common_fcm_options(self):
        if not self.settings.analytics_label:
            return None
        return messaging.FCMOptions(analytics_label=self.settings.analytics_label)

    def safe_send_to_device(
        self,
        device: Union[Dict[str, Any], Any],
        data: Dict[str, str],
        title: str,
        body: str,
        notification_name: Optional[str] = None,
        notification_type: Optional[str] = None,
        user: Optional[str] = None,
    ):
        """Send message to a single device, handling cleanup on token errors."""
        try:
            self.send_to_device(device, data, title, body)
        except messaging.UnregisteredError as e:
            self._disable_device(device, user=user, reason=f"Unregistered Device: {e}")
            return self._device_token(device)
        except messaging.SenderIdMismatchError as e:
            self._disable_device(device, user=user, reason=f"Sender ID Mismatch: {e}")
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
    ):
        self.ensure_initialized()
        message = messaging.Message(
            data=data,
            token=self._device_token(device),
            android=self.build_android_config(title, body),
            apns=self.build_apns_config(title, body, data),
            fcm_options=self.build_common_fcm_options(),
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
                    queue="notifications_queue",
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
    ):
        """Send a direct FCM notification without a Notification Log document."""
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
                    queue="notifications_queue",
                    device=device,
                    data=payload,
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    user=user,
                )
            else:
                self.safe_send_to_device(
                    device,
                    data=payload,
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    user=user,
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

    def _disable_device(self, device, user: Optional[str], reason: str = ""):
        device_name = self._device_name(device)
        device_token = self._device_token(device)
        if device_name:
            frappe.db.set_value("User Device", device_name, "enabled", 0)
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
    def _normalize_values(value):
        if value is None:
            return []
        if isinstance(value, (str, bytes, dict)):
            return [value]
        try:
            return list(value)
        except TypeError:
            return [value]


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
    """
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
    )


def _queue_send_device(
    device,
    data: Dict[str, str],
    title: str,
    body: str,
    notification_name: Optional[str] = None,
    notification_type: Optional[str] = None,
    user: Optional[str] = None,
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
        "route",
        "spa_route",
        "ticket_id",
        "conversation_id",
    }
)
_FCM_ELLIPSIS = "…"


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
    data: Dict[str, str], budget: int = FCM_DATA_BYTE_BUDGET
) -> Dict[str, str]:
    """Shrink an over-budget FCM data payload so Firebase accepts it.

    Truncates the largest non-routing value first (a long message body or an injected
    blob), preserving the small routing keys so the client can still act on the push.
    Best-effort: if only routing keys remain and they still exceed the budget
    (pathological), returns what it has rather than looping forever.
    """
    if _fcm_data_size(data) <= budget:
        return data

    fitted = dict(data)
    while _fcm_data_size(fitted) > budget:
        candidates = [
            (key, value)
            for key, value in fitted.items()
            if key not in _FCM_PRESERVED_KEYS and value
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
    return fit_data_to_fcm_limit(result)


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
    """Invalidate the cache for user devices when a User Device is updated or inserted."""
    user = doc.user
    invalidate_user_devices_cache(user)


def invalidate_user_devices_cache(user):
    """Invalidate the cache for user devices."""
    cache_key = f"user_devices:{user}"
    frappe.cache().delete_value(cache_key)
