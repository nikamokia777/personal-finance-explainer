from datetime import date
from decimal import Decimal

from backend import finance
from backend.agents import check_answer
from backend.csv_import import mask_pii, read_statement
from backend.database import SessionLocal
from tests.conftest import add_transaction, create_user


def test_csv_parser_handles_bom_and_bad_rows():
    content = (
        "﻿date,description,amount,currency\n"
        "2026-09-02,POS PURCHASE 4521 CARREFOUR TBILISI,-87.40,GEL\n"
        "not-a-date,GLOVO,-32.90,GEL\n"
        "2026-09-05,SALARY SEPTEMBER,2800.00,GEL\n"
    ).encode("utf-8")
    rows, errors, rows_total = read_statement(content, 1, "GEL")
    assert rows_total == 3
    assert [row["merchant"] for row in rows] == ["CARREFOUR", "SALARY"]
    assert rows[0]["type"] == "expense" and rows[1]["type"] == "income"
    assert errors == [{"row": 3, "reason": "invalid date or amount"}]


def test_same_day_identical_rows_are_kept():
    content = b"date,description,amount\n2026-09-02,COFFEE LAB,-6.50\n2026-09-02,COFFEE LAB,-6.50\n"
    rows, errors, rows_total = read_statement(content, 1, "GEL")
    assert len(rows) == 2
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]


def test_pii_is_masked():
    assert mask_pii("TRANSFER GE29NB0000000101904917") == "TRANSFER GE29 **** **** 4917"
    assert mask_pii("CARD 4111 1111 1111 1234 SHOP") == "CARD **** **** **** 1234 SHOP"


def test_money_uses_decimal():
    assert Decimal("0.10") + Decimal("0.20") == Decimal("0.30")
    assert finance.money(Decimal("0.10") + Decimal("0.20")) == "0.30"


def test_verify_numbers_allows_formatting():
    facts = {"currency": "GEL", "total_spent": "2340.50", "difference_percent": "21.3"}
    for text in ["2340.50 ₾", "2 340,50 ₾", "2340.5 ₾", "2 340 ₾", "21%"]:
        assert check_answer(text, facts, []) == []
    assert check_answer("500 ₾", facts, []) == ["number 500 is not in the facts"]


async def seed_two_months(user_id):
    await add_transaction(user_id, "2026-08-03", "100.00", "CAFE", "რესტორნები")
    await add_transaction(user_id, "2026-08-05", "300.00", "CARREFOUR", "სურსათი")
    await add_transaction(user_id, "2026-09-02", "250.00", "CAFE", "რესტორნები")
    await add_transaction(user_id, "2026-09-04", "280.00", "CARREFOUR", "სურსათი")
    await add_transaction(user_id, "2026-09-14", "650.00", "ZOOMMER", "შოპინგი")


async def test_compare_periods_top_increases():
    user_id, _ = await create_user()
    await seed_two_months(user_id)
    async with SessionLocal() as session:
        facts = await finance.compare_periods(session, user_id, date(2026, 9, 1), date(2026, 9, 30))
    assert facts["total_spent"] == "1180.00"
    assert facts["previous_total_spent"] == "400.00"
    assert facts["difference"] == "780.00"
    assert [(c["category"], c["difference"]) for c in facts["top_increases"]] == [("შოპინგი", "650.00"), ("რესტორნები", "150.00")]
    assert facts["top_increases"][0]["biggest_transactions"][0]["merchant"] == "ZOOMMER"
    assert facts["top_decreases"][0]["difference"] == "-20.00"


async def test_recurring_detection():
    user_id, _ = await create_user()
    for day in ["2026-07-07", "2026-08-07", "2026-09-06"]:
        await add_transaction(user_id, day, "9.99", "YOUTUBE PREMIUM", "გამოწერები")
    await add_transaction(user_id, "2026-09-14", "650.00", "ZOOMMER", "შოპინგი")
    async with SessionLocal() as session:
        facts = await finance.find_subscriptions(session, user_id, date(2026, 10, 15))
    assert [s["merchant"] for s in facts["subscriptions"]] == ["YOUTUBE PREMIUM"]
    assert facts["monthly_total"] == "9.99"


async def test_anomaly_detection():
    user_id, _ = await create_user()
    for month, amount in [(3, "40"), (4, "50"), (5, "50"), (6, "55"), (7, "60"), (8, "45")]:
        await add_transaction(user_id, f"2026-{month:02d}-05", amount, "CARREFOUR", "სურსათი")
    await add_transaction(user_id, "2026-09-10", "200.00", "CARREFOUR", "სურსათი")
    await add_transaction(user_id, "2026-09-20", "120.00", "CARREFOUR", "სურსათი")
    async with SessionLocal() as session:
        facts = await finance.find_anomalies(session, user_id, date(2026, 9, 1), date(2026, 9, 30))
    assert [a["amount"] for a in facts["anomalies"]] == ["200.00"]


async def test_simulate_saving():
    user_id, _ = await create_user()
    for day, amount in [("2026-07-10", "300.00"), ("2026-08-10", "200.00"), ("2026-09-10", "100.00")]:
        await add_transaction(user_id, day, amount, "CAFE", "რესტორნები")
    async with SessionLocal() as session:
        facts = await finance.simulate_saving(session, user_id, "რესტორნები", 30, 12, date(2026, 10, 15))
    assert facts["average_monthly_spent"] == "200.00"
    assert facts["monthly_saving"] == "60.00"
    assert facts["total_saving"] == "720.00"
