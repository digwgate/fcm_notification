from types import SimpleNamespace

import pytest

import fcm_notification.notification_center as notification_center


def install_permissions(monkeypatch):
    monkeypatch.setattr(notification_center.frappe, "only_for", lambda role: None)
    monkeypatch.setattr(
        notification_center.frappe,
        "has_permission",
        lambda doctype, ptype="read", doc=None: True,
        raising=False,
    )


def install_throw(monkeypatch):
    def throw(message, *args, **kwargs):
        raise ValueError(message)

    monkeypatch.setattr(notification_center.frappe, "throw", throw)


def install_settings(
    monkeypatch,
    roles=None,
    doctypes=None,
    blocked_patterns=None,
):
    settings = SimpleNamespace(
        notification_center_roles=[
            SimpleNamespace(role=role) for role in (roles or ["Sales User", "System Manager"])
        ],
        notification_center_doctypes=[
            SimpleNamespace(reference_doctype=doctype)
            for doctype in (doctypes or ["Task", "User"])
        ],
        notification_center_blocked_patterns=[
            SimpleNamespace(**pattern) for pattern in (blocked_patterns or [])
        ],
    )
    monkeypatch.setattr(
        notification_center.frappe,
        "get_single",
        lambda doctype: settings,
        raising=False,
    )


def test_get_recipients_unions_targets_and_filters_by_enabled_platform(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, roles=["Sales User"])

    def get_all(doctype, filters=None, fields=None, **kwargs):
        if doctype == "User Device":
            devices = [
                {
                    "name": "android-role",
                    "user": "role@example.com",
                    "platform": "Android",
                    "device_token": "role-android",
                },
                {
                    "name": "ios-group",
                    "user": "group@example.com",
                    "platform": "IOS",
                    "device_token": "group-ios",
                },
                {
                    "name": "android-manual",
                    "user": "manual@example.com",
                    "platform": "Android",
                    "device_token": "manual-android",
                },
                {
                    "name": "disabled-token",
                    "user": "manual@example.com",
                    "platform": "IOS",
                    "device_token": "",
                },
            ]
            if filters and filters.get("platform"):
                devices = [
                    device
                    for device in devices
                    if device["platform"] == filters["platform"]
                ]
            return devices

        if doctype == "Has Role":
            return [{"parent": "role@example.com"}, {"parent": "no-device@example.com"}]

        if doctype == "User Group Member":
            return [{"user": "group@example.com"}]

        if doctype == "User":
            return [
                {
                    "name": "role@example.com",
                    "full_name": "Role User",
                    "email": "role@example.com",
                    "enabled": 1,
                    "user_image": None,
                },
                {
                    "name": "manual@example.com",
                    "full_name": "Manual User",
                    "email": "manual@example.com",
                    "enabled": 1,
                    "user_image": None,
                },
            ]

        return []

    monkeypatch.setattr(notification_center.frappe, "get_all", get_all)

    result = notification_center.get_recipients(
        roles=["Sales User"],
        user_groups=["Mobile Users"],
        users=["manual@example.com"],
        platform="Android",
    )

    assert [user["user"] for user in result["users"]] == [
        "manual@example.com",
        "role@example.com",
    ]
    assert result["user_count"] == 2
    assert result["device_count"] == 2
    assert result["platform_counts"] == {"Android": 2}


def test_get_enabled_user_options_excludes_users_without_enabled_devices(monkeypatch):
    install_permissions(monkeypatch)

    def get_all(doctype, filters=None, fields=None, **kwargs):
        if doctype == "User Device":
            return [
                {
                    "name": "device-1",
                    "user": "has-device@example.com",
                    "platform": "IOS",
                    "device_token": "ios-token",
                }
            ]

        if doctype == "User":
            return [
                {
                    "name": "has-device@example.com",
                    "full_name": "Has Device",
                    "email": "has-device@example.com",
                    "enabled": 1,
                    "user_image": None,
                },
                {
                    "name": "no-device@example.com",
                    "full_name": "No Device",
                    "email": "no-device@example.com",
                    "enabled": 1,
                    "user_image": None,
                },
            ]

        return []

    monkeypatch.setattr(notification_center.frappe, "get_all", get_all)

    result = notification_center.get_enabled_user_options(txt="device", platform="IOS")

    assert result == [
        {
            "value": "has-device@example.com",
            "label": "Has Device",
            "description": "has-device@example.com",
        }
    ]


