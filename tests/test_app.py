import json

from sqlalchemy import select

from backend.database import SessionLocal
from backend.models import MerchantRule, Transaction
from backend.schemas import ExplanationDraft, QuestionAnalysis
from tests.conftest import add_transaction, create_user, wait_for_import
from tests.fakes import categorizer
from tests.test_logic import seed_two_months

STATEMENT = (
    "date,description,amount,currency\n"
    "2026-09-02,POS PURCHASE 4521 CARREFOUR TBILISI,-87.40,GEL\n"
    "2026-09-03,GLOVO ORDER 88123,-32.90,GEL\n"
    "2026-09-14,ZOOMMER ONLINE,-650.00,GEL\n"
)


async def upload(client, headers, content=STATEMENT):
    response = await client.post("/imports", headers=headers, files={"file": ("statement.csv", content.encode(), "text/csv")})
    assert response.status_code == 202
    return await wait_for_import(client, headers, response.json()["id"])


async def get_transactions(user_id):
    async with SessionLocal() as session:
        result = await session.scalars(select(Transaction).where(Transaction.user_id == user_id))
        return {t.merchant: t for t in result}


async def test_duplicate_import_is_skipped(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer(
        {"CARREFOUR": ("სურსათი", 0.95), "GLOVO": ("რესტორნები", 0.95), "ZOOMMER": ("შოპინგი", 0.95)}
    )
    first = await upload(client, headers)
    second = await upload(client, headers)
    assert first["rows_imported"] == 3
    assert second["rows_imported"] == 0
    assert second["duplicates_skipped"] == 3
    assert len(await get_transactions(user_id)) == 3


async def test_rules_applied_before_llm(client, fake_llm):
    user_id, headers = await create_user()
    categories = {c["name"]: c["id"] for c in (await client.get("/categories", headers=headers)).json()}
    async with SessionLocal() as session:
        for merchant, category in [("CARREFOUR", "სურსათი"), ("GLOVO", "რესტორნები"), ("ZOOMMER", "შოპინგი")]:
            session.add(MerchantRule(user_id=user_id, pattern=merchant, category_id=categories[category]))
        await session.commit()

    record = await upload(client, headers)
    assert record["status"] == "completed"
    assert fake_llm.count("MerchantCategorizationBatch") == 0
    assert (await get_transactions(user_id))["CARREFOUR"].category_source == "rule"


async def test_llm_receives_only_merchants(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({})
    statement = STATEMENT + "2026-09-20,TRANSFER GE29NB0000000101904917,-300.00,GEL\n"
    await upload(client, headers, statement)
    prompt = fake_llm.prompts("MerchantCategorizationBatch")[0]
    assert "<merchant>CARREFOUR</merchant>" in prompt
    for secret in ["87.40", "650", "2026-09", "POS PURCHASE", "0101904917"]:
        assert secret not in prompt


async def test_invalid_category_from_llm_is_rejected(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({"ZOOMMER": ("კრიპტო", 0.99)})
    record = await upload(client, headers, "date,description,amount\n2026-09-14,ZOOMMER ONLINE,-650.00\n")
    assert record["status"] == "awaiting_review"
    transaction = (await get_transactions(user_id))["ZOOMMER"]
    assert transaction.category_id is None
    assert transaction.suggested_category_id is None


async def test_low_confidence_goes_to_review(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({"GLOVO": ("რესტორნები", 0.5), "CARREFOUR": ("სურსათი", 0.95)})
    record = await upload(client, headers, "date,description,amount\n2026-09-03,GLOVO,-32.90\n2026-09-02,CARREFOUR,-87.40\n")
    assert record["needs_review_count"] == 1
    transactions = await get_transactions(user_id)
    assert transactions["GLOVO"].category_id is None
    assert transactions["GLOVO"].suggested_category_id is not None
    assert transactions["CARREFOUR"].category_source == "ai"


async def test_interrupt_and_resume(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({"GLOVO": ("რესტორნები", 0.5)})
    record = await upload(client, headers, "date,description,amount\n2026-09-03,GLOVO,-32.90\n")
    assert record["status"] == "awaiting_review"

    review = (await client.get(f"/imports/{record['id']}/review", headers=headers)).json()
    decisions = [{"transaction_id": review[0]["transaction_id"], "category_id": review[0]["suggested_category_id"]}]
    response = await client.post(f"/imports/{record['id']}/review", headers=headers, json={"decisions": decisions})
    assert response.json()["status"] == "completed"
    assert (await get_transactions(user_id))["GLOVO"].category_source == "user"

    again = await client.post(f"/imports/{record['id']}/review", headers=headers, json={"decisions": decisions})
    assert again.status_code == 409


async def test_review_creates_merchant_rule(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({})
    record = await upload(client, headers, "date,description,amount\n2026-09-18,BOOKSHOP SANTA,-34.50\n")
    review = (await client.get(f"/imports/{record['id']}/review", headers=headers)).json()
    categories = {c["name"]: c["id"] for c in (await client.get("/categories", headers=headers)).json()}
    decisions = [{"transaction_id": review[0]["transaction_id"], "category_id": categories["შოპინგი"], "remember": True}]
    await client.post(f"/imports/{record['id']}/review", headers=headers, json={"decisions": decisions})

    calls_before = fake_llm.count("MerchantCategorizationBatch")
    next_month = await upload(client, headers, "date,description,amount\n2026-10-18,BOOKSHOP SANTA,-20.00\n")
    assert next_month["status"] == "completed"
    assert fake_llm.count("MerchantCategorizationBatch") == calls_before


async def ask(client, headers, question):
    chat = (await client.post("/chat/sessions", headers=headers)).json()
    response = await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers, json={"content": question})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    return events[-1]["text"], [e["name"] for e in events if e["type"] == "step"]


async def test_verify_numbers_rejects_invented_number(client, fake_llm):
    user_id, headers = await create_user()
    await seed_two_months(user_id)
    fake_llm.answers["QuestionAnalysis"] = QuestionAnalysis(
        intent="explain_change", period="custom", start_date="2026-09-01", end_date="2026-09-30", refers_to_previous=False
    )
    fake_llm.answers["ExplanationDraft"] = ExplanationDraft(summary="სექტემბერში 999 ₾-ით მეტი დახარჯე.", details=[])

    answer, steps = await ask(client, headers, "რატომ დავხარჯე სექტემბერში მეტი?")

    assert steps.count("write_explanation") == 2
    assert "fallback_template" in steps
    assert "999" not in answer
    assert "780.00" in answer


async def test_correct_answer_is_sent(client, fake_llm):
    user_id, headers = await create_user()
    await seed_two_months(user_id)
    fake_llm.answers["QuestionAnalysis"] = QuestionAnalysis(
        intent="explain_change", period="custom", start_date="2026-09-01", end_date="2026-09-30", refers_to_previous=False
    )
    fake_llm.answers["ExplanationDraft"] = ExplanationDraft(summary="სექტემბერში 780.00 ₾-ით მეტი დახარჯე.", details=["ZOOMMER: 650.00 ₾"])

    answer, steps = await ask(client, headers, "რატომ დავხარჯე სექტემბერში მეტი?")

    assert steps.count("write_explanation") == 1
    assert answer.startswith("სექტემბერში 780.00 ₾-ით მეტი დახარჯე.")


async def test_investment_question_is_refused(client, fake_llm):
    user_id, headers = await create_user()
    fake_llm.answers["QuestionAnalysis"] = QuestionAnalysis(intent="off_topic", refers_to_previous=False)

    answer, steps = await ask(client, headers, "რომელ კრიპტოვალუტაში ჩავდო ფული?")

    assert steps == ["understand_question", "polite_refusal"]
    assert "ვერ დაგეხმარები" in answer
    assert fake_llm.count("ExplanationDraft") == 0


async def test_knowledge_answer_has_source(client, fake_llm, fake_embeddings):
    user_id, headers = await create_user()
    admin_id, admin_headers = await create_user("admin@example.com", "admin")
    response = await client.post(
        "/knowledge/articles",
        headers=admin_headers,
        json={"title": "ავარიული ფონდი", "content": "ავარიული ფონდი არის რეზერვი 3-6 თვის ხარჯებისთვის."},
    )
    assert response.status_code == 201
    fake_llm.answers["QuestionAnalysis"] = QuestionAnalysis(intent="finance_education", refers_to_previous=False)
    fake_llm.answers["ExplanationDraft"] = ExplanationDraft(summary="ავარიული ფონდი 3-6 თვის ხარჯს ფარავს.", details=[])

    answer, steps = await ask(client, headers, "რა არის ავარიული ფონდი?")

    assert "retrieve_knowledge" in steps
    assert "წყარო: ავარიული ფონდი" in answer


async def test_user_cannot_read_others_transactions(client, fake_llm):
    alice_id, alice = await create_user("alice@example.com")
    bob_id, bob = await create_user("bob@example.com")
    await add_transaction(alice_id, "2026-09-02", "10.00", "CARREFOUR")
    fake_llm.answers["MerchantCategorizationBatch"] = categorizer({})
    alice_import = await upload(client, alice)
    alice_chat = (await client.post("/chat/sessions", headers=alice)).json()
    transaction_id = (await client.get("/transactions", headers=alice)).json()["items"][0]["id"]

    assert (await client.get("/transactions", headers=bob)).json()["total"] == 0
    assert (await client.patch(f"/transactions/{transaction_id}", headers=bob, json={"category_id": 1})).status_code == 404
    assert (await client.get(f"/imports/{alice_import['id']}", headers=bob)).status_code == 404
    assert (await client.get(f"/chat/sessions/{alice_chat['id']}/messages", headers=bob)).status_code == 404


async def test_admin_cannot_read_user_transactions(client):
    user_id, user = await create_user("user@example.com")
    admin_id, admin = await create_user("admin@example.com", "admin")
    await add_transaction(user_id, "2026-09-02", "10.00", "CARREFOUR")

    assert (await client.get("/transactions", headers=admin)).json()["total"] == 0
    assert (await client.get("/analytics/overview", headers=admin)).status_code == 200
    assert (await client.get("/analytics/overview", headers=user)).status_code == 403
