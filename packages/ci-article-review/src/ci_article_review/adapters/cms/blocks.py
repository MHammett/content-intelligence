"""Markdown to WordPress block markup.

Drafts in this pipeline are Markdown all the way through: the review reads
Markdown, the handoff's FINAL DRAFT holds Markdown. WordPress stores post
content as HTML, and the block editor stores it as HTML wrapped in
``<!-- wp:* -->`` delimiters. Sending Markdown straight through is what the
adapter used to do, and it published a page whose headings were literal ``##``
characters and whose links were literal bracket syntax — a successful publish
of unusable content.

Two libraries were considered. ``markdown`` (Python-Markdown) does the part
that must not be hand-written: parsing Markdown. ``beautifulsoup4`` was
rejected — Python-Markdown's default output format is XHTML, so the generated
fragment is well-formed XML and the stdlib's ElementTree can walk it, which
keeps this to one new dependency for the one job that needs a library.

Content that already carries block delimiters is passed through untouched, so
a handoff written against the old behaviour (or by hand, in blocks) is not
re-processed.
"""

import logging
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

#: Present in any content the block editor has already serialised.
BLOCK_MARKER = "<!-- wp:"

#: Markdown extensions: ``extra`` brings tables, fenced code, and attribute
#: lists; ``sane_lists`` stops a list from swallowing the paragraph beneath it.
_MD_EXTENSIONS = ("extra", "sane_lists")

#: Element name -> (block name, extra JSON attributes). Anything absent falls
#: back to a raw ``wp:html`` block, which renders correctly and stays editable
#: as HTML — a worse editing experience than a native block, but never a
#: dropped or mangled element.
_SIMPLE_BLOCKS = {
    "p": ("paragraph", ""),
    "blockquote": ("quote", ""),
    "pre": ("code", ""),
    "hr": ("separator", ""),
    "table": ("table", ""),
    "figure": ("image", ""),
}

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def looks_like_block_markup(text):
    """True if the content is already WordPress block markup."""
    return BLOCK_MARKER in (text or "")


def _element_html(el):
    """Serialise one element, without the trailing text that follows it."""
    tail, el.tail = el.tail, None
    try:
        return ET.tostring(el, encoding="unicode", method="html").strip()
    finally:
        el.tail = tail


def _inner_html(el):
    """Serialise an element's children and text, but not its own tags."""
    parts = [el.text or ""]
    parts.extend(ET.tostring(c, encoding="unicode", method="html") for c in el)
    return "".join(parts).strip()


def _wrap(name, inner, attrs=""):
    return f"<!-- wp:{name}{attrs} -->\n{inner}\n<!-- /wp:{name} -->"


def _list_block(el):
    items = "".join(
        _wrap("list-item", f"<li>{_inner_html(li)}</li>") + "\n"
        for li in el.findall("li")
    )
    ordered = el.tag == "ol"
    attrs = ' {"ordered":true}' if ordered else ""
    tag = "ol" if ordered else "ul"
    return _wrap("list", f'<{tag} class="wp-block-list">\n{items}</{tag}>', attrs)


def _block_for(el):
    if el.tag in _HEADINGS:
        level = _HEADINGS[el.tag]
        # The block editor's heading block defaults to level 2 and only stores
        # the attribute when it differs.
        attrs = "" if level == 2 else f' {{"level":{level}}}'
        return _wrap("heading", _element_html(el), attrs)
    if el.tag in ("ul", "ol"):
        return _list_block(el)
    if el.tag in _SIMPLE_BLOCKS:
        name, attrs = _SIMPLE_BLOCKS[el.tag]
        return _wrap(name, _element_html(el), attrs)
    return _wrap("html", _element_html(el))


def to_blocks(markdown_text, strip_leading_h1=True):
    """Convert Markdown to WordPress block markup.

    ``strip_leading_h1`` drops a leading ``# Title`` line. WordPress renders the
    post title itself, so keeping it publishes the title twice — once as the
    theme's heading and once as an H1 in the body.

    Content that is already block markup is returned unchanged. If the
    generated HTML cannot be parsed (a raw named entity in the source, say),
    the whole document is emitted as a single ``wp:html`` block rather than
    failing the publish: one un-split block still renders correctly, and the
    warning says why it happened.
    """
    text = (markdown_text or "").strip()
    if not text:
        return ""
    if looks_like_block_markup(text):
        log.debug("Content is already block markup; passing it through unchanged.")
        return text

    try:
        import markdown as _markdown
    except ImportError:  # pragma: no cover - dependency is declared
        log.warning(
            "The 'markdown' package is not installed, so the draft is being "
            "published as-is. Markdown headings and links will appear as "
            "literal characters. Install ci-article-review's dependencies."
        )
        return text

    if strip_leading_h1:
        lines = text.splitlines()
        if lines and lines[0].startswith("# "):
            text = "\n".join(lines[1:]).strip()

    html = _markdown.markdown(text, extensions=list(_MD_EXTENSIONS))

    try:
        root = ET.fromstring(f"<root>{html}</root>")
    except ET.ParseError as e:
        log.warning(
            "Converted HTML could not be parsed (%s), so the whole document is "
            "being published as one wp:html block. It renders correctly but is "
            "not split into editable blocks.",
            e,
        )
        return _wrap("html", html)

    blocks = [_block_for(el) for el in root]
    # Text sitting directly under <root> means Markdown emitted a bare string
    # with no block parent; keep it rather than dropping it silently.
    if (root.text or "").strip():
        blocks.insert(0, _wrap("paragraph", f"<p>{root.text.strip()}</p>"))
    return "\n\n".join(b for b in blocks if b)
