"""Public API of ``fcm_notification`` — the surface other apps import.

This app is the TRANSPORT layer: device registry, send, token hygiene. What to
send, to whom and when belongs to the product app that calls these.

Every name here is re-exported from the module that implements it, so a consumer
never has to know (or track) which one that is:

- ``register_device`` / ``unbind_device`` / ``unbind_device_by_token`` /
  ``unbind_all_devices`` / ``get_devices`` /
  ``delete_user_devices`` / ``delete_guest_devices`` — ``device_registry``
- ``send_to_devices`` / ``DeviceSendResult`` / ``is_transient_error_code`` /
  ``supports_fid_targeting`` — ``send_notification``

None of these commit. The caller owns the transaction.
"""

from fcm_notification.device_registry import (
    delete_guest_devices,
    delete_user_devices,
    get_devices,
    register_device,
    token_hash,
    unbind_all_devices,
    unbind_device,
    unbind_device_by_token,
)
from fcm_notification.send_notification import (
    DeviceSendResult,
    is_transient_error_code,
    send_to_devices,
    supports_fid_targeting,
)

__all__ = [
    "DeviceSendResult",
    "delete_guest_devices",
    "delete_user_devices",
    "get_devices",
    "is_transient_error_code",
    "register_device",
    "send_to_devices",
    "supports_fid_targeting",
    "token_hash",
    "unbind_all_devices",
    "unbind_device",
    "unbind_device_by_token",
]
