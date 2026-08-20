"""Sync ``User Device`` owner permissions from settings (gap #7).

The DocType JSON used to hard-code two roles belonging to ONE product. A shared
app cannot ship that, so the roles moved into
``FCM Notification Settings.device_owner_roles`` (a Table of Role) and this runs
on every ``after_migrate``: each configured role gets a Custom DocPerm letting it
read/write/create its OWN device rows, and a role removed from the table loses it
again. No product repo is touched, and a site that configures nothing keeps only
the roles in the DocType itself.

The subtlety that shapes the module: Frappe's Meta REPLACES a doctype's standard
permissions with its Custom DocPerms as soon as ONE exists
(``model/meta.py:set_custom_permissions``). Adding an owner row naively would
therefore strip ``System Manager``. ``setup_custom_perms`` (Frappe's own helper)
copies the standard rows first, and this module only ever deletes rows it can
recognise as its own or as those copies.
"""

from __future__ import annotations

from typing import List, Tuple

import frappe
from frappe.permissions import setup_custom_perms

DOCTYPE = "User Device"
SETTINGS_DOCTYPE = "FCM Notification Settings"

# Seeded once, only where the roles already exist: the roles the DocType JSON
# used to hard-code, so the site that has been running with them is unchanged by
# the move. A site without those roles (anyone else) seeds nothing.
SEED_ROLES = ("Qnina Agent", "Qnina Customer")

# What "owns their device rows" means: manage your own registrations, nothing else.
OWNER_PERMISSIONS = {"read": 1, "write": 1, "create": 1, "if_owner": 1, "permlevel": 0}


def configured_device_owner_roles() -> List[str]:
    """Roles listed in the settings table, in order, de-duplicated."""
    settings = frappe.get_doc(SETTINGS_DOCTYPE)
    roles = [row.role for row in (settings.get("device_owner_roles") or []) if row.role]
    return list(dict.fromkeys(roles))


def seed_device_owner_roles() -> List[str]:
    """One-time seed of the settings table. Returns the roles it added.

    Only fires when the table is EMPTY and the roles exist, so it cannot fight an
    operator who has configured their own list.
    """
    settings = frappe.get_doc(SETTINGS_DOCTYPE)
    if settings.get("device_owner_roles"):
        return []

    present = frappe.get_all("Role", filters={"name": ["in", SEED_ROLES]}, pluck="name")
    if not present:
        return []

    for role in present:
        settings.append("device_owner_roles", {"role": role})
    settings.flags.ignore_permissions = True
    settings.save()
    return present


def _standard_perm_keys() -> set:
    """(role, permlevel, if_owner) of the rows the DocType JSON itself declares."""
    return {
        (row.role, row.permlevel or 0, row.if_owner or 0)
        for row in frappe.get_all(
            "DocPerm",
            filters={"parent": DOCTYPE},
            fields=["role", "permlevel", "if_owner"],
        )
    }


def _custom_perm_rows(**filters) -> List[dict]:
    return frappe.get_all(
        "Custom DocPerm",
        filters={"parent": DOCTYPE, **filters},
        fields=["name", "role", "permlevel", "if_owner"],
    )


def _add_owner_perm(role: str) -> bool:
    """Give ``role`` owner-scoped access. Returns whether a row was created."""
    if frappe.db.exists(
        "Custom DocPerm", {"parent": DOCTYPE, "role": role, "permlevel": 0}
    ):
        return False

    frappe.get_doc(
        {
            "doctype": "Custom DocPerm",
            "parent": DOCTYPE,
            "parenttype": "DocType",
            "parentfield": "permissions",
            "role": role,
            **OWNER_PERMISSIONS,
        }
    ).insert(ignore_permissions=True)
    return True


def _drop_mirrors_if_nothing_else_remains(standard_keys: set) -> bool:
    """Delete the Custom DocPerms when they are only copies of the standard rows.

    Leaving copies behind would freeze the doctype's permissions at whatever the
    JSON said the day the first owner role was configured. Deleting them is only
    safe while nothing else lives there — an operator's own Role Permission
    Manager rows are never touched.
    """
    remaining = _custom_perm_rows()
    if not remaining:
        return False

    keys = {(row.role, row.permlevel or 0, row.if_owner or 0) for row in remaining}
    if keys != standard_keys:
        return False

    frappe.db.delete("Custom DocPerm", {"parent": DOCTYPE})
    return True


def _apply_owner_roles(roles: List[str]) -> Tuple[List[str], List[str], bool]:
    """Reconcile Custom DocPerms with ``roles``. Returns (added, removed, reset)."""
    standard_keys = _standard_perm_keys()
    standard_roles = {role for role, _, _ in standard_keys}
    desired = [
        role
        for role in roles
        if role not in standard_roles and frappe.db.exists("Role", role)
    ]

    owner_rows = _custom_perm_rows(if_owner=1)
    removed = []
    for row in owner_rows:
        if row.role in desired or row.role in standard_roles:
            continue
        frappe.db.delete("Custom DocPerm", {"name": row.name})
        removed.append(row.role)

    if not desired:
        return [], removed, _drop_mirrors_if_nothing_else_remains(standard_keys)

    # Mirror the standard rows BEFORE adding anything, or Meta drops them.
    setup_custom_perms(DOCTYPE)
    added = [role for role in desired if _add_owner_perm(role)]
    return added, removed, False


def sync_device_owner_roles() -> dict:
    """``after_migrate`` entry point. Returns a summary for tests and logs."""
    if not frappe.db.exists("DocType", DOCTYPE):
        return {"seeded": [], "added": [], "removed": [], "reset": False}

    seeded = seed_device_owner_roles()
    added, removed, was_reset = _apply_owner_roles(configured_device_owner_roles())

    if seeded or added or removed or was_reset:
        frappe.clear_cache(doctype=DOCTYPE)
        frappe.logger().info(
            f"FCM device owner roles: seeded={seeded} added={added} "
            f"removed={removed} reset={was_reset}"
        )
    return {"seeded": seeded, "added": added, "removed": removed, "reset": was_reset}
