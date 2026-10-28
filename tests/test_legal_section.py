"""The validator of a legal section (#435): what it refuses, with which line, and that it never
quotes the text; what it accepts, with the bytes it hands on."""

import pytest

from hal.stack.legal_section import (
    CANONICAL_RULE,
    ELEMENTS,
    MAXIMUM_BYTES,
    LegalError,
    Link,
    parse_fragment,
    parse_general,
)

PATH = "legal/sections/hopin/datenschutz.html"
ANCHOR = "hopin"
ZONE = "edge-fixture.invalid"
CHARACTER_HINT = "write the character itself, not a reference such as &quot; or &nbsp;"
ALLOWED = f"allowed: {' '.join(ELEMENTS)}"
GOOD = (
    '<h2 id="hopin">HopIn</h2>\n'
    '<p>Intro <strong>fett</strong> <a href="https://example.org/a?b=1&amp;c=2">Link</a><br>Ende</p>\n'
    '<section id="hopin-daten">\n'
    "<h3>Daten</h3>\n"
    "<ul>\n<li>eins</li>\n<li>zwei</li>\n</ul>\n"
    "</section>\n"
)


def fragment(text: str | bytes, anchor: str = ANCHOR):
    data = text if isinstance(text, bytes) else text.encode()
    return parse_fragment(PATH, data, anchor, ZONE)


def refusal(text: str | bytes, anchor: str = ANCHOR) -> str:
    with pytest.raises(LegalError) as refused:
        fragment(text, anchor)
    return str(refused.value)


def in_paragraph(inline: str) -> str:
    return f'<h2 id="hopin">HopIn</h2>\n<p>{inline}</p>\n'


def test_a_section_in_the_grammar_hands_its_bytes_on_unchanged():
    """L14."""
    parsed = fragment(GOOD)

    (heading,) = parsed.headings
    assert (heading.id, heading.title) == ("hopin", "HopIn")
    assert heading.content == (
        '\n<p>Intro <strong>fett</strong> <a href="https://example.org/a?b=1&amp;c=2">Link</a>'
        "<br>Ende</p>\n"
    )
    (subsection,) = parsed.subsections
    assert (subsection.id, subsection.title) == ("hopin-daten", "Daten")
    assert subsection.content == "\n<ul>\n<li>eins</li>\n<li>zwei</li>\n</ul>\n"


