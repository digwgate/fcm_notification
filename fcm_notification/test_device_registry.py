"""Integration tests for the install-id keyed ``User Device`` registry.

Every path here is DB-level on purpose — the unique indexes, the raw-SQL token
clear, the insert-and-catch race, the hard deletes and the "issues no commit"
promise cannot be observed without a real table.

Run with::

    bench --site <site> run-tests --app fcm_notification \\
        --module fcm_notification.test_device_registry

(``bench run-tests`` prints its results to STDERR — read both streams, and check
a ``Ran N tests`` line actually appeared before reading silence as success.)
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from fcm_notification.device_registry import (
    _is_unique_violation,
    delete_guest_devices,
    delete_user_devices,
    get_devices,
    register_device,
    token_hash,
    unbind_all_devices,
    unbind_device,
    unbind_device_by_token,
)
from fcm_notification.fcm_notification.doctype.user_device.user_device import (
    handle_user_device,
)
from fcm_notification.send_notification import device_cache_key

DOCTYPE = "User Device"
_USER = "Administrator"
_OTHER_USER = "Guest"
_GUEST = "fcm-test-guest-1"
_PREFIX = "fcm-registry-test-"
_INSTALL_A = "fcm-registry-install-aaa"
_INSTALL_B = "fcm-registry-install-bbb"


def _token(suffix: str) -> str:
    return f"{_PREFIX}{suffix}"


def _row(name: str, *fields) -> dict:
    return frappe.db.get_value(DOCTYPE, name, list(fields), as_dict=True)


def _purge_committed() -> None:
    """Delete this module's rows AND COMMIT — for the ones a test made durable."""
    frappe.db.delete(DOCTYPE, {"device_token": ("like", f"{_PREFIX}%")})
    frappe.db.delete(DOCTYPE, {"installation_id": ("in", [_INSTALL_A, _INSTALL_B])})
    frappe.db.commit()


