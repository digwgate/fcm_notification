import json
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

import frappe

from fcm_notification.send_notification import (
    send_direct_notification,
    send_notification,
)

PLATFORMS = {"android": "Android", "ios": "IOS"}
DEFAULT_NOTIFICATION_TYPE = "Alert"
EXCLUDED_ROLES = {"Guest","Administrator"}
REFERENCE_FIELD_TYPES = {
    "Currency",
    "Int",
    "Long Int",
    "Float",
    "Percent",
    "Check",
    "Small Text",
    "Long Text",
    "Text Editor",
    "Markdown Editor",
    "Date",
    "Datetime",
    "Time",
    "Text",
    "Data",
    "Link",
    "Dynamic Link",
    "Select",
    "Read Only",
    "Duration",
    "Phone",
    "Autocomplete",
    "JSON",
}
STANDARD_REFERENCE_FIELDS = (
    ("name", "Name", "Data"),
    ("owner", "Created By", "Link"),
    ("creation", "Created On", "Datetime"),
    ("modified", "Last Updated On", "Datetime"),
    ("modified_by", "Last Updated By", "Link"),
    ("docstatus", "Document Status", "Int"),
)


def _require_system_manager():
    frappe.only_for("System Manager")


def _get_session_user() -> str:
    try:
        return frappe.session.user
    except RuntimeError:
        return "Administrator"


def _normalize_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value[0] in "[{":
            try:
                parsed = json.loads(value)
            except ValueError:
                return [value]
            return _normalize_list(parsed)
        return [value]
    if isinstance(value, dict):
        return [value.get("value") or value.get("name") or value.get("user")]
    try:
        values = list(value)
    except TypeError:
        return [frappe.as_unicode(value)]

    normalized = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("value") or item.get("name") or item.get("user")
        if item:
            normalized.append(frappe.as_unicode(item))
    return normalized


def _normalize_platform(platform: Optional[str]) -> Optional[str]:
    if not platform:
        return None
    normalized = frappe.as_unicode(platform).strip()
    if not normalized:
        return None
    return PLATFORMS.get(normalized.lower(), normalized)


def _normalize_roles(roles) -> List[str]:
    return [role for role in _normalize_list(roles) if role not in EXCLUDED_ROLES]


def _target_selected(roles, user_groups, users, platform) -> bool:
    return bool(
        _normalize_roles(roles)
        or _normalize_list(user_groups)
        or _normalize_list(users)
        or _normalize_platform(platform)
    )


def _get_enabled_device_rows(platform: Optional[str] = None) -> List[Dict[str, Any]]:
    filters = {"enabled": True}
    platform = _normalize_platform(platform)
    if platform:
        filters["platform"] = platform

    rows = frappe.get_all(
        "User Device",
        filters=filters,
        fields=["name", "user", "platform", "device_token"],
        order_by="creation desc",
    )

    seen_tokens = set()
    devices = []
    for row in rows:
        user = row.get("user")
        token = row.get("device_token")
        if not (user and token) or token in seen_tokens:
            continue
        seen_tokens.add(token)
        devices.append(row)
    return devices


def _get_users_by_roles(roles: Iterable[str]) -> set[str]:
    roles = _normalize_roles(roles)
    if not roles:
        return set()

    rows = frappe.get_all(
        "Has Role",
        filters={"parenttype": "User", "role": ["in", roles]},
        fields=["parent"],
        distinct=True,
    )
    return {row.get("parent") for row in rows if row.get("parent")}


def _get_users_by_groups(user_groups: Iterable[str]) -> set[str]:
    user_groups = _normalize_list(user_groups)
    if not user_groups:
        return set()

    rows = frappe.get_all(
        "User Group Member",
        filters={"parenttype": "User Group", "parent": ["in", user_groups]},
        fields=["user"],
        distinct=True,
    )
    return {row.get("user") for row in rows if row.get("user")}


