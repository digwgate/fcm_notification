"""Integration tests for the generic ``device_owner_roles`` -> Custom DocPerm sync.

The one that matters most is
``test_system_manager_survives_an_owner_role``: Frappe's Meta REPLACES a
doctype's standard permissions with its Custom DocPerms as soon as one exists,
so a naive "just add a row" sync silently strips every role the DocType JSON
declares.

These tests write to ``FCM Notification Settings`` and to Custom DocPerms, and
restore both in ``tearDown``. Prefer running them on the Super E dev site.

Run with::

    bench --site <site> run-tests --app fcm_notification \\
        --module fcm_notification.test_device_perms
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from fcm_notification import device_perms

DOCTYPE = "User Device"
SETTINGS = "FCM Notification Settings"
_TEST_ROLE = "FCM Device Owner Test Role"

# Row identity, not permission content — never carried across a restore.
_IDENTITY_FIELDS = {
    "name",
    "creation",
    "modified",
    "owner",
    "modified_by",
    "idx",
    "docstatus",
    "doctype",
    "_user_tags",
    "_comments",
    "_assign",
    "_liked_by",
}


def seed_roles(roles):
    """Pretend the app ships ``roles`` as its one-time seed list."""
    return mock.patch.object(device_perms, "SEED_ROLES", tuple(roles))


class TestDeviceOwnerPerms(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self._settings_snapshot = [
            row.role
            for row in (frappe.get_doc(SETTINGS).get("device_owner_roles") or [])
        ]
        # Snapshot EVERY flag, not a handful: restoring a partial row would
        # silently downgrade a permission the site actually relies on.
        self._perm_snapshot = [
            {key: value for key, value in row.items() if key not in _IDENTITY_FIELDS}
            for row in frappe.get_all(
                "Custom DocPerm", filters={"parent": DOCTYPE}, fields=["*"]
            )
        ]
        self._created_role = False
        if not frappe.db.exists("Role", _TEST_ROLE):
            frappe.get_doc(
                {"doctype": "Role", "role_name": _TEST_ROLE, "desk_access": 1}
            ).insert(ignore_permissions=True)
            self._created_role = True

    def tearDown(self):
        frappe.db.delete("Custom DocPerm", {"parent": DOCTYPE})
        for perm in self._perm_snapshot:
            frappe.get_doc(
                {
                    "doctype": "Custom DocPerm",
                    "parent": DOCTYPE,
                    "parenttype": "DocType",
                    "parentfield": "permissions",
                    **perm,
                }
            ).insert(ignore_permissions=True)
        self._set_roles(self._settings_snapshot)
        if self._created_role and frappe.db.exists("Role", _TEST_ROLE):
            frappe.delete_doc(
                "Role",
                _TEST_ROLE,
                ignore_permissions=True,
                force=True,
                delete_permanently=True,
            )
        frappe.clear_cache(doctype=DOCTYPE)
        super().tearDown()

    def _set_roles(self, roles):
        settings = frappe.get_doc(SETTINGS)
        settings.set("device_owner_roles", [])
        for role in roles:
            settings.append("device_owner_roles", {"role": role})
        settings.flags.ignore_permissions = True
        settings.save()
        frappe.clear_document_cache(SETTINGS)

    def _custom_perm_roles(self):
        return frappe.get_all(
            "Custom DocPerm", filters={"parent": DOCTYPE}, pluck="role"
        )

    # --- seeding -----------------------------------------------------------

    def test_seeds_the_named_roles_when_they_exist(self):
        self._set_roles([])
        with seed_roles([_TEST_ROLE]):
            seeded = device_perms.seed_device_owner_roles()

        self.assertEqual(seeded, [_TEST_ROLE])
        self.assertEqual(device_perms.configured_device_owner_roles(), [_TEST_ROLE])

    def test_seeds_nothing_when_the_roles_do_not_exist(self):
        self._set_roles([])
        with seed_roles(["FCM Role That Does Not Exist"]):
            seeded = device_perms.seed_device_owner_roles()

        self.assertEqual(seeded, [])
        self.assertEqual(device_perms.configured_device_owner_roles(), [])

    def test_seeding_never_overwrites_a_configured_table(self):
        self._set_roles([_TEST_ROLE])
        with seed_roles([_TEST_ROLE]):
            self.assertEqual(device_perms.seed_device_owner_roles(), [])
        self.assertEqual(device_perms.configured_device_owner_roles(), [_TEST_ROLE])

    # --- sync --------------------------------------------------------------

    def test_system_manager_survives_an_owner_role(self):
        """Adding a Custom DocPerm makes Meta ignore the DocType's own rows, so
        the standard rows have to be mirrored in the same breath."""
        standard_roles = set(
            frappe.get_all("DocPerm", filters={"parent": DOCTYPE}, pluck="role")
        )
        self._set_roles([_TEST_ROLE])

        with seed_roles([]):
            device_perms.sync_device_owner_roles()

        effective = {perm.role for perm in frappe.get_meta(DOCTYPE).permissions}
        self.assertTrue(
            standard_roles <= effective,
            f"the DocType's own roles vanished: {standard_roles - effective}",
        )
        self.assertIn(_TEST_ROLE, effective)

    def test_the_owner_row_is_scoped_to_its_own_documents(self):
        self._set_roles([_TEST_ROLE])
        with seed_roles([]):
            device_perms.sync_device_owner_roles()

        perm = frappe.db.get_value(
            "Custom DocPerm",
            {"parent": DOCTYPE, "role": _TEST_ROLE},
            ["read", "write", "create", "delete", "if_owner"],
            as_dict=True,
        )
        self.assertEqual(
            (perm.read, perm.write, perm.create, perm.if_owner), (1, 1, 1, 1)
        )
        self.assertFalse(perm.delete, "owning your device row is not owning the table")

    def test_sync_is_idempotent(self):
        self._set_roles([_TEST_ROLE])
        with seed_roles([]):
            first = device_perms.sync_device_owner_roles()
            before = sorted(self._custom_perm_roles())

            second = device_perms.sync_device_owner_roles()

        self.assertEqual(first["added"], [_TEST_ROLE])
        self.assertEqual(second["added"], [])
        self.assertEqual(second["removed"], [])
        self.assertEqual(sorted(self._custom_perm_roles()), before)

    def test_removing_a_role_removes_its_permission(self):
        self._set_roles([_TEST_ROLE])
        # SEED_ROLES is pinned empty throughout: an empty table would otherwise
        # be re-seeded on a site where the seed roles happen to exist.
        with seed_roles([]):
            device_perms.sync_device_owner_roles()
            self.assertIn(_TEST_ROLE, self._custom_perm_roles())

            self._set_roles([])
            result = device_perms.sync_device_owner_roles()

        self.assertEqual(result["removed"], [_TEST_ROLE])
        self.assertNotIn(_TEST_ROLE, self._custom_perm_roles())
        # With nothing owner-scoped left, the copies go too and the DocType's own
        # rows apply again.
        self.assertTrue(result["reset"])
        self.assertEqual(self._custom_perm_roles(), [])

    def test_an_empty_table_on_a_fresh_site_creates_no_custom_perms(self):
        self._set_roles([])
        frappe.db.delete("Custom DocPerm", {"parent": DOCTYPE})

        with seed_roles(["FCM Role That Does Not Exist"]):
            result = device_perms.sync_device_owner_roles()

        self.assertEqual(result["seeded"], [])
        self.assertEqual(result["added"], [])
        self.assertEqual(self._custom_perm_roles(), [])