def test_get_role_options_excludes_guest_and_sets_blank_description(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, roles=["System Manager"])

    monkeypatch.setattr(
        notification_center.frappe,
        "get_all",
        lambda doctype, **kwargs: [
            {"name": "Guest"},
            {"name": "System Manager"},
        ],
    )

    result = notification_center.get_role_options()

    assert result == [
        {
            "value": "System Manager",
            "label": "System Manager",
            "description": "",
        }
    ]


def test_get_role_options_uses_notification_center_settings(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, roles=["Sales User"])

    def get_all(doctype, filters=None, **kwargs):
        assert doctype == "Role"
        assert ["Role", "name", "in", ["Sales User"]] in filters
        return [{"name": "Sales User"}]

    monkeypatch.setattr(notification_center.frappe, "get_all", get_all)

    result = notification_center.get_role_options()

    assert result == [
        {
            "value": "Sales User",
            "label": "Sales User",
            "description": "",
        }
    ]


def test_get_user_group_options_sets_blank_description(monkeypatch):
    install_permissions(monkeypatch)

    monkeypatch.setattr(
        notification_center.frappe,
        "get_all",
        lambda doctype, **kwargs: [{"name": "Mobile Users"}],
    )

    result = notification_center.get_user_group_options()

    assert result == [
        {
            "value": "Mobile Users",
            "label": "Mobile Users",
            "description": "",
        }
    ]


def test_guest_role_is_ignored_when_resolving_recipients(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, roles=["System Manager"])

    def get_all(doctype, filters=None, fields=None, **kwargs):
        if doctype == "User Device":
            return [
                {
                    "name": "device-1",
                    "user": "system@example.com",
                    "platform": "Android",
                    "device_token": "system-token",
                },
                {
                    "name": "device-2",
                    "user": "guest@example.com",
                    "platform": "Android",
                    "device_token": "guest-token",
                },
            ]

        if doctype == "Has Role":
            roles = filters["role"][1]
            assert "Guest" not in roles
            return [{"parent": "system@example.com"}]

        if doctype == "User":
            return [
                {
                    "name": "system@example.com",
                    "full_name": "System User",
                    "email": "system@example.com",
                    "enabled": 1,
                    "user_image": None,
                }
            ]

        return []

    monkeypatch.setattr(notification_center.frappe, "get_all", get_all)

    result = notification_center.get_recipients(roles=["Guest", "System Manager"])

    assert [user["user"] for user in result["users"]] == ["system@example.com"]
    assert result["device_count"] == 1


def test_get_reference_fields_returns_jinja_snippets_with_document_values(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, doctypes=["Task"])

    class FakeDoc:
        name = "TASK-1"

        def as_dict(self):
            return {
                "name": "TASK-1",
                "subject": "Fix login",
                "description": "Users cannot sign in",
                "secret": "do-not-expose",
            }

    monkeypatch.setattr(
        notification_center.frappe,
        "get_cached_doc",
        lambda doctype, docname: FakeDoc(),
    )
    monkeypatch.setattr(
        notification_center.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(
            fields=[
                SimpleNamespace(
                    fieldname="subject",
                    label="Subject",
                    fieldtype="Data",
                    hidden=0,
                    options=None,
                ),
                SimpleNamespace(
                    fieldname="description",
                    label="Description",
                    fieldtype="Text",
                    hidden=0,
                    options=None,
                ),
                SimpleNamespace(
                    fieldname="secret",
                    label="Secret",
                    fieldtype="Password",
                    hidden=0,
                    options=None,
                ),
            ]
        ),
    )

    result = notification_center.get_reference_fields("Task", "TASK-1")

    assert result["doctype"] == "Task"
    assert result["docname"] == "TASK-1"
    assert {
        "fieldname": "subject",
        "label": "Subject",
        "fieldtype": "Data",
        "expression": "{{ doc.subject }}",
        "value_preview": "Fix login",
    } in result["fields"]
    assert {
        "fieldname": "description",
        "label": "Description",
        "fieldtype": "Text",
        "expression": "{{ doc.description }}",
        "value_preview": "Users cannot sign in",
    } in result["fields"]
    assert all(field["fieldname"] != "secret" for field in result["fields"])


