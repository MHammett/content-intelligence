"""Read what an image file carries, or build one that carries it.

Two modes, one subject.

``report`` prints what a file holds besides pixels: EXIF tag counts, the GPS
block tag by tag, the identity tags, the JPEG APPn/COM segments present, and
whether a handful of identifying strings appear anywhere in the bytes. It reads
with **exifread**, which shares no code with Pillow -- so when the uploader's
strip (``adapters/cms/images.py``) is checked against this, it is not Pillow
agreeing with itself. Two files given, it prints both and diffs the headline
numbers, which is the before/after a publish change needs.

``make`` writes a photo that carries what a phone writes: an 11-tag GPS block,
make, model, software, an artist, a body serial number, a capture time, an EXIF
orientation of 6, and a real sRGB ICC profile. The suite has its own fixture
builder for unit tests (``phone_photo`` in ``tests/test_wordpress_images.py``);
this one writes a photo-shaped file of a realistic size to disk, for the manual
and end-to-end checks the suite cannot do.

    uv run --with exifread python tools/image_metadata.py make photo.jpg
    uv run python tools/publish_rehearsal.py ...          # uploads it
    uv run --with exifread python tools/image_metadata.py report \\
        photo.jpg rehearsal-upload-photo.jpg

exifread is in the dev dependency group, so a plain ``uv run`` finds it in a
development checkout; the ``--with`` form above is for anywhere it is not.
"""

import argparse
import pathlib
import random
import sys

#: Strings planted by ``make``, looked for by ``report``. A tag count can go to
#: zero while the value is still sitting in the file somewhere else (an XMP
#: packet, a thumbnail, a comment), so the bytes are searched as well.
PLANTED = (b"ACME", b"Phone X Pro", b"Mike Hammett", b"F2LW90ABCDEF", b"PhoneOS")

#: The JPEG markers worth naming. APP1 carries EXIF *and* XMP, APP2 the ICC
#: profile -- which the strip keeps on purpose, because it describes colour.
_MARKERS = {
    0xE0: "APP0/JFIF",
    0xE1: "APP1/Exif-or-XMP",
    0xE2: "APP2/ICC",
    0xEC: "APP12/Ducky",
    0xED: "APP13/IPTC",
    0xEE: "APP14/Adobe",
    0xFE: "COM",
}

_IDENTITY = {
    "Artist",
    "BodySerialNumber",
    "Copyright",
    "DateTime",
    "DateTimeOriginal",
    "HostComputer",
    "LensModel",
    "Make",
    "Model",
    "Software",
}


def jpeg_segments(raw):
    """The APPn/COM markers present, read straight off the bytes.

    Walks the marker chain rather than trusting a library, because this is the
    file as a server will receive it and the question is what is physically in
    it. Returns an empty list for anything that is not a JPEG.
    """
    if raw[:2] != b"\xff\xd8":
        return []
    out, i = [], 2
    while i + 3 < len(raw) and raw[i] == 0xFF:
        marker = raw[i + 1]
        if marker == 0xDA:  # start of scan; pixel data from here on
            break
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        size = int.from_bytes(raw[i + 2 : i + 4], "big")
        if size < 2:
            break
        if marker in _MARKERS:
            out.append(_MARKERS[marker])
        i += 2 + size
    return out


def read(path):
    """``(tags, gps, identity, raw)`` for one file."""
    import exifread

    path = pathlib.Path(path)
    raw = path.read_bytes()
    with path.open("rb") as fh:
        tags = exifread.process_file(fh, details=False)
    gps = sorted(k[4:] for k in tags if k.startswith("GPS "))
    identity = sorted(k for k in tags if k.split()[-1] in _IDENTITY)
    return tags, gps, identity, raw


def report(path):
    tags, gps, identity, raw = read(path)
    name = pathlib.Path(path).name
    print(f"\n=== {name}  ({len(raw):,} bytes) ===")
    segments = jpeg_segments(raw)
    if segments:
        print(f"  JPEG segments   {segments}")
    print(f"  EXIF tags       {len(tags)}")
    print(f"  GPS tags        {len(gps)}")
    for key in gps:
        print(f"      {key} = {tags['GPS ' + key]}")
    print(f"  identity tags   {len(identity)}")
    for key in identity:
        print(f"      {key} = {tags[key]}")
    planted = [s.decode() for s in PLANTED if s in raw]
    print(f"  planted strings still in the bytes: {planted or 'none'}")
    # ISO-BMFF (HEIC, AVIF) keeps EXIF in a box, not an APP1 segment.
    for needle, label in (
        (b"Exif", "an 'Exif' box or marker"),
        (b"ICC_PROFILE", "ICC"),
    ):
        print(f"  {label}: {needle in raw}")
    return {
        "tags": len(tags),
        "gps": len(gps),
        "identity": len(identity),
        "bytes": len(raw),
    }


