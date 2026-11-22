import time
from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

import api_client as api
from api_client import APIError

st.set_page_config(page_title="ფინანსების ასისტენტი", page_icon="💰", layout="wide")

STATUS_NAMES = {
    "parsing": "ფაილის დამუშავება",
    "classifying": "კატეგორიზაცია",
    "awaiting_review": "გელოდება დადასტურება",
    "completed": "დასრულდა",
    "failed": "ვერ შესრულდა",
}
STEP_NAMES = {
    "understand_question": "კითხვის გაგება",
    "gather_facts": "ფაქტების დათვლა",
    "retrieve_knowledge": "ცნობარში ძებნა",
    "write_explanation": "პასუხის წერა",
    "verify_numbers": "რიცხვების შემოწმება",
    "fallback_template": "პასუხის შედგენა ფაქტებიდან",
    "polite_refusal": "პასუხი",
}
KIND_NAMES = {"expense": "ხარჯი", "income": "შემოსავალი"}


def token():
    return st.session_state["token"]


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except APIError as error:
        if error.status == 401:
            st.session_state.clear()
            st.rerun()
        st.error(str(error))
        return None


def money(value) -> str:
    return f"{Decimal(value):,.2f}".replace(",", " ") + " ₾"


def last_months(count: int = 12) -> list[str]:
    today = date.today()
    months = []
    for shift in range(count):
        index = today.year * 12 + today.month - 1 - shift
        months.append(f"{index // 12}-{index % 12 + 1:02d}")
    return months


def category_names(categories: list[dict]) -> dict:
    return {category["id"]: category["name"] for category in categories}


def login_page():
    st.title("💰 პირადი ფინანსების ასისტენტი")
    login_tab, register_tab = st.tabs(["შესვლა", "რეგისტრაცია"])

    with login_tab, st.form("login"):
        email = st.text_input("ელფოსტა")
        password = st.text_input("პაროლი", type="password")
        if st.form_submit_button("შესვლა", type="primary"):
            result = call(api.login, email, password)
            if result:
                st.session_state["token"] = result["access_token"]
                st.session_state["user"] = result["user"]
                st.rerun()

    with register_tab, st.form("register"):
        full_name = st.text_input("სახელი")
        email = st.text_input("ელფოსტა", key="register_email")
        password = st.text_input("პაროლი (მინიმუმ 8 სიმბოლო)", type="password", key="register_password")
        if st.form_submit_button("რეგისტრაცია"):
            if call(api.register, email, full_name, password):
                st.success("ანგარიში შეიქმნა. ახლა შედი სისტემაში.")


def dashboard_page():
    st.title("📊 დეშბორდი")
    month = st.selectbox("თვე", last_months())
    summary = call(api.get_summary, token(), month)
    if not summary:
        return
    spending = summary["spending"]
    comparison = summary["comparison"]

    left, right = st.columns(2)
    left.metric("ხარჯი", money(spending["total_spent"]), f"{money(comparison['difference'])} წინა თვესთან", delta_color="inverse")
    right.metric("შემოსავალი", money(spending["total_income"]))
    left.metric("ბალანსი", money(spending["net"]))
    right.metric("ცვლილება", f"{comparison['difference_percent'] or '—'}%")

    if spending["by_category"]:
        st.subheader("ხარჯები კატეგორიების მიხედვით")
        table = pd.DataFrame(spending["by_category"])
        table["amount"] = table["amount"].astype(float)
        st.bar_chart(table, x="category", y="amount", horizontal=True)

        st.subheader("დღიური ხარჯი")
        daily = pd.DataFrame(spending["daily"])
        daily["amount"] = daily["amount"].astype(float)
        st.bar_chart(daily, x="date", y="amount")

        st.subheader("ყველაზე დიდი მერჩანტები")
        st.table(pd.DataFrame([{"მერჩანტი": m["merchant"], "თანხა": money(m["amount"])} for m in spending["top_merchants"]]))

        st.subheader("ყველაზე დიდი ზრდა წინა თვესთან")
        for change in comparison["top_increases"]:
            st.write(f"⬆ **{change['category']}**: +{money(change['difference'])}")

    st.subheader("🔁 განმეორებადი გადასახადები")
    recurring = call(api.get_recurring, token())
    if recurring and recurring["subscriptions"]:
        st.table(pd.DataFrame([{"მერჩანტი": s["merchant"], "საშუალოდ": money(s["average_amount"])} for s in recurring["subscriptions"]]))
        st.write(f"თვეში სულ: **{money(recurring['monthly_total'])}**")
    else:
        st.caption("ვერ მოიძებნა.")

    st.subheader("⚠️ უჩვეულოდ დიდი ხარჯები")
    anomalies = call(api.get_anomalies, token(), month)
    if anomalies and anomalies["anomalies"]:
        st.table(pd.DataFrame([{"მერჩანტი": a["merchant"], "თანხა": money(a["amount"]), "თარიღი": a["date"]} for a in anomalies["anomalies"]]))
    else:
        st.caption("ვერ მოიძებნა.")