def test_get_reference_fields_falls_back_when_cached_doc_as_dict_fails(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, doctypes=["User"])

    class FakeDoc:
        name = "Administrator"

        def as_dict(self):
            raise AttributeError("'dict' object has no attribute 'as_dict'")

        def get_valid_dict(self):
            return {
                "name": "Administrator",
                "first_name": "Admin",
            }

    monkeypatch.setattr(
        notification_center.frappe,
        "get_cached_doc",
        lambda doctype, docname: FakeDoc(),
    )
    monkeypatch.setattr(
        notification_center.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(
            fields=[
                SimpleNamespace(
                    fieldname="first_name",
                    label="First Name",
                    fieldtype="Data",
                    hidden=0,
                    options=None,
                )
            ]
        ),
    )

    result = notification_center.get_reference_fields("User", "Administrator")

    assert {
        "fieldname": "first_name",
        "label": "First Name",
        "fieldtype": "Data",
        "expression": "{{ doc.first_name }}",
        "value_preview": "Admin",
    } in result["fields"]


def test_get_reference_fields_rejects_doctype_not_configured(monkeypatch):
    install_permissions(monkeypatch)
    install_throw(monkeypatch)
    install_settings(monkeypatch, doctypes=["Task"])

    with pytest.raises(ValueError, match="not enabled for Notification Center references"):
        notification_center.get_reference_fields("User")


def test_get_reference_doctype_options_returns_configured_doctypes(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch, doctypes=["Sales Order", "Task"])

    def get_all(doctype, filters=None, **kwargs):
        assert doctype == "DocType"
        assert ["DocType", "name", "in", ["Sales Order", "Task"]] in filters
        return [{"name": "Sales Order"}, {"name": "Task"}]

    monkeypatch.setattr(notification_center.frappe, "get_all", get_all)

    result = notification_center.get_reference_doctype_options()

    assert result == [
        {
            "value": "Sales Order",
            "label": "Sales Order",
            "description": "",
        },
        {
            "value": "Task",
            "label": "Task",
            "description": "",
        },
    ]


def test_collect_recipients_rejects_unconfigured_role(monkeypatch):
    install_permissions(monkeypatch)
    install_throw(monkeypatch)
    install_settings(monkeypatch, roles=["Sales User"])

    with pytest.raises(ValueError, match="not enabled for Notification Center targeting"):
        notification_center.get_recipients(roles=["Purchase User"])


