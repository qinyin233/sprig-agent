"""Bounded human clarification; answers never authorize tools or checks."""

import json
import sys

from .config import redact, terminal_text
from .json_budget import encoded_size

MAX_QUESTIONS, MAX_OPTIONS, MAX_ANSWER = 3, 6, 2048
MAX_PROMPT_BYTES, MAX_RESULT_BYTES = 8192, 8192
QUESTION_SCHEMA = {"type": "function", "function": {
    "name": "question", "description": "Ask 1-3 requirement/preference questions and await explicit human answers. Custom answers always allowed. Never request credentials or action approval. Other proposals in this response are skipped; replan after answers.",
    "parameters": {"type": "object", "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
        "items": {"type": "object", "properties": {
            "header": {"type": "string", "minLength": 1, "maxLength": 30},
            "question": {"type": "string", "minLength": 1, "maxLength": 512},
            "options": {"type": "array", "maxItems": MAX_OPTIONS, "items": {"type": "object", "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": 64},
                "description": {"type": "string", "maxLength": 160}}, "required": ["label", "description"], "additionalProperties": False}},
            "multiple": {"type": "boolean"}}, "required": ["header", "question"], "additionalProperties": False}}},
        "required": ["questions"], "additionalProperties": False}}}


def _text(value, limit, *, empty=False, multiline=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(char) < 32 and (not multiline or char not in "\n\t") for char in value)):
        raise ValueError("invalid question or answer text; check length and control characters")
    value.encode("utf-8")  # Reject lone surrogates before UI, history or HTTP.
    return value


def validate_questions(questions, secrets=()):
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError("question requires 1-3 questions")
    result = []
    for row in questions:
        if (not isinstance(row, dict) or set(row) - {"header", "question", "options", "multiple"}
                or not {"header", "question"} <= set(row) or type(row.get("multiple", False)) is not bool
                or not isinstance(row.get("options", []), list) or len(row.get("options", [])) > MAX_OPTIONS):
            raise ValueError("invalid question fields or options")
        options, labels = [], set()
        for option in row.get("options", []):
            if not isinstance(option, dict) or set(option) != {"label", "description"}:
                raise ValueError("question options require label and description")
            label = _text(redact(_text(option["label"], 64), secrets), 64)
            if label in labels:
                raise ValueError("question option labels must be unique after redaction")
            labels.add(label)
            options.append({"label": label, "description": _text(redact(_text(option["description"], 160, empty=True, multiline=True), secrets), 160, empty=True, multiline=True)})
        result.append({"header": _text(redact(_text(row["header"], 30), secrets), 30),
                       "question": _text(redact(_text(row["question"], 512, multiline=True), secrets), 512, multiline=True),
                       "options": options, "multiple": row.get("multiple", False)})
    encoded_size(result, max_bytes=MAX_PROMPT_BYTES)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("question prompt exceeds 8 KiB")
    return result


def answer_result(questions, answers, budget=MAX_RESULT_BYTES, secrets=()):
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("answer every question explicitly, or close the question without choosing")
    selected = []
    for question, values in zip(questions, answers):
        maximum = MAX_OPTIONS + 1 if question["multiple"] else 1
        if not isinstance(values, list) or not 1 <= len(values) <= maximum:
            raise ValueError("invalid number of answers for this question")
        clean = [_text(redact(_text(value, MAX_ANSWER, multiline=True), secrets), MAX_ANSWER, multiline=True) for value in values]
        if len(set(clean)) != len(clean):
            raise ValueError("answers must be unique after redaction")
        selected.append(clean)
    result = {"answered": True, "answers": selected, "not_authorization": True}
    encoded_size(result, max_bytes=min(budget, MAX_RESULT_BYTES))
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > min(budget, MAX_RESULT_BYTES):
        raise ValueError("question answers exceed the output budget")
    return result


def terminal_questions(questions, *, budget=MAX_RESULT_BYTES, secrets=(), read=None, emit=None):
    """No tty/EOF/default is an answer. Choice numbers use one-based indexes."""
    if read is None and not sys.stdin.isatty():
        return None
    read = read or input
    emit = emit or (lambda text: print(text, file=sys.stderr, flush=True))
    emit("Clarification only; answers go to the model and enabled session saves. They do not approve actions. /cancel closes without an answer.")
    answers = []
    for index, question in enumerate(questions):
        emit(terminal_text(f"{index + 1}/{len(questions)} {question['header']}: {question['question']}"))
        for number, option in enumerate(question["options"], 1):
            emit(terminal_text(f"  {number}. {option['label']} — {option['description']}"))
        while True:
            try:
                value = read("Choose number(s) separated by commas, or type : custom text; empty does not choose: ")
            except EOFError:
                return None
            if value.strip() == "/cancel":
                return None
            try:
                if question["options"]:
                    numbers_text, custom_marker, custom = value.partition(":")
                    numbers = numbers_text.strip().rstrip(",").split(",") if numbers_text.strip().rstrip(",") else []
                    if not all(part.strip().isdigit() for part in numbers):
                        raise ValueError("use a listed number or prefix a custom answer with ':'")
                    indexes = [int(part.strip()) - 1 for part in numbers]
                    if any(not 0 <= item < len(question["options"]) for item in indexes):
                        raise ValueError("choice number is outside this question's options")
                    values = [question["options"][item]["label"] for item in indexes]
                    if custom_marker:
                        values.append(custom.lstrip())
                else:
                    values = [value[1:].lstrip() if value.startswith(":") else value]
                partial = [*answers, values]
                # Check the current response immediately; global size is also
                # checked before the final answer is released to the agent.
                answer_result(questions[:index + 1], partial, budget, secrets)
                answers.append(values)
                break
            except (ValueError, UnicodeError):
                emit("Answer not sent: choose explicitly, remove duplicates, or shorten text to fit the output budget.")
    return answer_result(questions, answers, budget, secrets)["answers"]
