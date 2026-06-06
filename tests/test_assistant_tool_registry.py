from app.services.assistant_tool_inventory import (
    is_excluded_non_routable,
    iter_excluded_non_routable_tool_ids,
    iter_partner_tool_definitions,
)
from app.services.assistant_tool_registry import canonical_tool_id, iter_tool_aliases


def test_inventory_skips_excluded_non_routable_tools_by_default():
    excluded = set(iter_excluded_non_routable_tool_ids())
    catalog = {tool["tool_id"] for tool in iter_partner_tool_definitions()}

    assert excluded
    assert excluded.isdisjoint(catalog)


def test_non_routable_sentinel_ids_are_hard_blocked():
    for tool_id in [
        "read_file",
        "write_file",
        "list_dir",
        "file_delete",
        "shell_execute",
        "sql_query",
        "query_database",
        "fetch_url",
        "render_env_get",
        "render_env_set",
        "render_deploy",
        "run_git",
        "screenshot_url",
        "telegram_send",
        "whatsapp_send",
        "resolve_employee",
        "resolve_catering_order",
        "dev.run_script",
        "dash.cena_chat",
    ]:
        assert is_excluded_non_routable(tool_id)


def test_inventory_write_classifier_splits_underscore_verbs():
    tools = {
        tool["tool_id"]: tool
        for tool in iter_partner_tool_definitions(include_excluded=True)
    }

    assert tools["orders.update_status"]["read_write_class"] == "action_confirmation"
    assert tools["orders.mark_delivered"]["read_write_class"] == "action_confirmation"
    assert tools["drivers.reset_passcode"]["read_write_class"] == "action_confirmation"


def test_tool_alias_table_canonicalizes_confirmed_duplicates():
    aliases = dict(iter_tool_aliases())

    assert aliases["toast_live_tables"] == "toast.table_activity"
    assert aliases["fin.view_pnl"] == "finance.pnl_summary"
    assert canonical_tool_id("toast_live_tables") == "toast.table_activity"
    assert canonical_tool_id("toast.table_activity") == "toast.table_activity"


def test_tool_aliases_do_not_unblock_excluded_sentinels():
    aliases = dict(iter_tool_aliases())

    assert "read_file" not in aliases
    assert is_excluded_non_routable("read_file")