class TestDeviceRegistry(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _purge_committed()

    @classmethod
    def tearDownClass(cls):
        # The legacy path (handle_user_device) COMMITS by design, so its rows
        # outlive the test transaction. The instance _cleanup() below deletes
        # them inside that transaction and IntegrationTestCase.tearDown then
        # rolls the delete back — the cleanup undoes itself and the row is left
        # on the site for good. This runs after the rollback and commits, so
        # durable debris is actually reclaimed.
        _purge_committed()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        super().tearDown()

    def _cleanup(self):
        frappe.db.delete(DOCTYPE, {"device_token": ("like", f"{_PREFIX}%")})
        frappe.db.delete(DOCTYPE, {"installation_id": ("in", [_INSTALL_A, _INSTALL_B])})
        frappe.db.delete(DOCTYPE, {"guest_id": _GUEST})
        frappe.cache().delete_value(device_cache_key(user=_USER))
        frappe.cache().delete_value(device_cache_key(guest_id=_GUEST))
        frappe.cache().delete_value(f"user_devices:{_USER}")

    # --- upsert ------------------------------------------------------------

    def test_register_device_inserts_the_row_and_maps_the_wire_values(self):
        result = register_device(
            _INSTALL_A,
            _token("a"),
            "android",
            user=_USER,
            locale="ar",
            app_version="1.2.3",
            os_version="14",
            device_model="Pixel 8",
            push_permission="granted",
        )

        self.assertFalse(result["rebound"])
        row = _row(
            result["name"],
            "platform",
            "locale",
            "push_permission",
            "token_hash",
            "enabled",
            "last_seen_at",
            "version",
            "version_release",
            "model",
        )
        self.assertEqual(row.platform, "Android", "lowercase wire value must map")
        self.assertEqual(row.locale, "ar")
        self.assertEqual(row.push_permission, "granted")
        self.assertEqual(row.token_hash, token_hash(_token("a")))
        self.assertEqual(row.enabled, 1)
        self.assertIsNotNone(row.last_seen_at)
        # The three device facts land on the columns the shared doctype already
        # has — no duplicate app/OS/model columns.
        self.assertEqual(row.version, "1.2.3")
        self.assertEqual(row.version_release, "14")
        self.assertEqual(row.model, "Pixel 8")

    def test_ios_os_version_lands_on_the_ios_column(self):
        result = register_device(
            _INSTALL_A, _token("ios"), "ios", user=_USER, os_version="17.4"
        )

        row = _row(result["name"], "platform", "system_version", "version_release")
        self.assertEqual(row.platform, "IOS")
        self.assertEqual(row.system_version, "17.4")
        self.assertIsNone(row.version_release)

    def test_re_registering_the_same_install_updates_one_row(self):
        first = register_device(_INSTALL_A, _token("a"), "android", user=_USER)
        second = register_device(
            _INSTALL_A, _token("a2"), "android", user=_USER, locale="en"
        )

        self.assertEqual(first["name"], second["name"])
        self.assertEqual(frappe.db.count(DOCTYPE, {"installation_id": _INSTALL_A}), 1)
        row = _row(second["name"], "device_token", "token_hash", "locale")
        self.assertEqual(row.device_token, _token("a2"))
        self.assertEqual(row.token_hash, token_hash(_token("a2")))
        self.assertEqual(row.locale, "en")

    def test_platform_is_never_resent_on_an_upsert(self):
        """``platform`` is ``set_only_once``: re-sending a different value through
        the ORM would raise, so the upsert must not send it at all."""
        first = register_device(_INSTALL_A, _token("a"), "android", user=_USER)

        second = register_device(_INSTALL_A, _token("a"), "ios", user=_USER)

        self.assertEqual(first["name"], second["name"])
        self.assertEqual(_row(second["name"], "platform").platform, "Android")

    def test_re_registering_a_disabled_row_brings_it_back(self):
        first = register_device(_INSTALL_A, _token("a"), "android", user=_USER)
        unbind_device(_INSTALL_A, user=_USER)

        second = register_device(_INSTALL_A, _token("a"), "android", user=_USER)

        self.assertEqual(first["name"], second["name"])
        row = _row(second["name"], "enabled", "disabled_reason", "device_token")
        self.assertEqual(row.enabled, 1)
        self.assertFalse(row.disabled_reason)
        self.assertEqual(row.device_token, _token("a"))

    def test_a_guest_row_binds_to_the_user_at_login(self):
        guest = register_device(_INSTALL_A, _token("a"), "android", guest_id=_GUEST)

        bound = register_device(_INSTALL_A, _token("a"), "android", user=_USER)

        self.assertEqual(guest["name"], bound["name"])
        row = _row(bound["name"], "user", "guest_id")
        self.assertEqual(row.user, _USER)
        self.assertIsNone(row.guest_id, "the guest binding must not linger")

    # --- rebind ------------------------------------------------------------

    def test_a_token_held_by_another_install_is_taken_away_from_it(self):
        """The leak this whole change exists for: the previous row must stop
        being a live target the moment the token moves."""
        first = register_device(_INSTALL_A, _token("shared"), "android", user=_USER)

        second = register_device(
            _INSTALL_B, _token("shared"), "android", user=_OTHER_USER
        )

        self.assertTrue(second["rebound"])
        self.assertNotEqual(first["name"], second["name"])
        old = _row(
            first["name"], "enabled", "device_token", "token_hash", "disabled_reason"
        )
        self.assertEqual(old.enabled, 0)
        self.assertIsNone(old.device_token, "reqd or not, the token must be cleared")
        self.assertIsNone(old.token_hash)
        self.assertEqual(old.disabled_reason, "Rebound")
        self.assertEqual(
            _row(second["name"], "device_token").device_token, _token("shared")
        )

    def test_registering_an_unheld_token_is_not_a_rebind(self):
        result = register_device(_INSTALL_A, _token("fresh"), "android", user=_USER)
        self.assertFalse(result["rebound"])

    # --- indexes and races -------------------------------------------------

    def test_token_hash_is_unique_across_rows(self):
        register_device(_INSTALL_A, _token("unique"), "android", user=_USER)

        doc = frappe.get_doc(
            {
                "doctype": DOCTYPE,
                "user": _USER,
                "device_token": _token("unique"),
                "platform": "Android",
            }
        )
        with self.assertRaises(frappe.UniqueValidationError):
            doc.insert(ignore_permissions=True)

    def test_installation_id_is_unique_but_legacy_null_rows_do_not_collide(self):
        register_device(_INSTALL_A, _token("u1"), "android", user=_USER)

        # Two legacy rows with no installation id: Frappe stores an empty unique
        # Data field as NULL, and NULLs never collide.
        for suffix in ("legacy-1", "legacy-2"):
            frappe.get_doc(
                {
                    "doctype": DOCTYPE,
                    "user": _USER,
                    "device_token": _token(suffix),
                    "platform": "Android",
                }
            ).insert(ignore_permissions=True)

        self.assertEqual(
            frappe.db.count(DOCTYPE, {"device_token": ("like", f"{_PREFIX}legacy-%")}),
            2,
        )

        clash = frappe.get_doc(
            {
                "doctype": DOCTYPE,
                "user": _USER,
                "installation_id": _INSTALL_A,
                "device_token": _token("u2"),
                "platform": "Android",
            }
        )
        with self.assertRaises(frappe.UniqueValidationError):
            clash.insert(ignore_permissions=True)

    def test_unique_guard_accepts_both_exception_kinds(self):
        """``doc.insert()`` re-raises as ``UniqueValidationError``; raw SQL
        surfaces the driver's own 1062. A guard that knows only one is dead
        code that reads as careful."""
        self.assertTrue(_is_unique_violation(frappe.UniqueValidationError("dup")))

        register_device(_INSTALL_A, _token("driver"), "android", user=_USER)
        raw_error = None
        try:
            frappe.db.sql(
                """
                INSERT INTO `tabUser Device`
                    (name, creation, modified, owner, modified_by,
                     installation_id, device_token, platform)
                VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, %s)
                """,
                (
                    "FCM-RACE-ROW",
                    _USER,
                    _USER,
                    _INSTALL_A,
                    _token("driver-2"),
                    "Android",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the driver's own error class
            raw_error = exc

        self.assertIsNotNone(raw_error, "the unique index did not fire")
        self.assertTrue(_is_unique_violation(raw_error))
        self.assertFalse(isinstance(raw_error, frappe.UniqueValidationError))

    def test_a_lost_insert_race_falls_through_to_the_update(self):
        """Force the race: the pre-read misses, the insert hits the index, and
        the caller still ends up with ONE row carrying the new token."""
        existing = register_device(_INSTALL_A, _token("race"), "android", user=_USER)

        real_get_value = frappe.db.get_value
        state = {"first": True}

        def missing_once(doctype, filters=None, fieldname="name", *args, **kwargs):
            if (
                state["first"]
                and doctype == DOCTYPE
                and filters == {"installation_id": _INSTALL_A}
            ):
                state["first"] = False
                return None
            return real_get_value(doctype, filters, fieldname, *args, **kwargs)

        with mock.patch.object(frappe.db, "get_value", side_effect=missing_once):
            result = register_device(
                _INSTALL_A, _token("race-2"), "android", user=_USER
            )

        self.assertEqual(result["name"], existing["name"])
        self.assertEqual(frappe.db.count(DOCTYPE, {"installation_id": _INSTALL_A}), 1)
        self.assertEqual(
            _row(existing["name"], "device_token").device_token, _token("race-2")
        )

    # --- transaction ownership --------------------------------------------

    def test_the_new_apis_issue_no_commit(self):
        """A commit inside these would durably commit whatever the caller had
        open — the whole reason the BFF endpoint does not call the legacy path."""
        with mock.patch.object(frappe.db, "commit") as commit:
            result = register_device(_INSTALL_A, _token("nc"), "android", user=_USER)
            register_device(_INSTALL_B, _token("nc"), "android", user=_USER)
            get_devices(user=_USER)
            unbind_device(_INSTALL_B, user=_USER)
            delete_user_devices(_USER)
            commit.assert_not_called()
        self.assertTrue(result["name"])

    # --- unbind ------------------------------------------------------------

    def test_unbind_disables_and_clears_the_token(self):
        registered = register_device(_INSTALL_A, _token("out"), "android", user=_USER)

        self.assertTrue(unbind_device(_INSTALL_A, user=_USER))

        row = _row(
            registered["name"],
            "enabled",
            "disabled_reason",
            "device_token",
            "token_hash",
        )
        self.assertEqual(row.enabled, 0)
        self.assertEqual(row.disabled_reason, "Logged Out")
        self.assertIsNone(row.device_token)
        self.assertIsNone(row.token_hash)

    def test_unbind_ignores_a_row_owned_by_someone_else(self):
        registered = register_device(_INSTALL_A, _token("mine"), "android", user=_USER)

        self.assertFalse(unbind_device(_INSTALL_A, user=_OTHER_USER))
        self.assertEqual(_row(registered["name"], "enabled").enabled, 1)

    def test_unbind_of_an_unknown_install_is_a_silent_no_op(self):
        self.assertFalse(unbind_device("fcm-registry-install-zzz", user=_USER))
        self.assertFalse(unbind_device("", user=_USER))
        self.assertFalse(unbind_device(None, user=_USER))

    def test_unbind_of_a_guest_row_is_guest_scoped(self):
        register_device(_INSTALL_A, _token("g"), "android", guest_id=_GUEST)

        self.assertFalse(unbind_device(_INSTALL_A, guest_id="somebody-else"))
        self.assertTrue(unbind_device(_INSTALL_A, guest_id=_GUEST))

    # --- unbind by token / unbind everywhere --------------------------------

    def test_unbind_by_token_disables_the_row_holding_it(self):
        registered = register_device(_INSTALL_A, _token("bytok"), "android", user=_USER)

        self.assertTrue(unbind_device_by_token(_token("bytok"), user=_USER))

        row = _row(registered["name"], "enabled", "device_token", "token_hash")
        self.assertEqual(row.enabled, 0)
        self.assertIsNone(row.device_token)
        self.assertIsNone(row.token_hash)

    def test_unbind_by_token_is_owner_scoped(self):
        registered = register_device(_INSTALL_A, _token("theirs"), "android", user=_USER)

        self.assertFalse(unbind_device_by_token(_token("theirs"), user=_OTHER_USER))
        self.assertEqual(_row(registered["name"], "enabled").enabled, 1)

    def test_unbind_by_an_unknown_or_empty_token_is_a_silent_no_op(self):
        self.assertFalse(unbind_device_by_token(_token("never-registered"), user=_USER))
        self.assertFalse(unbind_device_by_token("", user=_USER))
        self.assertFalse(unbind_device_by_token(None, user=_USER))

    def test_unbind_all_logs_this_subject_out_everywhere(self):
        a = register_device(_INSTALL_A, _token("all-1"), "android", user=_USER)
        b = register_device(_INSTALL_B, _token("all-2"), "ios", user=_USER)

        self.assertEqual(unbind_all_devices(user=_USER), 2)

        for registered in (a, b):
            row = _row(registered["name"], "enabled", "device_token", "disabled_reason")
            self.assertEqual(row.enabled, 0)
            self.assertIsNone(row.device_token)
            self.assertEqual(row.disabled_reason, "Logged Out")
        self.assertEqual(get_devices(user=_USER), [])

    def test_unbind_all_never_reaches_another_subject(self):
        mine = register_device(_INSTALL_A, _token("mine-all"), "android", user=_USER)
        theirs = register_device(_INSTALL_B, _token("theirs-all"), "android", guest_id=_GUEST)

        self.assertEqual(unbind_all_devices(user=_USER), 1)

        self.assertEqual(_row(mine["name"], "enabled").enabled, 0)
        self.assertEqual(
            _row(theirs["name"], "enabled").enabled, 1, "another subject must be untouched"
        )

    def test_unbind_all_without_a_subject_touches_nothing(self):
        """The one mistake this must make unrepresentable.

        An unscoped bulk logout would empty the whole site's registry, so a call
        with neither subject has to be a 0 and not a match-everything filter.
        """
        registered = register_device(_INSTALL_A, _token("safe"), "android", user=_USER)

        self.assertEqual(unbind_all_devices(), 0)
        self.assertEqual(unbind_all_devices(user=None, guest_id=None), 0)
        self.assertEqual(unbind_all_devices(user="", guest_id=""), 0)

        self.assertEqual(_row(registered["name"], "enabled").enabled, 1)

    def test_unbind_all_is_idempotent(self):
        register_device(_INSTALL_A, _token("twice"), "android", user=_USER)

        self.assertEqual(unbind_all_devices(user=_USER), 1)
        self.assertEqual(unbind_all_devices(user=_USER), 0, "already-out rows are not re-written")

    # --- reads -------------------------------------------------------------

    def test_get_devices_returns_only_sendable_rows(self):
        register_device(_INSTALL_A, _token("live"), "android", user=_USER)
        register_device(_INSTALL_B, _token("dead"), "android", user=_USER)
        unbind_device(_INSTALL_B, user=_USER)
        frappe.cache().delete_value(device_cache_key(user=_USER))

        tokens = [device["device_token"] for device in get_devices(user=_USER)]

        self.assertIn(_token("live"), tokens)
        self.assertNotIn(_token("dead"), tokens)
        self.assertNotIn(None, tokens)

    def test_get_devices_reaches_guest_rows(self):
        register_device(_INSTALL_A, _token("guest"), "android", guest_id=_GUEST)
        frappe.cache().delete_value(device_cache_key(guest_id=_GUEST))

        devices = get_devices(guest_id=_GUEST)

        self.assertEqual([d["device_token"] for d in devices], [_token("guest")])
        self.assertEqual(get_devices(user=None, guest_id=None), [])

    def test_get_devices_caches_per_subject_and_is_invalidated_on_write(self):
        register_device(_INSTALL_A, _token("cache"), "android", user=_USER)
        get_devices(user=_USER)
        self.assertIsNotNone(frappe.cache().get_value(device_cache_key(user=_USER)))

        unbind_device(_INSTALL_A, user=_USER)

        self.assertIsNone(
            frappe.cache().get_value(device_cache_key(user=_USER)),
            "a write must not leave a stale device list behind",
        )

    # --- erasure -----------------------------------------------------------

    def test_delete_user_devices_hard_deletes_without_an_archive(self):
        registered = register_device(_INSTALL_A, _token("del"), "android", user=_USER)

        deleted = delete_user_devices(_USER)

        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(frappe.db.exists(DOCTYPE, registered["name"]))
        self.assertFalse(
            frappe.db.exists(
                "Deleted Document",
                {"deleted_doctype": DOCTYPE, "deleted_name": registered["name"]},
            ),
            "an 'erased' device must not survive as a Deleted Document blob",
        )

    def test_delete_guest_devices_reaches_rows_a_user_query_cannot(self):
        registered = register_device(
            _INSTALL_A, _token("gdel"), "android", guest_id=_GUEST
        )

        self.assertEqual(delete_user_devices(_USER), 0)
        self.assertEqual(delete_guest_devices(_GUEST), 1)
        self.assertFalse(frappe.db.exists(DOCTYPE, registered["name"]))

    def test_delete_helpers_ignore_an_empty_subject(self):
        self.assertEqual(delete_user_devices(None), 0)
        self.assertEqual(delete_guest_devices(""), 0)

    # --- validation --------------------------------------------------------

    def test_invalid_input_raises_validation_error(self):
        cases = [
            {"installation_id": "short"},
            {"installation_id": "has spaces and is long enough"},
            {"platform": "symbian"},
            {"locale": "fr"},
            {"push_permission": "maybe"},
            {"token": "  "},
        ]
        for case in cases:
            payload = {
                "installation_id": _INSTALL_A,
                "token": _token("v"),
                "platform": "android",
                "user": _USER,
            }
            payload.update(case)
            with self.subTest(case=case):
                with self.assertRaises(frappe.ValidationError):
                    register_device(
                        payload.pop("installation_id"),
                        payload.pop("token"),
                        payload.pop("platform"),
                        **payload,
                    )

    # --- legacy path (regression R1) ---------------------------------------

    def test_legacy_registration_rebinds_the_row_instead_of_inserting(self):
        """The sibling product's clients keep working: a token another row holds
        is MOVED to the caller, not inserted a second time (which the unique
        index would now reject with a 500)."""
        # Seed the OTHER user's row directly. It used to be seeded by passing
        # "user" to handle_user_device itself — which only worked because the
        # endpoint honoured a caller-supplied user, the cross-account hole this
        # suite now pins shut two tests below. A regression fixture must not be
        # built out of the bug it is meant to survive.
        original = register_device(
            _INSTALL_B, _token("legacy"), "android", user=_OTHER_USER
        )["name"]
        self.assertEqual(_row(original, "user").user, _OTHER_USER)

        rebound = handle_user_device(
            {"device_token": _token("legacy"), "platform": "Android"}
        )

        self.assertEqual(rebound.name, original, "the same ROW must be re-bound")
        self.assertEqual(
            frappe.db.count(DOCTYPE, {"device_token": _token("legacy")}),
            1,
            "a second live row is the cross-account leak this replaced",
        )
        row = _row(original, "user", "enabled", "disabled_reason", "token_hash")
        self.assertEqual(row.user, frappe.session.user)
        self.assertEqual(row.enabled, 1)
        self.assertFalse(row.disabled_reason)
        self.assertEqual(row.token_hash, token_hash(_token("legacy")))

    def test_the_caller_does_not_get_to_say_whose_device_it_is(self):
        """The cross-account hole: a caller-supplied ``user`` must be ignored.

        ``handle_user_device`` is whitelisted and saves with
        ``ignore_permissions=True``, so honouring a caller-supplied ``user`` let any
        authenticated caller register their OWN token under somebody else's account
        and receive that person's pushes. The row must bind to the SESSION user.
        """
        handle_user_device(
            {
                "device_token": _token("hijack"),
                "platform": "Android",
                "user": _OTHER_USER,
            }
        )

        row = _row(
            frappe.db.get_value(DOCTYPE, {"token_hash": token_hash(_token("hijack"))}, "name"),
            "user",
        )
        self.assertEqual(
            row.user,
            frappe.session.user,
            "a caller-supplied user was honoured — this is the push-hijack hole",
        )
        self.assertNotEqual(row.user, _OTHER_USER)

    def test_a_rebind_also_ignores_a_caller_supplied_user(self):
        """The same rule on the OTHER branch — the rebind path updates from the payload."""
        register_device(_INSTALL_B, _token("hijack2"), "android", user=_OTHER_USER)

        handle_user_device(
            {
                "device_token": _token("hijack2"),
                "platform": "Android",
                "user": _OTHER_USER,
            }
        )

        row = _row(
            frappe.db.get_value(DOCTYPE, {"token_hash": token_hash(_token("hijack2"))}, "name"),
            "user",
        )
        self.assertEqual(row.user, frappe.session.user, "the rebind must bind to the caller")

    def test_legacy_registration_leaves_installation_id_null(self):
        handle_user_device({"device_token": _token("legacy2"), "platform": "Android"})

        name = frappe.db.get_value(
            DOCTYPE, {"token_hash": token_hash(_token("legacy2"))}, "name"
        )
        self.assertIsNone(_row(name, "installation_id").installation_id)

    def test_the_controller_keeps_token_hash_in_step_with_the_token(self):
        doc = frappe.get_doc(
            {
                "doctype": DOCTYPE,
                "user": _USER,
                "device_token": _token("hashsync"),
                "platform": "Android",
            }
        )
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.token_hash, token_hash(_token("hashsync")))

        doc.device_token = _token("hashsync-2")
        doc.save(ignore_permissions=True)
        self.assertEqual(
            _row(doc.name, "token_hash").token_hash, token_hash(_token("hashsync-2"))
        )
