// Copyright (c) 2022, Raheeb and contributors
// For license information, please see license.txt

frappe.ui.form.on("FCM Notification Settings", {
	setup(frm) {
		frm.set_query("role", "notification_center_roles", () => ({
			filters: [["Role", "name", "not in", ["Guest", "Administrator"]]],
		}));
	},
});