def make(path, fmt=None, size=(1200, 800), quality=85):
    """Write a photo carrying a phone's metadata. Returns the path."""
    from PIL import ExifTags, Image, ImageCms
    from PIL.TiffImagePlugin import IFDRational as R

    path = pathlib.Path(path)
    fmt = fmt or {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
        ".avif": "AVIF",
        ".tif": "TIFF",
        ".tiff": "TIFF",
    }.get(path.suffix.lower())
    if fmt is None:
        raise SystemExit(f"cannot tell what format {path.suffix!r} means")

    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "ACME"
    exif[ExifTags.Base.Model] = "Phone X Pro"
    exif[ExifTags.Base.Software] = "PhoneOS 9.2.1"
    exif[ExifTags.Base.Artist] = "Mike Hammett"
    exif[ExifTags.Base.DateTime] = "2026:07:04 11:22:33"
    # Orientation 6 is "turn it 90 degrees clockwise to show it". It is here
    # because dropping the tag without turning the pixels is how a stripped
    # phone photo ends up on its side, and that is the case worth checking.
    exif[ExifTags.Base.Orientation] = 6
    # A dict under the sub-IFD's own tag is how Pillow is given one to write:
    # get_ifd() hands back a throwaway dict when the tag is absent.
    exif[ExifTags.Base.ExifOffset] = {
        ExifTags.Base.DateTimeOriginal: "2026:07:04 11:22:33",
        ExifTags.Base.BodySerialNumber: "F2LW90ABCDEF",
        ExifTags.Base.LensModel: "Phone X Pro back camera 5.1mm f/1.6",
        ExifTags.Base.ISOSpeedRatings: 50,
    }
    exif[ExifTags.Base.GPSInfo] = {
        ExifTags.GPS.GPSVersionID: b"\x02\x03\x00\x00",
        ExifTags.GPS.GPSLatitudeRef: "N",
        ExifTags.GPS.GPSLatitude: (R(41), R(52), R(5523, 100)),
        ExifTags.GPS.GPSLongitudeRef: "W",
        ExifTags.GPS.GPSLongitude: (R(87), R(37), R(4017, 100)),
        ExifTags.GPS.GPSAltitudeRef: 0,
        ExifTags.GPS.GPSAltitude: R(18142, 100),
        ExifTags.GPS.GPSTimeStamp: (R(16), R(22), R(33)),
        ExifTags.GPS.GPSDateStamp: "2026:07:04",
        ExifTags.GPS.GPSImgDirectionRef: "T",
        ExifTags.GPS.GPSImgDirection: R(28917, 100),
    }

    # A gradient with noise: not flat, so a quality change shows, and a realistic
    # size rather than the few hundred bytes a solid colour compresses to.
    w, h = size
    im = Image.new("RGB", size)
    random.seed(288)
    px = im.load()
    for y in range(h):
        for x in range(w):
            n = random.randint(-12, 12)
            px[x, y] = tuple(
                max(0, min(255, c + n))
                for c in (x * 255 // w, y * 255 // h, (x + y) * 255 // (w + h))
            )

    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    options = {"exif": exif, "icc_profile": icc}
    if fmt == "JPEG":
        options.update(quality=quality, comment=b"taken at home")
    elif fmt == "TIFF":
        # Pillow's TIFF writer cannot take an Image.Exif object: it reaches for a
        # file handle the object does not have.
        options["exif"] = exif.tobytes()
    im.save(path, fmt, **options)
    print(f"wrote {path} ({path.stat().st_size:,} bytes), {w}x{h} {fmt}, orientation 6")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_report = sub.add_parser("report", help="print what each file carries")
    p_report.add_argument("paths", nargs="+")

    p_make = sub.add_parser("make", help="write a photo carrying a phone's metadata")
    p_make.add_argument("path")
    p_make.add_argument("--format", dest="fmt", help="default: from the extension")
    p_make.add_argument("--width", type=int, default=1200)
    p_make.add_argument("--height", type=int, default=800)
    p_make.add_argument("--quality", type=int, default=85)

    args = parser.parse_args(argv)
    if args.mode == "make":
        make(args.path, args.fmt, (args.width, args.height), args.quality)
        return 0

    summaries = [(pathlib.Path(p).name, report(p)) for p in args.paths]
    if len(summaries) == 2:
        (before_name, before), (after_name, after) = summaries
        print(f"\n=== {before_name} -> {after_name} ===")
        for key in ("tags", "gps", "identity", "bytes"):
            print(f"  {key:9} {before[key]:>9,}  ->  {after[key]:>9,}")
        if before["gps"] and not after["gps"]:
            print("  the GPS block is gone, and it was there to begin with")
        elif after["gps"]:
            print(f"  !! {after['gps']} GPS tags SURVIVED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
