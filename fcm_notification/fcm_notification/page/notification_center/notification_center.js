frappe.pages["notification-center"].on_page_load = function (wrapper) {
	frappe.notification_center = new NotificationCenterPage(wrapper);
};

class NotificationCenterPage {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Notification Center"),
			single_column: true,
		});
		this.method = "fcm_notification.notification_center";
		this.fields = {};
		this.recipients = null;
		this.reference_fields = null;
		this.active_reference_target = "body";
		this.refresh_recipients = frappe.utils.debounce(() => this.load_recipients(), 350);
		this.refresh_reference_fields = frappe.utils.debounce(
			() => this.load_reference_fields(),
			250
		);
		this.make();
	}

	make() {
		this.page.set_primary_action(__("Send"), () => this.confirm_send(), "send");
		this.page.add_inner_button(__("Preview"), () => this.preview());
		this.page.add_inner_button(__("Refresh Recipients"), () => this.load_recipients());
		frappe.breadcrumbs.add("Fcm Notification");

		this.$body = $(`
			<div class="notification-center-page">
				<div class="notification-center-grid">
					<div class="notification-center-stack">
						<section class="notification-center-panel">
							<div class="notification-center-panel-header">
								<h2 class="notification-center-panel-title">${__("Recipients")}</h2>
								<span class="indicator-pill gray recipient-status">${__("0 users")}</span>
							</div>
							<div class="notification-center-fields recipients-fields">
								<div class="notification-center-field" data-fieldname="platform"></div>
								<div class="notification-center-field" data-fieldname="notification_type"></div>
								<div class="notification-center-field full-width" data-fieldname="roles"></div>
								<div class="notification-center-field full-width" data-fieldname="user_groups"></div>
								<div class="notification-center-field full-width" data-fieldname="users"></div>
							</div>
							<div class="notification-center-summary">
								<div class="notification-center-stat">
									<div class="notification-center-stat-value users-count">0</div>
									<div class="notification-center-stat-label">${__("Users")}</div>
								</div>
								<div class="notification-center-stat">
									<div class="notification-center-stat-value devices-count">0</div>
									<div class="notification-center-stat-label">${__("Devices")}</div>
								</div>
								<div class="notification-center-stat">
									<div class="notification-center-stat-value platforms-count">0</div>
									<div class="notification-center-stat-label">${__("Platforms")}</div>
								</div>
							</div>
							<div class="notification-center-recipient-list">
								<div class="notification-center-empty">${__("No recipients selected")}</div>
							</div>
						</section>

						<section class="notification-center-panel">
							<div class="notification-center-panel-header">
								<h2 class="notification-center-panel-title">${__("Delivery")}</h2>
							</div>
							<div class="notification-center-fields">
								<div class="notification-center-field" data-fieldname="enqueue"></div>
								<div class="notification-center-field" data-fieldname="create_notification_logs"></div>
							</div>
						</section>
					</div>

					<div class="notification-center-stack">
						<section class="notification-center-panel">
							<div class="notification-center-panel-header">
								<h2 class="notification-center-panel-title">${__("Message")}</h2>
							</div>
							<div class="notification-center-fields">
								<div class="notification-center-field" data-fieldname="template_type"></div>
								<div class="notification-center-field" data-fieldname="template_name"></div>
								<div class="notification-center-field full-width" data-fieldname="title"></div>
								<div class="notification-center-field full-width" data-fieldname="body"></div>
								<div class="notification-center-reference-helper full-width">
									<div class="notification-center-reference-header">
										<h3 class="notification-center-reference-title">${__("Reference Fields")}</h3>
										<span class="indicator-pill gray reference-status">${__("0 fields")}</span>
									</div>
									<div class="notification-center-fields reference-fields">
										<div class="notification-center-field" data-fieldname="doctype"></div>
										<div class="notification-center-field" data-fieldname="docname"></div>
									</div>
									<div class="notification-center-reference-toolbar">
										<input class="form-control input-xs reference-filter" type="text" placeholder="${__(
											"Filter fields"
										)}">
										<div class="btn-group btn-group-xs reference-targets">
											<button type="button" class="btn btn-default btn-xs reference-target-btn" data-target="title">${__(
												"Title"
											)}</button>
											<button type="button" class="btn btn-default btn-xs reference-target-btn active" data-target="body">${__(
												"Body"
											)}</button>
										</div>
									</div>
									<div class="notification-center-reference-list">
										<div class="notification-center-empty">${__("No reference DocType selected")}</div>
									</div>
								</div>
							</div>
						</section>

						<section class="notification-center-panel">
							<div class="notification-center-panel-header">
								<h2 class="notification-center-panel-title">${__("Preview")}</h2>
							</div>
							<div class="notification-center-preview">
								<div class="notification-center-empty">${__("No preview")}</div>
							</div>
							<div class="notification-center-actions">
								<button class="btn btn-default btn-sm preview-btn">${__("Preview")}</button>
								<button class="btn btn-primary btn-sm send-btn">${__("Send")}</button>
							</div>
						</section>
					</div>
				</div>
			</div>
		`).appendTo(this.page.body.empty());

		this.make_fields();
		this.bind_actions();
	}

	make_fields() {
		this.fields.platform = this.make_field("platform", {
			fieldtype: "Select",
			fieldname: "platform",
			label: __("Platform"),
			options: "\nAndroid\nIOS",
			change: () => {
				this.fields.users.set_value([]);
				this.refresh_recipients();
			},
		});
		this.fields.notification_type = this.make_field("notification_type", {
			fieldtype: "Select",
			fieldname: "notification_type",
			label: __("Type"),
			options: "\nAlert\nMention\nAssignment\nShare",
			default: "Alert",
		});
		this.fields.roles = this.make_field("roles", {
			fieldtype: "MultiSelectList",
			fieldname: "roles",
			label: __("Roles"),
			placeholder: __("Select roles"),
			get_data: (txt) =>
				frappe.xcall(`${this.method}.get_role_options`, {
					txt,
				}),
			change: () => this.refresh_recipients(),
		});
		this.fields.user_groups = this.make_field("user_groups", {
			fieldtype: "MultiSelectList",
			fieldname: "user_groups",
			label: __("User Groups"),
			placeholder: __("Select user groups"),
			get_data: (txt) =>
				frappe.xcall(`${this.method}.get_user_group_options`, {
					txt,
				}),
			change: () => this.refresh_recipients(),
		});
		this.fields.users = this.make_field("users", {
			fieldtype: "MultiSelectList",
			fieldname: "users",
			label: __("Individual Users"),
			placeholder: __("Select users"),
			get_data: (txt) =>
				frappe.xcall(`${this.method}.get_enabled_user_options`, {
					txt,
					platform: this.fields.platform.get_value(),
				}),
			change: () => this.refresh_recipients(),
		});
		this.fields.enqueue = this.make_field("enqueue", {
			fieldtype: "Check",
			fieldname: "enqueue",
			label: __("Enqueue"),
			default: 1,
		});
		this.fields.create_notification_logs = this.make_field("create_notification_logs", {
			fieldtype: "Check",
			fieldname: "create_notification_logs",
			label: __("Create Notification Logs"),
			default: 0,
		});
		this.fields.template_type = this.make_field("template_type", {
			fieldtype: "Select",
			fieldname: "template_type",
			label: __("Template Type"),
			options: "\nEmail Template\nNotification",
			change: () => this.on_template_type_change(),
		});
		this.fields.template_name = this.make_field("template_name", {
			fieldtype: "Link",
			fieldname: "template_name",
			label: __("Template"),
			options: "Email Template",
			change: () => this.load_template(),
		});
		this.fields.title = this.make_field("title", {
			fieldtype: "Data",
			fieldname: "title",
			label: __("Title"),
			reqd: 1,
			change: () => this.clear_preview(),
		});
		this.fields.body = this.make_field("body", {
			fieldtype: "Code",
			fieldname: "body",
			label: __("Body"),
			options: "Jinja",
			reqd: 1,
			change: () => this.clear_preview(),
		});
		this.fields.doctype = this.make_field("doctype", {
			fieldtype: "Link",
			fieldname: "doctype",
			label: __("DocType"),
			options: "DocType",
			change: () => this.on_doctype_change(),
		});
		this.fields.docname = this.make_field("docname", {
			fieldtype: "Link",
			fieldname: "docname",
			label: __("Document"),
			options: "DocType",
			change: () => this.on_docname_change(),
		});
	}

	make_field(fieldname, df) {
		const control = frappe.ui.form.make_control({
			parent: this.$body.find(`[data-fieldname="${fieldname}"]`).get(0),
			df,
			render_input: true,
		});
		if (df.default !== undefined) {
			control.set_value(df.default);
		}
		return control;
	}

	bind_actions() {
		this.$body.find(".preview-btn").on("click", () => this.preview());
		this.$body.find(".send-btn").on("click", () => this.confirm_send());
		this.$body.find(".reference-filter").on("input", () => this.refresh_reference_fields());
		this.$body.find(".reference-target-btn").on("click", (event) => {
			this.set_reference_target($(event.currentTarget).data("target"));
		});
		this.$body.on("click", ".notification-center-reference-row", (event) => {
			if ($(event.target).closest("button").length) {
				return;
			}
			this.insert_reference(
				$(event.currentTarget).data("expression"),
				this.active_reference_target
			);
		});
		this.$body.on("click", ".reference-insert-btn", (event) => {
			event.stopPropagation();
			const $button = $(event.currentTarget);
			this.insert_reference($button.data("expression"), $button.data("target"));
		});
	}

	on_template_type_change() {
		const template_type = this.fields.template_type.get_value() || "Email Template";
		this.fields.template_name.df.options = template_type;
		this.fields.template_name.set_value("");
		this.fields.template_name.refresh();
	}

	on_doctype_change() {
		const doctype = this.fields.doctype.get_value();
		this.fields.docname.df.options = doctype || "DocType";
		this.fields.docname.set_value("");
		this.fields.docname.refresh();
		this.clear_preview();
		this.load_reference_fields();
	}

	on_docname_change() {
		this.clear_preview();
		this.load_reference_fields();
	}

	has_targets() {
		const values = this.get_values();
		return Boolean(
			values.platform ||
				values.roles.length ||
				values.user_groups.length ||
				values.users.length
		);
	}

	get_values() {
		return {
			platform: this.fields.platform.get_value(),
			notification_type: this.fields.notification_type.get_value() || "Alert",
			roles: this.fields.roles.get_value() || [],
			user_groups: this.fields.user_groups.get_value() || [],
			users: this.fields.users.get_value() || [],
			enqueue: this.fields.enqueue.get_value() ? 1 : 0,
			create_notification_logs: this.fields.create_notification_logs.get_value() ? 1 : 0,
			template_type: this.fields.template_type.get_value(),
			template_name: this.fields.template_name.get_value(),
			title: this.fields.title.get_value(),
			body: this.fields.body.get_value(),
			doctype: this.fields.doctype.get_value(),
			docname: this.fields.docname.get_value(),
		};
	}

	async load_template() {
		const values = this.get_values();
		if (!values.template_type || !values.template_name) {
			return;
		}
		const template = await frappe.xcall(`${this.method}.get_message_template`, {
			template_type: values.template_type,
			template_name: values.template_name,
		});
		await this.fields.title.set_value(template.title || "");
		await this.fields.body.set_value(template.body || "");
		this.clear_preview();
	}

	async load_recipients() {
		if (!this.has_targets()) {
			this.recipients = null;
			this.render_recipients(null);
			return;
		}
		const values = this.get_values();
		const recipients = await frappe.xcall(`${this.method}.get_recipients`, {
			roles: values.roles,
			user_groups: values.user_groups,
			users: values.users,
			platform: values.platform,
		});
		this.recipients = recipients;
		this.render_recipients(recipients);
	}

	render_recipients(recipients) {
		const user_count = recipients?.user_count || 0;
		const device_count = recipients?.device_count || 0;
		const platform_count = Object.keys(recipients?.platform_counts || {}).length;

		this.$body.find(".recipient-status").text(__("{0} users", [user_count]));
		this.$body.find(".users-count").text(user_count);
		this.$body.find(".devices-count").text(device_count);
		this.$body.find(".platforms-count").text(platform_count);

		const users = recipients?.users || [];
		if (!users.length) {
			this.$body
				.find(".notification-center-recipient-list")
				.html(`<div class="notification-center-empty">${__("No recipients selected")}</div>`);
			return;
		}

		const html = users
			.map((user) => {
				const platforms = (user.platforms || [])
					.map((platform) => `<span class="badge badge-secondary">${frappe.utils.escape_html(platform)}</span>`)
					.join("");
				return `
					<div class="notification-center-recipient-row">
						<div>
							<div class="notification-center-recipient-name">${frappe.utils.escape_html(
								user.full_name || user.user
							)}</div>
							<div class="notification-center-recipient-email">${frappe.utils.escape_html(
								user.email || user.user
							)} · ${__("{0} devices", [user.enabled_devices || 0])}</div>
						</div>
						<div class="notification-center-platforms">${platforms}</div>
					</div>
				`;
			})
			.join("");
		this.$body.find(".notification-center-recipient-list").html(html);
	}

	async load_reference_fields() {
		const doctype = this.fields.doctype.get_value();
		if (!doctype) {
			this.reference_fields = null;
			this.render_reference_fields(null);
			return;
		}

		const result = await frappe.xcall(`${this.method}.get_reference_fields`, {
			doctype,
			docname: this.fields.docname.get_value(),
			txt: this.$body.find(".reference-filter").val(),
		});
		this.reference_fields = result;
		this.render_reference_fields(result);
	}

	render_reference_fields(result) {
		const fields = result?.fields || [];
		this.$body.find(".reference-status").text(__("{0} fields", [fields.length]));

		if (!result?.doctype) {
			this.$body
				.find(".notification-center-reference-list")
				.html(`<div class="notification-center-empty">${__("No reference DocType selected")}</div>`);
			return;
		}

		if (!fields.length) {
			this.$body
				.find(".notification-center-reference-list")
				.html(`<div class="notification-center-empty">${__("No fields found")}</div>`);
			return;
		}

		const html = fields
			.map((field) => {
				const expression = frappe.utils.escape_html(field.expression || "");
				const label = frappe.utils.escape_html(field.label || field.fieldname || "");
				const fieldname = frappe.utils.escape_html(field.fieldname || "");
				const fieldtype = frappe.utils.escape_html(field.fieldtype || "");
				const preview = frappe.utils.escape_html(field.value_preview || "");
				const preview_html = preview
					? `<div class="notification-center-reference-preview">${preview}</div>`
					: "";
				return `
					<div class="notification-center-reference-row" data-expression="${expression}">
						<div class="notification-center-reference-main">
							<div class="notification-center-reference-label">
								<span>${label}</span>
								<span class="badge badge-secondary">${fieldtype}</span>
							</div>
							<div class="notification-center-reference-expression">${expression}</div>
							<div class="notification-center-reference-fieldname">${fieldname}</div>
							${preview_html}
						</div>
						<div class="notification-center-reference-actions">
							<button type="button" class="btn btn-default btn-xs reference-insert-btn" data-target="title" data-expression="${expression}">${__(
					"Title"
				)}</button>
							<button type="button" class="btn btn-default btn-xs reference-insert-btn" data-target="body" data-expression="${expression}">${__(
					"Body"
				)}</button>
						</div>
					</div>
				`;
			})
			.join("");
		this.$body.find(".notification-center-reference-list").html(html);
	}

	set_reference_target(target) {
		this.active_reference_target = target === "title" ? "title" : "body";
		this.$body.find(".reference-target-btn").removeClass("active");
		this.$body
			.find(`.reference-target-btn[data-target="${this.active_reference_target}"]`)
			.addClass("active");
	}

	async insert_reference(expression, target) {
		if (!expression) {
			return;
		}
		const fieldname = target === "title" ? "title" : "body";
		const control = this.fields[fieldname];

		if (fieldname === "body" && control.editor) {
			control.editor.insert(expression);
			control.parse_validate_and_set_in_model(control.get_input_value());
			control.editor.focus();
		} else if (control.$input?.length) {
			const input = control.$input.get(0);
			const current = control.get_value() || "";
			if (typeof input.selectionStart === "number") {
				const start = input.selectionStart;
				const end = input.selectionEnd;
				await control.set_value(
					`${current.slice(0, start)}${expression}${current.slice(end)}`
				);
				input.focus();
				input.setSelectionRange(start + expression.length, start + expression.length);
			} else {
				await control.set_value(this.append_reference(current, expression, fieldname));
				input.focus();
			}
		} else {
			await control.set_value(
				this.append_reference(control.get_value() || "", expression, fieldname)
			);
			control.set_focus?.();
		}

		this.clear_preview();
	}

	append_reference(current, expression, fieldname) {
		if (!current) {
			return expression;
		}
		if (/\s$/.test(current)) {
			return `${current}${expression}`;
		}
		return fieldname === "body" ? `${current}\n${expression}` : `${current} ${expression}`;
	}

	clear_preview() {
		this.$body
			.find(".notification-center-preview")
			.html(`<div class="notification-center-empty">${__("No preview")}</div>`);
	}

	async preview() {
		const values = this.get_values();
		if (!values.title || !values.body) {
			frappe.msgprint(__("Title and Body are required."));
			return;
		}
		const preview = await frappe.xcall(`${this.method}.preview_notification`, {
			title: values.title,
			body: values.body,
			doctype: values.doctype,
			docname: values.docname,
		});
		this.render_preview(preview);
	}

	render_preview(preview) {
		this.$body.find(".notification-center-preview").html(`
			<div class="notification-center-preview-title">${frappe.utils.escape_html(preview.title || "")}</div>
			<div class="notification-center-preview-body">${frappe.utils.escape_html(preview.body || "")}</div>
		`);
	}

	async confirm_send() {
		const values = this.get_values();
		if (!values.title || !values.body) {
			frappe.msgprint(__("Title and Body are required."));
			return;
		}
		await this.load_recipients();
		const user_count = this.recipients?.user_count || 0;
		const device_count = this.recipients?.device_count || 0;
		if (!device_count) {
			frappe.msgprint(__("No enabled user devices matched the selected recipients."));
			return;
		}

		frappe.confirm(
			__("Send notification to {0} users on {1} devices?", [user_count, device_count]),
			() => this.send()
		);
	}

	async send() {
		const values = this.get_values();
		const result = await frappe.xcall(`${this.method}.send_notification_center`, values);
		this.recipients = result;
		this.render_recipients(result);
		frappe.show_alert({
			message: __("Notification sent to {0} users.", [result.user_count || 0]),
			indicator: "green",
		});
	}
}
