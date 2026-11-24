"""Text analysis for keyword search: lower-case word tokens, stopwords out, light stemming.

The stemmer is deliberately tiny (plural `-s`/`-es`/`-ies`, `-ing`, `-ed`) so that "returns",
"returned" and "returning" all match "return", and "shipping"/"shipped" match "ship". It
doesn't try to be Porter; a help center's vocabulary is small and readable rules win.
"""

import re

_WORD_RE = re.compile(r"[a-z0-9]+")
_POSSESSIVE_RE = re.compile("['" + chr(0x2019) + r"]s\b")  # straight or curly apostrophe

_STOPWORD_TEXT = """
a about after all also am an and any are as at be been before being but by can could d
did do does doing don for from get got had has have how i if in into is isn it its just
ll m me my no not of on or our out re s so some than that the their them then there these
they this those to too t us ve very was we were what when where which who why will with
won would you your
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_KEEP_DOUBLE = ("ll", "ss", "zz")


def _undouble(word: str) -> str:
    """shipp -> ship, but keep 'll'/'ss'/'zz' (bill, press)."""
    if len(word) > 3 and word[-1] == word[-2] and word[-2:] not in _KEEP_DOUBLE:
        return word[:-1]
    return word


def stem(word: str) -> str:
    if len(word) <= 3 or word.isdigit():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 5:
        return _undouble(word[:-3])
    if word.endswith("ed") and len(word) > 4:
        return _undouble(word[:-2])
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def analyze(text: str) -> list[str]:
    """Search terms for `text`, in order (duplicates kept for term frequency)."""
    text = _POSSESSIVE_RE.sub("", text.lower())
    return [stem(w) for w in _WORD_RE.findall(text) if w not in STOPWORDS]
