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

## Supporting Organization

The development of this app was commissioned by [Searchosis marketing Pvt Ltd](searchosis.com)

<img src="https://user-images.githubusercontent.com/246454/152739360-185e022a-3474-4d4a-9c89-5922bad401c0.png" width="120">
