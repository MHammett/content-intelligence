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


# ---------------------------------------------------------------------------
# Metadata
#
# A photo off a phone carries EXIF: the position it was taken at, the device
# make and model, the capture time. A media-library item is public the moment it
# exists, so uploading the file byte for byte publishes all of that before
# anyone has chosen to publish the post (issue #288: six JPEGs from one phone,
# every one of them carrying an 11-tag GPS block plus make, model and software,
# and nothing said so).
#
# Pillow does the stripping. Looked for the solved version before writing one:
#
#   * mat2 is *the* metadata-removal tool and the right answer on a Debian
#     desktop. It wants PyGObject, pycairo, GdkPixbuf and Perl's exiftool, has
#     no Windows install path, and is LGPLv3. This is a pip-installed tool that
#     runs on Windows.
#   * piexif drops the APP1 segment losslessly with no dependency at all, which
#     is the one thing Pillow cannot do. Last release 2019-07-01, 44 open
#     issues, no new PRs accepted; JPEG and WebP only, so a PNG's eXIf chunk and
#     its text chunks are out of reach; and it removes EXIF but not XMP, IPTC or
#     a JPEG comment. It also does nothing about orientation, which is the half
#     of this that is actually hard.
#   * metazap (3 releases, last 2024-09) wraps Pillow and piexif, so it inherits
#     piexif's limits and adds a second unmaintained layer.
#   * PyExifTool drives Phil Harvey's exiftool: a Perl program the user installs
#     separately, which a pip install cannot promise.
#   * picscrub does exactly this job, formats and all, but is not on PyPI.
#
# So Pillow -- the standard image dependency, Windows wheels, and the only one
# of the five that can also put the EXIF orientation into the pixels, which is
# what keeps a stripped phone photo from arriving sideways. The cost is a
# re-encode, and _jpeg_options and _webp_options below are where it is held down.
# ---------------------------------------------------------------------------

#: The types the strip handles. Everything else in ``IMAGE_TYPES`` is uploaded
#: as it is: a TIFF or a HEIC off a phone carries EXIF too, but re-encoding a
#: multi-page TIFF, an animation, or a format Pillow needs a plugin to open at
#: all would risk the image itself to remove metadata that is reported either
#: way. One of those types carrying a location is warned about instead, where
#: the images are listed and before anything is sent.
STRIPPABLE = (".jpg", ".jpeg", ".png", ".webp")

#: EXIF tags that name a person or a device rather than describing the picture.
#: By name, not number, so this reads as what it is and does not depend on which
#: ``ExifTags.Base`` members the installed Pillow happens to have.
_IDENTITY_TAGS = frozenset(
    {
        "Artist",
        "BodySerialNumber",
        "CameraOwnerName",
        "Copyright",
        "HostComputer",
        "ImageDescription",
        "ImageUniqueID",
        "LensMake",
        "LensModel",
        "LensSerialNumber",
        "Make",
        "Model",
        "Software",
        "UserComment",
        "XPAuthor",
        "XPComment",
        "XPKeywords",
        "XPSubject",
        "XPTitle",
    }
)

#: When the picture was taken, down to the sub-second and the UTC offset.
_TIME_TAGS = frozenset(
    {
        "DateTime",
        "DateTimeDigitized",
        "DateTimeOriginal",
        "OffsetTime",
        "OffsetTimeDigitized",
        "OffsetTimeOriginal",
        "SubsecTime",
        "SubsecTimeDigitized",
        "SubsecTimeOriginal",
    }
)

#: Tags that are accounted for by a line of their own, so they are not also
#: counted as camera settings.
_NOT_A_SETTING = _IDENTITY_TAGS | _TIME_TAGS | {"Orientation", "GPSInfo", "ExifOffset"}

#: A location can also be written into XMP, which is a separate block and is not
#: parsed here -- only looked at for these names.
_XMP_LOCATION = (b"GPSLatitude", b"GPSLongitude", b"GPSCoordinates", b"exif:GPS")


