import asyncio
import json
import logging
import os
import re
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from sqlalchemy import func, or_, select

from backend import finance, rag
from backend.csv_import import CsvError, read_statement
from backend.database import SessionLocal
from backend.models import Category, Import, MerchantRule, Transaction, User
from backend.prompts import (
    CATEGORIZE_PROMPT,
    DISCLAIMER,
    EXPLAIN_PROMPT,
    KNOWLEDGE_PROMPT,
    NO_KNOWLEDGE_TEXT,
    REFUSAL_TEXT,
    UNAVAILABLE_TEXT,
    UNDERSTAND_PROMPT,
)
from backend.schemas import ExplanationDraft, MerchantCategorizationBatch, QuestionAnalysis

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/uploads"))
CONFIDENCE_THRESHOLD = 0.8
BATCH_SIZE = 40
HISTORY_LIMIT = 10
MAX_ATTEMPTS = 2


def get_llm():
    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        temperature=0,
        timeout=60,
        max_retries=2,
    )


async def ask_llm(schema, messages):
    llm = get_llm().with_structured_output(schema).with_retry(stop_after_attempt=2)
    return await llm.ainvoke(messages)


async def user_categories(session, user_id: int) -> list[Category]:
    query = select(Category).where(or_(Category.user_id.is_(None), Category.user_id == user_id))
    return list(await session.scalars(query))


async def update_import(import_id: int, **values) -> None:
    async with SessionLocal() as session:
        record = await session.get(Import, import_id)
        for key, value in values.items():
            setattr(record, key, value)
        await session.commit()


async def save_merchant_rule(session, user_id: int, merchant: str, category_id: int) -> None:
    query = select(MerchantRule).where(MerchantRule.user_id == user_id, MerchantRule.pattern == merchant)
    rule = await session.scalar(query)
    if rule is None:
        session.add(MerchantRule(user_id=user_id, pattern=merchant, category_id=category_id))
    else:
        rule.category_id = category_id


