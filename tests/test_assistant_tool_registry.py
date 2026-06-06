from app.services.assistant_tool_inventory import (
    is_excluded_non_routable,
    iter_excluded_non_routable_tool_ids,
    iter_partner_tool_definitions,
)
from app.services.assistant_tool_registry import (
    canonical_tool_id,
    iter_builtin_tool_registrations,
    iter_tool_aliases,
)

WAVE1_ORDER_READ_TOOLS = {
    "orders.catering_by_status": "orders_catering_by_status",
    "orders.catering_by_store": "orders_catering_by_store",
    "orders.catering_count": "orders_catering_count",
    "orders.catering_driver_assignment_summary": "orders_catering_driver_assignment_summary",
    "orders.catering_fees_summary": "orders_catering_fees_summary",
    "orders.catering_item_mix": "orders_catering_item_mix",
    "orders.catering_late_risk": "orders_catering_late_risk",
    "orders.catering_live_tracking": "orders_catering_live_tracking",
    "orders.catering_needs_driver": "orders_catering_needs_driver",
    "orders.catering_next_30_days": "orders_catering_next_30_days",
    "orders.catering_order_items_safe": "orders_catering_order_items_safe",
    "orders.catering_order_lookup": "orders_catering_order_lookup",
    "orders.catering_payout_safe_summary": "orders_catering_payout_safe_summary",
    "orders.catering_pdf_status": "orders_catering_pdf_status",
    "orders.catering_returning_customers_aggregate": "orders_catering_returning_customers_aggregate",
    "orders.catering_today": "orders_catering_today",
    "orders.catering_tomorrow": "orders_catering_tomorrow",
    "orders.catering_tracking_missing": "orders_catering_tracking_missing",
    "orders.catering_uuid_status": "orders_catering_uuid_status",
    "orders.catering_week": "orders_catering_week",
    "orders.in_house_quote_lookup": "orders_in_house_quote_lookup",
    "orders.in_house_quotes_summary": "orders_in_house_quotes_summary",
}


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


def test_wave1_orders_reads_are_registered_without_write_like_actions():
    tools = {tool["tool_id"]: tool for tool in iter_builtin_tool_registrations()}

    for tool_id, handler in WAVE1_ORDER_READ_TOOLS.items():
        assert tools[tool_id]["handler"] == handler
        assert tools[tool_id]["matcher"] == handler
        assert tools[tool_id]["formatter"] == handler
        assert tools[tool_id]["read_write_class"] == "read_only"
        assert tools[tool_id]["status"] == "review_gated"
        assert tools[tool_id]["operator_enabled"] is True
        assert tools[tool_id]["required_permissions"] == ["ai.ask_claude", "orders.view"]
    assert "orders.update_status" not in tools
    assert "orders.reassign_store" not in tools
    assert "orders.refresh_ezcater_tracking" not in tools