@dataclass(frozen=True)
class Metadata:
    """What a local image carries besides its pixels.

    Read without writing anything and without the network, so it can be
    reported next to the file's path before the author is asked to confirm.
    """

    #: The GPS tags found, by name. Non-empty means the file says where it was
    #: taken.
    location: tuple = ()
    #: Make, model, software, serial numbers, copyright, embedded comments.
    identity: tuple = ()
    #: Capture-time tags.
    times: tuple = ()
    #: Blocks found whose contents are not itemised: ``XMP``, ``IPTC``,
    #: ``comment``, PNG text chunks.
    blocks: tuple = ()
    #: Remaining EXIF tags, which are camera settings (exposure, focal length).
    other: int = 0
    #: The EXIF orientation, 1 when upright or absent. Anything else has to be
    #: applied to the pixels, or stripping it leaves the photo on its side.
    orientation: int = 1
    #: Kept: colour, not a person.
    icc_profile: bool = False
    #: More than one frame (APNG, animated WebP), which is not re-encoded.
    animated: bool = False
    #: Why the file could not be read, when it could not be.
    unreadable: str = ""

    @property
    def found(self):
        """Whether there is anything here that a strip would remove."""
        return bool(
            self.location or self.identity or self.times or self.blocks or self.other
        )

    def summary(self):
        """One clause per kind of thing found, location first because it leads."""
        if self.unreadable:
            return f"could not be read ({self.unreadable})"
        parts = []
        if self.location:
            parts.append(f"GPS location ({len(self.location)} tags)")
        if self.identity:
            parts.append(", ".join(self.identity))
        if self.times:
            parts.append("capture time")
        parts.extend(self.blocks)
        if self.other:
            parts.append(f"{self.other} camera setting(s)")
        return ", ".join(parts) if parts else "none"


def _pillow(action):
    """Pillow, or an ``ImageError`` that says what to do about it.

    A declared dependency, so a failure here is a broken environment and not an
    optional feature missing. It is still raised as an image problem rather than
    an ``ImportError`` because of what must *not* happen: a publish that answers
    a missing scrubber by uploading the original file, which is all of #288.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover - a broken install
        raise ImageError(
            f"Pillow is needed to {action}, and it could not be imported ({e}). "
            "Run `uv sync` -- it is a declared dependency. To publish without "
            "it, pass --keep-image-metadata, which uploads each file exactly as "
            "it is, including any GPS position in its EXIF."
        ) from e
    return Image, ImageOps


def inspect_metadata(src):
    """What ``src`` carries, as a ``Metadata``. Writes nothing, sends nothing.

    ``src`` is a path or an open binary file, so one reading covers a file on
    disk and the bytes of one already in a media library.
    """
    Image, _ = _pillow("read an image's metadata")
    from PIL import ExifTags

    try:
        with Image.open(src) as im:
            exif = im.getexif()
            location = [
                ExifTags.GPSTAGS[tag]
                for tag in (exif.get_ifd(ExifTags.IFD.GPSInfo) or {})
                if tag in ExifTags.GPSTAGS
            ]
            # IFD0 holds make, model and software; the Exif sub-IFD holds the
            # capture time and the lens. Both, or half of what a phone writes
            # would be counted as a camera setting.
            tags = set(exif) | set(exif.get_ifd(ExifTags.IFD.Exif) or {})
            named = [ExifTags.TAGS.get(tag) for tag in tags]
            identity = sorted(n for n in named if n in _IDENTITY_TAGS)
            times = sorted(n for n in named if n in _TIME_TAGS)
            other = sum(1 for n in named if n not in _NOT_A_SETTING)

            blocks = []
            xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
            if xmp:
                blocks.append("XMP")
                raw = xmp.encode("utf-8", "replace") if isinstance(xmp, str) else xmp
                if any(name in raw for name in _XMP_LOCATION):
                    # Counted as a location although it is not itemised: the line
                    # this ends up on says "this file records where it was", and
                    # XMP can record it on its own.
                    location.append("XMP GPS")
            if im.info.get("photoshop") or im.info.get("iptc"):
                blocks.append("IPTC")
            if im.info.get("comment"):
                blocks.append("comment")
            if getattr(im, "text", None):
                blocks.append(f"{len(im.text)} PNG text chunk(s)")

            return Metadata(
                location=tuple(location),
                identity=tuple(identity),
                times=tuple(times),
                blocks=tuple(blocks),
                other=other,
                orientation=exif.get(ExifTags.Base.Orientation.value) or 1,
                icc_profile=bool(im.info.get("icc_profile")),
                animated=getattr(im, "n_frames", 1) > 1,
            )
    except ImageError:
        raise
    except Exception as e:  # truncated, or a format Pillow needs a plugin for
        return Metadata(unreadable=f"{type(e).__name__}: {e}")


def strippable(path):
    """Whether ``path``'s type is one the strip handles."""
    return Path(path).suffix.lower() in STRIPPABLE


