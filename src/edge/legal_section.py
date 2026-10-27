"""The validator of a legal section: an allow-list, never a sanitiser (docs/architecture.md §3).

A section is one text a unit ships for a legal page. `html.parser` only tokenises: every token is
checked against the grammar, and the validated tokens are written out again in canonical form. A
file that differs from that form is refused, so its bytes are published unchanged or not at all
and no parser can read a file other than the way this module did. Every refusal is one line that
names the file and the line, and never quotes the text.
"""

import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

MAXIMUM_BYTES = 64 * 1024
PAGES = ("impressum", "datenschutz", "nutzungsbedingungen")
ELEMENTS = ("h2", "h3", "section", "p", "ul", "ol", "li", "strong", "em", "code", "br", "a")
CANONICAL_RULE = (
    "not in canonical form (lowercase tags, double quotes, <br>, only &amp; &lt; &gt;, LF)"
)

_ATTRIBUTE_OF = {"h2": "id", "section": "id", "a": "href"}
_BLOCKS = ("p", "ul", "ol")
_MARKS = ("strong", "em", "code", "a")
_WHITESPACE = frozenset(" \n")
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_LINKED_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_LABEL = "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(\.{_LABEL})+")
_PATH_CHARACTERS = r"A-Za-z0-9\-._~/%:@!$'()*+,;="
_HTTPS = re.compile(
    rf"https://(?P<host>[^/?#]+)(?P<path>/[{_PATH_CHARACTERS}]*)?(\?[{_PATH_CHARACTERS}?&]*)?"
)
_MAILTO = re.compile(r"mailto:[A-Za-z0-9._%+-]+@(?P<host>.+)")
_PAGE_LINK = re.compile(rf"/(?P<page>{'|'.join(PAGES)})(#(?P<id>{_LINKED_ID.pattern}))?")
_SAME_PAGE_LINK = re.compile(rf"#(?P<id>{_LINKED_ID.pattern})")


class LegalError(Exception):
    """A legal section or frame is refused, or a page cannot be built; one line."""


@dataclass(frozen=True)
class Link:
    """A link to an id: of the same page (`page` is None) or of one of the three pages."""

    line: int
    page: str | None
    fragment: str


@dataclass(frozen=True)
class Heading:
    id: str
    title: str
    content: str  # the file's bytes after the heading, up to the next heading or section
    line: int


@dataclass(frozen=True)
class Parsed:
    headings: tuple[Heading, ...]  # a fragment's one h2, or the general part's h2s in file order
    subsections: tuple[Heading, ...]  # a fragment's sections, by their h3
    links: tuple[Link, ...]


@dataclass
class _Token:
    kind: str  # "start", "end" or "data"
    name: str  # the tag, or the text of a data token
    value: str | None  # the one allowed attribute's value
    line: int
    start: int
    end: int


class _Refused(Exception):
    def __init__(self, line: int, rule: str):
        super().__init__(rule)
        self.line = line
        self.rule = rule


