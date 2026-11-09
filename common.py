"""Shared helpers: OpenFGA HTTP calls + the authorization model."""
import json
import os

FGA_URL = os.environ.get("FGA_URL", "http://localhost:8080")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Same model as the DSL:
#   type user
#   type group     -> member: [user]
#   type folder    -> viewer: [user, group#member]
#   type document  -> parent: [folder]
#                     viewer: [user, group#member] or viewer from parent
MODEL = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "group",
            "relations": {"member": {"this": {}}},
            "metadata": {"relations": {"member": {"directly_related_user_types": [{"type": "user"}]}}},
        },
        {
            "type": "folder",
            "relations": {"viewer": {"this": {}}},
            "metadata": {
                "relations": {
                    "viewer": {"directly_related_user_types": [{"type": "user"}, {"type": "group", "relation": "member"}]}
                }
            },
        },
        {
            "type": "document",
            "relations": {
                "parent": {"this": {}},
                "viewer": {
                    "union": {
                        "child": [
                            {"this": {}},
                            {
                                "tupleToUserset": {
                                    "tupleset": {"relation": "parent"},
                                    "computedUserset": {"relation": "viewer"},
                                }
                            },
                        ]
                    }
                },
            },
            "metadata": {
                "relations": {
                    "parent": {"directly_related_user_types": [{"type": "folder"}]},
                    "viewer": {"directly_related_user_types": [{"type": "user"}, {"type": "group", "relation": "member"}]},
                }
            },
        },
    ],
}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)