def chat_page():
    st.title("💬 ასისტენტი")
    chats = call(api.get_chats, token()) or []
    left, right = st.columns([3, 1])
    if right.button("➕ ახალი ჩატი"):
        created = call(api.create_chat, token())
        if created:
            st.session_state["chat_id"] = created["id"]
            st.rerun()
    if not chats:
        st.info("დაიწყე ახალი ჩატი.")
        return

    titles = {chat["id"]: chat["title"] for chat in chats}
    chat_ids = list(titles)
    selected = st.session_state.get("chat_id")
    index = chat_ids.index(selected) if selected in chat_ids else 0
    chat_id = left.selectbox("ჩატი", chat_ids, index=index, format_func=lambda i: titles[i])
    st.session_state["chat_id"] = chat_id

    for message in call(api.get_messages, token(), chat_id) or []:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    question = st.chat_input("დაწერე კითხვა...")
    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            status = st.status("ვფიქრობ...")
            try:
                for event in api.send_message(token(), chat_id, question):
                    if event["type"] == "step":
                        status.write(STEP_NAMES.get(event["name"], event["name"]))
                    if event["type"] == "answer":
                        status.update(label="მზადაა", state="complete")
                        st.markdown(event["text"])
            except APIError as error:
                status.update(label="შეცდომა", state="error")
                st.error(str(error))

    if st.button("🗑 ჩატის წაშლა"):
        if call(api.delete_chat, token(), chat_id):
            st.session_state.pop("chat_id", None)
            st.rerun()


def review_form(import_id: int):
    items = call(api.get_review, token(), import_id) or []
    categories = call(api.get_categories, token()) or []
    st.subheader("დაადასტურე კატეგორიები")
    with st.form(f"review_{import_id}"):
        decisions = []
        for item in items:
            options = [c for c in categories if c["kind"] == item["type"]]
            ids = [c["id"] for c in options]
            names = category_names(options)
            default = ids.index(item["suggested_category_id"]) if item["suggested_category_id"] in ids else 0
            st.write(f"**{item['merchant']}** — {money(item['amount'])} ({item['date']})")
            left, right = st.columns([3, 1])
            category_id = left.selectbox("კატეგორია", ids, index=default, format_func=lambda i, n=names: n[i], key=f"category_{item['transaction_id']}")
            remember = right.checkbox("დაიმახსოვრე", value=True, key=f"remember_{item['transaction_id']}")
            decisions.append({"transaction_id": item["transaction_id"], "category_id": category_id, "remember": remember})
        if st.form_submit_button("დადასტურება", type="primary"):
            result = call(api.submit_review, token(), import_id, decisions)
            if result:
                st.success(result["summary"] or "შენახულია.")


