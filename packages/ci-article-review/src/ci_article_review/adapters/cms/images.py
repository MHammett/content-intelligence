"""Images in a draft: what an author can point at, and what must be true of it.

``blocks.py`` turns Markdown into block markup and ``wordpress.py`` talks to the
REST API. This is the part that is neither: the vocabulary the two share, and
the checks on the local files an author names, all of which can be made before
a single request is sent.

An image's source is one of three things. A URL (``http://`` or ``https://``)
is already hosted, and is never uploaded. Anything without a scheme is a path
on the author's disk, relative to the handoff file or absolute, and is what the
publish uploads. Any other scheme (``data:``, ``file:``, ``ftp:``) is refused
by name rather than guessed at.

A Windows drive letter is the one trap in that split: ``urlsplit`` reads the
``C`` of ``C:/photos/a.png`` as a URL scheme, so a path on the author's own
disk would be classed as an unsupported protocol without the check below.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

#: Where a source points. ``EMPTY`` is ``![alt]()``.
URL = "url"
LOCAL = "local"
UNSUPPORTED = "unsupported"
EMPTY = "empty"

#: Where an image sits in the draft.
#:
#: ``BLOCK``: alone in its paragraph, so it becomes an image block.
#: ``INLINE``: inside a sentence, list, quote, table or link, where it cannot.
#: ``UNPARSED``: the draft's HTML could not be parsed, so nothing was split
#: into blocks at all and every image is in the same position.
BLOCK = "block"
INLINE = "inline"
UNPARSED = "unparsed"

#: What a WordPress media library takes for an Image block, and the type each
#: extension is uploaded as. Written out, not read from ``mimetypes``: that
#: module asks the Windows registry, which has no entry for a type nobody
#: installed a viewer for, so ``.webp`` or ``.avif`` could come back as ``None``
#: on one machine and not another.
#:
#: SVG is here although stock WordPress refuses it. A site with an SVG plugin
#: takes it, and the server is the authority on what it accepts: a refusal
#: comes back as an error naming the image, which is better than a client-side
#: rule deciding for a site it cannot see.
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".svg": "image/svg+xml",
}

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:[\\/]")


class ImageError(ValueError):
    """An image in the draft cannot be published, with the reason.

    A ``ValueError`` so an escape from ``run_publish_pipeline`` takes the same
    logged ``sys.exit(1)`` a bad handoff already does.
    """


@dataclass(frozen=True)
class ImageRef:
    """One image the draft contains, as the Markdown parser read it."""

    src: str
    alt: str = ""
    #: The Markdown title, ``![alt](src "caption")``. Empty when there is none.
    caption: str = ""
    placement: str = BLOCK
    #: Why ``placement`` is what it is, when that needs saying (the parse error
    #: behind ``UNPARSED``).
    note: str = ""

    @property
    def kind(self):
        return source_kind(self.src)


@dataclass(frozen=True)
class UploadedImage:
    """Where an image now lives: an attachment in the media library."""

    attachment_id: int
    url: str
    #: The size ``url`` is: ``large`` when WordPress made one, else ``full``.
    size_slug: str


def source_kind(src):
    """Classify an image source as ``URL``, ``LOCAL``, ``UNSUPPORTED`` or ``EMPTY``."""
    src = (src or "").strip()
    if not src:
        return EMPTY
    if _DRIVE_LETTER.match(src):
        return LOCAL
    try:
        scheme = urlsplit(src).scheme.lower()
    except ValueError:  # an unclosed IPv6 bracket, say
        return UNSUPPORTED
    if scheme in ("http", "https"):
        return URL
    if scheme:
        return UNSUPPORTED
    # ``//host/path``, which a browser resolves against the page's own scheme.
    # An author's forward-slash UNC path looks identical; a URL is the reading
    # that leaves what they typed unchanged.
    if src.startswith("//"):
        return URL
    return LOCAL


def shorten(text, limit=80):
    """Keep a long source (a base64 ``data:`` URI) from swamping a message."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe(ref, n=None, total=None):
    """``Image 2 of 3, "photos/grid.png"``: which image a message is about."""
    src = shorten(ref.src.strip()) or "(no source)"
    where = f"Image {n} of {total}, " if n else "Image "
    return f'{where}"{src}"'


def local_candidates(src, base_dir=None):
    """The paths a local source could mean, most literal first.

    Percent-decoded second: ``my%20photo.png`` is how CommonMark (and the
    preview in most Markdown editors) needs a space written, so an author who
    previews the draft will have written it that way, but a file genuinely
    named ``100%25.png`` should still be found by its literal name.
    """
    base = Path(base_dir) if base_dir else Path.cwd()
    stripped = src.strip()
    paths = []
    for raw in dict.fromkeys((stripped, unquote(stripped))):
        path = Path(raw)
        paths.append(path if path.is_absolute() else base / path)
    return paths


def _is_file(path):
    try:
        return path.is_file()
    except (OSError, ValueError):  # an illegal Windows name, an embedded NUL
        return False


def _is_dir(path):
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def resolve_local_image(src, base_dir=None):
    """The file a local source names, or an ``ImageError`` saying why it cannot be used.

    Everything checked here is checked without the network, so a mistyped path
    fails before the first request rather than after an earlier image has
    already been uploaded. The reason is a clause, not a sentence, because the
    caller puts the image's name in front of it.
    """
    candidates = local_candidates(src, base_dir)
    found = next((p for p in candidates if _is_file(p)), None)
    if found is None:
        folder = next((p for p in candidates if _is_dir(p)), None)
        if folder is not None:
            raise ImageError(f"the path is a folder, not an image file: {folder}")
        looked = "\n        ".join(str(p) for p in candidates)
        raise ImageError(f"file not found. Looked for:\n        {looked}")

    ext = found.suffix.lower()
    if ext not in IMAGE_TYPES:
        kind = f"a {ext} file" if ext else "a file with no extension"
        raise ImageError(
            f"{found.name} is {kind}, not an image type WordPress can show in "
            f"an Image block. Supported: {', '.join(sorted(IMAGE_TYPES))}."
        )

    try:
        size = found.stat().st_size
        with found.open("rb") as fh:
            fh.read(1)  # os.access() is unreliable on Windows; opening is not
    except OSError as e:
        raise ImageError(f"the file cannot be read: {e.strerror or e}") from e
    if size == 0:
        raise ImageError("the file is empty (0 bytes)")
    # Normalised (``..`` folded away), because the publish lists this path before
    # the author says yes, and ``handoff/../shared/a.png`` hides which file it is.
    return Path(os.path.abspath(found))


def mime_for(path):
    """The MIME type ``path`` is uploaded as, from its extension."""
    return IMAGE_TYPES[Path(path).suffix.lower()]