def test_a_refusal_names_the_file_and_the_line_and_the_rule():
    """L11."""
    text = '<h2 id="hopin">HopIn</h2>\n<img src="x">\n'

    assert refusal(text) == f"legal: {PATH}: line 2: <img> is not allowed ({ALLOWED})"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('<h2 id="hopin">HopIn</h2>\n<script>alert(1)</script>\n', "<script> is not allowed"),
        ('<h2 id="hopin">HopIn</h2>\n<style>p{}</style>\n', "<style> is not allowed"),
        ('<h2 id="hopin">HopIn</h2>\n<form></form>\n', "<form> is not allowed"),
        ('<h2 id="hopin">HopIn</h2>\n<table></table>\n', "<table> is not allowed"),
        (in_paragraph('<img src="x">'), "<img> is not allowed"),
        (in_paragraph("</div>"), "</div> is not allowed"),
        (
            '<h2 id="hopin">HopIn</h2>\n<p class="x">a</p>\n',
            "attribute class is not allowed on <p>",
        ),
        (in_paragraph('<a href="https://a.de" id="x">a</a>'), "attribute id is not allowed on <a>"),
        (in_paragraph('<a href="https://a.de" href="https://b.de">a</a>'), "repeats the attribute"),
        (in_paragraph("<a href>a</a>"), "attribute href has no value"),
        (in_paragraph("<a>a</a>"), "<a> needs the attribute href"),
        ("<h2>HopIn</h2>\n<p>a</p>\n", "<h2> needs the attribute id"),
        (in_paragraph("<!-- a -->"), "comments, doctype, CDATA and processing instructions"),
        (in_paragraph("<?php x ?>"), "comments, doctype, CDATA and processing instructions"),
        (in_paragraph("<![CDATA[x]]>"), "comments, doctype, CDATA and processing instructions"),
        ("<!doctype html>\n", "comments, doctype, CDATA and processing instructions"),
        (in_paragraph("<br/>"), CANONICAL_RULE),
        (in_paragraph("<p/>"), "<p/> is not allowed"),
    ],
)
def test_a_token_outside_the_allow_list_is_refused(text, rule):
    """L7."""
    assert rule in refusal(text)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (in_paragraph('<a href="https://a.de">x <a href="https://b.de">y</a></a>'), "plain text"),
        (in_paragraph("<strong><em>x</em></strong>"), "plain text"),
        (in_paragraph("<strong></strong>"), "plain text"),
        ('<h2 id="hopin">HopIn</h2>\n<p><ul><li>a</li></ul></p>\n', "<ul> is not allowed here"),
        (
            '<h2 id="hopin">HopIn</h2>\n<ul><li>a<ul><li>b</li></ul></li></ul>\n',
            "<ul> is not allowed",
        ),
        ('<h2 id="hopin">HopIn</h2>\n<p></p>\n', "<p> is empty"),
        ('<h2 id="hopin">HopIn</h2>\n<p>  </p>\n', "<p> is empty"),
        ('<h2 id="hopin">HopIn</h2>\n<ul><li></li></ul>\n', "<li> is empty"),
        ('<h2 id="hopin">HopIn</h2>\n<ul></ul>\n', "<ul> needs an <li>"),
        ('<h2 id="hopin">HopIn</h2>\nloose text\n', "text outside a paragraph"),
        (
            '<h2 id="hopin">HopIn</h2>\n<p>a</p>\n<h2 id="hopin">b</h2>\n',
            "<h2> is not allowed here",
        ),
        ('<h2 id="hopin">HopIn</h2>\n<p>a</p>\n<p>b</p>\n<h3>x</h3>\n', "<h3> is not allowed here"),
        ('<h2 id="hopin">HopIn</h2>\n<p>a', "<p> is never closed"),
        ('<h2 id="hopin">HopIn</h2>\n<p>a</p>\ntail', "text outside a paragraph"),
        ('<h2 id="hopin">HopIn</h2>\n<p>a</p>\n</p>\n', "</p> is not allowed here"),
        ('<h2 id="hopin"></h2>\n', "<h2> needs its text"),
        ("", "<h2> is expected"),
        ('<p>a</p>\n<h2 id="hopin">HopIn</h2>\n', "<p> is not allowed here"),
    ],
)
def test_the_content_model_of_the_grammar_is_enforced(text, rule):
    """L7."""
    assert rule in refusal(text)


def test_a_section_inside_a_section_is_refused():
    """L7."""
    inner = '<section id="hopin-a">\n<h3>A</h3>\n<section id="hopin-b">\n'

    assert "<section> is not allowed here" in refusal(f'<h2 id="hopin">HopIn</h2>\n{inner}')


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (in_paragraph("a<br/>b"), 2),
        ('<h2 id="hopin">HopIn</h2>\n<P>a</P>\n', 2),
        ("<h2 id='hopin'>HopIn</h2>\n<p>a</p>\n", 1),
        (in_paragraph("a > b"), 2),
        (in_paragraph("AT&T"), 2),
        ('<h2 id="hopin" >HopIn</h2>\n<p>a</p>\n', 1),
        ('<h2 id="hopin">HopIn</h2>\n<p>a</p >\n', 2),
    ],
)
def test_a_text_that_is_not_in_canonical_form_is_refused_at_the_first_differing_line(text, line):
    """L14."""
    assert refusal(text) == f"legal: {PATH}: line {line}: {CANONICAL_RULE}"


@pytest.mark.parametrize(
    "written", ["a&nbsp;b", "&auml;", "&quot;a&quot;", "a&copy;", "a&#160;b", "a&#x41;b"]
)
def test_a_character_reference_is_refused_with_the_remedy(written):
    """L14: the line says to write the character, and never quotes the text."""
    message = refusal(in_paragraph(written))

    assert message == f"legal: {PATH}: line 2: {CANONICAL_RULE}; {CHARACTER_HINT}"
    assert written not in message