def import_page():
    st.title("📥 ამონაწერის იმპორტი")
    st.caption("CSV ფაილი: date, description, amount, currency (მაქსიმუმ 2 MB)")
    file = st.file_uploader("აირჩიე CSV ფაილი", type=["csv"])
    if file and st.button("ატვირთვა", type="primary"):
        created = call(api.upload_statement, token(), file.name, file.getvalue())
        if created:
            with st.spinner("ამონაწერი მუშავდება..."):
                record = created
                for _ in range(120):
                    record = call(api.get_import, token(), created["id"])
                    if not record or record["status"] in ("completed", "failed", "awaiting_review"):
                        break
                    time.sleep(1)
            if record:
                if record["status"] == "failed":
                    st.error(record["error_message"])
                else:
                    st.success(f"დაემატა {record['rows_imported']}, დუბლიკატი {record['duplicates_skipped']}, "
                               f"დასადასტურებელი {record['needs_review_count']}")
                for error in record["errors"]:
                    st.warning(f"სტრიქონი {error['row']}: {error['reason']}")

    imports = call(api.get_imports, token()) or []
    for record in imports:
        if record["status"] == "awaiting_review":
            st.divider()
            st.write(f"იმპორტი #{record['id']} ({record['filename']})")
            review_form(record["id"])

    st.subheader("ჩემი იმპორტები")
    if imports:
        st.table(pd.DataFrame([{
            "ფაილი": r["filename"],
            "სტატუსი": STATUS_NAMES.get(r["status"], r["status"]),
            "დაემატა": r["rows_imported"],
            "დუბლიკატი": r["duplicates_skipped"],
        } for r in imports]))


def transactions_page():
    st.title("💳 ტრანზაქციები")
    categories = call(api.get_categories, token()) or []
    names = category_names(categories)

    left, right = st.columns(2)
    search = left.text_input("ძებნა")
    only_uncategorized = right.checkbox("მხოლოდ უკატეგორიო")
    result = call(api.get_transactions, token(), search=search, uncategorized=only_uncategorized, size=100)
    if not result:
        return
    st.caption(f"სულ {result['total']} ტრანზაქცია")
    items = result["items"]
    if items:
        st.dataframe(pd.DataFrame([{
            "თარიღი": t["date"],
            "მერჩანტი": t["merchant"],
            "თანხა": ("-" if t["type"] == "expense" else "+") + money(t["amount"]),
            "კატეგორია": t["category_name"] or "❓",
            "წყარო": t["category_source"] or "",
        } for t in items]), hide_index=True)

        st.subheader("კატეგორიის შეცვლა")
        labels = {t["id"]: f"{t['date']} · {t['merchant']} · {money(t['amount'])}" for t in items}
        transaction_id = st.selectbox("ტრანზაქცია", list(labels), format_func=lambda i: labels[i])
        transaction = next(t for t in items if t["id"] == transaction_id)
        options = [c["id"] for c in categories if c["kind"] == transaction["type"]]
        with st.form("change_category"):
            category_id = st.selectbox("კატეგორია", options, format_func=lambda i: names[i])
            remember = st.checkbox("დაიმახსოვრე ამ მერჩანტისთვის")
            if st.form_submit_button("შენახვა"):
                if call(api.change_category, token(), transaction_id, category_id, remember):
                    st.rerun()
        if st.button("🗑 ტრანზაქციის წაშლა"):
            if call(api.delete_transaction, token(), transaction_id):
                st.rerun()

    st.subheader("ხელით დამატება")
    with st.form("add_transaction", clear_on_submit=True):
        day = st.date_input("თარიღი")
        amount = st.text_input("თანხა")
        kind = st.selectbox("ტიპი", ["expense", "income"], format_func=lambda k: KIND_NAMES[k])
        description = st.text_input("აღწერა")
        if st.form_submit_button("დამატება"):
            data = {"date": day.isoformat(), "amount": amount, "type": kind, "description": description}
            if call(api.create_transaction, token(), data):
                st.rerun()


def budgets_page():
    st.title("🎯 ბიუჯეტები")
    month = st.selectbox("თვე", last_months())
    status = call(api.get_budgets, token(), month)
    if status:
        for budget in status["budgets"]:
            used = float(budget["used_percent"] or 0)
            mark = "🔴" if budget["exceeded"] else "🟢"
            st.write(f"{mark} **{budget['category']}**: {money(budget['spent'])} / {money(budget['limit'])}")
            st.progress(min(used / 100, 1.0))
        if not status["budgets"]:
            st.info("ბიუჯეტი ჯერ არ გაქვს.")

    categories = [c for c in call(api.get_categories, token()) or [] if c["kind"] == "expense"]
    names = category_names(categories)
    with st.form("set_budget"):
        st.subheader("ლიმიტის დაყენება")
        category_id = st.selectbox("კატეგორია", list(names), format_func=lambda i: names[i])
        limit = st.text_input("თვიური ლიმიტი")
        if st.form_submit_button("შენახვა", type="primary"):
            if call(api.set_budget, token(), category_id, limit):
                st.rerun()


