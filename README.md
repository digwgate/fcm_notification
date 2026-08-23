# FCM Notification for ERPNext
Send notifications created in Frappe or ERPNext as push notication via Firebase Cloud Message(FCM)

### Steps to use the app:

1. Install the app into your site. [(Refer)](https://frappeframework.com/docs/v13/user/en/bench/frappe-commands#app-installation)

2. Create a new Server Script with values given below<br />
  i. Script Type: **DocType Event**<br />
  ii. Reference Document Type: **Notification Log**<br />
  iii. DocType Event: **Before Insert**<br />
  iv. Script: `frappe.call("fcm_notification.send_notification.send_notification", doc=doc)`<br />
To learn more about server scripts [see this link.](https://frappeframework.com/docs/v13/user/en/desk/scripting/server-script) 

2. Add your FCM server key in FCM Notification Settings. [(Refer)](https://intercom.help/push-monkey/en/articles/1649592-how-to-set-up-your-fcm-keys-previously-called-gcm)

3. Link your device id to each user using the **User Device** DocType.

4. Optionally create a notification in Frappe/ERPNext. [(Refer)](https://docs.erpnext.com/docs/v12/user/manual/en/setting-up/notifications)

5. Run an event that triggers any notification. The notifcation will be send the respetive user via FCM if they have subscribed to it.


### Device token cleanup

A daily scheduled job (`fcm_notification/token_sweep.py`) keeps the **User Device** table lean so
sends aren't fanned out to dead tokens. Each run:

1. **soft-disables** rows untouched for `token_staleness_days` (default 90), then
2. **hard-deletes** rows that have been disabled for `disabled_token_retention_days` (default 30).

Deletes are permanent (no `Deleted Document` copy), and each pass handles at most 5,000 rows per
run, oldest first — so a large backlog drains over consecutive days instead of risking the job's
timeout.

Both windows live in **FCM Notification Settings → Configuration → Token Lifecycle**, and fall back
to the defaults above while unset. Tick **Disable Token Sweep** in the same section to switch the
job off.

Staleness is measured on `COALESCE(last_seen_at, modified)`: `last_seen_at` is the registration
timestamp a client refreshes on every launch, and `modified` covers rows that predate it. Retention
still counts from `modified` — for a disabled row that is when it was disabled.

### Programmatic API (0.1.0)

Other apps import `fcm_notification.api`. It is the transport layer only — what to send, to whom and
when belongs to the calling app. **None of these commit; the caller owns the transaction.**

```python
from fcm_notification.api import (
    register_device,        # idempotent upsert keyed on installation_id -> {"name", "rebound"}
    unbind_device,          # owner-scoped logout disable -> bool (False = silent no-op)
    get_devices,            # (user=..., guest_id=...) enabled rows that still hold a token
    delete_user_devices,    # erasure: hard delete, no Deleted Document copy -> count
    delete_guest_devices,
    send_to_devices,        # chunked multicast -> [DeviceSendResult(device, ok, error_code, disabled)]
    is_transient_error_code,
    supports_fid_targeting,
)
```

`send_to_devices(devices, title, body, data=None, opts=None)` sends in chunks of 50, passes `data`
through verbatim (after the 4 KB trim) and honours per-message `priority`, `ttl`, `collapse`,
`channel_id` and `analytics_label`, writing the Android and APNs equivalents of each. A token is
disabled when FCM says it is dead (`UNREGISTERED`, `SenderIdMismatch`), and for `INVALID_ARGUMENT`
only when something else in the same chunk succeeded — a chunk where every token is rejected the
same way is a payload bug and is logged instead. 429/5xx/timeouts never disable anything.

Registration writes `installation_id` and a UNIQUE `token_hash`, so one token can never be live on
two rows: re-registering a token another install holds clears it there (`disabled_reason = Rebound`).
The older whitelisted `handle_user_device` stays token-keyed for existing clients and re-binds that
row instead of inserting a second one.

`send_direct_notification(..., opts=None)` (and the `dispatch_direct` it wraps) takes the same per-message
`opts` — a chat thread passes `{"collapse_key": <conversation>}` so a burst folds into one banner.
With `enqueue=True` the `opts` ride the RQ job, so **restart the workers when upgrading to this
version** — a job enqueued by new code and picked up by an old worker fails with `TypeError` on the
unknown kwarg.

Two switches, both in **FCM Notification Settings**: **Push Desk Notification Logs** (on unless
unticked) gates the `Notification Log` hook for sites whose devices belong to customers, and
**Device Owner Roles** lists the roles allowed to manage their own `User Device` rows — synced to
Custom DocPerms on every migrate.

## Supporting Organization

The development of this app was commissioned by [Searchosis marketing Pvt Ltd](searchosis.com)

<img src="https://user-images.githubusercontent.com/246454/152739360-185e022a-3474-4d4a-9c89-5922bad401c0.png" width="120">
