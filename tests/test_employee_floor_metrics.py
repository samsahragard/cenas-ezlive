from app.services.employee_floor_metrics import (
    EmployeeFloorCheck,
    EmployeeFloorItem,
    EmployeeFloorTable,
    average,
    calculate_base_plus_tips,
    calculate_day_stats,
    calculate_dessert_attach,
    calculate_drink_attach,
    calculate_tip_percent,
    calculate_tips_per_hour,
    flatten_checks,
    format_money,
)


def _sample_day():
    return {
        "tables": [
            EmployeeFloorTable(
                name="61 B",
                checks=[
                    EmployeeFloorCheck(
                        id=110,
                        status="open",
                        covers=2,
                        seated_at=20 * 60,
                        drinks_fired_at=20 * 60 + 5,
                        kitchen_fired_at=20 * 60 + 16,
                        total=40.00,
                        items=[
                            EmployeeFloorItem("Monday Rita", 2, "cocktail"),
                            EmployeeFloorItem("Quesadilla", 1, "food"),
                        ],
                    ),
                    EmployeeFloorCheck(
                        id=104,
                        status="closed",
                        covers=1,
                        seated_at=18 * 60,
                        kitchen_fired_at=18 * 60 + 7,
                        closed_at=18 * 60 + 35,
                        payment_method="cash",
                        total=11.34,
                        tip=None,
                        items=[EmployeeFloorItem("Tortilla Soup", 1, "food")],
                    ),
                ],
            ),
            EmployeeFloorTable(
                name="63",
                checks=[
                    EmployeeFloorCheck(
                        id=119,
                        status="closed",
                        covers=4,
                        seated_at=18 * 60 + 40,
                        drinks_fired_at=18 * 60 + 45,
                        kitchen_fired_at=18 * 60 + 57,
                        closed_at=20 * 60 + 1,
                        payment_method="credit",
                        total=92.78,
                        tip=20.41,
                        items=[
                            EmployeeFloorItem("Monday Rita", 2, "cocktail"),
                            EmployeeFloorItem("Fish El Rey", 1, "food"),
                            EmployeeFloorItem("Birthday Churros", 1, "dessert"),
                        ],
                    )
                ],
            ),
        ]
    }


def test_flatten_and_day_stats_handle_open_pending_and_cash_unknown_tips():
    stats = calculate_day_stats(_sample_day(), pending_tip_rate=0.18, hours=5, base_pay=45)

    assert len(flatten_checks(_sample_day())) == 3
    assert stats["total_tables"] == 2
    assert stats["total_checks"] == 3
    assert stats["open_checks"] == 1
    assert stats["closed_checks"] == 2
    assert stats["sales"] == 144.12
    assert stats["recorded_tips"] == 20.41
    assert stats["pending_estimated_tips"] == 7.2
    assert stats["covers"] == 7
    assert stats["average_check"] == 48.04
    assert stats["bar_drink_count"] == 4
    assert stats["drink_attach_pct"] == 66.7
    assert stats["dessert_attach_pct"] == 33.3
    assert stats["seat_to_first_drink_avg_min"] == 5.0
    assert stats["seat_to_kitchen_fire_avg_min"] == 13.3
    assert stats["average_table_turn_min"] == 58.0
    assert stats["tips_per_hour"] == 5.52
    assert stats["base_plus_tips"] == 72.61


def test_tip_percent_keeps_null_cash_tip_neutral():
    cash = EmployeeFloorCheck(id=1, status="closed", total=20.00, tip=None, payment_method="cash")
    credit = EmployeeFloorCheck(id=2, status="closed", total=50.00, tip=10.00, payment_method="credit")

    assert calculate_tip_percent(cash) is None
    assert calculate_tip_percent(credit) == 20.0


def test_attach_rates_and_divide_by_zero_are_safe():
    assert calculate_drink_attach([]) is None
    assert calculate_dessert_attach([]) is None
    assert calculate_tips_per_hour(10, 0) is None
    assert calculate_tips_per_hour(None, 5) is None
    assert calculate_day_stats({"tables": []})["average_check"] is None


def test_small_formatting_and_math_helpers():
    assert average([1, None, 3]) == 2
    assert average([]) is None
    assert format_money(1234.5) == "$1,234.50"
    assert format_money(None) == "Not available"
    assert calculate_base_plus_tips(40, 12.5) == 52.5
    assert calculate_base_plus_tips(None, None) is None
