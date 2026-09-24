"""Fixed phase-1 chat intents: substring match on the user's query, no NLP."""

from __future__ import annotations

import re

INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    ("should_train_today", ["should i train", "train today", "should i rest", "rest today"]),
    ("upcoming", ["coming up", "upcoming", "calendar", "my plan", "next race", "schedule"]),
    ("week_summary", ["how was my week", "week summary", "this week", "weekly summary"]),
    ("sleep_check", ["how did i sleep", "sleep trend", "my sleep", "sleep"]),
    ("streak", ["streak", "days in a row", "consistency"]),
    ("load_check", ["training load", "how much have i trained", "volume"]),
]

EXAMPLE_QUESTIONS = [
    "Should I train today?",
    "What's coming up?",
    "How was my week?",
    "How did I sleep?",
    "What's my streak?",
    "What's my training load?",
]


def match_intent(query: str) -> str:
    normalized = re.sub(r"[^\w\s]", "", query.lower()).strip()
    for intent, patterns in INTENT_PATTERNS:
        if any(p in normalized for p in patterns):
            return intent
    return "help"
