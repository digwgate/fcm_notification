// Copyright (c) 2025, Mohammed Awni and contributors
// For license information, please see license.txt

frappe.ui.form.on("Notification Payload", {
	onload(frm) {
		update_child_field_options(frm);
	},

	for_doctype(frm) {
		update_child_field_options(frm);
	},
});

frappe.ui.form.on("Notification Payload Property", {
	doc_field(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row) return;

		if (!row.key) {
			frappe.model.set_value(cdt, cdn, "key", row.doc_field);
		}
	},
});

function update_child_field_options(frm) {
	if (!frm.fields_dict?.fields_mapper?.grid) return;

	if (!frm.doc.for_doctype) {
		frm.fields_dict.fields_mapper.grid.update_docfield_property("doc_field", "options", []);
		return;
	}

	frappe.model.with_doctype(frm.doc.for_doctype, () => {
		const meta = frappe.get_meta(frm.doc.for_doctype);
		if (!meta || !Array.isArray(meta.fields)) return;

		const options = meta.fields
			.filter(
				(df) =>
					df.fieldname &&
					df.fieldtype &&
					!frappe.model.no_value_type.includes(df.fieldtype)
			)
			.map((df) => df.fieldname);

		frm.fields_dict.fields_mapper.grid.update_docfield_property(
			"doc_field",
			"options",
			options
		);
		frm.refresh_field("fields_mapper");
	});
}
