"""``User Device`` registry: register, unbind, read, erase.

Everything here is **install-id keyed**: ``installation_id`` (a Firebase
Installation ID) is the row key and the FCM registration token is the device's
credential, not its identity. That inversion is what fixes the cross-account
push leak the token-keyed path had — re-registering a token that another row
holds moves the token instead of creating a second live row.

The legacy whitelisted path (``user_device.handle_user_device``) stays
token-keyed for the sibling product; see its docstring.

**No function here commits.** The caller owns the transaction: these APIs run
inside a request that may still fail, and a commit in the middle of one would
durably commit whatever else that request had open.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import now_datetime

from fcm_notification.send_notification import (
    DEVICE_CACHE_TTL_SECONDS,
    device_cache_key,
    invalidate_guest_devices_cache,
    invalidate_user_devices_cache,
)

DOCTYPE = "User Device"

# The wire speaks lowercase (contract), the DocType Select does not.
PLATFORM_BY_WIRE_VALUE = {"android": "Android", "ios": "IOS", "web": "Web"}
LOCALES = ("ar", "en")
PUSH_PERMISSIONS = ("granted", "denied", "provisional", "not_determined")

# FIDs are 22-char URL-safe base64, but other install-id schemes exist and the
# column is 128 wide; keep the charset tight enough to be safe in a LIKE-free
# equality filter and reject anything that cannot be an id at all.
_INSTALLATION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_.:-]{8,128}\Z")

# Same policy as the Notification Log lane: the newest handful of devices per
# subject. A phone that has not registered in months is not worth a send slot.
MAX_DEVICES_PER_SUBJECT = 5

_SAVEPOINT = "fcm_register_device"


def token_hash(token: Optional[str]) -> Optional[str]:
    """sha256 of a registration token, or ``None`` when there is no token.

    The hash — not the token — carries the UNIQUE index: MariaDB refuses UNIQUE
    on the TEXT column the token lives in, and retyping that column would
    truncate rows on a site that already has data.
    """
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_unique_violation(exc: Exception) -> bool:
    """Whether ``exc`` is a duplicate-key error, whichever layer raised it.

    ``doc.insert()`` catches the driver error itself and re-raises
    ``frappe.UniqueValidationError``; raw SQL surfaces the driver's 1062. A guard
    that checks only one of the two is dead code that reads as careful.
    """
    return isinstance(
        exc, frappe.UniqueValidationError
    ) or frappe.db.is_unique_key_violation(exc)


def _validated_installation_id(installation_id: Any) -> str:
    value = (installation_id or "").strip() if isinstance(installation_id, str) else ""
    if not _INSTALLATION_ID_PATTERN.match(value):
        frappe.throw(_("Invalid installation id."))
    return value


def _validated_platform(platform: Any) -> str:
    value = PLATFORM_BY_WIRE_VALUE.get(str(platform or "").strip().lower())
    if not value:
        frappe.throw(_("Invalid device platform."))
    return value


def _validated_choice(value: Any, allowed: tuple, label: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text not in allowed:
        frappe.throw(_("Invalid {0}.").format(label))
    return text


def _device_info_columns(
    platform: str,
    app_version: Optional[str],
    os_version: Optional[str],
    device_model: Optional[str],
) -> Dict[str, Any]:
    """Map the API's three device facts onto the columns this DocType already has.

    The doctype is shared, so app/OS/model do NOT get duplicate columns: they land
    on ``version``, ``system_version`` (iOS) / ``version_release`` (everything
    else) and ``model``.
    """
    columns: Dict[str, Any] = {}
    if app_version:
        columns["version"] = app_version
    if device_model:
        columns["model"] = device_model
    if os_version:
        columns["system_version" if platform == "IOS" else "version_release"] = (
            os_version
        )
    return columns


def _invalidate(subjects) -> None:
    for user, guest_id in subjects:
        invalidate_user_devices_cache(user)
        invalidate_guest_devices_cache(guest_id)


def _release_token_from_other_rows(installation_id: str, digest: str) -> bool:
    """Clear ``digest``'s token off every row that is NOT this install.

    Issued as raw SQL, and issued BEFORE the insert: ``device_token`` is ``reqd``,
    so the ORM refuses to clear it — and a merely-disabled row that kept its token
    would still collide with the UNIQUE index the insert is about to hit.

    Returns whether anything was rebound.
    """
    rows = frappe.get_all(
        DOCTYPE,
        filters={"token_hash": digest},
        fields=["name", "user", "guest_id", "installation_id"],
    )
    stale = [row for row in rows if row.get("installation_id") != installation_id]
    if not stale:
        return False

    placeholders = ", ".join(["%s"] * len(stale))
    frappe.db.sql(
        f"""
        UPDATE `tabUser Device`
        SET enabled = 0,
            device_token = NULL,
            token_hash = NULL,
            disabled_reason = 'Rebound',
            modified = %s,
            modified_by = %s
        WHERE name IN ({placeholders})
        """,
        [now_datetime(), frappe.session.user, *[row["name"] for row in stale]],
    )
    _invalidate((row.get("user"), row.get("guest_id")) for row in stale)
    return True


def _update_device(name: str, values: Dict[str, Any]) -> None:
    """Write the upsert's values onto an existing row.

    ``db.set_value`` rather than the ORM: ``platform`` is ``set_only_once`` and a
    re-registration must never resend it, and the row being updated may currently
    have a cleared (``reqd``-violating) token from an earlier rebind.
    """
    before = (
        frappe.db.get_value(DOCTYPE, name, ["user", "guest_id"], as_dict=True)
        or frappe._dict()
    )
    frappe.db.set_value(DOCTYPE, name, values)
    # db.set_value fires no document hooks, so invalidation must be explicit —
    # including for the subject the row belonged to a moment ago.
    _invalidate(
        [
            (before.get("user"), before.get("guest_id")),
            (values.get("user"), values.get("guest_id")),
        ]
    )


def _insert_device(
    installation_id: str, platform: str, values: Dict[str, Any]
) -> Optional[str]:
    """Insert the row, or return ``None`` if a racer inserted it first.

    There is no row to ``FOR UPDATE`` on a first registration, so the race is won
    by insert-and-catch. The savepoint is what lets the caller's transaction
    survive the failed insert.
    """
    doc = frappe.new_doc(DOCTYPE)
    doc.installation_id = installation_id
    doc.platform = platform
    doc.update(values)

    frappe.db.savepoint(_SAVEPOINT)
    try:
        doc.insert(ignore_permissions=True)
    except Exception as exc:
        frappe.db.rollback(save_point=_SAVEPOINT)
        if not _is_unique_violation(exc):
            raise
        return None
    frappe.db.release_savepoint(_SAVEPOINT)
    return doc.name


def register_device(
    installation_id,
    token,
    platform,
    *,
    user=None,
    guest_id=None,
    locale=None,
    app_version=None,
    os_version=None,
    device_model=None,
    push_permission=None,
) -> dict:
    """Idempotent upsert of one install's device row.

    Returns ``{"name": <User Device name>, "rebound": bool}``, where ``rebound``
    says the token was taken away from a different install (a reinstall, or the
    same phone handed to another account).

    ``platform`` arrives lowercase on the wire and is mapped to the Select here;
    it is written only on insert, because the field is ``set_only_once``.

    Raises ``frappe.ValidationError`` on an invalid installation id, platform,
    locale or push permission. Issues NO commit.
    """
    installation_id = _validated_installation_id(installation_id)
    platform_value = _validated_platform(platform)
    locale = _validated_choice(locale, LOCALES, "locale")
    push_permission = _validated_choice(
        push_permission, PUSH_PERMISSIONS, "push permission"
    )
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        frappe.throw(_("A device token is required."))

    digest = token_hash(token)
    rebound = _release_token_from_other_rows(installation_id, digest)

    values: Dict[str, Any] = {
        "user": user or None,
        "guest_id": guest_id or None,
        "device_token": token,
        "token_hash": digest,
        "locale": locale,
        "push_permission": push_permission,
        "last_seen_at": now_datetime(),
        "enabled": 1,
        "disabled_reason": "",
    }
    values.update(
        _device_info_columns(platform_value, app_version, os_version, device_model)
    )

    name = frappe.db.get_value(DOCTYPE, {"installation_id": installation_id}, "name")
    if not name:
        name = _insert_device(installation_id, platform_value, values)
        if name:
            return {"name": name, "rebound": rebound}
        # A concurrent request inserted this install between our read and our
        # insert; its row is the one to update.
        name = frappe.db.get_value(
            DOCTYPE, {"installation_id": installation_id}, "name"
        )
        if not name:
            frappe.throw(_("Could not register this device."))

    _update_device(name, values)
    return {"name": name, "rebound": rebound}


def _logout_rows(names: List[str], subjects) -> None:
    """Disable + de-credential the named rows. NO commit; the caller owns the txn.

    The token is cleared to NULL as well as disabled, so the next account on this
    handset can register the same token without colliding with a dead row — and
    so the row stops being a deliverable push target the instant it is written.
    The row itself SURVIVES: a relaunch re-registers onto it by ``installation_id``
    instead of inserting a duplicate. Hard deletion is erasure's job, not logout's.
    """
    if not names:
        return
    placeholders = ", ".join(["%s"] * len(names))
    frappe.db.sql(
        f"""
        UPDATE `tabUser Device`
        SET enabled = 0,
            device_token = NULL,
            token_hash = NULL,
            disabled_reason = 'Logged Out',
            modified = %s,
            modified_by = %s
        WHERE name IN ({placeholders})
        """,
        [now_datetime(), frappe.session.user, *names],
    )
    _invalidate(subjects)


def _owner_filters(user, guest_id) -> Optional[Dict[str, Any]]:
    """The filter dict scoping a query to ONE subject, or ``None`` for neither.

    Never returns an unscoped dict: an empty filter on a bulk logout would log
    out the whole site.
    """
    if user:
        return {"user": user}
    if guest_id:
        return {"guest_id": guest_id}
    return None


def unbind_device(installation_id, *, user=None, guest_id=None) -> bool:
    """Disable ONE install's row on logout, owner-scoped.

    Returns ``False`` — a silent no-op, never an error — for an unknown install or
    a row that belongs to somebody else. Issues NO commit.
    """
    value = (installation_id or "").strip() if isinstance(installation_id, str) else ""
    if not value:
        return False

    row = frappe.db.get_value(
        DOCTYPE, {"installation_id": value}, ["name", "user", "guest_id"], as_dict=True
    )
    if not row:
        return False

    owned = (user and row.get("user") == user) or (
        guest_id and row.get("guest_id") == guest_id
    )
    if not owned:
        return False

    _logout_rows([row["name"]], [(row.get("user"), row.get("guest_id"))])
    return True


def unbind_device_by_token(token, *, user=None, guest_id=None) -> bool:
    """Disable the row holding ``token``, owner-scoped. Same no-op contract.

    For a client that knows its FCM token but not its installation id — the
    legacy shape. Keyed on ``token_hash`` (the UNIQUE column), never on the raw
    token: the hash is what the index covers, and it keeps the credential out of
    the WHERE clause a slow-query log might capture.
    """
    digest = token_hash(token)
    if not digest:
        return False

    row = frappe.db.get_value(
        DOCTYPE, {"token_hash": digest}, ["name", "user", "guest_id"], as_dict=True
    )
    if not row:
        return False

    owned = (user and row.get("user") == user) or (
        guest_id and row.get("guest_id") == guest_id
    )
    if not owned:
        return False

    _logout_rows([row["name"]], [(row.get("user"), row.get("guest_id"))])
    return True


def unbind_all_devices(*, user=None, guest_id=None) -> int:
    """Log this subject out EVERYWHERE. Returns how many rows were unbound.

    Scoped by construction: with neither subject this returns 0 rather than
    matching every row on the site — an unscoped bulk logout is the one mistake
    this function must make unrepresentable.

    Only rows that are still live (enabled, still holding a token) are touched,
    so a repeat call is a 0 rather than a second write.
    """
    filters = _owner_filters(user, guest_id)
    if filters is None:
        return 0

    rows = frappe.get_all(
        DOCTYPE,
        filters={**filters, "enabled": 1, "device_token": ["is", "set"]},
        fields=["name", "user", "guest_id"],
    )
    if not rows:
        return 0

    _logout_rows(
        [row["name"] for row in rows],
        [(row.get("user"), row.get("guest_id")) for row in rows],
    )
    return len(rows)


def get_devices(user=None, guest_id=None) -> List[Dict[str, Any]]:
    """Sendable devices for ONE subject — enabled, and still holding a token.

    A disabled or token-cleared row is not a device you can push to, so it never
    reaches the send path. Cached for an hour under a per-subject key; every
    writer here invalidates it.
    """
    if not user and not guest_id:
        return []

    cache_key = device_cache_key(user=user, guest_id=guest_id)
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    filters: Dict[str, Any] = {"enabled": 1, "device_token": ["is", "set"]}
    if user:
        filters["user"] = user
    else:
        filters["guest_id"] = guest_id

    devices = frappe.get_all(
        DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "device_token",
            "user",
            "guest_id",
            "locale",
            "platform",
            "push_permission",
            "installation_id",
        ],
        order_by="last_seen_at desc, modified desc",
        limit=MAX_DEVICES_PER_SUBJECT,
    )
    frappe.cache().set_value(
        cache_key, devices, expires_in_sec=DEVICE_CACHE_TTL_SECONDS
    )
    return devices


def _delete_devices(filters: Dict[str, Any], subject: tuple) -> int:
    names = frappe.get_all(DOCTYPE, filters=filters, pluck="name")
    for name in names:
        # delete_permanently: without it delete_doc copies the whole row —
        # token included — into tabDeleted Document, which no erasure step
        # scrubs and no policy expires.
        frappe.delete_doc(
            DOCTYPE,
            name,
            ignore_permissions=True,
            force=True,
            delete_permanently=True,
        )
    _invalidate([subject])
    return len(names)


def delete_user_devices(user) -> int:
    """Hard-delete every device row of ``user``. Returns the row count.

    The erasure lane's device step. Issues no commit — the erasure transaction
    owns it.
    """
    if not user:
        return 0
    return _delete_devices({"user": user}, (user, None))


def delete_guest_devices(guest_id) -> int:
    """Hard-delete every device row of a pre-login subject. Returns the count."""
    if not guest_id:
        return 0
    return _delete_devices({"guest_id": guest_id}, (None, guest_id))
