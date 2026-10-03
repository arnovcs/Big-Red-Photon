"""Signup form validation: plain HTML form fields → a checked SignupForm, or field errors."""

import re

from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from app.onboarding.web_claim import normalize_phone

MAX_NAME_CHARS = 30
_NAME = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '\-])*$")  # letters, spaces, ' and -


class SignupForm(BaseModel):
    first_name: str
    phone: str  # canonical, e.g. +16075551234
    bank_code: str

    @field_validator("first_name")
    @classmethod
    def _first_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise PydanticCustomError("name", "Please enter your first name.")
        if len(name) > MAX_NAME_CHARS or not _NAME.match(name):
            raise PydanticCustomError("name", "Use letters only, up to 30 characters.")
        return name

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        phone = normalize_phone(value)
        if phone is None:
            raise PydanticCustomError(
                "phone", "Enter the phone number you use for iMessage, like (607) 555-0123."
            )
        return phone

    @field_validator("bank_code")
    @classmethod
    def _bank_code(cls, value: str, info: ValidationInfo) -> str:
        code = value.strip().upper()
        allowed = (info.context or {}).get("bank_codes", set())
        if code not in allowed:
            raise PydanticCustomError("bank", "Choose one of the demo banks.")
        return code


def parse_signup(data: dict[str, str], bank_codes: set[str]) -> tuple[SignupForm | None, dict]:
    """(form, {}) if valid, else (None, {field: message})."""
    try:
        return SignupForm.model_validate(data, context={"bank_codes": bank_codes}), {}
    except ValidationError as exc:
        return None, {str(err["loc"][0]): err["msg"] for err in exc.errors()}
