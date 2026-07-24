"""Integration tests for the daily ``User Device`` token-staleness sweep.

Covers:

- Disabled row past retention window → hard-deleted.
- Enabled row past staleness window → soft-disabled (not yet deleted).
- Disabled row within retention window → preserved.
- Enabled row within staleness window → preserved.
- ``token_sweep_disabled = 1`` short-circuits the run.
- Unset settings (a Single saved before these fields existed) fall back to
  the documented defaults *with the sweep on*.
- The soft-disable pass issues one bulk UPDATE and invalidates the device
  cache once per distinct user.
- Counts logged via ``frappe.logger().info`` in the documented format.

Each test seeds + cleans up its own ``User Device`` rows. Historical
``modified`` timestamps are written via ``frappe.db.set_value(...,
update_modified=False)`` since the framework auto-bumps ``modified`` on
every save.
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from fcm_notification import token_sweep

_TEST_USER = "Administrator"
_TEST_TOKEN_PREFIX = "fcm-sweep-test-"
_SETTINGS = "FCM Notification Settings"


def _insert_user_device(token_suffix: str, *, enabled: bool, days_ago: int) -> str:
    """Insert a ``User Device`` row with a back-dated ``modified`` timestamp.

    Returns the row's ``name`` for assertions / cleanup.
    """
    doc = frappe.get_doc(
        {
            "doctype": "User Device",
            "user": _TEST_USER,
            "device_token": f"{_TEST_TOKEN_PREFIX}{token_suffix}",
            "platform": "Android",
            "enabled": 1 if enabled else 0,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()

    # Back-date `modified`. update_modified=False prevents the framework
    # from re-stamping it during the write.
    if days_ago > 0:
        backdate = add_to_date(now_datetime(), days=-days_ago)
        frappe.db.set_value(
            "User Device", doc.name, "modified", backdate, update_modified=False
        )
    return doc.name


def _cleanup_test_rows() -> None:
    frappe.db.delete(
        "User Device",
        {"device_token": ("like", f"{_TEST_TOKEN_PREFIX}%")},
    )


def _set_sweep_settings(*, disabled: int, retention: int, staleness: int) -> None:
    frappe.db.set_single_value(
        _SETTINGS,
        {
            "token_sweep_disabled": disabled,
            "disabled_token_retention_days": retention,
            "token_staleness_days": staleness,
        },
    )
    frappe.clear_document_cache(_SETTINGS)


class TestTokenSweep(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        _cleanup_test_rows()
        # Pin the documented defaults (on / 30 / 90) so the assertions below
        # don't depend on whatever the site has configured.
        _set_sweep_settings(disabled=0, retention=30, staleness=90)
        frappe.db.commit()

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        _cleanup_test_rows()
        frappe.clear_document_cache(_SETTINGS)

    def tearDown(self):
        _cleanup_test_rows()
        super().tearDown()

    # --- per-row behaviour ------------------------------------------------

    def test_disabled_row_past_retention_is_hard_deleted(self):
        name = _insert_user_device("hard-delete", enabled=False, days_ago=60)
        self.assertTrue(frappe.db.exists("User Device", name))

        result = token_sweep.run()

        self.assertGreaterEqual(result["hard_deleted"], 1)
        self.assertFalse(frappe.db.exists("User Device", name))

    def test_enabled_row_past_staleness_is_soft_disabled(self):
        name = _insert_user_device("soft-disable", enabled=True, days_ago=120)
        self.assertEqual(frappe.db.get_value("User Device", name, "enabled"), 1)

        result = token_sweep.run()

        self.assertGreaterEqual(result["soft_disabled"], 1)
        # Row still exists (would land in a future sweep's hard-delete
        # window once retention_days have passed).
        self.assertTrue(frappe.db.exists("User Device", name))
        self.assertEqual(frappe.db.get_value("User Device", name, "enabled"), 0)

    def test_disabled_row_within_retention_is_preserved(self):
        name = _insert_user_device("retain-disabled", enabled=False, days_ago=5)

        token_sweep.run()

        self.assertTrue(frappe.db.exists("User Device", name),
            "Rows disabled within the retention window must NOT be deleted.")

    def test_enabled_row_within_staleness_is_preserved(self):
        name = _insert_user_device("retain-enabled", enabled=True, days_ago=30)

        token_sweep.run()

        self.assertTrue(frappe.db.exists("User Device", name))
        self.assertEqual(frappe.db.get_value("User Device", name, "enabled"), 1,
            "Rows active within the staleness window must NOT be soft-disabled.")

    # --- settings ---------------------------------------------------------

    def test_kill_switch_short_circuits_the_run(self):
        # Stage rows that WOULD be acted on if the sweep ran.
        del_name = _insert_user_device("kill-del", enabled=False, days_ago=60)
        soft_name = _insert_user_device("kill-soft", enabled=True, days_ago=120)

        frappe.db.set_single_value(_SETTINGS, "token_sweep_disabled", 1)
        frappe.clear_document_cache(_SETTINGS)
        try:
            result = token_sweep.run()
        finally:
            frappe.db.set_single_value(_SETTINGS, "token_sweep_disabled", 0)
            frappe.clear_document_cache(_SETTINGS)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["hard_deleted"], 0)
        self.assertEqual(result["soft_disabled"], 0)
        # Both staged rows are untouched.
        self.assertTrue(frappe.db.exists("User Device", del_name))
        self.assertEqual(frappe.db.get_value("User Device", soft_name, "enabled"), 1)

    def test_unset_settings_keep_the_sweep_on_with_documented_defaults(self):
        """A Single saved before these fields existed has no ``tabSingles`` row
        for them, and Frappe reads that as ``0`` — never the DocField default.

        The opt-*out* kill switch is what makes that safe: unset must read as
        "sweep on, 30 / 90", not "sweep silently off".
        """
        unset = frappe._dict(
            {
                "token_sweep_disabled": 0,
                "disabled_token_retention_days": 0,
                "token_staleness_days": 0,
            }
        )
        with mock.patch(
            "fcm_notification.token_sweep.frappe.get_cached_doc", return_value=unset
        ):
            self.assertEqual(token_sweep._read_settings(), (True, 30, 90))

    # --- bulk set_value + cache invalidation ------------------------------

    def test_soft_disable_issues_single_bulk_set_value(self):
        """N stale tokens must trigger exactly ONE ``frappe.db.set_value`` call
        (the bulk filter-dict form), not one call per row."""
        _insert_user_device("bulk-a", enabled=True, days_ago=120)
        _insert_user_device("bulk-b", enabled=True, days_ago=120)
        _insert_user_device("bulk-c", enabled=True, days_ago=120)

        original_set_value = frappe.db.set_value
        set_value_calls: list[tuple] = []

        def capturing_set_value(doctype, name_or_filter, *args, **kwargs):
            if doctype == "User Device":
                set_value_calls.append((doctype, name_or_filter, args, kwargs))
            return original_set_value(doctype, name_or_filter, *args, **kwargs)

        with (
            mock.patch("frappe.db.set_value", side_effect=capturing_set_value),
            mock.patch("fcm_notification.token_sweep.invalidate_user_devices_cache"),
        ):
            token_sweep._soft_disable_stale_tokens(90)

        user_device_calls = [c for c in set_value_calls if c[0] == "User Device"]
        self.assertEqual(
            len(user_device_calls),
            1,
            f"Expected 1 bulk set_value call for User Device, got {len(user_device_calls)}."
            " Each extra call is an N+1 regression.",
        )
        # The single call must use the filter-dict form (name_or_filter is a dict).
        _, name_or_filter, _, _ = user_device_calls[0]
        self.assertIsInstance(
            name_or_filter,
            dict,
            "The bulk set_value call must use a filter dict, not a bare row name.",
        )

    def test_soft_disable_invalidates_cache_per_distinct_user(self):
        """After the bulk soft-disable, ``invalidate_user_devices_cache`` must be
        called once per *distinct* affected user (not once per token row)."""
        _insert_user_device("cache-a", enabled=True, days_ago=120)
        _insert_user_device("cache-b", enabled=True, days_ago=120)

        with mock.patch(
            "fcm_notification.token_sweep.invalidate_user_devices_cache"
        ) as mock_invalidate:
            token_sweep._soft_disable_stale_tokens(90)

        # Both tokens belong to _TEST_USER (Administrator). Cache must be
        # invalidated exactly once for that user, not once per token row.
        self.assertEqual(
            mock_invalidate.call_count,
            1,
            f"Expected 1 invalidate_user_devices_cache call for 1 distinct user, "
            f"got {mock_invalidate.call_count}.",
        )
        mock_invalidate.assert_called_once_with(_TEST_USER)

    def test_soft_disable_no_candidates_skips_mutation_and_cache(self):
        """With NO qualifying rows, the ``if not candidates`` guard must
        short-circuit before any DB mutation or cache invalidation.

        ``frappe.get_all`` is stubbed to ``[]`` so the assertion isolates the
        guard regardless of any stray ``User Device`` rows on the test site.
        """
        with (
            mock.patch(
                "fcm_notification.token_sweep.frappe.get_all",
                return_value=[],
            ),
            mock.patch(
                "frappe.db.set_value",
                wraps=frappe.db.set_value,
            ) as mock_set_value,
            mock.patch(
                "fcm_notification.token_sweep.invalidate_user_devices_cache"
            ) as mock_invalidate,
        ):
            count = token_sweep._soft_disable_stale_tokens(90)

        self.assertEqual(count, 0)
        # No User Device mutation issued (the guard returned before set_value).
        user_device_set_value_calls = [
            c for c in mock_set_value.call_args_list if c.args and c.args[0] == "User Device"
        ]
        self.assertEqual(
            len(user_device_set_value_calls),
            0,
            "With zero candidates, no User Device set_value call should fire.",
        )
        # Cache invalidation must NOT run for an empty candidate set.
        self.assertEqual(
            mock_invalidate.call_count,
            0,
            "With zero candidates, invalidate_user_devices_cache must not be called.",
        )

    # --- logging ----------------------------------------------------------

    def test_run_logs_count_summary_in_documented_format(self):
        _insert_user_device("log-del", enabled=False, days_ago=60)
        _insert_user_device("log-soft", enabled=True, days_ago=120)

        with mock.patch("fcm_notification.token_sweep.frappe.logger") as mock_logger:
            mock_logger.return_value = mock.MagicMock()
            token_sweep.run()

        # frappe.logger() is a factory; it returns a logger and we call
        # .info() on it. Inspect the chained call.
        info_calls = mock_logger.return_value.info.call_args_list
        self.assertGreater(len(info_calls), 0)
        msg = info_calls[-1].args[0]
        # Exact format: "FCM token sweep: hard-deleted N, soft-disabled M"
        self.assertRegex(
            msg,
            r"^FCM token sweep: hard-deleted \d+, soft-disabled \d+$",
        )
