import json
import os

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TIMEOUT = 120

ERROR_MESSAGES = {
    401: "სესია ამოიწურა ან მონაცემები არასწორია.",
    403: "ამ მოქმედების უფლება არ გაქვს.",
    404: "ჩანაწერი ვერ მოიძებნა.",
    409: "მოქმედება ახლა ვერ შესრულდება.",
    413: "ფაილი ძალიან დიდია (მაქსიმუმ 2 MB).",
    422: "შეყვანილი მონაცემები არასწორია.",
    429: "ძალიან ბევრი მოთხოვნა, სცადე მოგვიანებით.",
    503: "AI ასისტენტი დროებით მიუწვდომელია.",
}


class APIError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def error_from_response(response: httpx.Response) -> APIError:
    message = ERROR_MESSAGES.get(response.status_code, "სერვერზე შეცდომა მოხდა.")
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str):
        message = f"{message} ({detail})"
    return APIError(message, response.status_code)


def request(method: str, path: str, token: str | None = None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.request(method, API_BASE_URL + path, headers=headers, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError:
        raise APIError("სერვერთან დაკავშირება ვერ მოხერხდა.")
    if response.status_code >= 400:
        raise error_from_response(response)
    if response.status_code == 204:
        return True
    return response.json()


def stream(method: str, path: str, token: str, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.stream(method, API_BASE_URL + path, headers=headers, timeout=TIMEOUT, **kwargs) as response:
            if response.status_code >= 400:
                response.read()
                raise error_from_response(response)
            for line in response.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
    except httpx.HTTPError:
        raise APIError("სერვერთან კავშირი გაწყდა.")


def register(email, full_name, password):
    return request("POST", "/auth/register", json={"email": email, "full_name": full_name, "password": password})


def login(email, password):
    return request("POST", "/auth/login", json={"email": email, "password": password})


def get_categories(token):
    return request("GET", "/categories", token)


def create_category(token, name, kind, system=False):
    return request("POST", "/categories", token, json={"name": name, "kind": kind, "system": system})


def rename_category(token, category_id, name):
    return request("PUT", f"/categories/{category_id}", token, json={"name": name})


def delete_category(token, category_id):
    return request("DELETE", f"/categories/{category_id}", token)


def get_transactions(token, **filters):
    params = {key: value for key, value in filters.items() if value not in (None, "")}
    return request("GET", "/transactions", token, params=params)


def create_transaction(token, data):
    return request("POST", "/transactions", token, json=data)


def change_category(token, transaction_id, category_id, remember):
    return request("PATCH", f"/transactions/{transaction_id}", token, json={"category_id": category_id, "remember": remember})


def delete_transaction(token, transaction_id):
    return request("DELETE", f"/transactions/{transaction_id}", token)


def upload_statement(token, filename, content):
    return request("POST", "/imports", token, files={"file": (filename, content, "text/csv")})


def get_imports(token):
    return request("GET", "/imports", token)


def get_import(token, import_id):
    return request("GET", f"/imports/{import_id}", token)


def get_review(token, import_id):
    return request("GET", f"/imports/{import_id}/review", token)


def submit_review(token, import_id, decisions):
    return request("POST", f"/imports/{import_id}/review", token, json={"decisions": decisions})


def get_budgets(token, month):
    return request("GET", "/budgets", token, params={"month": month})


def set_budget(token, category_id, monthly_limit):
    return request("PUT", f"/budgets/{category_id}", token, json={"monthly_limit": monthly_limit})


def delete_budget(token, category_id):
    return request("DELETE", f"/budgets/{category_id}", token)


def get_summary(token, month):
    return request("GET", "/summary", token, params={"month": month})


def get_recurring(token):
    return request("GET", "/summary/recurring", token)


def get_anomalies(token, month):
    return request("GET", "/summary/anomalies", token, params={"month": month})


def create_report(token, month, force=False):
    return request("POST", f"/reports/{month}", token, params={"force": force})


def get_reports(token):
    return request("GET", "/reports", token)


def get_chats(token):
    return request("GET", "/chat/sessions", token)


def create_chat(token):
    return request("POST", "/chat/sessions", token)


def get_messages(token, chat_id):
    return request("GET", f"/chat/sessions/{chat_id}/messages", token)


def send_message(token, chat_id, content):
    return stream("POST", f"/chat/sessions/{chat_id}/messages", token, json={"content": content})


def delete_chat(token, chat_id):
    return request("DELETE", f"/chat/sessions/{chat_id}", token)


def get_articles(token):
    return request("GET", "/knowledge/articles", token)


def create_article(token, title, content, is_published=True):
    return request("POST", "/knowledge/articles", token, json={"title": title, "content": content, "is_published": is_published})


def update_article(token, article_id, title, content, is_published):
    return request("PUT", f"/knowledge/articles/{article_id}", token, json={"title": title, "content": content, "is_published": is_published})


def delete_article(token, article_id):
    return request("DELETE", f"/knowledge/articles/{article_id}", token)


def get_statistics(token):
    return {
        "overview": request("GET", "/analytics/overview", token),
        "sources": request("GET", "/analytics/categorization-sources", token),
        "accuracy": request("GET", "/analytics/ai-accuracy", token),
    }
