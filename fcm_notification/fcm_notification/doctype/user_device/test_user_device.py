# Copyright (c) 2022, Raheeb and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from fcm_notification.send_notification import get_user_devices

_TEST_USER = "Administrator"
_TEST_TOKEN = "user-device-cache-test"


class TestUserDevice(IntegrationTestCase):
    def tearDown(self):
        frappe.db.delete("User Device", {"device_token": _TEST_TOKEN})
        frappe.cache().delete_value(f"user_devices:{_TEST_USER}")
        super().tearDown()

    def test_delete_invalidates_the_device_cache(self):
        """``get_user_devices`` caches enabled devices for an hour. Without an
        ``on_trash`` hook a deleted device keeps being pushed to for that hour,
        because deleting our row doesn't invalidate the token at FCM.
        """
        doc = frappe.get_doc(
            {
                "doctype": "User Device",
                "user": _TEST_USER,
                "device_token": _TEST_TOKEN,
                "platform": "Android",
                "enabled": 1,
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()

        # Warm the cache, and confirm the new device is in it.
        cached = get_user_devices(_TEST_USER)
        self.assertIn(_TEST_TOKEN, [d["device_token"] for d in cached])

        frappe.delete_doc("User Device", doc.name, ignore_permissions=True, force=True)

        self.assertIsNone(
            frappe.cache().get_value(f"user_devices:{_TEST_USER}"),
            "Deleting a User Device must invalidate the cached device list.",
        )