class _Tokeniser(HTMLParser):
    """The tokens of a text, each with its line and its offsets in the canonical form."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []
        self.canonical = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        if tag not in ELEMENTS:
            raise _Refused(line, f"<{tag}> is not allowed (allowed: {' '.join(ELEMENTS)})")
        value = self._attribute(tag, attrs, line)
        written = (
            f"<{tag}"
            + (f' {_ATTRIBUTE_OF[tag]}="{_escape(value)}"' if value is not None else "")
            + ">"
        )
        self._add("start", tag, value, line, written)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        raise _Refused(
            self.getpos()[0], CANONICAL_RULE if tag == "br" else f"<{tag}/> is not allowed"
        )

    def handle_endtag(self, tag: str) -> None:
        line = self.getpos()[0]
        if tag not in ELEMENTS:
            raise _Refused(line, f"</{tag}> is not allowed (allowed: {' '.join(ELEMENTS)})")
        self._add("end", tag, None, line, f"</{tag}>")

    def handle_data(self, data: str) -> None:
        written = _escape(data)
        if self.tokens and self.tokens[-1].kind == "data":
            self.tokens[-1].name += data
            self.tokens[-1].end += len(written)
            self.canonical += written
        else:
            self._add("data", data, None, self.getpos()[0], written)

    def handle_comment(self, data: str) -> None:
        self._refuse_markup()

    def handle_decl(self, decl: str) -> None:
        self._refuse_markup()

    def handle_pi(self, data: str) -> None:
        self._refuse_markup()

    def unknown_decl(self, data: str) -> None:
        self._refuse_markup()

    def _refuse_markup(self) -> None:
        raise _Refused(
            self.getpos()[0], "comments, doctype, CDATA and processing instructions are not allowed"
        )

    def _attribute(self, tag: str, attrs: list[tuple[str, str | None]], line: int) -> str | None:
        names = [name for name, _ in attrs]
        for name in names:
            if names.count(name) > 1:
                raise _Refused(line, f"<{tag}> repeats the attribute {name}")
            if name != _ATTRIBUTE_OF.get(tag):
                raise _Refused(line, f"attribute {name} is not allowed on <{tag}>")
        if tag in _ATTRIBUTE_OF and not attrs:
            raise _Refused(line, f"<{tag}> needs the attribute {_ATTRIBUTE_OF[tag]}")
        if attrs and attrs[0][1] is None:
            raise _Refused(line, f"attribute {attrs[0][0]} has no value")
        return attrs[0][1] if attrs else None

    def _add(self, kind: str, name: str, value: str | None, line: int, written: str) -> None:
        start = len(self.canonical)
        self.canonical += written
        self.tokens.append(_Token(kind, name, value, line, start, len(self.canonical)))


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def refusal(path: str, line: int | None, rule: str) -> LegalError:
    return LegalError(f"{path}: line {line}: {rule}" if line else f"{path}: {rule}")


def _tokens(path: str, data: bytes) -> tuple[str, list[_Token]]:
    """The text and its tokens once its encoding, size and canonical form are right."""
    if len(data) > MAXIMUM_BYTES:
        raise refusal(path, None, f"is {len(data)} bytes, over the {MAXIMUM_BYTES} byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise refusal(path, data[: error.start].count(b"\n") + 1, "is not UTF-8") from error
    if text.startswith("﻿"):
        raise refusal(path, 1, "starts with a byte order mark")
    for number, line in enumerate(text.split("\n"), start=1):
        if any(unicodedata.category(character) == "Cc" for character in line):
            raise refusal(path, number, "has a control character or a CR (only LF may end a line)")
    tokeniser = _Tokeniser()
    try:
        tokeniser.feed(text)
        tokeniser.close()
    except _Refused as refused:
        raise refusal(path, refused.line, refused.rule) from refused
    if tokeniser.canonical != text:
        raise refusal(path, _first_difference(text, tokeniser.canonical), CANONICAL_RULE)
    return text, tokeniser.tokens


def _first_difference(text: str, canonical: str) -> int:
    for number, (written, expected) in enumerate(
        zip(text.split("\n"), canonical.split("\n"), strict=False), start=1
    ):
        if written != expected:
            return number
    return min(text.count("\n"), canonical.count("\n")) + 1


class _Reader:
    """The grammar of a section over its tokens."""

    def __init__(self, path: str, text: str, tokens: list[_Token], zone: str):
        self.path = path
        self.text = text
        self.tokens = tokens
        self.zone = zone
        self.position = 0
        self.links: list[Link] = []

    def refuse(self, line: int, rule: str) -> LegalError:
        return refusal(self.path, line, rule)

    def peek(self) -> _Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def next(self, unclosed: _Token) -> _Token:
        if (token := self.peek()) is None:
            raise self.refuse(unclosed.line, f"<{unclosed.name}> is never closed")
        self.position += 1
        return token

    def skip_whitespace(self) -> None:
        while (token := self.peek()) and token.kind == "data" and set(token.name) <= _WHITESPACE:
            self.position += 1

    def opens(self, *names: str) -> bool:
        token = self.peek()
        return token is not None and token.kind == "start" and token.name in names

    def unexpected(self, token: _Token | None, expected: str) -> LegalError:
        if token is None:
            return self.refuse(len(self.text.split("\n")), f"ends too early ({expected})")
        if token.kind == "data":
            return self.refuse(
                token.line, "text outside a paragraph, list item or heading is not allowed"
            )
        slash = "/" if token.kind == "end" else ""
        return self.refuse(token.line, f"<{slash}{token.name}> is not allowed here ({expected})")

    def read_tag(self, name: str, expected: str) -> _Token:
        token = self.peek()
        if token is None or token.kind != "start" or token.name != name:
            raise self.unexpected(token, expected)
        self.position += 1
        return token

    def heading(self, name: str) -> tuple[_Token, str, _Token]:
        """The heading element at the current token: its opening tag, its text, its closing tag."""
        opening = self.read_tag(name, f"<{name}> is expected")
        text = self.next(opening)
        if text.kind != "data" or not text.name.strip():
            raise self.refuse(opening.line, f"<{name}> needs its text")
        closing = self.next(opening)
        if closing.kind != "end" or closing.name != name:
            raise self.unexpected(closing, f"</{name}> is expected")
        return opening, text.name, closing

    def blocks(self) -> int:
        count = 0
        while (opening := self.peek()) and opening.kind == "start" and opening.name in _BLOCKS:
            self.position += 1
            if opening.name == "p":
                self.inline(opening)
            else:
                self.read_list(opening)
            self.skip_whitespace()
            count += 1
        return count

    def read_list(self, opening: _Token) -> None:
        self.skip_whitespace()
        items = 0
        while self.opens("li"):
            item = self.read_tag("li", "")
            self.inline(item)
            self.skip_whitespace()
            items += 1
        if not items:
            raise self.refuse(opening.line, f"<{opening.name}> needs an <li>")
        closing = self.next(opening)
        if closing.kind != "end" or closing.name != opening.name:
            raise self.unexpected(closing, f"<li> or </{opening.name}> is expected")

    def inline(self, container: _Token) -> None:
        """The text and marks of a paragraph or list item, up to its closing tag."""
        has_text = False
        while True:
            token = self.next(container)
            if token.kind == "data":
                has_text = has_text or bool(token.name.strip())
            elif token.kind == "start" and token.name == "br":
                continue
            elif token.kind == "start" and token.name in _MARKS:
                self.mark(token)
                has_text = True
            elif token.kind == "end" and token.name == container.name:
                break
            else:
                raise self.unexpected(
                    token, f"<{container.name}> holds text, <br> and <{'>, <'.join(_MARKS)}> only"
                )
        if not has_text:
            raise self.refuse(container.line, f"<{container.name}> is empty")

    def mark(self, opening: _Token) -> None:
        text = self.next(opening)
        if text.kind != "data" or not text.name.strip():
            raise self.refuse(opening.line, f"<{opening.name}> holds plain text only")
        closing = self.next(opening)
        if closing.kind != "end" or closing.name != opening.name:
            raise self.refuse(opening.line, f"<{opening.name}> holds plain text only")
        if opening.name == "a":
            self.link(opening)

    def link(self, opening: _Token) -> None:
        href = opening.value or ""
        if match := _SAME_PAGE_LINK.fullmatch(href):
            self.links.append(Link(opening.line, None, match["id"]))
        elif match := _PAGE_LINK.fullmatch(href):
            if match["id"]:
                self.links.append(Link(opening.line, match["page"], match["id"]))
        elif not self.is_external(href):
            raise self.refuse(
                opening.line,
                "link is not allowed (https, mailto, #id or one of the three pages)",
            )

    def is_external(self, href: str) -> bool:
        if match := _HTTPS.fullmatch(href):
            host = match["host"]
            own_page = (match["path"] or "").removeprefix("/").split("/")[0] in PAGES
            if host in (self.zone, f"www.{self.zone}") and own_page:
                return False  # the short form of a link to one's own pages
            return _is_host(host)
        match = _MAILTO.fullmatch(href)
        return bool(match) and _is_host(match["host"])


def _is_host(host: str) -> bool:
    """Two or more lowercase labels, none a number at the end (no IP address)."""
    return bool(_HOST.fullmatch(host)) and not host.rsplit(".", 1)[1].isdigit()


def _end_of(reader: _Reader) -> None:
    reader.skip_whitespace()
    if (token := reader.peek()) is not None:
        raise reader.unexpected(token, "nothing may follow the last closing tag")


def parse_general(path: str, data: bytes, zone: str) -> Parsed:
    """The general part: several `h2`, each with at least one block, in the file's order."""
    text, tokens = _tokens(path, data)
    reader = _Reader(path, text, tokens, zone)
    headings: list[Heading] = []
    reader.skip_whitespace()
    while reader.opens("h2"):
        opening, title, closing = reader.heading("h2")
        if not _SLUG.fullmatch(opening.value or ""):
            raise reader.refuse(opening.line, "the h2 id is not lowercase words joined by hyphens")
        reader.skip_whitespace()
        if not reader.blocks():
            raise reader.unexpected(reader.peek(), "<h2> needs a paragraph or list")
        following = reader.peek()
        stop = following.start if following else len(text)
        headings.append(Heading(opening.value or "", title, text[closing.end : stop], opening.line))
    if not headings:
        raise reader.refuse(1, "has no <h2>")
    _end_of(reader)
    return Parsed(tuple(headings), (), tuple(reader.links))