def _jpeg_options(im):
    """Save options that re-encode a JPEG at the quality it already is.

    The source's own quantisation tables and chroma subsampling, so the file
    neither drops a quality step nor gains size; what is left is one DCT round
    trip, which is what putting the orientation into the pixels costs and cannot
    be avoided.

    Pillow's own ``quality="keep"`` does exactly this and cannot be used here:
    it reads ``im.format``, and the image the orientation has been applied to
    has ``format`` set to ``None`` (python-pillow/Pillow#5527). So both values
    are read off the original and passed explicitly.
    """
    options = {"comment": b"", "optimize": True}
    # Pillow's JPEG writer takes the COM comment from ``im.info`` when the save
    # does not name one (``info.get("comment", im.info.get("comment"))`` in
    # JpegImagePlugin), so it is the one block that has to be cleared by hand.
    quantization = getattr(im, "quantization", None)
    if quantization:
        options["qtables"] = quantization
    else:  # unreachable for a JPEG Pillow opened; a guess beats a crash
        options["quality"] = 95
    try:
        from PIL import JpegImagePlugin

        options["subsampling"] = JpegImagePlugin.get_sampling(im)
    except Exception:
        pass  # Pillow's default, which is what an unreadable layout would get
    return options


def _webp_options(raw):
    """Save options for a WebP, from what the file already is.

    A lossless WebP (a ``VP8L`` chunk) is re-saved lossless and comes out
    pixel-identical. A lossy one is re-saved at 95, which can make the file
    bigger: nothing in the format records the quality it was written at, so the
    choice is between growing it and degrading it, and growing it is the one
    that cannot spoil a photograph.
    """
    return {"lossless": True} if b"VP8L" in raw[:64] else {"quality": 95, "method": 6}


def strip_metadata(path, out_dir):
    """Write a copy of ``path`` into ``out_dir`` with its metadata removed.

    Returns the new path, under the original file name, so the upload still
    names the file the author named. The original is never modified.

    What survives: the pixels, with the EXIF orientation applied to them, and
    the ICC profile, which describes colour and not a person. What does not:
    EXIF (the GPS block, make, model, software, serial numbers, capture time,
    camera settings), XMP, IPTC, and any comment or PNG text chunk.

    Raises ``ImageError`` when the copy cannot be made, and is never answered by
    uploading the original: a scrubber that quietly gives up is worse than one
    that was never there, because the publish still looks like it worked.
    """
    Image, ImageOps = _pillow("strip an image's metadata")
    path, target = Path(path), Path(out_dir) / Path(path).name
    try:
        raw = path.read_bytes()
        with Image.open(path) as im:
            fmt = im.format
            # Read before the transpose: it returns a new image carrying neither
            # ``format`` nor the JPEG tables (Pillow#5527), and the ICC profile
            # lives in ``info``, which only the PNG writer carries over itself.
            icc = im.info.get("icc_profile")
            if fmt == "JPEG":
                options = _jpeg_options(im)
            elif fmt == "WEBP":
                options = _webp_options(raw)
            else:
                options = {}
            out = ImageOps.exif_transpose(im)
            if icc:
                options["icc_profile"] = icc
            # Explicit, although all three of Pillow's writers take EXIF from the
            # save call and never from ``im.info``: this is the line the module
            # exists for, and it should not be something a reader has to go and
            # confirm in Pillow's source.
            options["exif"] = b""
            out.save(target, format=fmt, **options)
    except ImageError:
        raise
    except Exception as e:
        detail = e.strerror if isinstance(e, OSError) and e.strerror else e
        raise ImageError(
            f"the metadata could not be stripped ({type(e).__name__}: {detail}). "
            "Fix or convert the file, or pass --keep-image-metadata to upload it "
            "as it is, including any GPS position in its EXIF."
        ) from e
    return target