@pytest.mark.parametrize("ending", ["", "\n", "\n\n\n", "\n \n"])
def test_whitespace_after_the_last_closing_tag_is_free(ending):
    """D1."""
    text = f'<h2 id="hopin">HopIn</h2>\n<p>a</p>{ending}'

    assert fragment(text).headings[0].content == f"\n<p>a</p>{ending}"


@pytest.mark.parametrize(
    "between",
    ["", "\n", " ", "\n  \n"],
)
def test_spaces_and_line_feeds_between_block_tags_are_free_and_handed_on_as_written(between):
    """D1: the section keeps the bytes the app wrote."""
    text = f'<h2 id="hopin">HopIn</h2>\n<ul>{between}<li>a</li>{between}</ul>\n'

    assert fragment(text).headings[0].content == f"\n<ul>{between}<li>a</li>{between}</ul>\n"


@pytest.mark.parametrize("between", ["\t", "\u00a0", "\r"])
def test_a_tab_a_no_break_space_or_a_cr_between_block_tags_is_refused(between):
    """D1."""
    text = f'<h2 id="hopin">HopIn</h2>\n<ul>{between}<li>a</li></ul>\n'

    assert "legal: " in refusal(text)


def test_the_three_character_references_are_the_canonical_way_to_write_them():
    """L14."""
    parsed = fragment(in_paragraph("a &amp; b &lt; c &gt; d"))

    assert parsed.headings[0].content == "\n<p>a &amp; b &lt; c &gt; d</p>\n"


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (b"\xef\xbb\xbf" + GOOD.encode(), "starts with a byte order mark"),
        (GOOD.replace("\n", "\r\n").encode(), "control character or a CR"),
        (GOOD.replace("Intro", "In\tro").encode(), "control character or a CR"),
        (GOOD.replace("Intro", "In\x00ro").encode(), "control character or a CR"),
        (b'<h2 id="hopin">H\xff</h2>\n<p>a</p>\n', "is not UTF-8"),
    ],
)
def test_an_encoding_or_line_end_the_contract_does_not_allow_is_refused(data, rule):
    """L14."""
    assert rule in refusal(data)


def test_a_section_over_64_kib_is_refused_with_its_size():
    """L11."""
    text = in_paragraph("x" * MAXIMUM_BYTES)

    message = refusal(text)

    assert f"is {len(text.encode())} bytes, over the {MAXIMUM_BYTES} byte limit" in message


def test_a_section_of_exactly_64_kib_is_accepted():
    """L11."""
    shell = len(in_paragraph("").encode())

    assert fragment(in_paragraph("x" * (MAXIMUM_BYTES - shell)))


@pytest.mark.parametrize(
    "text",
    [
        '<h2 id="other">HopIn</h2>\n<p>a</p>\n',
        '<h2 id="HopIn">HopIn</h2>\n<p>a</p>\n',
        GOOD.replace('id="hopin-daten"', 'id="daten"'),
        GOOD.replace('id="hopin-daten"', 'id="hopin-Daten"'),
        GOOD.replace('id="hopin-daten"', 'id="hopin-"'),
        GOOD.replace('id="hopin-daten"', 'id="hopin-titel"'),
        GOOD.replace('id="hopin-daten"', 'id="hopin-daten-titel"'),
        GOOD.replace('id="hopin-daten"', 'id="other-daten"'),
    ],
)
def test_ids_follow_the_app_key(text):
    """L6."""
    assert "id must be" in refusal(text) or "may not end in titel" in refusal(text)


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "http://example.org/",
        "//example.org/",
        "https://example.org:8443/",
        "https://user@example.org/",
        "https://example.org/a#b",
        "https://example.org/a b",
        "https://Example.org/",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://1.2.3.4/",
        "mailto:a@example.org?subject=x",
        "mailto:a@localhost",
        "ftp://example.org/",
        "/other",
        "datenschutz",
        f"https://{ZONE}/datenschutz",
        f"https://www.{ZONE}/impressum",
        f"https://{ZONE}/nutzungsbedingungen",
        "#",
        "#Big",
    ],
)
def test_a_link_beyond_https_mailto_and_the_three_pages_is_refused(href):
    """L7."""
    assert "link is not allowed" in refusal(in_paragraph(f'<a href="{href}">x</a>'))