def _get_user_details(users: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    user_list = sorted({user for user in _normalize_list(users) if user})
    if not user_list:
        return {}

    rows = frappe.get_all(
        "User",
        filters={"name": ["in", user_list], "enabled": 1},
        fields=["name", "full_name", "email", "user_image", "enabled"],
        order_by="full_name asc",
    )

    details = {}
    for row in rows:
        name = row.get("name")
        if not name:
            continue
        details[name] = row
    return details


def _selected_usernames(roles, user_groups, users, platform) -> set[str]:
    selected = set(_normalize_list(users))
    selected.update(_get_users_by_roles(roles))
    selected.update(_get_users_by_groups(user_groups))

    if not selected and _normalize_platform(platform):
        selected = {row["user"] for row in _get_enabled_device_rows(platform)}

    return {user for user in selected if user}


def _collect_recipients(
    roles=None,
    user_groups=None,
    users=None,
    platform=None,
) -> Dict[str, Any]:
    if not _target_selected(roles, user_groups, users, platform):
        frappe.throw("Select at least one role, user group, user, or platform.")

    platform = _normalize_platform(platform)
    devices = _get_enabled_device_rows(platform)
    devices_by_user = defaultdict(list)
    for device in devices:
        devices_by_user[device["user"]].append(device)

    selected = _selected_usernames(roles, user_groups, users, platform)
    eligible = sorted(selected.intersection(devices_by_user))
    user_details = _get_user_details(eligible)

    users_summary = []
    selected_devices = []
    for user in eligible:
        detail = user_details.get(user)
        if not detail:
            continue
        user_devices = devices_by_user[user]
        selected_devices.extend(user_devices)
        platforms = sorted(
            {device.get("platform") for device in user_devices if device.get("platform")}
        )
        users_summary.append(
            {
                "user": user,
                "full_name": detail.get("full_name") or user,
                "email": detail.get("email") or user,
                "user_image": detail.get("user_image"),
                "enabled_devices": len(user_devices),
                "platforms": platforms,
            }
        )

    users_summary.sort(key=lambda row: (row["full_name"].lower(), row["user"]))
    platform_counts = Counter(
        device.get("platform") for device in selected_devices if device.get("platform")
    )

    return {
        "users": users_summary,
        "user_count": len(users_summary),
        "device_count": len(selected_devices),
        "platform_counts": dict(platform_counts),
        "devices": selected_devices,
        "devices_by_user": {
            user: devices
            for user, devices in devices_by_user.items()
            if any(summary["user"] == user for summary in users_summary)
        },
    }


def _public_recipient_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "users": summary["users"],
        "user_count": summary["user_count"],
        "device_count": summary["device_count"],
        "platform_counts": summary["platform_counts"],
    }


@frappe.whitelist()
def get_recipients(roles=None, user_groups=None, users=None, platform=None):
    _require_system_manager()
    return _public_recipient_summary(
        _collect_recipients(
            roles=roles,
            user_groups=user_groups,
            users=users,
            platform=platform,
        )
    )


@frappe.whitelist()
def get_enabled_user_options(txt="", platform=None, limit=20):
    _require_system_manager()
    platform = _normalize_platform(platform)
    devices = _get_enabled_device_rows(platform)
    device_users = {device["user"] for device in devices}
    details = _get_user_details(device_users)

    txt = frappe.as_unicode(txt or "").lower()
    options = []
    for user, detail in sorted(
        details.items(), key=lambda row: ((row[1].get("full_name") or row[0]).lower(), row[0])
    ):
        if user not in device_users:
            continue
        label = detail.get("full_name") or user
        email = detail.get("email") or user
        haystack = f"{label} {email} {user}".lower()
        if txt and txt not in haystack:
            continue
        options.append(
            {
                "value": user,
                "label": label,
                "description": email,
            }
        )
        if len(options) >= int(limit or 20):
            break
    return options