def parse_fragment(path: str, data: bytes, anchor: str, zone: str) -> Parsed:
    """A block of one unit: an `h2` with the unit's anchor, an intro and subsections."""
    text, tokens = _tokens(path, data)
    reader = _Reader(path, text, tokens, zone)
    reader.skip_whitespace()
    opening, title, closing = reader.heading("h2")
    if opening.value != anchor:
        raise reader.refuse(opening.line, f'the h2 id must be "{anchor}"')
    reader.skip_whitespace()
    reader.blocks()
    following = reader.peek()
    stop = following.start if following else len(text)
    heading = Heading(anchor, title, text[closing.end : stop], opening.line)
    subsections: list[Heading] = []
    while reader.opens("section"):
        subsections.append(_subsection(reader, anchor, text))
        reader.skip_whitespace()
    _end_of(reader)
    return Parsed((heading,), tuple(subsections), tuple(reader.links))


def _subsection(reader: _Reader, anchor: str, text: str) -> Heading:
    opening = reader.read_tag("section", "")
    identifier = opening.value or ""
    slug = identifier.removeprefix(f"{anchor}-")
    if identifier == slug or not _SLUG.fullmatch(slug):
        raise reader.refuse(opening.line, f'the section id must be "{anchor}-" and words')
    if slug.split("-")[-1] == "titel":
        raise reader.refuse(opening.line, "the section id may not end in titel")
    reader.skip_whitespace()
    _, title, closing = reader.heading("h3")
    reader.skip_whitespace()
    if not reader.blocks():
        raise reader.unexpected(reader.peek(), "<section> needs a paragraph or list")
    end = reader.next(opening)
    if end.kind != "end" or end.name != "section":
        raise reader.unexpected(end, "</section> is expected")
    return Heading(identifier, title, text[closing.end : end.start], opening.line)