def read_rows(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_rows(path: str, rows: list[dict]) -> None:
    Path(path).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


class ImportState(TypedDict, total=False):
    import_id: int
    user_id: int
    rows_file: str
    rows_total: int
    duplicates_skipped: int
    errors: list
    unknown_merchants: list
    ai_results: dict
    review_transaction_ids: list
    review_decisions: list
    status: str
    error_message: str


async def parse_csv(state: ImportState) -> dict:
    import_id = state["import_id"]
    await update_import(import_id, status="parsing", current_step="parse_csv")
    upload = UPLOAD_DIR / f"import-{import_id}.csv"
    content = upload.read_bytes()
    upload.unlink(missing_ok=True)

    async with SessionLocal() as session:
        user = await session.get(User, state["user_id"])
        try:
            rows, errors, rows_total = await asyncio.to_thread(read_statement, content, user.id, user.currency)
        except CsvError as error:
            return {"status": "failed", "error_message": str(error)}

        fingerprints = [row["fingerprint"] for row in rows]
        query = select(Transaction.fingerprint).where(
            Transaction.user_id == user.id, Transaction.fingerprint.in_(fingerprints)
        )
        existing = set(await session.scalars(query))

    if not rows:
        return {"status": "failed", "error_message": "No valid transactions in the file", "errors": errors, "rows_total": rows_total}

    rows_file = str(UPLOAD_DIR / f"import-{import_id}.json")
    write_rows(rows_file, [row for row in rows if row["fingerprint"] not in existing])
    return {
        "status": "classifying",
        "rows_file": rows_file,
        "rows_total": rows_total,
        "duplicates_skipped": len(existing),
        "errors": errors,
    }


async def apply_rules(state: ImportState) -> dict:
    await update_import(state["import_id"], current_step="apply_rules")
    rows = read_rows(state["rows_file"])
    async with SessionLocal() as session:
        rules = list(await session.scalars(select(MerchantRule).where(MerchantRule.user_id == state["user_id"])))
        kinds = {category.id: category.kind for category in await user_categories(session, state["user_id"])}

    for row in rows:
        row.update(category_id=None, suggested_category_id=None, category_source=None, category_confidence=None)
        for rule in rules:
            if rule.pattern in row["merchant"] and kinds.get(rule.category_id) == row["type"]:
                row.update(category_id=rule.category_id, category_source="rule", category_confidence=1.0)
                break

    write_rows(state["rows_file"], rows)
    unknown = sorted({row["merchant"] for row in rows if row["category_id"] is None})
    return {"unknown_merchants": unknown, "ai_results": {}}


async def classify_unknown(state: ImportState) -> dict:
    await update_import(state["import_id"], status="classifying", current_step="classify_unknown")
    async with SessionLocal() as session:
        categories = await user_categories(session, state["user_id"])
    category_list = "\n".join(f"- {category.name} ({category.kind})" for category in categories)
    system_prompt = CATEGORIZE_PROMPT.replace("{categories}", category_list)

    merchants = state["unknown_merchants"]
    results = {}
    for start in range(0, len(merchants), BATCH_SIZE):
        batch = merchants[start:start + BATCH_SIZE]
        merchant_list = "\n".join(f"<merchant>{merchant}</merchant>" for merchant in batch)
        try:
            answer = await ask_llm(MerchantCategorizationBatch, [("system", system_prompt), ("human", merchant_list)])
        except Exception as error:
            logger.warning("Import %s: AI categorization failed (%s)", state["import_id"], type(error).__name__)
            continue
        for item in answer.items:
            if item.merchant in batch:
                results[item.merchant] = {"category": item.category, "confidence": item.confidence}
    return {"ai_results": results}


async def save_transactions(state: ImportState) -> dict:
    await update_import(state["import_id"], current_step="save_transactions")
    rows = read_rows(state["rows_file"])

    async with SessionLocal() as session:
        categories = {category.name: category for category in await user_categories(session, state["user_id"])}
        transactions = []
        for row in rows:
            answer = state["ai_results"].get(row["merchant"])
            category = categories.get(answer["category"]) if answer else None
            if row["category_id"] is None and category is not None and category.kind == row["type"]:
                if answer["confidence"] >= CONFIDENCE_THRESHOLD:
                    row.update(category_id=category.id, category_source="ai", category_confidence=answer["confidence"])
                else:
                    row.update(suggested_category_id=category.id, category_confidence=answer["confidence"])

            transactions.append(Transaction(
                user_id=state["user_id"],
                import_id=state["import_id"],
                date=date.fromisoformat(row["date"]),
                amount=Decimal(row["amount"]),
                type=row["type"],
                description=row["description"],
                merchant=row["merchant"],
                category_id=row["category_id"],
                suggested_category_id=row["suggested_category_id"],
                category_source=row["category_source"],
                category_confidence=row["category_confidence"],
                fingerprint=row["fingerprint"],
            ))

        session.add_all(transactions)
        await session.flush()
        review_ids = [t.id for t in transactions if t.category_id is None]

        record = await session.get(Import, state["import_id"])
        record.rows_total = state["rows_total"]
        record.rows_imported = len(transactions)
        record.duplicates_skipped = state["duplicates_skipped"]
        record.needs_review_count = len(review_ids)
        record.errors = state["errors"]
        if review_ids:
            record.status = "awaiting_review"
            record.current_step = "confirm_categories"
        await session.commit()

    Path(state["rows_file"]).unlink(missing_ok=True)
    return {"review_transaction_ids": review_ids}


async def confirm_categories(state: ImportState) -> dict:
    decisions = interrupt({"transaction_ids": state["review_transaction_ids"]})
    return {"review_decisions": decisions}


async def apply_review(state: ImportState) -> dict:
    async with SessionLocal() as session:
        for decision in state["review_decisions"]:
            transaction = await session.get(Transaction, decision["transaction_id"])
            if transaction is None or transaction.user_id != state["user_id"]:
                continue
            guess = transaction.suggested_category_id
            transaction.was_corrected = guess is not None and guess != decision["category_id"]
            transaction.category_id = decision["category_id"]
            transaction.category_source = "user"
            transaction.category_confidence = None
            if decision["remember"]:
                await save_merchant_rule(session, state["user_id"], transaction.merchant, decision["category_id"])

        query = select(func.count()).select_from(Transaction).where(
            Transaction.import_id == state["import_id"], Transaction.category_id.is_(None)
        )
        record = await session.get(Import, state["import_id"])
        record.needs_review_count = await session.scalar(query)
        await session.commit()
    return {}


async def finish_import(state: ImportState) -> dict:
    async with SessionLocal() as session:
        record = await session.get(Import, state["import_id"])
        record.status = "completed"
        record.current_step = None
        record.summary = (
            f"{record.rows_imported} added, {record.duplicates_skipped} duplicates skipped, "
            f"{len(record.errors)} rows with errors, {record.needs_review_count} still uncategorized."
        )
        await session.commit()
    return {"status": "completed"}


async def fail_import(state: ImportState) -> dict:
    await update_import(
        state["import_id"],
        status="failed",
        current_step=None,
        error_message=state["error_message"],
        errors=state.get("errors", []),
        rows_total=state.get("rows_total", 0),
    )
    return {}


def route_after_parse(state: ImportState) -> str:
    return "fail_import" if state["status"] == "failed" else "apply_rules"


def route_after_rules(state: ImportState) -> str:
    return "classify_unknown" if state["unknown_merchants"] else "save_transactions"


def route_after_save(state: ImportState) -> str:
    return "confirm_categories" if state["review_transaction_ids"] else "finish_import"


def build_import_graph(checkpointer):
    graph = StateGraph(ImportState)
    graph.add_node("parse_csv", parse_csv)
    graph.add_node("apply_rules", apply_rules)
    graph.add_node("classify_unknown", classify_unknown)
    graph.add_node("save_transactions", save_transactions)
    graph.add_node("confirm_categories", confirm_categories)
    graph.add_node("apply_review", apply_review)
    graph.add_node("finish_import", finish_import)
    graph.add_node("fail_import", fail_import)

    graph.add_edge(START, "parse_csv")
    graph.add_conditional_edges("parse_csv", route_after_parse, ["fail_import", "apply_rules"])
    graph.add_conditional_edges("apply_rules", route_after_rules, ["classify_unknown", "save_transactions"])
    graph.add_edge("classify_unknown", "save_transactions")
    graph.add_conditional_edges("save_transactions", route_after_save, ["confirm_categories", "finish_import"])
    graph.add_edge("confirm_categories", "apply_review")
    graph.add_edge("apply_review", "finish_import")
    graph.add_edge("finish_import", END)
    graph.add_edge("fail_import", END)
    return graph.compile(checkpointer=checkpointer)


def import_config(import_id: int) -> dict:
    return {
        "configurable": {"thread_id": f"import-{import_id}"},
        "tags": ["import"],
        "metadata": {"import_id": import_id},
    }


async def run_import(graph, import_id: int, user_id: int) -> None:
    try:
        await graph.ainvoke({"import_id": import_id, "user_id": user_id}, import_config(import_id))
    except Exception as error:
        logger.error("Import %s crashed (%s)", import_id, type(error).__name__)
        await update_import(import_id, status="failed", current_step=None, error_message="Something went wrong while processing the file")


async def resume_import(graph, import_id: int, decisions: list[dict]) -> None:
    await graph.ainvoke(Command(resume=decisions), import_config(import_id))


class ChatState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    user_id: int
    today: str
    report_month: str
    intent: str
    analysis: dict
    facts: dict
    knowledge_chunks: list
    draft: dict | None
    number_issues: list
    attempts: int
    answer: str


NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def find_numbers(text: str) -> list[Decimal]:
    text = re.sub(r"(?<=\d)[ ,](?=\d{3}\b)", "", text)
    return [Decimal(number.replace(",", ".")) for number in NUMBER_PATTERN.findall(text)]


def allowed_numbers(facts: dict, chunks: list[dict]) -> set[Decimal]:
    text = json.dumps(facts, ensure_ascii=False) + " ".join(chunk["content"] for chunk in chunks)
    allowed = set()
    for number in find_numbers(text):
        allowed.add(number)
        allowed.add(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        allowed.add(number.quantize(Decimal("1"), rounding=ROUND_DOWN))
        allowed.add(number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return allowed


def check_answer(text: str, facts: dict, chunks: list[dict]) -> list[str]:
    allowed = allowed_numbers(facts, chunks)
    issues = [f"number {number} is not in the facts" for number in find_numbers(text) if number not in allowed]
    if facts.get("currency") == "GEL" and re.search(r"\$|€|USD|EUR", text):
        issues.append("the answer uses a different currency")
    if re.search(r"[а-яА-Я]", text):
        issues.append("the answer contains Russian letters")
    return issues


def draft_text(draft: dict) -> str:
    return "\n".join([draft["summary"], *draft["details"], *draft["suggestions"]])


def format_answer(draft: dict, chunks: list[dict]) -> str:
    lines = [draft["summary"]]
    lines += [f"• {detail}" for detail in draft["details"]]
    lines += [f"→ {suggestion}" for suggestion in draft["suggestions"]]
    if chunks:
        titles = sorted({chunk["title"] for chunk in chunks})
        lines.append(f"წყარო: {', '.join(titles)}")
    lines.append(f"_{DISCLAIMER}_")
    return "\n\n".join(lines)


def template_answer(intent: str, facts: dict, chunks: list[dict]) -> str:
    currency = facts.get("currency", "GEL")
    if intent == "finance_education" and chunks:
        lines = [chunks[0]["content"], f"წყარო: {chunks[0]['title']}"]
    elif intent == "explain_change":
        lines = [
            f"{facts['period']}: დახარჯე {facts['total_spent']} {currency}, "
            f"{facts['compare_to']}: {facts['previous_total_spent']} {currency}. სხვაობა: {facts['difference']} {currency}."
        ]
        lines += [f"• {c['category']}: +{c['difference']} {currency}" for c in facts["top_increases"]]
    elif intent == "budget_status":
        lines = [f"• {b['category']}: {b['spent']} / {b['limit']} {currency}" for b in facts["budgets"]] or ["ბიუჯეტები არ გაქვს."]
    elif intent == "subscriptions":
        lines = [f"• {s['merchant']}: {s['average_amount']} {currency}" for s in facts["subscriptions"]]
        lines.append(f"თვეში სულ: {facts['monthly_total']} {currency}")
    elif intent == "what_if" and "error" not in facts:
        lines = [f"{facts['category']}: {facts['months']} თვეში დაზოგავდი {facts['total_saving']} {currency}."]
    elif intent == "what_if":
        lines = ["მითხარი, რომელ კატეგორიაზე გინდა ნაკლების ხარჯვა."]
    elif intent == "monthly_report":
        spending = facts["spending"]
        lines = [f"{spending['period']}: დახარჯე {spending['total_spent']} {currency}, შემოსავალი {spending['total_income']} {currency}."]
        lines += [f"• {c['category']}: {c['amount']} {currency} ({c['percent']}%)" for c in spending["by_category"][:5]]
    else:
        lines = [f"{facts['period']}: დახარჯე {facts['total_spent']} {currency}, შემოსავალი {facts['total_income']} {currency}."]
        lines += [f"• {c['category']}: {c['amount']} {currency} ({c['percent']}%)" for c in facts["by_category"][:5]]
    lines.append(f"_{DISCLAIMER}_")
    return "\n\n".join(lines)


async def understand_question(state: ChatState) -> dict:
    question = state["messages"][-1].content.replace("<", "").replace(">", "")
    history = [(message.type, message.content) for message in state["messages"][-HISTORY_LIMIT - 1:-1]]
    async with SessionLocal() as session:
        categories = ", ".join(category.name for category in await user_categories(session, state["user_id"]))
    prompt = (
        UNDERSTAND_PROMPT.replace("{today}", state["today"])
        .replace("{categories}", categories)
        .replace("{previous}", json.dumps(state.get("analysis") or {}, ensure_ascii=False))
    )
    reset = {"facts": {}, "knowledge_chunks": [], "draft": None, "number_issues": [], "attempts": 0, "answer": ""}

    try:
        result = await ask_llm(QuestionAnalysis, [("system", prompt), *history, ("human", f"<question>{question}</question>")])
    except Exception as error:
        logger.warning("understand_question failed (%s)", type(error).__name__)
        return {**reset, "intent": "error"}

    analysis = result.model_dump(mode="json")
    previous = state.get("analysis") or {}
    if result.refers_to_previous:
        for key in ("category", "compare_to", "saving_percent", "saving_months"):
            if analysis[key] is None:
                analysis[key] = previous.get(key)
    return {**reset, "intent": result.intent, "analysis": analysis}


async def polite_refusal(state: ChatState) -> dict:
    text = UNAVAILABLE_TEXT if state["intent"] == "error" else REFUSAL_TEXT
    return {"answer": text, "messages": [AIMessage(text)]}


async def gather_facts(state: ChatState) -> dict:
    today = date.fromisoformat(state["today"])
    analysis = state["analysis"]
    start_date = date.fromisoformat(analysis["start_date"]) if analysis.get("start_date") else None
    end_date = date.fromisoformat(analysis["end_date"]) if analysis.get("end_date") else None
    start, end = finance.resolve_period(analysis.get("period", "this_month"), today, start_date, end_date)
    intent = state["intent"]

    async with SessionLocal() as session:
        user = await session.get(User, state["user_id"])
        if intent == "explain_change":
            facts = await finance.compare_periods(session, user.id, start, end)
        elif intent == "budget_status":
            facts = await finance.budget_status(session, user.id, start)
        elif intent == "subscriptions":
            facts = await finance.find_subscriptions(session, user.id, today)
        elif intent == "what_if":
            facts = await finance.simulate_saving(
                session, user.id, analysis.get("category"),
                analysis.get("saving_percent") or 10, analysis.get("saving_months") or 12, today,
            )
        elif intent == "monthly_report":
            spending = await finance.spending_summary(session, user.id, start, end)
            spending.pop("daily")
            facts = {
                "spending": spending,
                "comparison": await finance.compare_periods(session, user.id, start, end),
                "budgets": await finance.budget_status(session, user.id, start),
                "anomalies": await finance.find_anomalies(session, user.id, start, end),
            }
        else:
            facts = await finance.spending_summary(session, user.id, start, end, analysis.get("category"))
            facts.pop("daily")
        facts["currency"] = user.currency
    return {"facts": facts}


async def retrieve_knowledge(state: ChatState) -> dict:
    try:
        chunks = await rag.search_knowledge(state["messages"][-1].content)
    except Exception as error:
        logger.warning("retrieve_knowledge failed (%s)", type(error).__name__)
        chunks = []
    if not chunks:
        return {"knowledge_chunks": [], "answer": NO_KNOWLEDGE_TEXT, "messages": [AIMessage(NO_KNOWLEDGE_TEXT)]}
    return {"knowledge_chunks": chunks}


async def write_explanation(state: ChatState) -> dict:
    question = state["messages"][-1].content.replace("<", "").replace(">", "")
    if state["intent"] == "finance_education":
        articles = "\n\n".join(f"<article title=\"{c['title']}\">{c['content']}</article>" for c in state["knowledge_chunks"])
        system_prompt = KNOWLEDGE_PROMPT
        user_prompt = f"{articles}\n\n<question>{question}</question>"
    else:
        facts = state["facts"]
        system_prompt = EXPLAIN_PROMPT.replace("{currency}", facts.get("currency", "GEL"))
        user_prompt = f"<facts>{json.dumps(facts, ensure_ascii=False)}</facts>\n\n<question>{question}</question>"
    if state.get("number_issues"):
        user_prompt += "\n\nYour previous answer was rejected: " + "; ".join(state["number_issues"])
        user_prompt += ". Write it again using only numbers from the facts, in clean language."

    try:
        draft = (await ask_llm(ExplanationDraft, [("system", system_prompt), ("human", user_prompt)])).model_dump()
    except Exception as error:
        logger.warning("write_explanation failed (%s)", type(error).__name__)
        draft = None
    return {"draft": draft, "attempts": state.get("attempts", 0) + 1}


async def verify_numbers(state: ChatState) -> dict:
    draft = state["draft"]
    if draft is None:
        return {"number_issues": ["the AI did not answer"]}
    issues = check_answer(draft_text(draft), state.get("facts", {}), state.get("knowledge_chunks", []))
    if issues:
        return {"number_issues": issues}
    answer = format_answer(draft, state.get("knowledge_chunks", []))
    return {"number_issues": [], "answer": answer, "messages": [AIMessage(answer)]}


async def fallback_template(state: ChatState) -> dict:
    text = template_answer(state["intent"], state.get("facts", {}), state.get("knowledge_chunks", []))
    return {"answer": text, "messages": [AIMessage(text)]}


def route_start(state: ChatState) -> str:
    return "gather_facts" if state.get("report_month") else "understand_question"


def route_question(state: ChatState) -> str:
    if state["intent"] in ("off_topic", "error"):
        return "polite_refusal"
    if state["intent"] == "finance_education":
        return "retrieve_knowledge"
    return "gather_facts"


def route_after_knowledge(state: ChatState) -> str:
    return "write_explanation" if state["knowledge_chunks"] else END


def route_after_check(state: ChatState) -> str:
    if not state["number_issues"]:
        return END
    if state["draft"] is not None and state["attempts"] < MAX_ATTEMPTS:
        return "write_explanation"
    return "fallback_template"


def build_chat_graph(checkpointer):
    graph = StateGraph(ChatState)
    graph.add_node("understand_question", understand_question)
    graph.add_node("polite_refusal", polite_refusal)
    graph.add_node("gather_facts", gather_facts)
    graph.add_node("retrieve_knowledge", retrieve_knowledge)
    graph.add_node("write_explanation", write_explanation)
    graph.add_node("verify_numbers", verify_numbers)
    graph.add_node("fallback_template", fallback_template)

    graph.add_conditional_edges(START, route_start, ["understand_question", "gather_facts"])
    graph.add_conditional_edges(
        "understand_question", route_question, ["polite_refusal", "retrieve_knowledge", "gather_facts"]
    )
    graph.add_conditional_edges("retrieve_knowledge", route_after_knowledge, ["write_explanation", END])
    graph.add_edge("gather_facts", "write_explanation")
    graph.add_edge("write_explanation", "verify_numbers")
    graph.add_conditional_edges(
        "verify_numbers", route_after_check, ["write_explanation", "fallback_template", END]
    )
    graph.add_edge("polite_refusal", END)
    graph.add_edge("fallback_template", END)
    return graph.compile(checkpointer=checkpointer)
