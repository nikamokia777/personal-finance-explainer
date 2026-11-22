from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from sqlalchemy import func, select

from backend.models import Budget, Category, Transaction

UNCATEGORIZED = "უკატეგორიო"


def money(value) -> str:
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent(part, whole) -> str | None:
    if whole == 0:
        return None
    return str((Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def month_start(day: date, shift: int = 0) -> date:
    index = day.year * 12 + day.month - 1 + shift
    return date(index // 12, index % 12 + 1, 1)


def month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def parse_month(text: str | None) -> date:
    if text is None:
        return month_start(date.today())
    return datetime.strptime(text, "%Y-%m").date()


def period_label(start: date, end: date) -> str:
    if start.day == 1 and end == month_end(start):
        return start.strftime("%Y-%m")
    return f"{start} - {end}"


def resolve_period(period: str, today: date, start: date | None = None, end: date | None = None) -> tuple[date, date]:
    if period == "last_month":
        first = month_start(today, -1)
        return first, month_end(first)
    if period == "last_3_months":
        return month_start(today, -3), month_end(month_start(today, -1))
    if period == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if period == "custom" and start:
        return start, end or start
    first = month_start(today)
    return first, month_end(first)


def previous_period(start: date, end: date) -> tuple[date, date]:
    if start.day == 1 and end == month_end(end):
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        return month_start(start, -months), month_end(month_start(start, -1))
    length = end - start
    return start - length - timedelta(days=1), start - timedelta(days=1)


async def load_transactions(session, user_id: int, start: date, end: date, kind: str = "expense") -> list[dict]:
    query = (
        select(Transaction.date, Transaction.amount, Transaction.merchant, Transaction.category_id, Category.name)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == kind,
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .order_by(Transaction.date)
    )
    result = await session.execute(query)
    return [
        {
            "date": row.date,
            "amount": row.amount,
            "merchant": row.merchant,
            "category_id": row.category_id,
            "category": row.name or UNCATEGORIZED,
        }
        for row in result
    ]


def total(transactions: list[dict]) -> Decimal:
    return sum((t["amount"] for t in transactions), Decimal("0"))


def totals_by(transactions: list[dict], key: str) -> dict:
    totals = defaultdict(Decimal)
    for t in transactions:
        totals[t[key]] += t["amount"]
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


async def spending_summary(session, user_id: int, start: date, end: date, category: str | None = None) -> dict:
    expenses = await load_transactions(session, user_id, start, end, "expense")
    incomes = await load_transactions(session, user_id, start, end, "income")
    if category:
        expenses = [t for t in expenses if t["category"] == category]

    total_spent = total(expenses)
    total_income = total(incomes)
    by_category = totals_by(expenses, "category")
    by_merchant = totals_by(expenses, "merchant")
    by_day = defaultdict(Decimal)
    for t in expenses:
        by_day[t["date"]] += t["amount"]

    return {
        "period": period_label(start, end),
        "category": category,
        "total_spent": money(total_spent),
        "total_income": money(total_income),
        "net": money(total_income - total_spent),
        "expense_count": len(expenses),
        "by_category": [
            {"category": name, "amount": money(amount), "percent": percent(amount, total_spent)}
            for name, amount in by_category.items()
        ],
        "top_merchants": [
            {"merchant": name, "amount": money(amount)} for name, amount in list(by_merchant.items())[:5]
        ],
        "daily": [{"date": day.isoformat(), "amount": money(amount)} for day, amount in sorted(by_day.items())],
    }


async def compare_periods(session, user_id: int, start: date, end: date) -> dict:
    previous_start, previous_end = previous_period(start, end)
    current = await load_transactions(session, user_id, start, end)
    previous = await load_transactions(session, user_id, previous_start, previous_end)
    current_totals = totals_by(current, "category")
    previous_totals = totals_by(previous, "category")

    changes = []
    for name in set(current_totals) | set(previous_totals):
        now = current_totals.get(name, Decimal("0"))
        before = previous_totals.get(name, Decimal("0"))
        biggest = sorted([t for t in current if t["category"] == name], key=lambda t: t["amount"], reverse=True)[:3]
        changes.append({
            "category": name,
            "amount": money(now),
            "previous_amount": money(before),
            "difference": money(now - before),
            "difference_percent": percent(now - before, before),
            "biggest_transactions": [
                {"merchant": t["merchant"], "amount": money(t["amount"]), "date": t["date"].isoformat()}
                for t in biggest
            ],
        })
    changes.sort(key=lambda change: Decimal(change["difference"]), reverse=True)

    total_now = total(current)
    total_before = total(previous)
    return {
        "period": period_label(start, end),
        "compare_to": period_label(previous_start, previous_end),
        "total_spent": money(total_now),
        "previous_total_spent": money(total_before),
        "difference": money(total_now - total_before),
        "difference_percent": percent(total_now - total_before, total_before),
        "top_increases": [c for c in changes if Decimal(c["difference"]) > 0][:3],
        "top_decreases": [c for c in reversed(changes) if Decimal(c["difference"]) < 0][:3],
    }


async def budget_status(session, user_id: int, month: date) -> dict:
    start, end = month_start(month), month_end(month)
    spent = (
        select(Transaction.category_id, func.sum(Transaction.amount).label("spent"))
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .group_by(Transaction.category_id)
        .subquery()
    )
    query = (
        select(Budget.category_id, Category.name, Budget.monthly_limit, func.coalesce(spent.c.spent, 0))
        .join(Category, Category.id == Budget.category_id)
        .outerjoin(spent, spent.c.category_id == Budget.category_id)
        .where(Budget.user_id == user_id)
        .order_by(Category.name)
    )
    budgets = []
    for category_id, name, limit, used in await session.execute(query):
        budgets.append({
            "category_id": category_id,
            "category": name,
            "limit": money(limit),
            "spent": money(used),
            "remaining": money(limit - used),
            "used_percent": percent(used, limit),
            "exceeded": used > limit,
        })
    return {"month": start.strftime("%Y-%m"), "budgets": budgets}


async def find_subscriptions(session, user_id: int, today: date) -> dict:
    transactions = await load_transactions(session, user_id, month_start(today, -12), today)
    by_merchant = defaultdict(list)
    for t in transactions:
        by_merchant[t["merchant"]].append(t)

    subscriptions = []
    for merchant, items in by_merchant.items():
        months = {(t["date"].year, t["date"].month) for t in items}
        if len(months) < 3:
            continue
        average = total(items) / len(items)
        same_amount = all(abs(t["amount"] - average) <= average * Decimal("0.10") for t in items)
        gaps = [(later["date"] - earlier["date"]).days for earlier, later in zip(items, items[1:])]
        monthly = all(28 <= gap <= 32 for gap in gaps)
        if same_amount and monthly:
            subscriptions.append({
                "merchant": merchant,
                "category": items[-1]["category"],
                "average_amount": money(average),
                "payments": len(items),
                "last_payment": items[-1]["date"].isoformat(),
            })

    subscriptions.sort(key=lambda s: Decimal(s["average_amount"]), reverse=True)
    monthly_total = sum((Decimal(s["average_amount"]) for s in subscriptions), Decimal("0"))
    return {"subscriptions": subscriptions, "monthly_total": money(monthly_total)}


async def find_anomalies(session, user_id: int, start: date, end: date) -> dict:
    current = await load_transactions(session, user_id, start, end)
    history = await load_transactions(session, user_id, month_start(start, -6), start - timedelta(days=1))
    history_by_category = defaultdict(list)
    for t in history:
        if t["category_id"] is not None:
            history_by_category[t["category_id"]].append(t["amount"])

    anomalies = []
    for t in current:
        past = history_by_category.get(t["category_id"], [])
        if t["category_id"] is None or len(past) < 5:
            continue
        typical = median(past)
        if t["amount"] > typical * 3:
            anomalies.append({
                "merchant": t["merchant"],
                "category": t["category"],
                "amount": money(t["amount"]),
                "date": t["date"].isoformat(),
                "category_median": money(typical),
                "times_median": str((t["amount"] / typical).quantize(Decimal("0.1"))),
            })
    return {"period": period_label(start, end), "anomalies": anomalies}


async def simulate_saving(session, user_id: int, category: str | None, saving_percent: float, months: int, today: date) -> dict:
    if not category:
        return {"error": "category_not_specified"}
    start, end = month_start(today, -3), month_end(month_start(today, -1))
    expenses = [t for t in await load_transactions(session, user_id, start, end) if t["category"] == category]
    average = total(expenses) / 3
    rate = Decimal(str(saving_percent)) / 100
    return {
        "category": category,
        "based_on": period_label(start, end),
        "average_monthly_spent": money(average),
        "saving_percent": str(saving_percent),
        "months": months,
        "monthly_saving": money(average * rate),
        "total_saving": money(average * rate * months),
    }
