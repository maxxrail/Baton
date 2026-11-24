"""The runtime's own customer-facing sentences, in the customer's language.

Most replies are written by the model, which answers in the customer's language on its own.
A few are written by the runtime (`ESCALATION_REPLY`, the grounding guardrail's hedge note and
abstention, the fallback and error replies), and those were English only, so a Spanish
conversation could end with an English sentence (found by the `return_spanish_speaker`
simulation). `localize` swaps each known English sentence for its translation.

Language detection is deliberately small: it counts common function words in what the
customer has written. It only has to tell English from Spanish and French reliably enough to
pick a fixed sentence; anything it isn't sure about stays English.
"""

import re
from typing import Literal

Language = Literal["en", "es", "fr"]

_EN = "the and you my is to it of for with this that have can please"
_ES = "el la los las que de mi por para con una un es quiero pedido gracias hola puede devolver"
_FR = "le la les des du de mon ma est pour avec une un je commande merci bonjour vous suivi"
_WORDS: dict[Language, frozenset[str]] = {
    "en": frozenset(_EN.split(" ")),
    "es": frozenset(_ES.split(" ")),
    "fr": frozenset(_FR.split(" ")),
}
_TOKEN = re.compile(r"[a-zà-ÿ]+")
_MIN_HITS = 3  # below this, stay in English


def detect_language(text: str) -> Language:
    tokens = _TOKEN.findall(text.lower())
    counts = {lang: sum(t in words for t in tokens) for lang, words in _WORDS.items()}
    candidates: tuple[Language, ...] = ("es", "fr")
    best = max(candidates, key=lambda lang: counts[lang])
    if counts[best] >= _MIN_HITS and counts[best] > counts["en"]:
        return best
    return "en"


# English sentence (exactly as the runtime writes it) -> translations.
_TRANSLATIONS: dict[str, dict[Language, str]] = {}


def register(english: str, es: str, fr: str) -> str:
    """Declare a runtime sentence's translations; returns the English text unchanged."""
    _TRANSLATIONS[english] = {"es": es, "fr": fr}
    return english


def localize(text: str, language: Language) -> str:
    """`text` with every registered runtime sentence translated (model text is untouched)."""
    if language == "en":
        return text
    for english, translations in _TRANSLATIONS.items():
        if english in text:
            text = text.replace(english, translations[language])
    return text
