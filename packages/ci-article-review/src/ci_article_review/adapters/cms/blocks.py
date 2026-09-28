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

Images are found the same way, in Python-Markdown's output rather than with a
pattern of our own, so titles, ``<angle-bracket>`` paths, reference-style
images and escapes all arrive already parsed. An image alone in its paragraph
becomes a ``core/image`` block. Where the image *lives* is not decided here:
putting a local file on the site takes a request this module must not make, so
``to_blocks`` is handed a map of what has already been uploaded, and refuses to
emit a local path it was not given a URL for. That refusal is the point. A
relative ``src`` is a broken image on the live site, and before this a publish
that shipped one, wrapped in a paragraph block, looked exactly like a good one.
"""

import json
import logging
import xml.etree.ElementTree as ET
from html import escape as _escape
from html.parser import HTMLParser

from .images import (
    BLOCK,
    EMPTY,
    INLINE,
    LOCAL,
    UNPARSED,
    UNSUPPORTED,
    ImageError,
    ImageRef,
    describe,
    shorten,
)

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

#: The attributes an image block carries. Anything else on an ``<img>`` (a
#: width from an attribute list, a class from raw HTML) has nowhere to go.
_IMAGE_ATTRS = frozenset({"src", "alt", "title"})


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


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _attr(value):
    """Escape an attribute value the way the block editor does when it saves one."""
    return _escape(str(value), quote=False).replace('"', "&quot;")


def _text(value):
    return _escape(str(value), quote=False)


def image_block(url, alt="", caption="", attachment_id=None, size_slug=None):
    """A ``core/image`` block, serialised the way the block editor saves one.

    The editor validates a block by running its ``save()`` over the attributes
    in the delimiter comment and comparing the result with the stored HTML. A
    block that differs by so much as a class name opens with "This block
    contains unexpected or invalid content", which is the opposite of easy to
    edit by hand. So this follows ``save()``: the ``figure`` carries
    ``wp-block-image`` and ``size-<slug>``, the ``img`` carries
    ``wp-image-<id>``, and a caption is a ``figcaption`` with
    ``wp-element-caption``. ``url``, ``alt`` and ``caption`` are declared by the
    block as HTML-sourced, so they live in the markup and are not repeated in
    the comment, where they would be flagged as a mismatch.

    With an ``attachment_id`` the block points at a media-library item, and the
    editor's image settings (replace, resize, alt text) work against it. Without
    one it points at a bare URL, which is a valid image block too: an image
    hosted elsewhere is not in this site's media library and has no id.
    """
    hosted = attachment_id is not None
    if hosted:
        size_slug = size_slug or "full"
        attrs = {"id": attachment_id, "sizeSlug": size_slug, "linkDestination": "none"}
        figure_class = f"wp-block-image size-{size_slug}"
        img_class = f' class="wp-image-{attachment_id}"'
    else:
        attrs = {}
        figure_class = "wp-block-image"
        img_class = ""
    caption = (caption or "").strip()
    figcaption = (
        f'<figcaption class="wp-element-caption">{_text(caption)}</figcaption>'
        if caption
        else ""
    )
    figure = (
        f'<figure class="{figure_class}">'
        f'<img src="{_attr(url)}" alt="{_attr(alt)}"{img_class}/>'
        f"{figcaption}</figure>"
    )
    delimiter_attrs = f" {json.dumps(attrs, separators=(',', ':'))}" if attrs else ""
    return _wrap("image", figure, delimiter_attrs)


def _image_only_children(el):
    """The ``<img>`` elements of a paragraph holding nothing else, or ``[]``.

    Two images on consecutive lines are one paragraph to Markdown and two
    figures to the person who wrote them, so a paragraph of images with only
    whitespace or line breaks between them counts.
    """
    if el.tag != "p" or (el.text or "").strip():
        return []
    imgs = []
    for child in el:
        if (child.tail or "").strip():
            return []
        if child.tag == "img":
            imgs.append(child)
        elif child.tag != "br":
            return []
    return imgs


def _ref(img, placement, note=""):
    return ImageRef(
        src=img.get("src") or "",
        alt=img.get("alt") or "",
        caption=img.get("title") or "",
        placement=placement,
        note=note,
    )


def _images_in_tree(root):
    """Every ``<img>`` in the tree as ``(ImageRef, element)``, in document order."""
    standalone = {id(img) for el in root for img in _image_only_children(el)}
    return [
        (_ref(img, BLOCK if id(img) in standalone else INLINE), img)
        for img in root.iter("img")
    ]


class _ImgScanner(HTMLParser):
    """Finds ``<img>`` tags in HTML too broken for ElementTree to parse."""

    def __init__(self, note):
        super().__init__(convert_charrefs=True)
        self.refs = []
        self._note = note

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            a = dict(attrs)
            self.refs.append(
                ImageRef(
                    src=a.get("src") or "",
                    alt=a.get("alt") or "",
                    caption=a.get("title") or "",
                    placement=UNPARSED,
                    note=self._note,
                )
            )


def _scan_html(html, note):
    scanner = _ImgScanner(note)
    scanner.feed(html)
    scanner.close()
    return scanner.refs


def _drop_leading_h1(text):
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:]).strip()
    return text


def _render(markdown, text):
    """Markdown to ``(html, root, error)``. ``root`` is None if the HTML is not XML."""
    html = markdown.markdown(text, extensions=list(_MD_EXTENSIONS))
    try:
        return html, ET.fromstring(f"<root>{html}</root>"), None
    except ET.ParseError as e:
        return html, None, e


def _markdown_module():
    try:
        import markdown
    except ImportError:  # pragma: no cover - dependency is declared
        return None
    return markdown


def find_images(markdown_text, strip_leading_h1=True):
    """Every image in a draft, in document order, without converting anything.

    The publish checks these before it sends a request or asks for a yes, so an
    image that cannot be published stops it early. It reads the same rendered
    HTML ``to_blocks`` does, so the two cannot disagree about what is an image.
    """
    text = (markdown_text or "").strip()
    if not text or looks_like_block_markup(text):
        return []
    markdown = _markdown_module()
    if markdown is None:  # pragma: no cover - dependency is declared
        return []
    if strip_leading_h1:
        text = _drop_leading_h1(text)
    html, root, error = _render(markdown, text)
    if root is None:
        return _scan_html(html, str(error))
    return [ref for ref, _ in _images_in_tree(root)]


def image_problems(refs, resolved=None):
    """Why each image cannot be published, as ``{index into refs: reason}``.

    Only what is wrong with the image as written. Whether its file exists is
    the caller's to check (``images.resolve_local_image``), and so is whether it
    has been uploaded: pass ``resolved`` to include that, as ``to_blocks`` does,
    and leave it off to ask only about the draft, as the publish's early check
    does.

    A local path is the case that must not slip through, wherever it sits: it
    is a broken image once published. A URL the converter cannot turn into a
    block (one inside a sentence) still renders, as it always has, and is left
    alone.
    """
    problems = {}
    for i, ref in enumerate(refs):
        kind = ref.kind
        if kind == LOCAL and ref.placement == INLINE:
            problems[i] = (
                "it sits inside other content (a sentence, list, quote, table "
                "or link), so it cannot become an image block, and a local file "
                "cannot be uploaded from there. Put the image on a line of its "
                "own, with a blank line above and below."
            )
        elif kind == LOCAL and ref.placement == UNPARSED:
            problems[i] = (
                f"the draft's HTML could not be parsed ({ref.note}), so it "
                "cannot be split into blocks and this local file cannot be "
                "uploaded and pointed at. Fix the markup the error names (an "
                "unclosed tag, or a raw named entity such as &nbsp;) and re-run."
            )
        elif ref.placement != BLOCK:
            continue
        elif kind == EMPTY:
            problems[i] = (
                "it has no source. ![alt](...) needs a path to a file, or an "
                "https:// URL, between the parentheses."
            )
        elif kind == UNSUPPORTED:
            problems[i] = (
                f"its source ({shorten(ref.src)}) is not a path to a file or an "
                "http(s) URL, so it cannot be published. Save the image as a "
                "file and point at that."
            )
        elif kind == LOCAL and resolved is not None and ref.src not in resolved:
            problems[i] = (
                "it is a local file that was never uploaded, so there is no URL "
                "for the block to point at."
            )
    return problems


def _raise_for_problems(refs, resolved=None):
    problems = image_problems(refs, resolved)
    if problems:
        raise ImageError(
            "\n".join(
                f"{describe(refs[i], i + 1, len(refs))}: {reason}"
                for i, reason in sorted(problems.items())
            )
        )


def _image_block_for(img, resolved):
    ref = _ref(img, BLOCK)
    dropped = sorted(set(img.attrib) - _IMAGE_ATTRS)
    if dropped:
        log.warning(
            "Image %r has %s, which an image block cannot carry: only the "
            "source, alt text and caption are kept.",
            shorten(ref.src),
            ", ".join(dropped),
        )
    hosted = resolved.get(ref.src)
    if hosted is not None:
        return image_block(
            hosted.url, ref.alt, ref.caption, hosted.attachment_id, hosted.size_slug
        )
    return image_block(ref.src.strip(), ref.alt, ref.caption)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def to_blocks(markdown_text, strip_leading_h1=True, resolved=None):
    """Convert Markdown to WordPress block markup.

    ``strip_leading_h1`` drops a leading ``# Title`` line. WordPress renders the
    post title itself, so keeping it publishes the title twice — once as the
    theme's heading and once as an H1 in the body.

    ``resolved`` maps an image's source, exactly as written in the draft, to the
    ``UploadedImage`` it became. An image with an ``http(s)`` source needs no
    entry. A local one that has none raises ``ImageError`` instead of being
    written out as a path the live site cannot serve.

    Content that is already block markup is returned unchanged. If the
    generated HTML cannot be parsed (a raw named entity in the source, say),
    the whole document is emitted as a single ``wp:html`` block rather than
    failing the publish: one un-split block still renders correctly, and the
    warning says why it happened. Unless the draft names a local image, which
    that single block would carry out as a broken path: then it fails, saying so.
    """
    text = (markdown_text or "").strip()
    if not text:
        return ""
    if looks_like_block_markup(text):
        log.debug("Content is already block markup; passing it through unchanged.")
        return text

    markdown = _markdown_module()
    if markdown is None:  # pragma: no cover - dependency is declared
        log.warning(
            "The 'markdown' package is not installed, so the draft is being "
            "published as-is. Markdown headings and links will appear as "
            "literal characters. Install ci-article-review's dependencies."
        )
        return text

    if strip_leading_h1:
        text = _drop_leading_h1(text)

    html, root, error = _render(markdown, text)
    if root is None:
        _raise_for_problems(_scan_html(html, str(error)))
        log.warning(
            "Converted HTML could not be parsed (%s), so the whole document is "
            "being published as one wp:html block. It renders correctly but is "
            "not split into editable blocks.",
            error,
        )
        return _wrap("html", html)

    resolved = resolved or {}
    _raise_for_problems([ref for ref, _ in _images_in_tree(root)], resolved)

    blocks = []
    for el in root:
        imgs = _image_only_children(el)
        if imgs:
            blocks.extend(_image_block_for(img, resolved) for img in imgs)
        else:
            blocks.append(_block_for(el))
    # Text sitting directly under <root> means Markdown emitted a bare string
    # with no block parent; keep it rather than dropping it silently.
    if (root.text or "").strip():
        blocks.insert(0, _wrap("paragraph", f"<p>{root.text.strip()}</p>"))
    return "\n\n".join(b for b in blocks if b)
