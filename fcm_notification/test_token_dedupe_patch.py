"""Integration tests for the ``pre_model_sync`` token dedupe + hash backfill.

The rows are inserted with raw SQL and a NULL ``token_hash`` on purpose: that is
exactly the pre-migrate state (duplicate ``device_token`` values, no hash yet),
and it is the only way to create duplicates once the UNIQUE index exists.

Run with::

    bench --site <site> run-tests --app fcm_notification \\
        --module fcm_notification.test_token_dedupe_patch
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from fcm_notification.device_registry import token_hash
from fcm_notification.patches.v0_1_0 import dedupe_user_device_tokens as patch

DOCTYPE = "User Device"
_PREFIX = "fcm-dedupe-test-"
_SHARED = f"{_PREFIX}shared-token"


def _insert_raw(name: str, token, *, days_ago: int, enabled: int = 1) -> str:
    """Insert a row the way the pre-0.1.0 app did: token only, no hash."""
    frappe.db.sql(
        """
        INSERT INTO `tabUser Device`
            (name, creation, modified, owner, modified_by,
             user, device_token, token_hash, platform, enabled)
        VALUES (%s, NOW() - INTERVAL %s DAY, NOW() - INTERVAL %s DAY,
                'Administrator', 'Administrator',
                'Administrator', %s, NULL, 'Android', %s)
        """,
        (name, days_ago, days_ago, token, enabled),
    )
    return name


class TestTokenDedupePatch(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        super().tearDown()

    def _cleanup(self):
        frappe.db.sql(
            "DELETE FROM `tabUser Device` WHERE name LIKE %s", (f"{_PREFIX}%",)
        )
        frappe.db.delete(DOCTYPE, {"device_token": ("like", f"{_PREFIX}%")})

    def _row(self, name, *fields):
        return frappe.db.get_value(DOCTYPE, name, list(fields), as_dict=True)

    # --- dedupe ------------------------------------------------------------

    def test_the_newest_row_keeps_the_token(self):
        oldest = _insert_raw(f"{_PREFIX}old", _SHARED, days_ago=30)
        middle = _insert_raw(f"{_PREFIX}mid", _SHARED, days_ago=10)
        newest = _insert_raw(f"{_PREFIX}new", _SHARED, days_ago=1)

        summary = patch.run()

        self.assertEqual(summary["rebound"], 2)
        kept = self._row(newest, "device_token", "token_hash", "enabled")
        self.assertEqual(kept.device_token, _SHARED)
        self.assertEqual(kept.token_hash, token_hash(_SHARED))
        self.assertEqual(kept.enabled, 1)

        for name in (oldest, middle):
            row = self._row(
                name, "device_token", "token_hash", "enabled", "disabled_reason"
            )
            self.assertIsNone(row.device_token, "a loser must not keep the token")
            self.assertIsNone(row.token_hash)
            self.assertEqual(row.enabled, 0)
            self.assertEqual(row.disabled_reason, "Rebound")

    def test_a_disabled_row_never_outranks_a_live_one(self):
        """The keeper is the newest ENABLED row, not simply the newest row.

        Disabling TOUCHES ``modified`` — the sweep depends on that to start the
        retention clock — so a row the sweep disabled yesterday is always
        "newer" than a live row that has merely been quiet for a month. Ordering
        on ``modified`` alone hands the token to the dead row and clears it off
        the handset that is still using it, which reads as a silent push outage
        for a real user until their app happens to re-register.
        """
        live = _insert_raw(f"{_PREFIX}live", _SHARED, days_ago=30, enabled=1)
        recently_disabled = _insert_raw(
            f"{_PREFIX}dead", _SHARED, days_ago=1, enabled=0
        )

        patch.run()

        kept = self._row(live, "device_token", "token_hash", "enabled")
        self.assertEqual(
            kept.device_token, _SHARED, "the live row must keep the token"
        )
        self.assertEqual(kept.token_hash, token_hash(_SHARED))
        self.assertEqual(kept.enabled, 1)

        loser = self._row(recently_disabled, "device_token", "token_hash")
        self.assertIsNone(loser.device_token)
        self.assertIsNone(loser.token_hash)

    def test_the_unique_index_would_accept_the_result(self):
        """The point of the patch: after it, no two rows share a token hash."""
        _insert_raw(f"{_PREFIX}a", _SHARED, days_ago=5)
        _insert_raw(f"{_PREFIX}b", _SHARED, days_ago=1)

        patch.run()

        hashes = frappe.db.sql(
            """
            SELECT token_hash, COUNT(*) AS occurrences
            FROM `tabUser Device`
            WHERE token_hash IS NOT NULL
            GROUP BY token_hash HAVING occurrences > 1
            """,
            as_dict=True,
        )
        self.assertEqual(hashes, [], f"duplicate hashes survive: {hashes}")

    def test_blank_tokens_become_null(self):
        """An empty string is not a token, and every empty row would otherwise
        hash to the same value and collide with the others."""
        blank = _insert_raw(f"{_PREFIX}blank", "", days_ago=3)

        summary = patch.run()

        self.assertGreaterEqual(summary["blanked"], 1)
        row = self._row(blank, "device_token", "token_hash")
        self.assertIsNone(row.device_token)
        self.assertIsNone(row.token_hash)

    def test_unique_tokens_are_only_backfilled(self):
        alone = _insert_raw(f"{_PREFIX}alone", f"{_PREFIX}only-mine", days_ago=2)

        summary = patch.run()

        self.assertGreaterEqual(summary["hashed"], 1)
        row = self._row(alone, "device_token", "token_hash", "enabled")
        self.assertEqual(row.device_token, f"{_PREFIX}only-mine")
        self.assertEqual(row.token_hash, token_hash(f"{_PREFIX}only-mine"))
        self.assertEqual(row.enabled, 1, "a row with a unique token is untouched")

    # --- re-runs and dry runs ---------------------------------------------

    def test_a_second_run_changes_nothing(self):
        _insert_raw(f"{_PREFIX}i1", _SHARED, days_ago=5)
        _insert_raw(f"{_PREFIX}i2", _SHARED, days_ago=1)
        patch.run()

        second = patch.run()

        self.assertEqual(second["rebound"], 0)
        self.assertEqual(second["hashed"], 0)
        self.assertEqual(second["blanked"], 0)

    def test_a_dry_run_reports_without_writing(self):
        loser = _insert_raw(f"{_PREFIX}d1", _SHARED, days_ago=5)
        _insert_raw(f"{_PREFIX}d2", _SHARED, days_ago=1)

        summary = patch.run(dry_run=True)

        self.assertEqual(summary["rebound"], 1)
        self.assertEqual(summary["hashed"], 2)
        row = self._row(loser, "device_token", "token_hash", "enabled")
        self.assertEqual(row.device_token, _SHARED, "a dry run must write nothing")
        self.assertIsNone(row.token_hash)
        self.assertEqual(row.enabled, 1)
