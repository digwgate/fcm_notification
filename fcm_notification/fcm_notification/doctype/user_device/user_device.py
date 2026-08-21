# Copyright (c) 2022, Raheeb and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from fcm_notification.device_registry import token_hash
from fcm_notification.send_notification import invalidate_user_devices_cache


class UserDevice(Document):
    def validate(self):
        """Keep ``token_hash`` in lockstep with ``device_token``.

        The hash is what carries the UNIQUE index — MariaDB refuses UNIQUE on the
        TEXT column the token itself lives in — so it has to be derived on every
        ORM write, including the legacy registration path below, or the index
        protects nothing. Writers that bypass the ORM (``db.set_value``, the raw
        rebind UPDATE) set both columns themselves.
        """
        self.token_hash = token_hash(self.device_token)


@frappe.whitelist()
def handle_user_device(device_data: dict) -> dict:
    """Legacy, TOKEN-keyed device registration.

    Deliberately NOT install-id keyed: the clients that call this have no
    installation id, and Frappe stores an empty ``unique`` Data field as NULL, so
    their rows never collide on ``installation_id``. New clients use
    ``device_registry.register_device`` instead.

    What changed in 0.1.0: when another row already holds this token, that ROW is
    re-bound to the caller (user updated, re-enabled, disable reason cleared)
    instead of a second row being inserted. The old ``{token, user}`` lookup left
    the previous owner's row enabled — a shared or re-logged-in phone kept
    receiving the previous account's pushes — and, with the token now UNIQUE, the
    second insert would be a 500.

    ``platform`` is never re-sent on that path: the field is ``set_only_once``.

    Keeps its commit, unlike the new APIs: this is a self-contained whitelisted
    call with no caller transaction to protect.
    """
    device_token = device_data.get("device_token")
    if not device_token:
        frappe.throw(frappe._("Device ID is required"))

    device_data = dict(device_data)
    # The caller does NOT get to say whose device this is. This used to be a
    # setdefault, so a caller-supplied "user" won through to a save that runs
    # with ignore_permissions=True — any authenticated caller could register
    # their OWN token under somebody else's account and receive that person's
    # pushes. The endpoint's documented job has always been "upsert the CALLING
    # user's device", so this makes the code say what the contract says.
    device_data["user"] = frappe.session.user

    name = frappe.db.get_value(
        "User Device", {"token_hash": token_hash(device_token)}, "name"
    )
    if name:
        user_device = frappe.get_doc("User Device", name)
        previous_user = user_device.user
        device_data.pop("platform", None)
        user_device.update(device_data)
        user_device.enabled = 1
        user_device.disabled_reason = ""
        user_device.last_seen_at = now_datetime()
        user_device.save(ignore_permissions=True)
        frappe.db.commit()
        # The row's own hooks invalidate the NEW owner; the account that just
        # lost the device has to be invalidated explicitly.
        invalidate_user_devices_cache(previous_user)
        return user_device

    user_device = frappe.new_doc("User Device")
    user_device.update(device_data)
    user_device.last_seen_at = now_datetime()
    user_device.save(ignore_permissions=True)
    frappe.db.commit()
    return {"device_token": user_device.device_token, "result": "success"}


@frappe.whitelist(methods=["DELETE"])
def unregister_notification_token(device_token: str | None = None) -> None:
    """Unregister a notification token for the current user."""
    filters = {
        "user": frappe.session.user,
    }
    if device_token:
        filters["device_token"] = device_token
    # Remove the token from the user's devices
    frappe.db.delete(
        "User Device",
        filters,
    )
    invalidate_user_devices_cache(frappe.session.user)

    return None
