// Copyright (c) 2025, Mohammed Awni and contributors
// For license information, please see license.txt

// Reserved keys that should never be mapped or emitted in payloads
const RESERVED_FIELDS = ["doctype", "docname"];

frappe.ui.form.on("Notification Payload", {
    onload(frm) {
        // Seed child table options when the form opens
        update_child_field_options(frm);
    },

    for_doctype(frm) {
        // Refresh options whenever the source doctype changes
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

    key(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row || !row.key) return;

        if (RESERVED_FIELDS.includes(row.key.toLowerCase())) {
            frappe.model.set_value(cdt, cdn, "key", "");
            frappe.throw(__("Cannot use reserved key: {0}", [row.key]));
        }
    },
});

function update_child_field_options(frm) {
    if (!frm.fields_dict?.fields_mapper?.grid) return;

    if (!frm.doc.for_doctype) {
        frm.fields_dict.fields_mapper.grid.update_docfield_property("doc_field", "options", []);
        return;
    }

    // Pull field options from the selected doctype and apply to child table select
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