def reports_page():
    st.title("📄 თვიური ანგარიშები")
    month = st.selectbox("თვე", last_months())
    force = st.checkbox("თავიდან შექმნა")
    if st.button("ანგარიშის მიღება", type="primary"):
        with st.spinner("ანგარიში იწერება..."):
            report = call(api.create_report, token(), month, force)
        if report:
            st.markdown(report["explanation"]["text"])
            with st.expander("ფაქტები, რომლებზეც ანგარიში დაეყრდნო"):
                st.json(report["facts"])


def categories_page():
    st.title("🏷️ კატეგორიები")
    categories = call(api.get_categories, token()) or []
    st.table(pd.DataFrame([{
        "სახელი": c["name"],
        "ტიპი": KIND_NAMES[c["kind"]],
        "სისტემური": "✅" if c["is_system"] else "",
    } for c in categories]))

    with st.form("new_category", clear_on_submit=True):
        st.subheader("ახალი კატეგორია")
        name = st.text_input("სახელი")
        kind = st.selectbox("ტიპი", ["expense", "income"], format_func=lambda k: KIND_NAMES[k])
        system = st.checkbox("სისტემური (ყველასთვის)") if st.session_state["user"]["role"] == "admin" else False
        if st.form_submit_button("დამატება"):
            if call(api.create_category, token(), name, kind, system):
                st.rerun()

    own = [c for c in categories if not c["is_system"]]
    if own:
        st.subheader("ჩემი კატეგორიის წაშლა")
        names = category_names(own)
        category_id = st.selectbox("კატეგორია", list(names), format_func=lambda i: names[i])
        if st.button("🗑 წაშლა"):
            if call(api.delete_category, token(), category_id):
                st.rerun()


def admin_page():
    st.title("🛠 ადმინისტრირება")
    knowledge_tab, statistics_tab = st.tabs(["ფინანსური ცნობარი", "სტატისტიკა"])

    with knowledge_tab:
        with st.form("new_article", clear_on_submit=True):
            title = st.text_input("სათაური")
            content = st.text_area("ტექსტი")
            if st.form_submit_button("დამატება"):
                if call(api.create_article, token(), title, content):
                    st.rerun()
        for article in call(api.get_articles, token()) or []:
            with st.expander(article["title"]):
                st.write(article["content"])
                if st.button("🗑 წაშლა", key=f"delete_article_{article['id']}"):
                    if call(api.delete_article, token(), article["id"]):
                        st.rerun()

    with statistics_tab:
        statistics = call(api.get_statistics, token())
        if statistics:
            overview = statistics["overview"]
            left, middle, right = st.columns(3)
            left.metric("მომხმარებლები", overview["users"])
            middle.metric("იმპორტები", overview["imports"])
            right.metric("ტრანზაქციები", overview["transactions"])
            st.subheader("კატეგორიზაციის წყარო")
            st.table(pd.DataFrame([{"წყარო": k, "რაოდენობა": v} for k, v in statistics["sources"].items()]))
            st.subheader("AI-ის სიზუსტე")
            st.write(statistics["accuracy"])


def main():
    if "token" not in st.session_state:
        login_page()
        return

    user = st.session_state["user"]
    pages = {
        "📊 დეშბორდი": dashboard_page,
        "💬 ასისტენტი": chat_page,
        "📥 იმპორტი": import_page,
        "💳 ტრანზაქციები": transactions_page,
        "🎯 ბიუჯეტები": budgets_page,
        "📄 ანგარიშები": reports_page,
        "🏷️ კატეგორიები": categories_page,
    }
    if user["role"] == "admin":
        pages["🛠 ადმინისტრირება"] = admin_page

    st.sidebar.write(f"**{user['full_name']}**")
    page = st.sidebar.radio("მენიუ", list(pages))
    if st.sidebar.button("გასვლა"):
        st.session_state.clear()
        st.rerun()
    pages[page]()


main()
