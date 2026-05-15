"""Phase 2 / Block 1C — ribbon content router tests.

Per the 1C spec §11:
  - framing matrix: same Task viewed as owner_todo / owner_category /
    observer / escalated_to → the four relations + framings
  - one-source-two-items: owner on the matching domain page gets two
    items for one Task, same item_type/item_id
  - role + store-scope filter: a Tomball GM doesn't get Copperfield
    items; partner gets both stores'
  - page-relevance: dashboard = alerts + todo only; domain page =
    full domain detail
  - dismissal exclusion: a today-dated RibbonItemDismissal hides the
    item; a yesterday-dated one doesn't (daily reset)
  - sort: _ribbon_sort_key — alert>warn>info, soonest-deadline-first,
    null deadline last
  - per-adapter: each of the four adapters in isolation
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import (
    Task, TaskAuditLog, RibbonItemDismissal, Signal, User, ScheduledEvent,
    AmbientSignal,
)
from app.services import ribbon as rb
from app.services.ribbon import (
    RibbonItem,
    ribbon_items_for,
    _ribbon_sort_key,
    _task_severity,
    _task_to_items,
    _signal_to_item,
    _scheduled_event_to_item,
    _sales_insight_to_item,
    _ambient_signal_to_item,
)


_NOW = datetime(2026, 5, 14, 12, 0, 0)


# ============================================================
# _task_severity
# ============================================================

def test_task_severity_escalated_is_alert():
    t = SimpleNamespace(escalated_to_user_id=9,
                        deadline_at=_NOW + timedelta(days=5))
    assert _task_severity(t, _NOW) == "alert"


def test_task_severity_past_deadline_is_alert():
    t = SimpleNamespace(escalated_to_user_id=None,
                        deadline_at=_NOW - timedelta(hours=1))
    assert _task_severity(t, _NOW) == "alert"


def test_task_severity_within_24h_is_warn():
    t = SimpleNamespace(escalated_to_user_id=None,
                        deadline_at=_NOW + timedelta(hours=6))
    assert _task_severity(t, _NOW) == "warn"


def test_task_severity_far_future_is_info():
    t = SimpleNamespace(escalated_to_user_id=None,
                        deadline_at=_NOW + timedelta(days=5))
    assert _task_severity(t, _NOW) == "info"


# ============================================================
# _ribbon_sort_key
# ============================================================

def _item(severity, deadline):
    return RibbonItem(
        category="todo", severity=severity, item_type="task", item_id=1,
        deadline_at=deadline, can_dismiss=True, can_check=True,
        relation="owner_todo",
    )


def test_sort_severity_desc():
    items = [_item("info", None), _item("alert", None), _item("warn", None)]
    items.sort(key=_ribbon_sort_key)
    assert [i.severity for i in items] == ["alert", "warn", "info"]


def test_sort_deadline_asc_within_band():
    a = _item("warn", _NOW + timedelta(hours=5))
    b = _item("warn", _NOW + timedelta(hours=1))
    items = [a, b]
    items.sort(key=_ribbon_sort_key)
    assert items[0] is b  # soonest deadline first


def test_sort_null_deadline_last_within_band():
    a = _item("warn", None)
    b = _item("warn", _NOW + timedelta(hours=1))
    items = [a, b]
    items.sort(key=_ribbon_sort_key)
    assert items[0] is b and items[1] is a  # null deadline sorts last


# ============================================================
# Adapter: _task_to_items
# ============================================================

def _task(**over):
    base = dict(
        id=1, title="SPECS liquor order", description=None,
        owner_user_id=10, assigned_by_user_id=20,
        store_scope="tomball", category="vendor",
        deadline_at=_NOW + timedelta(hours=3),
        completed_at=None, completed_by_user_id=None,
        escalated_to_user_id=None, escalated_at=None,
        created_at=_NOW, updated_at=_NOW,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_task_adapter_owner_gets_todo_and_domain():
    """An owner of a vendor task gets TWO items: owner_todo in todo,
    owner_category in vendors — same item_type/item_id."""
    t = _task(owner_user_id=10, category="vendor")
    viewer = SimpleNamespace(id=10, permission_level="cook",
                             store_scope="tomball")
    items = _task_to_items(t, viewer, None, "Cook Ten")
    assert len(items) == 2
    by_cat = {i.category: i for i in items}
    assert by_cat["todo"].relation == "owner_todo"
    assert by_cat["vendors"].relation == "owner_category"
    assert by_cat["todo"].item_id == by_cat["vendors"].item_id == 1
    assert by_cat["todo"].item_type == by_cat["vendors"].item_type == "task"


def test_task_adapter_owner_general_gets_todo_only():
    """A general task has no domain category — owner gets ONE item."""
    t = _task(owner_user_id=10, category="general")
    viewer = SimpleNamespace(id=10, permission_level="cook",
                             store_scope="tomball")
    items = _task_to_items(t, viewer, None, "Cook Ten")
    assert len(items) == 1
    assert items[0].category == "todo"
    assert items[0].relation == "owner_todo"


def test_task_adapter_observer_gets_domain_only_no_todo():
    """A non-owner non-escalated viewer gets only the domain-category
    observer item — NOT a todo item (the manager's-to-do-shows-nothing
    rule)."""
    t = _task(owner_user_id=10, category="vendor")
    viewer = SimpleNamespace(id=99, permission_level="gm",
                             store_scope="tomball")
    items = _task_to_items(t, viewer, None, "Cook Ten")
    assert len(items) == 1
    assert items[0].category == "vendors"
    assert items[0].relation == "observer"
    assert items[0].can_check is False  # observers can't complete


def test_task_adapter_escalated_to_gets_todo_escalated():
    """An escalated-to viewer gets a todo-category item with the
    escalated_to relation + can_check True."""
    t = _task(owner_user_id=10, category="vendor", escalated_to_user_id=99)
    viewer = SimpleNamespace(id=99, permission_level="gm",
                             store_scope="tomball")
    items = _task_to_items(t, viewer, None, "Cook Ten")
    todo = next(i for i in items if i.category == "todo")
    assert todo.relation == "escalated_to"
    assert todo.can_check is True


# ============================================================
# Adapter: _signal_to_item / _scheduled_event_to_item /
#          _sales_insight_to_item
# ============================================================

def test_signal_adapter_maps_category_by_prefix():
    """samai 1C spec §13.1 map: orders.* → caterings (ezCater
    catering-order fulfillment, NOT vendors — refines the placeholder)."""
    sig = SimpleNamespace(id=5, rule_name="orders.late_delivery",
                          severity="warn", subject_label="Order X",
                          action_text="Call the driver.", store_id="tomball")
    item = _signal_to_item(sig, None)
    assert item.category == "caterings"   # orders.* → caterings (§13.1)
    assert item.item_type == "signal"
    assert item.relation == "observer"
    assert item.can_check is True
    assert item.severity == "warn"


@pytest.mark.parametrize("rule_name,expected", [
    ("vendor.invoice_overdue", "vendors"),
    ("produce.price_spike", "vendors"),
    ("orders.ezcater_rejection_rate", "caterings"),
    ("sales.daily_low", "sales"),
    ("labor.overtime_spike", "employee"),
    ("attendance.callout_spike", "employee"),
    ("server.low_tip_rate", "employee"),
    ("customer.bad_review", "employee"),
    ("kitchen.prep_yield_low", "employee"),
    ("system.deploy_failed", "maintenance"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_signal_adapter_full_13_1_prefix_map(rule_name, expected):
    """The full samai §13.1 domain-prefix map — all ten anomaly
    domains routed to their ribbon category."""
    sig = SimpleNamespace(id=1, rule_name=rule_name, severity="info",
                          subject_label="X", action_text=None,
                          store_id=None)
    assert _signal_to_item(sig, None).category == expected


def test_signal_adapter_full_rulename_override():
    """§13.1 override: kitchen.equipment_down → maintenance (NOT
    employee, which the kitchen. prefix would give) — the full
    rule_name override is checked first."""
    sig = SimpleNamespace(id=2, rule_name="kitchen.equipment_down",
                          severity="alert", subject_label="Fryer down",
                          action_text="Call the tech.", store_id="tomball")
    assert _signal_to_item(sig, None).category == "maintenance"


def test_signal_adapter_unknown_prefix_defaults():
    sig = SimpleNamespace(id=6, rule_name="weird.unknown_rule",
                          severity="info", subject_label="X",
                          action_text=None, store_id=None)
    item = _signal_to_item(sig, None)
    assert item.category == "employee"  # _SIGNAL_DEFAULT_RIBBON


def test_scheduled_event_adapter_catering_vs_event():
    cat = SimpleNamespace(id=1, category="catering", title="Wedding",
                          scheduled_at=_NOW + timedelta(days=2),
                          store="tomball")
    ev = SimpleNamespace(id=2, category="event", title="Spirit night",
                         scheduled_at=_NOW + timedelta(days=1),
                         store="copperfield")
    assert _scheduled_event_to_item(cat).category == "caterings"
    assert _scheduled_event_to_item(ev).category == "events"
    item = _scheduled_event_to_item(cat)
    assert item.item_type == "scheduled_event"
    assert item.can_check is False  # you don't "complete" an event
    assert item.relation == "observer"


def test_sales_insight_adapter_duck_typed():
    """_sales_insight_to_item is pure / duck-typed (the SalesInsight
    model is 1F, not yet built) — feed it a stand-in."""
    insight = SimpleNamespace(id=7, severity="alert",
                              headline="95F + Astros game tonight",
                              detail="Expect a late dinner rush.")
    item = _sales_insight_to_item(insight)
    assert item.category == "sales"
    assert item.item_type == "sales_insight"
    assert item.severity == "alert"
    assert item.relation == "observer"


def test_ambient_signal_adapter_basic():
    """_ambient_signal_to_item (1J §7.1) — category straight from the
    signal (no _*_TO_RIBBON map), always observer, can_check False
    (mirrors scheduled_event), text/sub_text from the payload."""
    sig = SimpleNamespace(
        id=42, source="weather", category="maintenance", severity="warn",
        payload={"headline": "Tomball: 99F, heat advisory",
                 "detail": "High 99F — expect a delivery-heavy night."})
    item = _ambient_signal_to_item(sig)
    assert item.item_type == "ambient_signal"
    assert item.item_id == 42
    assert item.category == "maintenance"      # straight from signal.category
    assert item.severity == "warn"
    assert item.relation == "observer"
    assert item.can_dismiss is True
    assert item.can_check is False             # not "completable" (§7.2)
    assert item.deadline_at is None


def test_ambient_signal_adapter_category_passthrough_and_defaults():
    """category passes straight through for all three ambient
    categories; an out-of-range severity falls back to info; an empty
    payload degrades to a safe placeholder headline."""
    cat = _ambient_signal_to_item(SimpleNamespace(
        id=1, source="catering_pipeline", category="caterings",
        severity="info", payload={"headline": "Upcoming: Smith wedding"}))
    assert cat.category == "caterings"
    ev = _ambient_signal_to_item(SimpleNamespace(
        id=2, source="events", category="events", severity="bogus",
        payload={}))
    assert ev.category == "events"
    assert ev.severity == "info"               # bad severity → info
    assert ev.render_for(SimpleNamespace(id=1))["text"] == "Ambient signal"


# ============================================================
# render_for — the framing matrix (1C spec §5)
# ============================================================

@pytest.mark.parametrize("relation,expect_in_text", [
    ("owner_todo", "SPECS liquor order"),
    ("owner_category", "SPECS liquor order"),
    ("observer", "SPECS liquor order"),
    ("escalated_to", "ESCALATED"),
], ids=["relation=owner_todo", "relation=owner_category",
        "relation=observer", "relation=escalated_to"])
def test_render_for_task_framing(relation, expect_in_text):
    item = RibbonItem(
        category="todo", severity="warn", item_type="task", item_id=1,
        deadline_at=_NOW + timedelta(hours=3), can_dismiss=True,
        can_check=True, relation=relation,
        ctx={"title": "SPECS liquor order", "deadline_str": "5/14 3:00PM",
             "owner_name": "Andres"},
    )
    out = item.render_for(SimpleNamespace(id=1))
    assert expect_in_text in out["text"]
    assert "styling_class" in out
    if relation == "escalated_to":
        assert "ribbon-item--escalated" in out["styling_class"]
    if relation == "observer":
        assert "Andres" in (out["sub_text"] or "")


def test_render_for_signal_is_observer_style():
    item = _signal_to_item(SimpleNamespace(
        id=1, rule_name="labor.understaffed", severity="warn",
        subject_label="Tomball lunch", action_text="Call standby pool.",
        store_id="tomball"), None)
    out = item.render_for(SimpleNamespace(id=1))
    assert out["text"] == "Tomball lunch"
    assert out["sub_text"] == "Call standby pool."


def test_render_for_ambient_signal_is_observer_style():
    """The ambient_signal render_for branch (1J §7.3) — observer-style,
    the payload's headline/detail become text/sub_text, severity drives
    the styling class."""
    item = _ambient_signal_to_item(SimpleNamespace(
        id=3, source="outages", category="maintenance", severity="alert",
        payload={"headline": "Copperfield: power outage",
                 "detail": "CenterPoint reports ~400 customers affected."}))
    out = item.render_for(SimpleNamespace(id=1))
    assert out["text"] == "Copperfield: power outage"
    assert out["sub_text"] == "CenterPoint reports ~400 customers affected."
    assert "ribbon-item--alert" in out["styling_class"]


# ============================================================
# Router — store-scope filter, page-relevance, dismissal
# ============================================================

@pytest.fixture
def router_db(db_session, monkeypatch):
    """Seed users + tasks + signals + events into db_session, bind it
    as ribbon.py's SessionLocal. Returns (db, users-by-role)."""
    users = {
        "partner": User(id=1, full_name="Sam", email="s@x.test",
                        passcode_hash="x", permission_level="partner",
                        store_scope=None, active=True, first_login_done=True),
        "gm_tom": User(id=2, full_name="Anna", email="a@x.test",
                       passcode_hash="x", permission_level="gm",
                       store_scope="tomball", active=True,
                       first_login_done=True),
        "cook_tom": User(id=3, full_name="Cook T", email="ct@x.test",
                         passcode_hash="x", permission_level="cook",
                         store_scope="tomball", active=True,
                         first_login_done=True),
        "cook_cop": User(id=4, full_name="Cook C", email="cc@x.test",
                         passcode_hash="x", permission_level="cook",
                         store_scope="copperfield", active=True,
                         first_login_done=True),
    }
    db_session.add_all(list(users.values()))
    db_session.commit()
    monkeypatch.setattr(rb, "SessionLocal", lambda: db_session)
    return db_session, users