@pytest.mark.parametrize(
    "href",
    [
        "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:32023D1795",
        "https://example.org",
        f"https://{ZONE}/",
        "mailto:name@example.org",
        "mailto:a.b+c@sub.example.org",
        "/impressum",
        "/datenschutz#anmeldung",
        "#hopin-daten",
    ],
)
def test_a_link_the_contract_allows_is_accepted(href):
    """L7."""
    assert fragment(in_paragraph(f'<a href="{href}">x</a>'))


def test_a_link_to_an_id_is_reported_with_its_page_and_line():
    """L13."""
    text = in_paragraph(
        '<a href="#hopin-daten">a</a> <a href="/datenschutz#anmeldung">b</a> '
        '<a href="/impressum">c</a>'
    )

    assert fragment(text).links == (
        Link(2, None, "hopin-daten"),
        Link(2, "datenschutz", "anmeldung"),
    )


def test_a_refusal_never_quotes_the_text():
    """L11."""
    marker = "geheimerwortlaut"

    for text in (
        in_paragraph(f'<img alt="{marker}">'),
        in_paragraph(f'<a href="javascript:{marker}">x</a>'),
        in_paragraph(f"&{marker};"),
        f'<h2 id="{marker}">HopIn</h2>\n<p>a</p>\n',
        in_paragraph(f"<p class={marker}>"),
    ):
        assert marker not in refusal(text)


GENERAL = (
    '<h2 id="verantwortlicher">Verantwortlicher</h2>\n'
    "<p>Felix<br>Erlangen</p>\n"
    '<h2 id="rechte">Ihre Rechte</h2>\n'
    "<p>Sie haben Rechte:</p>\n"
    "<ul><li>Auskunft</li></ul>\n"
)


def general(text: str):
    return parse_general("legal/sections/allgemein/datenschutz.html", text.encode(), ZONE)


def test_the_general_part_reads_several_headings_in_the_files_order():
    """L14."""
    parsed = general(GENERAL)

    assert [heading.id for heading in parsed.headings] == ["verantwortlicher", "rechte"]
    assert [heading.title for heading in parsed.headings] == ["Verantwortlicher", "Ihre Rechte"]
    assert parsed.headings[0].content == "\n<p>Felix<br>Erlangen</p>\n"
    assert parsed.headings[1].content == "\n<p>Sie haben Rechte:</p>\n<ul><li>Auskunft</li></ul>\n"
    assert parsed.subsections == ()


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("", "has no <h2>"),
        ('<h2 id="a">A</h2>\n', "<h2> needs a paragraph or list"),
        ('<h2 id="a">A</h2>\n<h2 id="b">B</h2>\n<p>b</p>\n', "<h2> needs a paragraph or list"),
        ('<h2 id="Big">A</h2>\n<p>a</p>\n', "the h2 id is not lowercase words"),
        ('<h2 id="a_b">A</h2>\n<p>a</p>\n', "the h2 id is not lowercase words"),
        ('<h2 id="a">A</h2>\n<section id="a-b">\n<h3>B</h3>\n<p>b</p>\n</section>\n', "<section>"),
        ('<h2 id="a">A</h2>\n<h3>x</h3>\n', "<h3> is not allowed here"),
        ('<h2 id="a">A</h2>\n<p>a</p>\n<img src="x">\n', "<img> is not allowed"),
        ('<h2 id="a">A</h2>\n<p>a<br/></p>\n', CANONICAL_RULE),
    ],
)
def test_the_general_part_is_refused_by_its_own_grammar(text, rule):
    """L7."""
    with pytest.raises(LegalError) as refused:
        general(text)

    assert rule in str(refused.value)


def test_a_heading_title_keeps_its_escaped_bytes_so_no_markup_can_come_of_it():
    """L7, L14."""
    text = (
        '<h2 id="hopin">A &lt;img src=x&gt; &amp; B</h2>\n<p>a</p>\n'
        '<section id="hopin-daten">\n<h3>T &lt;script&gt;</h3>\n<p>x</p>\n</section>\n'
    )

    parsed = fragment(text)

    assert parsed.headings[0].title == "A &lt;img src=x&gt; &amp; B"
    assert parsed.subsections[0].title == "T &lt;script&gt;"
