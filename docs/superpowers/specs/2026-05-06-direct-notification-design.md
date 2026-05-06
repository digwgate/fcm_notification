# Direct Notification Sending Design

## Goal

Add a Python-only function for sending Firebase Cloud Messaging notifications directly, without creating or relying on the `Notification Log` DocType. The function must support both user-based delivery and explicit device-token delivery, and it must support optional background enqueueing.

## Current Context

The app currently sends push notifications through `fcm_notification/send_notification.py`. `Notification Log.after_insert` calls `send_notification()`, which loads or receives a `Notification Log` document and delegates to `FCMNotificationService.dispatch()`.

The existing service already owns the important delivery behavior:

- Firebase app initialization from `FCM Notification Settings`.
- Android, APNS, and shared FCM option construction.
- User device lookup through `get_user_devices()`.
- Per-device sending through `safe_send_to_device()`.
- Invalid-token cleanup for known `User Device` records.
- Optional queueing through `_queue_send_device()` on `notifications_queue`.

The direct-send feature should reuse those behaviors instead of duplicating FCM delivery logic.

## Proposed Approach

Add a direct dispatch path inside `FCMNotificationService`, plus a Python-only module-level helper named `send_direct_notification()`.

The new helper will sit beside the existing `send_notification()` entrypoint, but it will not use `@frappe.whitelist()` for now. A later UI/API module can call this Python function or wrap it with its own whitelisted API when that feature is designed.

The existing `Notification Log` hook and `send_notification()` behavior will remain unchanged.

## API Shape

The direct helper will use explicit direct-send names:

```python
send_direct_notification(
    title,
    body,
    users=None,
    devices=None,
    data=None,
    doctype=None,
    docname=None,
    notification_type=None,
    enqueue=False,
)
```

Arguments:

- `title`: notification title. HTML is stripped before delivery.
- `body`: notification message body. HTML is stripped before delivery.
- `users`: optional single user string or iterable of user strings. Enabled `User Device` records will be fetched for each user.
- `devices`: optional explicit device token, iterable of tokens, device dict, or iterable of device dicts.
- `data`: optional dictionary of extra FCM data payload fields.
- `doctype`: optional document type context.
- `docname`: optional document name context.
- `notification_type`: optional trigger/type value used for settings filtering and payload `type`.
- `enqueue`: optional boolean. Defaults to `False`, so direct sends run immediately unless the caller opts into queueing.

At least one of `users` or `devices` must be provided.

## Payload Rules

The direct payload will include these normalized keys when values are present:

- `title`
- `message`
- `doctype`
- `docname`
- `type`

Extra `data` keys will be merged into the payload. Explicit function arguments are authoritative for `title`, `message`, `doctype`, `docname`, and `type`, so `data` cannot override those values.

The final data payload will pass through the existing `stringify_data()` helper so FCM receives string values.

## Recipient Flow

The direct dispatch method will:

1. Normalize `users` into a list of user IDs.
2. Fetch enabled devices for each user through the existing cached `get_user_devices()`.
3. Normalize `devices` into device dictionaries.
4. Deduplicate all recipients by `device_token`.
5. Return without error if valid users have no enabled devices.
6. Send each device immediately or enqueue each device based on `enqueue`.

Device dictionaries may include `device_token`, `name`, and `user`. Device names let existing cleanup disable the corresponding `User Device` record when Firebase reports an invalid token.

## Settings Behavior

The direct path will honor `FCM Notification Settings.notifications_trigger_type` when `notification_type` is provided, matching the current `Notification Log` dispatch behavior. If the settings table is empty, all direct sends are allowed.

If the settings table restricts notification types and the supplied `notification_type` is not allowed, no notification is sent.

## Error Handling

Validation will fail early with `frappe.throw()` when:

- Neither `users` nor `devices` is provided.
- A direct device entry is missing `device_token`.

Delivery errors will continue to use `safe_send_to_device()`:

- `messaging.UnregisteredError` disables the known `User Device` record when possible and invalidates that user's cached devices.
- `messaging.SenderIdMismatchError` follows the same cleanup path.
- Other send failures are logged and do not stop delivery to remaining devices.

## Tests

Add focused tests around the new direct behavior with Firebase delivery mocked:

- User and explicit-device recipients are both supported.
- Duplicate device tokens are sent only once.
- `enqueue=False` sends immediately.
- `enqueue=True` enqueues to `notifications_queue`.
- Direct payload includes title, message, document context, type, and extra data.
- Explicit function arguments override conflicting keys in `data`.
- Missing recipients and tokenless direct devices fail validation.

The tests should avoid real Firebase calls.

## Non-Goals

- No whitelisted API in this change.
- No UI or DocType for composing notifications in this change.
- No changes to the existing `Notification Log` hook behavior.
- No changes to FCM settings schema.
