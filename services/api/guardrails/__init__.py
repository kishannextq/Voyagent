"""Deterministic content rails (policy in guardrails/policy.yaml).

check_input(text)    -> InputVerdict: blocked? which rule? text with PII redacted
check_output(text)   -> OutputVerdict: text with PII redacted, unsafe links removed, leaks withheld
tool_output(service, text) -> text wrapped as delimited data, capped

Rules are matched on NFKC-normalised, lower-cased text with zero-width characters removed, so
simple obfuscation (fullwidth letters, invisible characters) doesn't bypass them. Logs record
rule ids only, never the text.
"""

import logging
import pathlib
import re
import unicodedata
from dataclasses import dataclass, field

import yaml
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("voyagent.guardrails")

POLICY_PATH = pathlib.Path(__file__).parent / "policy.yaml"

PII_PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),
    "passport": re.compile(r"\bpassport\s*(?:no\.?|number|#)?\s*[:#]?\s*[A-Z0-9]{6,9}\b", re.I),
    # Needs a phone shape (international +, (area) code, trunk 0, or ddd-ddd-dddd) so prices,
    # dates, booking refs and long order numbers aren't mistaken for phone numbers.
    "phone": re.compile(
        r"(?<![\w-])(?:\+\d{1,3}[ .-]?(?:\(?\d{1,4}\)?[ .-]?){1,4}\d{2,4}"
        r"|\(\d{2,4}\)[ .-]?\d{3,4}[ .-]?\d{3,4}"
        r"|0\d{2,4}[ .-]?\d{3,4}[ .-]?\d{3,4}"
        r"|\d{3}-\d{3}-\d{4})(?![\w-])"
    ),
}
_ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿]")
_LINK = re.compile(r"\[([^\]]*)\]\(\s*([a-zA-Z][a-zA-Z0-9+.-]*):[^)]*\)")


class _Pattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    regex: str


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_chars: int
    block_patterns: list[_Pattern]
    redact_pii: list[str]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_chars: int
    canary: str
    redact_pii: list[str]
    allowed_link_schemes: list[str]


class _Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_chars: int


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: _Input
    output: _Output
    tool_output: _Tool


def load(path: pathlib.Path = POLICY_PATH) -> tuple[Policy, list[tuple[str, re.Pattern]]]:
    policy = Policy(**yaml.safe_load(path.read_text(encoding="utf-8")))
    for kind in policy.input.redact_pii + policy.output.redact_pii:
        if kind not in PII_PATTERNS:
            raise ValueError(f"unknown PII type {kind}")
    compiled = [(p.id, re.compile(p.regex, re.I | re.S | re.M)) for p in policy.input.block_patterns]
    return policy, compiled


POLICY, _BLOCK = load()


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return re.sub(r"[ \t]+", " ", text).lower()


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str, kinds: list[str]) -> tuple[str, list[str]]:
    found: list[str] = []
    # Fixed precedence, most specific first: a card number must not be half-eaten as a phone.
    for kind in sorted(kinds, key=list(PII_PATTERNS).index):
        pattern = PII_PATTERNS[kind]

        def sub(m: re.Match, kind=kind) -> str:
            raw = m.group(0)
            if kind == "card":
                digits = re.sub(r"\D", "", raw)
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return raw  # not a card number (dates, prices, flight numbers...)
            if kind == "phone" and len(re.sub(r"\D", "", raw)) < 9:
                return raw
            found.append(kind)
            return f"[{kind} removed]"

        text = pattern.sub(sub, text)
    return text, found


@dataclass
class InputVerdict:
    allowed: bool
    text: str
    rule: str | None = None
    redacted: list[str] = field(default_factory=list)


def check_input(text: str) -> InputVerdict:
    if len(text) > POLICY.input.max_chars:
        return InputVerdict(False, text, "too-long")
    norm = normalise(text)
    for rule_id, pattern in _BLOCK:
        if pattern.search(norm):
            log.warning("guardrail_input_blocked", extra={"rule": rule_id})
            return InputVerdict(False, text, rule_id)
    clean, found = redact(text, POLICY.input.redact_pii)
    if found:
        log.info("guardrail_input_redacted", extra={"pii": sorted(set(found))})
    return InputVerdict(True, clean, None, found)


@dataclass
class OutputVerdict:
    text: str
    withheld: bool = False
    redacted: list[str] = field(default_factory=list)
    links_removed: int = 0


WITHHELD = "This response was withheld by the content policy. Please try rephrasing your request."


def check_output(text: str) -> OutputVerdict:
    if POLICY.output.canary.lower() in normalise(text):
        log.error("guardrail_output_leak", extra={"rule": "canary"})
        return OutputVerdict(WITHHELD, withheld=True)
    text = text[: POLICY.output.max_chars]
    text, found = redact(text, POLICY.output.redact_pii)
    removed = 0

    def link(m: re.Match) -> str:
        nonlocal removed
        if m.group(2).lower() in POLICY.output.allowed_link_schemes:
            return m.group(0)
        removed += 1
        return m.group(1)  # keep the label, drop the unsafe target (javascript:, data:, file:...)

    text = _LINK.sub(link, text)
    if found or removed:
        log.info("guardrail_output_cleaned", extra={"pii": sorted(set(found)), "links_removed": removed})
    return OutputVerdict(text, False, found, removed)


def tool_output(service: str, text: str) -> str:
    """Tool results as clearly delimited data (never instructions), length-capped."""
    body = str(text)[: POLICY.tool_output.max_chars]
    body = re.sub(r"</?tool_output[^>]*>", "", body, flags=re.I)
    return f'<tool_output source="{service}">\n{body}\n</tool_output>'