def _add_task(db, **over):
    base = dict(
        title="T", description=None, owner_user_id=3,
        assigned_by_user_id=2, store_scope="tomball", category="vendor",
        deadline_at=_NOW + timedelta(hours=3), completed_at=None,
        created_at=_NOW, updated_at=_NOW,
    )
    base.update(over)
    t = Task(**base)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_router_returns_empty_for_none_user(router_db):
    db, users = router_db
    assert ribbon_items_for("orders", None, "tomball") == []


def test_router_one_source_two_items_for_owner_on_domain_page(router_db):
    """Owner viewing the vendors domain page → exactly two items for
    one vendor task (todo + vendors), same item_id."""
    db, users = router_db
    t = _add_task(db, owner_user_id=3, category="vendor",
                  store_scope="tomball")
    items = ribbon_items_for("vendors", users["cook_tom"], "tomball")
    task_items = [i for i in items if i.item_type == "task"
                  and i.item_id == t.id]
    assert len(task_items) == 2
    assert {i.category for i in task_items} == {"todo", "vendors"}


def test_router_store_scope_filter_gm_scoped(router_db):
    """A Tomball GM does not see a Copperfield-scoped task they don't
    own; the partner sees both stores'."""
    db, users = router_db
    # Copperfield task owned by cook_cop
    t_cop = _add_task(db, owner_user_id=4, category="vendor",
                      store_scope="copperfield")
    # Tomball GM (store-scoped) viewing the vendors page
    gm_items = ribbon_items_for("vendors", users["gm_tom"], "tomball")
    assert not any(i.item_id == t_cop.id for i in gm_items)
    # Partner (store-unscoped) sees it
    partner_items = ribbon_items_for("vendors", users["partner"], "tomball")
    assert any(i.item_id == t_cop.id and i.item_type == "task"
               for i in partner_items)


