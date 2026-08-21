# W0 migration runbook — `fcm_notification` 0.1.0

This release adds two UNIQUE indexes to `User Device` (`installation_id`, `token_hash`)
and a `pre_model_sync` patch that makes the existing rows fit them. The app is
**shared**: `qnina.new` is a production install of it, so the upgrade is an
owner-supervised sitting, not a background migrate.

Everything below assumes:

```sh
BENCH=/Volumes/sam9902t/Projects/frappe/frappe_16
APP=$BENCH/apps/fcm_notification
cd $BENCH
```

One checkout serves both sites on this bench — check out the branch **once**, then
migrate both sites in the same sitting.

---

## A. Take a copy of the `qnina.new` database

Never dry-run against the live site. `bench backup` writes a gzipped dump without
anyone having to read `site_config.json`:

```sh
bench --site qnina.new backup
# → sites/qnina.new/private/backups/<timestamp>-qnina_new-database.sql.gz
ls -t sites/qnina.new/private/backups/*-database.sql.gz | head -1
```

Restore it into a scratch site (delete it when the sitting is over):

```sh
bench new-site qnina.copy --admin-password admin --db-name qnina_copy
bench --site qnina.copy restore $(ls -t sites/qnina.new/private/backups/*-database.sql.gz | head -1)
bench --site qnina.copy list-apps           # confirm fcm_notification came across
```

The scratch site now holds the real row distribution with the OLD schema — which
is the state the patch has to survive.

---

## B. Dry-run the patch against the copy

The app code on disk is already the new branch; the copy's schema is not. The dry
run handles that (it reports duplicates even before `token_hash` exists) and
**writes nothing**:

```sh
cd $APP && git checkout feature/0.1.0-hardening && cd $BENCH

bench --site qnina.copy execute \
  fcm_notification.patches.v0_1_0.dedupe_user_device_tokens.report
```

It prints, per duplicated token, the row it would KEEP (newest by `modified`) and
every row it would CLEAR, then a summary:

```
Duplicate tokens: N
  token …<last 12 chars> (3 rows)
    KEEP  DEV-Android-00042 user=someone@example.com modified=2026-08-01 …
    CLEAR DEV-Android-00017 user=other@example.com  enabled=1 modified=2026-05-…
Summary (no writes): {'blanked': .., 'rebound': .., 'hashed': .., 'column_missing': True}
```

Read the CLEAR list before continuing: those devices lose their row's token and
re-register on their next app launch. If the count is surprising, stop.

Then run the real migrate **on the copy** and check the result:

```sh
bench --site qnina.copy migrate

bench --site qnina.copy mariadb <<'SQL'
SELECT COUNT(*) AS rows_total,
       COUNT(device_token) AS with_token,
       COUNT(token_hash) AS with_hash,
       SUM(disabled_reason = 'Rebound') AS rebound
FROM `tabUser Device`;

-- must return zero rows: the unique index depends on it
SELECT token_hash, COUNT(*) c FROM `tabUser Device`
WHERE token_hash IS NOT NULL GROUP BY token_hash HAVING c > 1;

SHOW INDEX FROM `tabUser Device` WHERE Key_name IN ('token_hash','installation_id');
SQL
```

Also confirm the permission sync did the right thing on a site that HAS the qnina
roles (the two rows that left `user_device.json` must come back as Custom
DocPerms, and `System Manager` must still be there):

```sh
bench --site qnina.copy mariadb <<'SQL'
SELECT role, if_owner, `read`, `write`, `create` FROM `tabCustom DocPerm`
WHERE parent = 'User Device';
SQL
```

Optionally run the app's own tests against the copy (they print to **stderr** —
capture both streams, and check a `Ran N tests` line appeared):

```sh
bench --site qnina.copy run-tests --app fcm_notification 2>&1 | tee /tmp/fcm-tests.log
grep -E '^Ran [0-9]+ tests' /tmp/fcm-tests.log   # no match = the suite never ran
```

Tear the copy down when you are done:

```sh
bench drop-site qnina.copy --force
```

---

## C. The sitting: migrate `qnina.new`, then `supere.plat`

```sh
# 0. one checkout for both sites
cd $APP && git checkout feature/0.1.0-hardening && cd $BENCH

# 1. fresh safety backup of the live site
bench --site qnina.new backup --with-files

# 2. the production sibling first — it is the one with data to migrate
bench --site qnina.new migrate
bench --site qnina.new clear-cache

# 3. Super E: first install, then migrate
bench --site supere.plat install-app fcm_notification
bench --site supere.plat migrate
bench --site supere.plat clear-cache

# 4. Super E is customer-facing: do NOT push Desk Notification Logs to it
bench --site supere.plat console <<'PY'
frappe.db.set_single_value("FCM Notification Settings", "notification_log_pushes_enabled", 0)
frappe.db.commit()
PY

# 5. assets + workers
bench build --apps fcm_notification
bench restart
```

Post-checks on both sites:

```sh
bench --site qnina.new mariadb <<'SQL'
SELECT token_hash, COUNT(*) c FROM `tabUser Device`
WHERE token_hash IS NOT NULL GROUP BY token_hash HAVING c > 1;   -- expect empty
SELECT COUNT(*) FROM `tabUser Device` WHERE enabled = 1 AND device_token IS NOT NULL;
SQL

bench --site qnina.new execute \
  "frappe.db.get_value" --kwargs "{'doctype':'Patch Log','filters':{'patch':['like','%dedupe_user_device_tokens%']},'fieldname':'patch'}"
```

Smoke-test the legacy client path on qnina (regression R1) — registering a token
another row holds must re-bind that row and return 200, not 500.

---

## D. Revert

The code revert is a branch switch; the data changes are deliberately
self-healing (every cleared token re-registers on the device's next launch).

```sh
# 1. stop the two behaviours first, without a deploy:
bench --site <site> console <<'PY'
frappe.db.set_single_value("FCM Notification Settings", "token_sweep_disabled", 1)
frappe.db.set_single_value("FCM Notification Settings", "notification_log_pushes_enabled", 0)
frappe.db.commit()
PY

# 2. drop the two unique indexes (check the names first — Frappe names a
#    `unique: 1` Data field's index after the field):
bench --site <site> mariadb <<'SQL'
SHOW INDEX FROM `tabUser Device` WHERE Key_name IN ('token_hash','installation_id');
ALTER TABLE `tabUser Device` DROP INDEX `token_hash`;
ALTER TABLE `tabUser Device` DROP INDEX `installation_id`;
SQL

# 3. go back to the previous code and re-migrate
cd $APP && git checkout dev && cd $BENCH
bench --site <site> migrate
bench restart
```

Notes:

- The columns themselves can stay — nothing on `dev` reads them, and dropping
  them would lose the ids the next attempt needs.
- The `Patch Log` row for the dedupe survives a revert. That is fine: the patch is
  idempotent, and on a re-attempt Frappe skips it (delete that one row if you want
  it to run again).
- The dedupe cannot be undone from the app: rows it cleared are disabled with
  `disabled_reason = 'Rebound'` and their tokens are gone. Restore the pre-sitting
  backup if you genuinely need them back.
- `after_migrate` writes Custom DocPerms for `User Device`. Reverting the code
  leaves them in place; `DELETE FROM \`tabCustom DocPerm\` WHERE parent = 'User Device'`
  (then `bench --site <site> clear-cache`) restores the DocType's own permissions.
