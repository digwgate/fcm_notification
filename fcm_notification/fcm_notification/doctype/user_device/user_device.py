# Copyright (c) 2022, Raheeb and contributors
# For license information, please see license.txt

from fcm_notification.send_notification import invalidate_user_devices_cache
import frappe
from frappe.model.document import Document


class UserDevice(Document):
    pass


@frappe.whitelist()
def handle_user_device(device_data: dict) -> dict:
    device_token = device_data.get("device_token")
    if not device_token:
        frappe.throw(frappe._("Device ID is required"))
    if frappe.db.exists(
        "User Device", {"device_token": device_token, "user": frappe.session.user}
    ):
        # Update the existing record
        user_device = frappe.get_doc("User Device", {"device_token": device_token})
        user_device.update(device_data)
        user_device.save(ignore_permissions=True)
        frappe.db.commit()
        return user_device
    if "user" not in device_data.keys():
        device_data["user"] = frappe.session.user
    user_device = frappe.new_doc("User Device")
    user_device.update(device_data)
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