def test_send_notification_center_uses_filtered_devices_for_direct_delivery(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch)
    sent = []

    monkeypatch.setattr(
        notification_center,
        "_collect_recipients",
        lambda *args, **kwargs: {
            "users": [
                {
                    "user": "target@example.com",
                    "full_name": "Target User",
                    "email": "target@example.com",
                    "enabled_devices": 1,
                    "platforms": ["Android"],
                }
            ],
            "user_count": 1,
            "device_count": 1,
            "platform_counts": {"Android": 1},
            "devices_by_user": {
                "target@example.com": [
                    {
                        "name": "device-1",
                        "user": "target@example.com",
                        "platform": "Android",
                        "device_token": "android-token",
                    }
                ]
            },
            "devices": [
                {
                    "name": "device-1",
                    "user": "target@example.com",
                    "platform": "Android",
                    "device_token": "android-token",
                }
            ],
        },
    )
    monkeypatch.setattr(
        notification_center,
        "_render_notification_content",
        lambda title, body, doctype=None, docname=None: ("Rendered Title", "Rendered Body"),
    )
    monkeypatch.setattr(
        notification_center,
        "send_direct_notification",
        lambda **kwargs: sent.append(kwargs),
    )

    result = notification_center.send_notification_center(
        users=["target@example.com"],
        platform="Android",
        title="Title",
        body="Body",
        doctype="Task",
        docname="TASK-1",
        enqueue=True,
        create_notification_logs=False,
    )

    assert sent == [
        {
            "title": "Rendered Title",
            "body": "Rendered Body",
            "devices": [
                {
                    "name": "device-1",
                    "user": "target@example.com",
                    "platform": "Android",
                    "device_token": "android-token",
                }
            ],
            "data": {},
            "doctype": "Task",
            "docname": "TASK-1",
            "notification_type": "Alert",
            "enqueue": True,
        }
    ]
    assert result["delivery_mode"] == "direct"
    assert result["user_count"] == 1
    assert result["device_count"] == 1


def test_get_message_template_loads_email_template_content(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch)

    monkeypatch.setattr(
        notification_center.frappe,
        "get_doc",
        lambda doctype, name: SimpleNamespace(
            subject="Template {{ doc.name }}",
            use_html=0,
            response="Body {{ doc.status }}",
            response_html="<p>Body</p>",
        ),
    )

    result = notification_center.get_message_template("Email Template", "Welcome")

    assert result == {
        "title": "Template {{ doc.name }}",
        "body": "Body {{ doc.status }}",
    }


def test_preview_notification_renders_jinja_with_document_context(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch)

    class FakeDoc:
        name = "TASK-1"
        status = "Open"

        def as_dict(self):
            return {"name": self.name, "status": self.status}

    monkeypatch.setattr(
        notification_center.frappe,
        "get_cached_doc",
        lambda doctype, docname: FakeDoc(),
    )

    def render_template(template, context):
        return (
            template.replace("{{ doc.name }}", context["doc"].name)
            .replace("{{ name }}", context["name"])
            .replace("{{ doc.status }}", context["doc"].status)
        )

    monkeypatch.setattr(notification_center.frappe, "render_template", render_template)

    result = notification_center.preview_notification(
        title="Task {{ doc.name }}",
        body="{{ name }} is {{ doc.status }}",
        doctype="Task",
        docname="TASK-1",
    )

    assert result == {"title": "Task TASK-1", "body": "TASK-1 is Open"}


def test_preview_notification_rejects_unconfigured_reference_doctype(monkeypatch):
    install_permissions(monkeypatch)
    install_throw(monkeypatch)
    install_settings(monkeypatch, doctypes=["Task"])

    with pytest.raises(ValueError, match="not enabled for Notification Center references"):
        notification_center.preview_notification(
            title="User {{ doc.name }}",
            body="Blocked reference",
            doctype="User",
            docname="Administrator",
        )