def test_router_page_relevance_dashboard_alerts_and_todo_only(router_db):
    """On the main dashboard (unknown slug → dashboard rules), only
    alert-severity items + the viewer's todo show. A far-future info
    task owned by someone else (observer, info) is filtered out."""
    db, users = router_db
    # info-severity vendor task owned by cook_cop — observer for gm_tom
    _add_task(db, owner_user_id=4, category="vendor",
              store_scope="both",  # in scope for everyone
              deadline_at=_NOW + timedelta(days=10))
    items = ribbon_items_for("dashboard", users["gm_tom"], "tomball")
    # the observer info item is in 'vendors', severity info → filtered
    assert not any(i.category == "vendors" and i.severity == "info"
                   for i in items)


def test_router_page_relevance_domain_page_full_detail(router_db):
    """On the vendors domain page, the viewer sees vendors-category
    detail at all severities (including the info observer item)."""
    db, users = router_db
    _add_task(db, owner_user_id=4, category="vendor", store_scope="both",
              deadline_at=_NOW + timedelta(days=10))
    items = ribbon_items_for("vendors", users["gm_tom"], "tomball")
    assert any(i.category == "vendors" for i in items)


def test_router_dismissal_exclusion_today_vs_yesterday(router_db):
    """An item dismissed today is absent; a yesterday-dated dismissal
    does not hide it (daily reset)."""
    db, users = router_db
    t = _add_task(db, owner_user_id=3, category="general",
                  store_scope="tomball")
    # baseline — the owner sees their todo item
    base = ribbon_items_for("dashboard", users["cook_tom"], "tomball")
    assert any(i.item_id == t.id for i in base)
    # dismiss it TODAY
    db.add(RibbonItemDismissal(
        user_id=3, item_type="task", item_id=t.id,
        dismiss_day=date.today().isoformat()))
    db.commit()
    after = ribbon_items_for("dashboard", users["cook_tom"], "tomball")
    assert not any(i.item_id == t.id for i in after)
    # a YESTERDAY-dated dismissal would not hide it — swap the row's day
    row = db.query(RibbonItemDismissal).one()
    row.dismiss_day = (date.today() - timedelta(days=1)).isoformat()
    db.commit()
    reset = ribbon_items_for("dashboard", users["cook_tom"], "tomball")
    assert any(i.item_id == t.id for i in reset)


