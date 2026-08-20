"""Make ``User Device`` tokens unique-able — BEFORE the unique index exists.

Registered under ``[pre_model_sync]`` on purpose. 0.1.0 adds a UNIQUE
``token_hash`` (sha256 of ``device_token``); on a site that has been running the
token-keyed registration path, the same token can already sit on several rows,
and creating the index on that data fails the whole migrate.

Three things happen here, all idempotent:

1. ``token_hash`` is created if the model sync has not created it yet — this
   patch runs first, so on an upgrade it usually has to add the column itself.
2. Empty-string tokens become NULL. They are not tokens, and they would all hash
   to the same value and collide with each other.
3. Duplicate tokens are resolved: the NEWEST row keeps the token; every older row
   is disabled with ``device_token``/``token_hash`` cleared and
   ``disabled_reason = 'Rebound'``. That is the same outcome a re-registration
   would produce, and the device re-registers on its next launch anyway.
4. ``token_hash`` is backfilled for every row that still holds a token.

Dry run first — it reports exactly which rows it would touch and writes nothing::

    bench --site <copy-of-the-site> execute \\
        fcm_notification.patches.v0_1_0.dedupe_user_device_tokens.report
"""

from __future__ import annotations

import hashlib
from typing import Dict, List

import frappe

DOCTYPE = "User Device"
TABLE = "tabUser Device"


def _table_exists() -> bool:
    return frappe.db.table_exists(DOCTYPE)


def _column_exists(column: str) -> bool:
    return column in frappe.db.get_table_columns(DOCTYPE)


def _ensure_token_hash_column() -> bool:
    """Add ``token_hash`` when the model sync has not created it yet.

    The dedupe has to write the column, and in ``pre_model_sync`` the DocType
    JSON has not been applied. Adding it with the exact type the DocField
    produces (``Data`` with ``length: 64``) keeps the later sync a no-op.
    """
    if _column_exists("token_hash"):
        return False
    frappe.db.sql_ddl(f"ALTER TABLE `{TABLE}` ADD COLUMN `token_hash` varchar(64)")
    return True


def _duplicate_groups() -> Dict[str, List[dict]]:
    """Rows sharing a token, newest first inside each group."""
    rows = frappe.db.sql(
        f"""
        SELECT name, device_token, user, enabled, modified, creation
        FROM `{TABLE}`
        WHERE device_token IS NOT NULL
          AND device_token != ''
          AND device_token IN (
              SELECT device_token FROM (
                  SELECT device_token
                  FROM `{TABLE}`
                  WHERE device_token IS NOT NULL AND device_token != ''
                  GROUP BY device_token
                  HAVING COUNT(*) > 1
              ) AS duplicated
          )
        ORDER BY device_token ASC, modified DESC, creation DESC, name DESC
        """,
        as_dict=True,
    )
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(row["device_token"], []).append(row)
    return groups


def _rows_missing_hash() -> List[dict]:
    return frappe.db.sql(
        f"""
        SELECT name, device_token
        FROM `{TABLE}`
        WHERE device_token IS NOT NULL
          AND device_token != ''
          AND (token_hash IS NULL OR token_hash = '')
        """,
        as_dict=True,
    )


def _blank_token_rows() -> List[str]:
    return frappe.db.sql_list(f"SELECT name FROM `{TABLE}` WHERE device_token = ''")


def run(dry_run: bool = False) -> dict:
    """Dedupe + backfill. Returns counts; writes nothing when ``dry_run``."""
    if not _table_exists():
        return {"skipped": True, "blanked": 0, "rebound": 0, "hashed": 0}

    added_column = False
    if not dry_run:
        added_column = _ensure_token_hash_column()
    elif not _column_exists("token_hash"):
        # Nothing to report on a column that does not exist yet; the real run
        # creates it first.
        return {
            "skipped": False,
            "dry_run": True,
            "blanked": len(_blank_token_rows()),
            "rebound": sum(max(len(g) - 1, 0) for g in _duplicate_groups().values()),
            "hashed": None,
            "column_missing": True,
        }

    blank_rows = _blank_token_rows()
    if blank_rows and not dry_run:
        frappe.db.sql(
            f"UPDATE `{TABLE}` SET device_token = NULL, token_hash = NULL "
            f"WHERE device_token = ''"
        )

    groups = _duplicate_groups()
    losers = [row["name"] for rows in groups.values() for row in rows[1:]]
    if losers and not dry_run:
        placeholders = ", ".join(["%s"] * len(losers))
        frappe.db.sql(
            f"""
            UPDATE `{TABLE}`
            SET enabled = 0,
                device_token = NULL,
                token_hash = NULL,
                disabled_reason = 'Rebound'
            WHERE name IN ({placeholders})
            """,
            losers,
        )

    hashed = 0
    if not dry_run:
        for row in _rows_missing_hash():
            digest = hashlib.sha256(row["device_token"].encode("utf-8")).hexdigest()
            frappe.db.sql(
                f"UPDATE `{TABLE}` SET token_hash = %s WHERE name = %s",
                (digest, row["name"]),
            )
            hashed += 1
    else:
        hashed = len(_rows_missing_hash())

    summary = {
        "skipped": False,
        "dry_run": dry_run,
        "added_column": added_column,
        "blanked": len(blank_rows),
        "rebound": len(losers),
        "hashed": hashed,
    }
    frappe.logger().info(f"User Device token dedupe: {summary}")
    return summary


def report() -> dict:
    """Dry run: print exactly what the patch would do, change nothing."""
    if not _table_exists():
        print(f"{DOCTYPE} table does not exist — nothing to do.")
        return {"skipped": True}

    groups = _duplicate_groups()
    print(f"Duplicate tokens: {len(groups)}")
    for token, rows in groups.items():
        keeper = rows[0]
        print(f"\n  token …{token[-12:]} ({len(rows)} rows)")
        print(
            f"    KEEP  {keeper['name']} user={keeper['user']} modified={keeper['modified']}"
        )
        for row in rows[1:]:
            print(
                f"    CLEAR {row['name']} user={row['user']} "
                f"enabled={row['enabled']} modified={row['modified']}"
            )

    summary = run(dry_run=True)
    print(f"\nSummary (no writes): {summary}")
    return summary


def execute():
    run()