def test_send_notification_center_creates_logs_and_dispatches_filtered_devices(monkeypatch):
    install_permissions(monkeypatch)
    install_settings(monkeypatch)
    inserted = []
    dispatched = []

    monkeypatch.setattr(
        notification_center,
        "_collect_recipients",
        lambda *args, **kwargs: {
            "users": [
                {
                    "user": "target@example.com",
                    "full_name": "Target User",
                    "email": "target@example.com",
                    "enabled_devices": 1,
                    "platforms": ["IOS"],
                }
            ],
            "user_count": 1,
            "device_count": 1,
            "platform_counts": {"IOS": 1},
            "devices_by_user": {
                "target@example.com": [
                    {
                        "name": "device-1",
                        "user": "target@example.com",
                        "platform": "IOS",
                        "device_token": "ios-token",
                    }
                ]
            },
            "devices": [
                {
                    "name": "device-1",
                    "user": "target@example.com",
                    "platform": "IOS",
                    "device_token": "ios-token",
                }
            ],
        },
    )
    monkeypatch.setattr(
        notification_center,
        "_render_notification_content",
        lambda title, body, doctype=None, docname=None: ("Rendered Title", "Rendered Body"),
    )
    monkeypatch.setattr(
        notification_center, "_get_session_user", lambda: "sender@example.com"
    )

    class FakeNotificationLog:
        def __init__(self, values):
            self.__dict__.update(values)
            self.flags = SimpleNamespace()
            self.name = "LOG-1"

        def insert(self, ignore_permissions=False):
            inserted.append(
                {
                    "ignore_permissions": ignore_permissions,
                    "for_user": self.for_user,
                    "subject": self.subject,
                    "email_content": self.email_content,
                    "send_now": self.send_now,
                    "skip_fcm_send": self.flags.skip_fcm_send,
                }
            )
            return self

    monkeypatch.setattr(
        notification_center.frappe,
        "get_doc",
        lambda values: FakeNotificationLog(values),
    )
    monkeypatch.setattr(
        notification_center,
        "send_notification",
        lambda notification, send_async=None, devices=None, ignore_skip_flag=False: dispatched.append(
            {
                "notification": notification.name,
                "send_async": send_async,
                "devices": devices,
                "ignore_skip_flag": ignore_skip_flag,
            }
        ),
    )

    result = notification_center.send_notification_center(
        users=["target@example.com"],
        platform="IOS",
        title="Title",
        body="Body",
        doctype="Task",
        docname="TASK-1",
        enqueue=False,
        create_notification_logs=True,
    )

    assert inserted == [
        {
            "ignore_permissions": True,
            "for_user": "target@example.com",
            "subject": "Rendered Title",
            "email_content": "Rendered Body",
            "send_now": True,
            "skip_fcm_send": True,
        }
    ]
    assert dispatched == [
        {
            "notification": "LOG-1",
            "send_async": False,
            "devices": [
                {
                    "name": "device-1",
                    "user": "target@example.com",
                    "platform": "IOS",
                    "device_token": "ios-token",
                }
            ],
            "ignore_skip_flag": True,
        }
    ]
    assert result["delivery_mode"] == "notification_log"
    assert result["notification_logs"] == ["LOG-1"]


def test_send_notification_center_blocks_configured_pattern_after_render(monkeypatch):
    install_permissions(monkeypatch)
    install_throw(monkeypatch)
    install_settings(
        monkeypatch,
        blocked_patterns=[
            {"pattern": "blocked phrase", "match_type": "Contains", "case_sensitive": 0}
        ],
    )
    sent = []

    monkeypatch.setattr(
        notification_center,
        "_collect_recipients",
        lambda *args, **kwargs: {
            "users": [
                {
                    "user": "target@example.com",
                    "full_name": "Target User",
                    "email": "target@example.com",
                    "enabled_devices": 1,
                    "platforms": ["Android"],
                }
            ],
            "user_count": 1,
            "device_count": 1,
            "platform_counts": {"Android": 1},
            "devices_by_user": {"target@example.com": []},
            "devices": [
                {
                    "name": "device-1",
                    "user": "target@example.com",
                    "platform": "Android",
                    "device_token": "android-token",
                }
            ],
        },
    )
    monkeypatch.setattr(
        notification_center,
        "_render_notification_content",
        lambda title, body, doctype=None, docname=None: (
            "Rendered Title",
            "Rendered body with Blocked Phrase",
        ),
    )
    monkeypatch.setattr(
        notification_center,
        "send_direct_notification",
        lambda **kwargs: sent.append(kwargs),
    )

    with pytest.raises(ValueError, match="blocked word or pattern"):
        notification_center.send_notification_center(
            users=["target@example.com"],
            title="Title",
            body="Body",
        )

    assert sent == []


def test_send_notification_center_requires_a_target(monkeypatch):
    install_permissions(monkeypatch)
    install_throw(monkeypatch)
    install_settings(monkeypatch)

    with pytest.raises(ValueError, match="Select at least one"):
        notification_center.send_notification_center(title="Title", body="Body")
