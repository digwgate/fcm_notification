import re

import firebase_admin
import frappe
from firebase_admin import credentials, messaging
from frappe import enqueue


def initialize_firebase():
    firebase_settings = frappe.get_single("Firebase Settings")
    service_account = firebase_settings.service_account
    try:
        admin = firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(frappe.get_site_path() + service_account)
        firebase_admin.initialize_app(cred)


def user_id(doc):
    user_email = doc.for_user
    user_device_id = get_user_devices(user_email)
    return user_device_id


def get_user_devices(user):
    """Get all enabled devices for a user, with caching."""
    cache_key = f"user_devices:{user}"
    cached_devices = frappe.cache().get_value(cache_key)

    if cached_devices is not None:
        return cached_devices

    devices = frappe.get_all(
        "User Device",
        filters={"user": user, "enabled": True},
        fields=["Distinct(device_id) as device_id", "name", "user"],
        order_by="creation desc",
        limit_page_length=5,
    )

    frappe.cache().set_value(
        cache_key, devices, expires_in_sec=3600
    )  # Cache for 1 hour
    return devices


@frappe.whitelist()
def send_notification(doc, event=None):
    if event != None:
        device_ids = user_id(doc)
        for dvs in device_ids:
            enqueue(
                process_notification,
                queue="notifications_queue",
                device=dvs,
                notification=doc,
            )


def convert_message(message):
    CLEANR = re.compile("<.*?>")
    cleanmessage = re.sub(CLEANR, "", message)
    # cleantitle = re.sub(CLEANR, "",title)
    return cleanmessage


def process_notification(device, notification):
    initialize_firebase()
    message = notification.email_content
    title = notification.subject
    if message:
        message = convert_message(message)
    if title:
        title = convert_message(title)
    data = {
        "doctype": notification.document_type,
        "docname": notification.document_name,
        "title": title,
        "message": message,
    }
    # Get customer address lat, long and send it to the agent via notification if the doctype is SalesOrder
    if notification.document_type == "Sales Order":
        customer_address = frappe.get_cached_value(
            "Sales Order", notification.document_name, "customer_address"
        )
        address = frappe.get_cached_doc("Address", customer_address)
        if address and address.custom_latitude:
            data["latitude"] = address.custom_latitude
            data["longitude"] = address.custom_longitude
        send_fcm_message(device, title, message, data)


def send_fcm_message(device, title, message, data, is_alert=True):
    initialize_firebase()
    """Send FCM notification to a specific device"""
    try:
        messaging.send(
            message=messaging.Message(
                data=data,
                token=device.device_id,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        title=title,
                        body=message,
                        channel_id="high_importance_channel",
                    ),
                    fcm_options=messaging.AndroidFCMOptions(
                        analytics_label="assign_agent_to_order",
                    ),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            alert=(
                                messaging.ApsAlert(title=title, body=message)
                                if is_alert
                                else None
                            ),
                            sound="default",
                            custom_data=data,
                        ),
                    )
                ),
                fcm_options=messaging.FCMOptions(
                    analytics_label="assign_agent_to_order",
                ),
            ),
        )
    except messaging.UnregisteredError as e:
        frappe.db.set_value("User Device", device.name, "enabled", 0)
        # Invalidate cache for this device
        invalidate_user_devices_cache(device.user)
        frappe.db.commit()
        frappe.log_error(
            title="Unregistered Device",
            message=f"Device {device.device_id} is unregistered. Error: {str(e)}",
            reference_doctype="User Device",
            reference_name=device.name,
        )
        return device.device_id
    except messaging.SenderIdMismatchError:
        frappe.db.set_value("User Device", device.name, "enabled", 0)
        # Invalidate cache for this device
        invalidate_user_devices_cache(device.user)
        frappe.db.commit()
        frappe.log_error(
            title="Sender ID Mismatch",
            message=f"Device {device.device_id} is mismatched: {str(e)}",
            reference_doctype="User Device",
            reference_name=device.name,
        )
        return device.device_id
    except Exception as e:
        frappe.error_log(f"Error sending notification: {e}")
        return None


def invalidate_user_devices_cache_hooks(doc, method):
    """Invalidate the cache for user devices when a User Device is updated or inserted."""
    user = doc.user
    invalidate_user_devices_cache(user)


def invalidate_user_devices_cache(user):
    """Invalidate the cache for user devices."""
    cache_key = f"user_devices:{user}"
    frappe.cache().delete_value(cache_key)