@frappe.whitelist()
def get_role_options(txt="", limit=20):
    _require_system_manager()
    filters = [["Role", "name", "not in", sorted(EXCLUDED_ROLES)]]
    if txt:
        filters.append(["Role", "name", "like", f"%{txt}%"])
    rows = frappe.get_all(
        "Role",
        filters=filters,
        fields=["name"],
        order_by="name asc",
        limit_page_length=int(limit or 20),
    )
    return [
        {"value": row["name"], "label": row["name"], "description": ""}
        for row in rows
        if row["name"] not in EXCLUDED_ROLES
    ]


@frappe.whitelist()
def get_user_group_options(txt="", limit=20):
    _require_system_manager()
    filters = {}
    if txt:
        filters["name"] = ["like", f"%{txt}%"]
    rows = frappe.get_all(
        "User Group",
        filters=filters,
        fields=["name"],
        order_by="name asc",
        limit_page_length=int(limit or 20),
    )
    return [
        {"value": row["name"], "label": row["name"], "description": ""}
        for row in rows
    ]


def _reference_value_preview(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    preview = frappe.as_unicode(value)
    if len(preview) > 80:
        return f"{preview[:77]}..."
    return preview


def _reference_field_row(fieldname, label, fieldtype, values=None) -> Dict[str, str]:
    return {
        "fieldname": fieldname,
        "label": label or fieldname,
        "fieldtype": fieldtype,
        "expression": f"{{{{ doc.{fieldname} }}}}",
        "value_preview": _reference_value_preview((values or {}).get(fieldname)),
    }


def _is_reference_field(df) -> bool:
    fieldname = getattr(df, "fieldname", None)
    fieldtype = getattr(df, "fieldtype", None)
    if not fieldname or not fieldtype:
        return False
    if getattr(df, "hidden", 0):
        return False
    return fieldtype in REFERENCE_FIELD_TYPES


@frappe.whitelist()
def get_reference_fields(doctype=None, docname=None, txt="", limit=50):
    _require_system_manager()
    if not doctype:
        return {"doctype": "", "docname": "", "fields": []}

    values = {}
    if docname:
        values = {
            key: value
            for key, value in _get_document_context(doctype=doctype, docname=docname).items()
            if key != "doc"
        }

    if docname is None and not frappe.has_permission(doctype, "read"):
        frappe.throw(f"Not permitted to read {doctype}.")

    meta = frappe.get_meta(doctype)
    fields = []
    seen = set()
    for fieldname, label, fieldtype in STANDARD_REFERENCE_FIELDS:
        fields.append(_reference_field_row(fieldname, label, fieldtype, values))
        seen.add(fieldname)

    for df in meta.fields:
        if not _is_reference_field(df) or df.fieldname in seen:
            continue
        fields.append(
            _reference_field_row(df.fieldname, df.label, df.fieldtype, values)
        )
        seen.add(df.fieldname)

    txt = frappe.as_unicode(txt or "").lower()
    if txt:
        fields = [
            field
            for field in fields
            if txt
            in " ".join(
                [
                    field["fieldname"],
                    field["label"],
                    field["fieldtype"],
                    field["expression"],
                    field["value_preview"],
                ]
            ).lower()
        ]

    return {
        "doctype": doctype,
        "docname": docname,
        "fields": fields[: int(limit or 50)],
    }


def _document_as_dict(doc) -> Dict[str, Any]:
    if hasattr(doc, "as_dict"):
        try:
            return doc.as_dict()
        except AttributeError:
            pass
    if hasattr(doc, "get_valid_dict"):
        return dict(doc.get_valid_dict())
    if hasattr(doc, "__dict__"):
        return {
            key: value
            for key, value in doc.__dict__.items()
            if not key.startswith("_")
        }
    return dict(doc)


def _get_document_context(doctype=None, docname=None) -> Dict[str, Any]:
    if not (doctype or docname):
        return {}
    if not (doctype and docname):
        frappe.throw("Select both Reference DocType and Reference Document.")

    doc = frappe.get_cached_doc(doctype, docname)
    if not frappe.has_permission(doctype, "read", doc):
        frappe.throw(f"Not permitted to read {doctype} {docname}.")

    context = _document_as_dict(doc)
    context["doc"] = doc
    return context


def _render_template(value: Optional[str], context: Dict[str, Any]) -> str:
    value = value or ""
    return frappe.render_template(value, context) if value else ""


def _render_notification_content(title, body, doctype=None, docname=None):
    context = _get_document_context(doctype=doctype, docname=docname)
    return _render_template(title, context), _render_template(body, context)


@frappe.whitelist()
def preview_notification(title="", body="", doctype=None, docname=None):
    _require_system_manager()
    rendered_title, rendered_body = _render_notification_content(
        title=title,
        body=body,
        doctype=doctype,
        docname=docname,
    )
    return {"title": rendered_title, "body": rendered_body}


@frappe.whitelist()
def get_message_template(template_type=None, template_name=None):
    _require_system_manager()
    if not (template_type and template_name):
        return {"title": "", "body": ""}

    if template_type not in ("Email Template", "Notification"):
        frappe.throw(f"Unsupported template type: {template_type}")

    template = frappe.get_doc(template_type, template_name)
    if not frappe.has_permission(template_type, "read", template):
        frappe.throw(f"Not permitted to read {template_type}.")

    if template_type == "Email Template":
        body = template.response_html if template.use_html else template.response
        return {"title": template.subject or "", "body": body or ""}

    return {"title": template.subject or "", "body": template.message or ""}


def _coerce_bool(value) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _create_notification_log(
    user: str,
    title: str,
    body: str,
    doctype: Optional[str],
    docname: Optional[str],
    notification_type: str,
    enqueue: bool,
):
    notification = frappe.get_doc(
        {
            "doctype": "Notification Log",
            "subject": title,
            "email_content": body,
            "for_user": user,
            "type": notification_type,
            "document_type": doctype,
            "document_name": docname,
            "from_user": _get_session_user(),
        }
    )
    notification.send_now = not enqueue
    notification.flags.skip_fcm_send = True
    notification.insert(ignore_permissions=True)
    return notification


@frappe.whitelist()
def send_notification_center(
    roles=None,
    user_groups=None,
    users=None,
    platform=None,
    title="",
    body="",
    doctype=None,
    docname=None,
    enqueue=False,
    create_notification_logs=False,
    notification_type=DEFAULT_NOTIFICATION_TYPE,
    data=None,
):
    _require_system_manager()

    if not title:
        frappe.throw("Title is required.")
    if not body:
        frappe.throw("Body is required.")

    summary = _collect_recipients(
        roles=roles,
        user_groups=user_groups,
        users=users,
        platform=platform,
    )
    if not summary["devices"]:
        frappe.throw("No enabled user devices matched the selected recipients.")

    enqueue = _coerce_bool(enqueue)
    create_notification_logs = _coerce_bool(create_notification_logs)
    notification_type = notification_type or DEFAULT_NOTIFICATION_TYPE
    rendered_title, rendered_body = _render_notification_content(
        title=title,
        body=body,
        doctype=doctype,
        docname=docname,
    )

    if isinstance(data, str):
        data = json.loads(data) if data else {}
    data = data or {}

    notification_logs = []
    if create_notification_logs:
        for user in [row["user"] for row in summary["users"]]:
            notification = _create_notification_log(
                user=user,
                title=rendered_title,
                body=rendered_body,
                doctype=doctype,
                docname=docname,
                notification_type=notification_type,
                enqueue=enqueue,
            )
            notification_logs.append(notification.name)
            send_notification(
                notification,
                send_async=enqueue,
                devices=summary["devices_by_user"].get(user, []),
                ignore_skip_flag=True,
            )
        delivery_mode = "notification_log"
    else:
        send_direct_notification(
            title=rendered_title,
            body=rendered_body,
            devices=summary["devices"],
            data=data,
            doctype=doctype,
            docname=docname,
            notification_type=notification_type,
            enqueue=enqueue,
        )
        delivery_mode = "direct"

    response = _public_recipient_summary(summary)
    response.update(
        {
            "status": "success",
            "delivery_mode": delivery_mode,
            "enqueue": enqueue,
            "notification_logs": notification_logs,
        }
    )
    return response
