"""Daily ``User Device`` token-staleness sweep.

Hard-deletes long-disabled rows + soft-disables long-inactive ones so the
table stays lean and FCM dispatch fan-out doesn't waste RPC on dead
devices. The backend owns this lifecycle: mobile clients always send the
latest token, and the backend deals with cleanup.

Two cleanup passes per run:

1. **Soft-disable** rows where ``enabled=1`` AND
   ``modified < now() - token_staleness_days`` (default 90 days),
   stamping ``disabled_reason = "Stale"`` in the same UPDATE.
   Mirrors the FCM Flutter doc's recommended staleness window for tokens
   that haven't seen any feedback in a while.

2. **Hard-delete** rows where ``enabled=0`` AND
   ``modified < now() - disabled_token_retention_days`` (default 30
   days). The grace period gives a temporarily-offline device a chance
   to come back before its row is permanently dropped.

   Deliberately **no** ``disabled_reason`` clause: retention applies to
   every disabled row whatever disabled it — a dead token, this sweep, or
   ``disable_user_devices``. So a row soft-disabled by an account disable
   is still hard-deleted once retention passes, and the device simply
   re-registers on the client's next launch.

Order doesn't matter for correctness (a just-soft-disabled row has
``modified=now`` so it can't satisfy the hard-delete cutoff in the same
run), but soft-first keeps the log line's recent-action ordering
predictable.

Each pass handles at most ``_MAX_ROWS_PER_RUN`` rows, oldest first; a
backlog drains over consecutive runs.

Registered via ``hooks.py`` ``scheduler_events.daily``. Honors
``FCM Notification Settings.token_sweep_disabled`` as a kill switch.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from fcm_notification.send_notification import invalidate_user_devices_cache

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_STALENESS_DAYS = 90

# ponytail: per-run cap instead of batching-with-commits. A "Daily" job runs on
# the `default` queue, whose RQ timeout is 300s — an uncapped first run against
# a large backlog would be killed mid-loop and, with no commit inside the loop,
# roll back entirely: zero progress, repeated every night. Capped, each run
# makes durable progress and the backlog drains over days, which is harmless
# because the rows involved are already disabled and excluded from sends.
# Upgrade path if draining is too slow: raise this, or move the job to a
# "Daily Long" frequency (1500s queue) and batch with a commit per batch.
_MAX_ROWS_PER_RUN = 5000


def _read_settings() -> tuple[bool, int, int]:
    """Return ``(enabled, retention_days, staleness_days)`` from FCM Notification Settings.

    Every field falls back to its documented default when unset, which is
    the normal state on a site whose Single was last saved before these
    fields existed: Frappe reads a missing ``tabSingles`` row as ``0``, so a
    DocField ``default`` never reaches it.

    That is also why the kill switch is stored opt-*out*
    (``token_sweep_disabled``) rather than ``..._enabled`` — an unset field
    reads falsy, so the sweep keeps running instead of silently switching
    itself off the first time the app is upgraded.
    """
    settings = frappe.get_cached_doc("FCM Notification Settings")
    enabled = not cint(settings.get("token_sweep_disabled"))
    retention_days = (
        cint(settings.get("disabled_token_retention_days")) or _DEFAULT_RETENTION_DAYS
    )
    staleness_days = (
        cint(settings.get("token_staleness_days")) or _DEFAULT_STALENESS_DAYS
    )
    return enabled, retention_days, staleness_days


def _soft_disable_stale_tokens(staleness_days: int) -> int:
    """Flip ``enabled=0`` on rows that have been silent past the staleness window.

    Stamps ``disabled_reason = "Stale"`` in the same UPDATE, so every row
    this pass disables carries the reason it was disabled for.

    Touches ``modified`` so the row enters the retention window for the
    next sweep's hard-delete pass. Issues a single bulk ``frappe.db.set_value``
    call (filter-dict form) instead of one call per row, reducing N+1 DB
    round-trips to 1. After the bulk update the per-user device cache is
    explicitly invalidated for every affected user because ``db.set_value``
    does not fire document hooks (including the ``User Device`` cache
    invalidation hooks).

    Capped at ``_MAX_ROWS_PER_RUN``, oldest first — which also keeps the
    ``IN`` list of the bulk UPDATE to a sane size.
    """
    cutoff = add_to_date(now_datetime(), days=-staleness_days)
    candidates = frappe.get_all(
        "User Device",
        filters={"enabled": 1, "modified": ["<", cutoff]},
        fields=["name", "user"],
        order_by="modified asc",
        limit=_MAX_ROWS_PER_RUN,
    )
    if not candidates:
        return 0

    stale_names = [row["name"] for row in candidates]
    # Single bulk UPDATE — replaces N individual set_value calls. Both
    # columns go in the one call; a second set_value for the reason would
    # reintroduce the round-trip this form exists to remove.
    frappe.db.set_value(
        "User Device",
        {"name": ["in", stale_names]},
        {"enabled": 0, "disabled_reason": "Stale"},
    )

    # Invalidate the per-user device cache for every affected user.
    # db.set_value fires no document hooks, so invalidation must be explicit.
    affected_users: set[str] = {row["user"] for row in candidates if row.get("user")}
    for user in affected_users:
        invalidate_user_devices_cache(user)

    return len(candidates)


def _hard_delete_disabled_tokens(retention_days: int) -> int:
    """Hard-delete rows that have been disabled past the retention window.

    ``delete_permanently=True`` is what makes this actually reclaim space:
    without it ``frappe.delete_doc`` copies every row into ``tabDeleted
    Document`` as a full JSON blob, which has no default retention — the job
    would relocate the table rather than prune it. Device rows carry no
    attachments and are re-registered by the client on next launch, so there
    is nothing to restore.

    The ``on_trash`` hook already invalidates the per-user device cache row by
    row; the pass below repeats it once per user *after* the deletes so a
    concurrent read can't repopulate the cache with a row that is about to
    disappear.

    Capped at ``_MAX_ROWS_PER_RUN``, oldest first.
    """
    cutoff = add_to_date(now_datetime(), days=-retention_days)
    candidates = frappe.get_all(
        "User Device",
        filters={"enabled": 0, "modified": ["<", cutoff]},
        fields=["name", "user"],
        order_by="modified asc",
        limit=_MAX_ROWS_PER_RUN,
    )
    if not candidates:
        return 0

    affected_users: set[str] = set()
    for row in candidates:
        frappe.delete_doc(
            "User Device",
            row["name"],
            ignore_permissions=True,
            force=True,
            delete_permanently=True,
        )
        if row.get("user"):
            affected_users.add(row["user"])

    for user in affected_users:
        invalidate_user_devices_cache(user)

    return len(candidates)


def run() -> dict[str, int | bool]:
    """Daily scheduler entry point.

    Returns a small summary dict for tests + observability; the cron caller
    discards the return value. Counts are also logged via
    ``frappe.logger().info``::

        FCM token sweep: hard-deleted N, soft-disabled M

    Honors ``FCM Notification Settings.token_sweep_disabled``: when checked,
    the function logs a "skipped" line and returns without touching any rows.
    """
    enabled, retention_days, staleness_days = _read_settings()
    if not enabled:
        frappe.logger().info(
            "FCM token sweep: skipped (disabled in FCM Notification Settings)"
        )
        return {"hard_deleted": 0, "soft_disabled": 0, "skipped": True}

    soft_count = _soft_disable_stale_tokens(staleness_days)
    hard_count = _hard_delete_disabled_tokens(retention_days)

    frappe.logger().info(
        f"FCM token sweep: hard-deleted {hard_count}, soft-disabled {soft_count}"
    )
    return {"hard_deleted": hard_count, "soft_disabled": soft_count, "skipped": False}
