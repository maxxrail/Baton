from baton.channels.citations import render_citations, render_for_channel
from baton.runtime.agent import Citation

CANADA = Citation(
    article_id="shipping-international",
    title="International orders and shipping to Canada",
    url="https://help.example/articles/shipping-international",
)
COSTS = Citation(
    article_id="shipping-costs",
    title="Shipping costs and free shipping",
    url="https://help.example/articles/shipping-costs",
)
TEXT = (
    "Canada takes 6-10 business days [kb:shipping-international]. It costs CA$18 "
    "[kb:shipping-international], and US orders over $75 ship free [kb:shipping-costs]."
)


def test_markdown_numbers_links_and_lists_sources() -> None:
    out = render_citations(TEXT, [CANADA, COSTS], "markdown")
    assert "6-10 business days [1](https://help.example/articles/shipping-international)." in out
    assert "ship free [2](https://help.example/articles/shipping-costs)." in out
    assert out.endswith(
        "Sources:\n"
        "1. [International orders and shipping to Canada]"
        "(https://help.example/articles/shipping-international)\n"
        "2. [Shipping costs and free shipping](https://help.example/articles/shipping-costs)"
    )


def test_footnotes_for_terminal_and_email() -> None:
    out = render_for_channel(TEXT, [CANADA, COSTS], "cli")
    assert "business days [1]. It costs CA$18 [1], and US orders over $75 ship free [2]." in out
    assert (
        "[2] Shipping costs and free shipping - https://help.example/articles/shipping-costs" in out
    )
    assert render_for_channel(TEXT, [CANADA, COSTS], "email") == out


def test_sms_drops_tags_and_adds_one_link() -> None:
    out = render_for_channel(TEXT, [CANADA, COSTS], "sms")
    assert "[kb:" not in out
    assert "business days. It costs CA$18, and" in out
    assert out.endswith("More: https://help.example/articles/shipping-international")


def test_unknown_articles_are_dropped_never_rendered_as_dead_links() -> None:
    out = render_citations("Fact [kb:made-up]. Other [kb:shipping-costs].", [COSTS], "footnotes")
    assert "made-up" not in out
    assert out.startswith("Fact. Other [1].")


def test_text_without_citations_is_unchanged() -> None:
    assert render_citations("Hello there!", [], "markdown") == "Hello there!"


def test_repeated_tags_for_the_same_article_collapse() -> None:
    text = (
        "Canada takes 6-10 days [kb:shipping-international]. [kb:shipping-international]\n\n"
        "Next paragraph [kb:shipping-international] [kb:shipping-costs]."
    )
    out = render_citations(text, [CANADA, COSTS], "footnotes")
    assert out.startswith("Canada takes 6-10 days [1].\n\nNext paragraph [1] [2].")
