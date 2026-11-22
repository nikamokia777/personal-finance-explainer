from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    currency: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class ProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["expense", "income"] = "expense"
    system: bool = False


class CategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TransactionCreate(BaseModel):
    date: date
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    type: Literal["expense", "income"]
    description: str = Field(min_length=1, max_length=255)
    category_id: int | None = None


class TransactionUpdate(BaseModel):
    category_id: int
    remember: bool = False


class BudgetUpdate(BaseModel):
    monthly_limit: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class ReviewDecision(BaseModel):
    transaction_id: int
    category_id: int
    remember: bool = False


class ReviewRequest(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1)


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ArticleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    is_published: bool = True


class MerchantCategory(BaseModel):
    merchant: str
    category: str = Field(description="One of the allowed categories, exactly as written in the list")
    confidence: float = Field(ge=0, le=1)


class MerchantCategorizationBatch(BaseModel):
    items: list[MerchantCategory]


class QuestionAnalysis(BaseModel):
    intent: Literal[
        "spending_summary",
        "explain_change",
        "budget_status",
        "subscriptions",
        "what_if",
        "monthly_report",
        "finance_education",
        "off_topic",
    ]
    period: Literal["this_month", "last_month", "last_3_months", "this_year", "custom"] = "this_month"
    start_date: date | None = Field(default=None, description='Only when period="custom"')
    end_date: date | None = None
    category: str | None = Field(default=None, description="Category name, if the user mentioned one")
    compare_to: Literal["previous_period", "same_period_last_year"] | None = None
    saving_percent: float | None = Field(default=None, gt=0, le=100, description="For what_if")
    saving_months: int | None = Field(default=None, gt=0, le=120)
    refers_to_previous: bool = Field(description='Whether the question continues the previous one, e.g. "and last month?"')


class ExplanationDraft(BaseModel):
    summary: str = Field(max_length=400, description="The main answer in 1-2 sentences")
    details: list[str] = Field(description="Short points about what happened. Only numbers from the facts")
    suggestions: list[str] = Field(default_factory=list, max_length=3, description="Practical notes, no investment advice")