def test_router_signal_source_wired(router_db):
    """The Signal adapter is live — an open in-scope signal surfaces."""
    db, users = router_db
    db.add(Signal(
        rule_name="labor.understaffed", severity="warn",
        store_id="tomball", subject_id="S1", subject_label="Tomball lunch",
        trigger_at=_NOW, payload={}, action_text="Call standby.",
        surfaces=[], audience_roles=[]))
    db.commit()
    items = ribbon_items_for("roster", users["gm_tom"], "tomball")
    assert any(i.item_type == "signal" for i in items)


def test_router_scheduled_event_source_wired(router_db):
    """The ScheduledEvent adapter is live — an upcoming in-scope event
    surfaces in caterings/events."""
    db, users = router_db
    db.add(ScheduledEvent(
        store="tomball", category="catering", title="Henderson wedding",
        scheduled_at=_NOW + timedelta(days=3), status="confirmed"))
    db.commit()
    items = ribbon_items_for("caterings", users["gm_tom"], "tomball")
    assert any(i.item_type == "scheduled_event"
               and i.category == "caterings" for i in items)


def test_router_ambient_signal_source_wired(router_db):
    """The AmbientSignal adapter is live (1J §7) — a live in-scope
    ambient signal surfaces in its category. The fifth gather source,
    alongside Task / Signal / ScheduledEvent / SalesInsight. Also
    proves the valid_until_at >= now read-filter: an expired ambient
    signal does NOT surface."""
    db, users = router_db
    now = datetime.utcnow()
    db.add(AmbientSignal(
        source="weather", signal_key="tomball:forecast:wired-test",
        payload={"headline": "Tomball: 95F and humid",
                 "detail": "High 95F — expect a delivery-heavy dinner."},
        payload_hash="h" * 64, store_scope="both", category="maintenance",
        severity="warn", valid_until_at=now + timedelta(hours=6),
        created_at=now, updated_at=now, last_seen_at=now))
    db.commit()
    items = ribbon_items_for("maintenance", users["gm_tom"], "tomball")
    ambient = [i for i in items if i.item_type == "ambient_signal"]
    assert len(ambient) == 1
    assert ambient[0].category == "maintenance"
    # an expired ambient signal does NOT surface (valid_until_at < now)
    db.add(AmbientSignal(
        source="weather", signal_key="tomball:forecast:expired",
        payload={"headline": "stale"}, payload_hash="x" * 64,
        store_scope="both", category="maintenance", severity="info",
        valid_until_at=now - timedelta(hours=1),
        created_at=now, updated_at=now, last_seen_at=now))
    db.commit()
    items2 = ribbon_items_for("maintenance", users["gm_tom"], "tomball")
    assert len([i for i in items2 if i.item_type == "ambient_signal"]) == 1
