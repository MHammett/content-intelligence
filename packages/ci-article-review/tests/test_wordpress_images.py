"""Images in a publication: found, checked, uploaded, and written as native blocks.

Before this, a Markdown image was converted like any other inline element and
wrapped in a paragraph block: ``<p><img src="img/pic.png"></p>``, with the
author's local path as the ``src``. On the live site that is a broken image, and
the publish that produced it reported success.

So these assert what a green publish did not: that a local file is uploaded and
the block points at where it landed, that the alt text reaches both the block
and the media-library item, and that anything which cannot be published stops
the publish before the post exists, naming the image. The network is a fake
WordPress that records every request, so what is asserted is what left the
process.
"""

import base64
import inspect
import io
import json
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from PIL import ExifTags, Image, ImageCms, features
from PIL.TiffImagePlugin import IFDRational

from ci_article_review import handoff_parser
from ci_article_review.adapters.cms import blocks
from ci_article_review.adapters.cms import images as img
from ci_article_review.adapters.cms import wordpress as wp
from ci_article_review.adapters.cms.blocks import image_block, to_blocks
from ci_article_review.adapters.cms.images import ImageError, UploadedImage
from ci_article_review.handoff_parser import parse_publication_handoff

WP_CONFIG = {
    "site_url": "https://example.com",
    "username": "editor",
    "application_password": "pass word here",
    "rest_api_endpoint": "/wp-json/wp/v2",
}
RANK_MATH = {"auto_set_og_tags": True, "default_schema_type": "Article"}

#: A real 1x1 PNG. The fake server never looks inside it; it is here so the
#: bytes that are uploaded are recognisably an image's.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _pixels(data):
    """``(format, size, pixels)`` of an image's bytes.

    What an upload can be asserted against now that the bytes that leave are a
    scrubbed copy and not the file on disk: the picture has to be the same one,
    byte-for-byte equality is gone on purpose, and which of the two a test means
    should be visible in the test.
    """
    with Image.open(io.BytesIO(data)) as im:
        return im.format, im.size, im.convert("RGBA").tobytes()


class Resp:
    """Enough of ``requests.Response`` for the adapter: status, JSON, and raising."""

    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeWordPress:
    """The parts of WordPress's REST API that publishing an image touches.

    Records every request in order, and for an upload reads the file while the
    handle is still open (the adapter closes it as soon as the call returns).
    """

    def __init__(self):
        self.requests = []  # ("POST", url), in the order they were sent
        self.uploads = []  # what the media endpoint received
        self.alt_updates = []  # JSON bodies sent to /media/<id>
        self.created = []  # JSON bodies sent to /posts or /pages
        self.upload_attempts = 0
        self.next_media_id = 500
        #: Upload numbers (1-based) that fail, mapped to the response they get.
        self.fail_upload = {}
        #: An exception to raise instead of answering, by upload number.
        self.raise_on_upload = {}
        self.large = True  # WordPress made a "large" size
        self.echo_alt = True  # the upload response reports the alt it was given
        self.alt_update_response = None  # overrides the /media/<id> answer
        self.upload_body_override = None
        #: Overrides the answer to a create outright (an error, say). Left None,
        #: the answer is built from the post that was sent: see ``_created``.
        self.post_response = None
        #: Slugs already in use. WordPress adds ``-2`` to a taken slug when a post
        #: is published, and not before: ``wp_unique_post_slug`` returns early for
        #: a draft, so a draft keeps the slug it was given however many share it.
        self.taken_slugs = set()
        #: Core's ``page`` type has no excerpt (it is not in the type's
        #: ``supports``), so WordPress ignores the field and the answer has none,
        #: unless the site adds the support. A post always has one.
        self.pages_have_excerpts = False
        #: Without ``post-thumbnails`` support WordPress drops ``featured_media``
        #: from a create without an error, and answers ``0``.
        self.thumbnails_supported = True

    def post(self, url, **kw):
        self.requests.append(("POST", url))
        if url.endswith("/media"):
            return self._upload(**kw)
        if "/media/" in url:
            self.alt_updates.append(kw["json"])
            if self.alt_update_response is not None:
                return self.alt_update_response
            return Resp(200, {"id": 1, "alt_text": kw["json"]["alt_text"]})
        if "rankmath" in url:
            return Resp(200, {})
        self.created.append(kw["json"])
        if self.post_response is not None:
            return self.post_response
        return self._created(url, kw["json"])

    def _created(self, url, payload):
        """What WordPress answers to a create, built from what was sent.

        The three fields a handoff can set are echoed the way WordPress stores
        them, because the adapter reads them back and these are the cases that
        read-back exists for: the slug as ``sanitize_title`` leaves it (and a
        draft with no slug has none), the excerpt where the post type has one,
        and ``featured_media`` where the theme supports it.
        """
        status = payload.get("status", "draft")
        slug = _sanitize_title(payload.get("slug") or "")
        if status == "publish":
            slug = slug or _sanitize_title(payload.get("title") or "")
            base, n = slug, 2
            while slug in self.taken_slugs:
                slug, n = f"{base}-{n}", n + 1
        body = {
            "id": 7,
            "link": "https://example.com/p/",
            "slug": slug,
            "featured_media": (
                payload.get("featured_media", 0) if self.thumbnails_supported else 0
            ),
        }
        if url.endswith("/posts") or self.pages_have_excerpts:
            excerpt = payload.get("excerpt", "")
            body["excerpt"] = {
                "raw": excerpt,
                "rendered": f"<p>{excerpt}</p>\n" if excerpt else "",
                "protected": False,
            }
        return Resp(200, body)

    def get(self, url, **kw):
        self.requests.append(("GET", url))
        return Resp(200, [{"id": 3, "slug": "s"}])

    def _upload(self, **kw):
        self.upload_attempts += 1
        n = self.upload_attempts
        if n in self.raise_on_upload:
            raise self.raise_on_upload[n]
        if n in self.fail_upload:
            return self.fail_upload[n]
        name, fh, mime = kw["files"]["file"]
        data = dict(kw.get("data") or {})
        self.uploads.append(
            {
                "name": name,
                "mime": mime,
                "bytes": fh.read(),
                "data": data,
                "headers": dict(kw["headers"]),
                "timeout": kw["timeout"],
            }
        )
        if self.upload_body_override is not None:
            return Resp(201, self.upload_body_override)
        media_id = self.next_media_id
        self.next_media_id += 1
        stem, _, ext = name.rpartition(".")
        base = "https://example.com/wp-content/uploads/2026/09"
        sizes = {"full": {"source_url": f"{base}/{name}"}}
        if self.large:
            sizes["large"] = {"source_url": f"{base}/{stem}-1024x576.{ext}"}
        return Resp(
            201,
            {
                "id": media_id,
                "source_url": f"{base}/{name}",
                "alt_text": data.get("alt_text", "") if self.echo_alt else "",
                "media_details": {"sizes": sizes},
            },
        )

    @property
    def media_requests(self):
        return [r for r in self.requests if r[1].endswith("/media")]

    @property
    def post_requests(self):
        return [r for r in self.requests if r[1].endswith(("/posts", "/pages"))]


def _sanitize_title(text):
    """What WordPress's ``sanitize_title`` makes of ``text``: how a slug is cleaned.

    The steps that matter here, in the order ``sanitize_title_with_dashes`` takes
    them: lowercase, entities removed, ``.`` to a hyphen, everything outside
    ``a-z0-9 _-`` dropped (which is what takes the colon out of ``0:00``),
    whitespace to hyphens, runs of hyphens collapsed, the ends trimmed.
    """
    text = re.sub(r"&.+?;", "", text.lower()).replace(".", "-")
    text = re.sub(r"\s+", "-", re.sub(r"[^%a-z0-9 _-]", "", text))
    return re.sub(r"-+", "-", text).strip("-")


def _push(tmp_path, markdown, fake=None, files=None, params=None, **kw):
    """Run ``wp.push`` against the fake, with ``files`` (name -> bytes) on disk.

    ``params`` adds to (or overrides) the post's parameters: a slug, an excerpt,
    a featured image, or ``post_type`` to leave the default page.
    """
    fake = fake or FakeWordPress()
    for name, data in (files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pub_params = {
        "title": "Grid Report",
        "post_type": "page",
        "tags": [],
        **(params or {}),
    }
    with (
        patch("ci_article_review.adapters.cms.wordpress.requests.post", fake.post),
        patch("ci_article_review.adapters.cms.wordpress.requests.get", fake.get),
    ):
        result = wp.push(
            markdown,
            pub_params,
            WP_CONFIG,
            RANK_MATH,
            image_base_dir=tmp_path,
            **kw,
        )
    return result, fake


# ---------------------------------------------------------------------------
# What a source is
# ---------------------------------------------------------------------------


class TestSourceKind:
    @pytest.mark.parametrize(
        "src",
        [
            "a.png",
            "./a.png",
            "../x/a.png",
            "images/grid map.png",
            "/abs/a.png",
            "my%20photo.png",
            "C:/photos/a.png",
            "c:\\photos\\a.png",
            "D:\\a.png",
        ],
    )
    def test_a_path_is_local(self, src):
        """The Windows drive letter is the case: ``urlsplit`` reads ``C`` as a
        URL scheme, which would class a path on the author's disk as a
        protocol nobody supports."""
        assert img.source_kind(src) == img.LOCAL

    @pytest.mark.parametrize(
        "src",
        ["https://e.com/a.png", "http://e.com/a.png", "HTTPS://E.COM/A.PNG"],
    )
    def test_http_is_a_url(self, src):
        assert img.source_kind(src) == img.URL

    def test_a_scheme_relative_url_is_a_url(self):
        assert img.source_kind("//cdn.example.com/a.png") == img.URL

    @pytest.mark.parametrize(
        "src",
        ["data:image/png;base64,AAAA", "file:///C:/a.png", "ftp://h/a.png"],
    )
    def test_any_other_scheme_is_named_unsupported(self, src):
        assert img.source_kind(src) == img.UNSUPPORTED

    @pytest.mark.parametrize("src", ["", "   ", None])
    def test_nothing_is_empty(self, src):
        assert img.source_kind(src) == img.EMPTY

    def test_a_malformed_url_is_unsupported_not_a_crash(self):
        assert img.source_kind("http://[bad") == img.UNSUPPORTED


# ---------------------------------------------------------------------------
# Finding images in Markdown
# ---------------------------------------------------------------------------


class TestFindImages:
    def test_a_plain_image(self):
        (ref,) = blocks.find_images("![Grid map](maps/grid.png)")
        assert (ref.src, ref.alt, ref.caption) == ("maps/grid.png", "Grid map", "")
        assert ref.placement == img.BLOCK
        assert ref.kind == img.LOCAL

    def test_the_title_is_the_caption(self):
        (ref,) = blocks.find_images('![Grid map](grid.png "Figure 1: The grid")')
        assert ref.caption == "Figure 1: The grid"

    def test_an_empty_alt_is_kept_as_empty(self):
        (ref,) = blocks.find_images("![](grid.png)")
        assert ref.alt == ""

    def test_consecutive_lines_are_two_images_not_one_paragraph(self):
        """Markdown makes them one paragraph; the author wrote two figures."""
        refs = blocks.find_images("![a](1.png)\n![b](2.png)")
        assert [r.src for r in refs] == ["1.png", "2.png"]
        assert {r.placement for r in refs} == {img.BLOCK}

    def test_document_order_is_kept(self):
        md = "![one](1.png)\n\nText.\n\n![two](https://e.com/2.png)\n\n![three](3.png)"
        assert [r.alt for r in blocks.find_images(md)] == ["one", "two", "three"]

    def test_an_image_inside_a_sentence_is_inline(self):
        (ref,) = blocks.find_images("Look at ![icon](icon.png) here.")
        assert ref.placement == img.INLINE

    def test_words_after_the_image_make_it_inline(self):
        """Not an image-only paragraph: turning it into a block would drop them."""
        (ref,) = blocks.find_images("![a](x.png) and then some words")
        assert ref.placement == img.INLINE

    def test_a_hard_line_break_between_images_still_makes_two_images(self):
        """Two trailing spaces make Markdown emit a <br> between them."""
        refs = blocks.find_images("![a](1.png)  \n![b](2.png)")
        assert [r.placement for r in refs] == [img.BLOCK, img.BLOCK]

    def test_a_linked_image_is_inline(self):
        (ref,) = blocks.find_images("[![a](1.png)](https://example.com)")
        assert ref.placement == img.INLINE

    @pytest.mark.parametrize(
        "wrapper", ["- {}", "> {}", "1. {}"], ids=["list", "quote", "ordered"]
    )
    def test_an_image_in_a_list_or_quote_is_inline(self, wrapper):
        (ref,) = blocks.find_images(wrapper.format("![a](1.png)"))
        assert ref.placement == img.INLINE

    def test_a_reference_style_image_is_found_with_its_title(self):
        md = '![a][r]\n\n[r]: img/pic.png "A caption"'
        (ref,) = blocks.find_images(md)
        assert (ref.src, ref.caption, ref.placement) == (
            "img/pic.png",
            "A caption",
            img.BLOCK,
        )

    def test_angle_brackets_carry_a_space(self):
        (ref,) = blocks.find_images("![a](<my photo.png>)")
        assert ref.src == "my photo.png"

    def test_a_closed_raw_html_image_is_found_too(self):
        (ref,) = blocks.find_images('<img src="x.png" alt="raw" />')
        assert ref.src == "x.png"

    def test_a_windows_path_with_forward_slashes_survives_parsing(self):
        (ref,) = blocks.find_images("![a](C:/Users/me/grid.png)")
        assert ref.src == "C:/Users/me/grid.png"
        assert ref.kind == img.LOCAL

    def test_a_backslash_escape_is_eaten_by_markdown(self):
        """Why the docs say forward slashes: this is Markdown's doing, not ours,
        and the file that is then looked for is not the one the author typed."""
        (ref,) = blocks.find_images(r"![a](C:\photos\_x\pic.png)")
        assert ref.src == "C:\\photos_x\\pic.png"

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_nothing_to_find_in_nothing(self, text):
        assert blocks.find_images(text) == []

    def test_already_block_markup_is_not_searched(self):
        text = "<!-- wp:paragraph --><p>![a](1.png)</p><!-- /wp:paragraph -->"
        assert blocks.find_images(text) == []

    def test_unparseable_html_still_finds_the_images(self):
        """An unclosed <br> stops ElementTree; the images must not vanish with it."""
        refs = blocks.find_images("![a](x.png)\n\nline<br>break")
        assert [r.src for r in refs] == ["x.png"]
        assert refs[0].placement == img.UNPARSED
        assert refs[0].note  # says why


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


class TestImageBlock:
    def test_a_hosted_image_matches_what_the_editor_saves(self):
        """Pinned exactly because the editor validates a block by re-running its
        save() over the delimiter's attributes and comparing the HTML. A class
        name off by one opens as "unexpected or invalid content" — the opposite
        of easy to hand-edit."""
        out = image_block(
            "https://example.com/wp-content/uploads/2026/09/a-1024x576.png",
            alt="Grid map",
            caption="Figure 1",
            attachment_id=12,
            size_slug="large",
        )
        assert out == (
            '<!-- wp:image {"id":12,"sizeSlug":"large","linkDestination":"none"} -->\n'
            '<figure class="wp-block-image size-large">'
            '<img src="https://example.com/wp-content/uploads/2026/09/a-1024x576.png" '
            'alt="Grid map" class="wp-image-12"/>'
            '<figcaption class="wp-element-caption">Figure 1</figcaption></figure>\n'
            "<!-- /wp:image -->"
        )

    def test_no_caption_means_no_figcaption(self):
        out = image_block("https://e.com/a.png", "A", "", 3, "full")
        assert "figcaption" not in out

    def test_an_empty_alt_is_still_written(self):
        """The editor writes ``alt=""`` for a decorative image, and so does this."""
        assert 'alt=""' in image_block("https://e.com/a.png", "", "", 3, "full")

    def test_a_url_image_has_no_id_and_says_so_plainly(self):
        out = image_block("https://e.com/a.png", alt="A")
        assert out == (
            "<!-- wp:image -->\n"
            '<figure class="wp-block-image"><img src="https://e.com/a.png" alt="A"/></figure>\n'
            "<!-- /wp:image -->"
        )

    def test_the_default_size_is_full(self):
        assert '"sizeSlug":"full"' in image_block("https://e.com/a.png", "", "", 3)

    def test_alt_and_caption_are_escaped(self):
        out = image_block(
            "https://e.com/a.png?x=1&y=2",
            alt='He said "hi" & <b>',
            caption="A < B & C",
            attachment_id=1,
            size_slug="full",
        )
        assert 'alt="He said &quot;hi&quot; &amp; &lt;b&gt;"' in out
        assert 'src="https://e.com/a.png?x=1&amp;y=2"' in out
        assert ">A &lt; B &amp; C</figcaption>" in out

    def test_the_delimiter_carries_only_what_the_block_stores_there(self):
        """url, alt and caption are HTML-sourced attributes; repeating them in
        the comment would be flagged as a mismatch."""
        out = image_block("https://e.com/a.png", "Alt", "Cap", 9, "large")
        comment = out.splitlines()[0]
        assert json.loads(comment.split(" ", 2)[2].rsplit(" -->", 1)[0]) == {
            "id": 9,
            "sizeSlug": "large",
            "linkDestination": "none",
        }


class TestToBlocksWithImages:
    RESOLVED = {
        "maps/grid.png": UploadedImage(412, "https://e.com/up/grid-1024.png", "large"),
    }

    def test_an_uploaded_image_becomes_an_image_block(self):
        out = to_blocks(
            '# T\n\nIntro.\n\n![Grid](maps/grid.png "Fig 1")\n\nAfter.',
            resolved=self.RESOLVED,
        )
        assert '<!-- wp:image {"id":412,"sizeSlug":"large"' in out
        assert 'src="https://e.com/up/grid-1024.png"' in out
        assert "maps/grid.png" not in out, "the local path must not reach the site"
        assert "<!-- wp:paragraph -->\n<p>Intro.</p>" in out
        assert out.index("Intro.") < out.index("wp:image") < out.index("After.")

    def test_it_is_not_a_paragraph_with_an_img_inside(self):
        """What shipped before: the image, wrapped in a paragraph block."""
        out = to_blocks("![Grid](maps/grid.png)", resolved=self.RESOLVED)
        assert "<p><img" not in out
        assert "wp:paragraph" not in out

    def test_consecutive_images_are_separate_blocks(self):
        out = to_blocks(
            "![a](1.png)\n![b](https://e.com/2.png)",
            resolved={"1.png": UploadedImage(1, "https://e.com/1.png", "full")},
        )
        assert out.count("<!-- wp:image") == 2

    def test_a_url_image_needs_no_entry(self):
        out = to_blocks("![a](https://e.com/a.png)")
        assert 'src="https://e.com/a.png"' in out
        assert "wp:image -->" in out  # no id, so no attributes

    def test_a_local_image_with_no_url_raises_rather_than_shipping_a_path(self):
        with pytest.raises(
            ImageError, match=r'Image 1 of 1, "maps/grid.png".*never uploaded'
        ):
            to_blocks("![Grid](maps/grid.png)")

    def test_a_local_image_inside_a_sentence_raises(self):
        with pytest.raises(ImageError, match=r'"icon.png".*inside other content'):
            to_blocks("Look at ![icon](icon.png) here.")

    def test_a_url_image_inside_a_sentence_is_left_as_it_always_was(self):
        """It renders, so there is nothing to refuse and no reason to change it."""
        out = to_blocks("Look at ![icon](https://e.com/i.png) here.")
        assert "<p>Look at <img" in out

    def test_words_beside_an_image_are_never_dropped(self):
        """The failure a wrong "image-only paragraph" test would have: the
        image becomes a block and the sentence around it disappears."""
        out = to_blocks("![a](https://e.com/x.png) and then some words")
        assert "and then some words" in out

    def test_a_local_image_in_unparseable_html_raises_rather_than_shipping_a_path(self):
        with pytest.raises(ImageError, match=r'"x.png".*could not be parsed'):
            to_blocks("![a](x.png)\n\nline<br>break")

    def test_unparseable_html_with_only_urls_still_publishes_as_before(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = to_blocks("![a](https://e.com/x.png)\n\nline<br>break")
        assert out.startswith("<!-- wp:html -->")
        assert "could not be parsed" in caplog.text

    def test_attributes_an_image_block_cannot_carry_are_reported(self, caplog):
        with caplog.at_level(logging.WARNING):
            to_blocks("![a](https://e.com/x.png){: width=300 }")
        assert "width" in caplog.text
        assert "cannot carry" in caplog.text

    def test_a_document_without_images_is_unaffected(self):
        out = to_blocks("# T\n\n## Sub\n\nBody with [a link](https://e.com).")
        assert "wp:image" not in out
        assert "<!-- wp:heading -->" in out

    def test_an_empty_source_is_refused(self):
        with pytest.raises(ImageError, match="no source"):
            to_blocks("![a]()")

    def test_a_data_uri_is_refused_by_name(self):
        with pytest.raises(ImageError, match="not a path to a file or an http"):
            to_blocks("![a](data:image/png;base64,AAAA)")

    def test_every_problem_is_reported_not_just_the_first(self):
        with pytest.raises(ImageError) as err:
            to_blocks("![a](one.png)\n\n![b](two.png)")
        assert '"one.png"' in str(err.value) and '"two.png"' in str(err.value)


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------


class TestResolveLocalImage:
    def test_relative_to_the_given_directory(self, tmp_path):
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "grid.png").write_bytes(PNG)
        found = img.resolve_local_image("images/grid.png", tmp_path)
        assert found == tmp_path / "images" / "grid.png"

    def test_not_relative_to_the_working_directory(self, tmp_path, monkeypatch):
        """The handoff, not the shell, decides: the same file resolves the same
        way from wherever the command runs."""
        handoff_dir = tmp_path / "handoff"
        elsewhere = tmp_path / "elsewhere"
        handoff_dir.mkdir()
        elsewhere.mkdir()
        (handoff_dir / "grid.png").write_bytes(PNG)
        monkeypatch.chdir(elsewhere)
        assert (
            img.resolve_local_image("grid.png", handoff_dir) == handoff_dir / "grid.png"
        )

    def test_dot_dot_is_folded_out_of_the_path_the_author_is_shown(self, tmp_path):
        """The publish lists this path before asking for a yes. A join such as
        ``handoff/../shared/a.png`` names the same file and hides which one."""
        (tmp_path / "shared").mkdir()
        (tmp_path / "handoff").mkdir()
        target = tmp_path / "shared" / "a.png"
        target.write_bytes(PNG)
        found = img.resolve_local_image("../shared/a.png", tmp_path / "handoff")
        assert found == target
        assert ".." not in found.parts

    def test_an_absolute_path(self, tmp_path):
        target = tmp_path / "abs.png"
        target.write_bytes(PNG)
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert img.resolve_local_image(str(target), other) == target

    def test_an_absolute_path_with_forward_slashes(self, tmp_path):
        target = tmp_path / "abs.png"
        target.write_bytes(PNG)
        assert img.resolve_local_image(target.as_posix(), tmp_path / "x") == target

    def test_a_percent_encoded_space_is_found(self, tmp_path):
        """How CommonMark, and the preview in most editors, need it written."""
        target = tmp_path / "my photo.png"
        target.write_bytes(PNG)
        assert img.resolve_local_image("my%20photo.png", tmp_path) == target

    def test_a_literal_percent_in_a_real_name_wins(self, tmp_path):
        literal = tmp_path / "100%25.png"
        decoded = tmp_path / "100%.png"
        literal.write_bytes(PNG)
        decoded.write_bytes(PNG)
        assert img.resolve_local_image("100%25.png", tmp_path) == literal

    def test_the_extension_is_matched_case_insensitively(self, tmp_path):
        target = tmp_path / "SHOT.PNG"
        target.write_bytes(PNG)
        assert img.resolve_local_image("SHOT.PNG", tmp_path) == target

    def test_a_missing_file_lists_where_it_looked(self, tmp_path):
        with pytest.raises(ImageError) as err:
            img.resolve_local_image("nope/grid.png", tmp_path)
        assert "file not found" in str(err.value)
        assert str(tmp_path / "nope" / "grid.png") in str(err.value)

    def test_a_folder_is_not_an_image(self, tmp_path):
        (tmp_path / "images").mkdir()
        with pytest.raises(ImageError, match="folder"):
            img.resolve_local_image("images", tmp_path)

    @pytest.mark.parametrize("name", ["report.pdf", "notes.txt", "README", "clip.mp4"])
    def test_a_file_that_is_not_an_image_is_refused(self, tmp_path, name):
        """WordPress would take a PDF into the media library happily, and the
        image block would then point at something that is not an image."""
        (tmp_path / name).write_bytes(b"not an image")
        with pytest.raises(ImageError, match="not an image type"):
            img.resolve_local_image(name, tmp_path)

    def test_an_empty_file_is_refused(self, tmp_path):
        (tmp_path / "empty.png").write_bytes(b"")
        with pytest.raises(ImageError, match="empty"):
            img.resolve_local_image("empty.png", tmp_path)

    def test_an_unreadable_file_is_refused_with_the_reason(self, tmp_path, monkeypatch):
        (tmp_path / "locked.png").write_bytes(PNG)
        real_open = Path.open

        def deny(self, *a, **kw):
            if self.name == "locked.png":
                raise PermissionError(13, "Permission denied")
            return real_open(self, *a, **kw)

        monkeypatch.setattr(Path, "open", deny)
        with pytest.raises(ImageError, match="cannot be read: Permission denied"):
            img.resolve_local_image("locked.png", tmp_path)

    @pytest.mark.parametrize(
        "name,mime",
        [
            ("a.png", "image/png"),
            ("a.JPG", "image/jpeg"),
            ("a.jpeg", "image/jpeg"),
            ("a.gif", "image/gif"),
            ("a.webp", "image/webp"),
            ("a.avif", "image/avif"),
            ("a.svg", "image/svg+xml"),
        ],
    )
    def test_the_upload_type_comes_from_the_extension_not_the_registry(
        self, name, mime
    ):
        assert img.mime_for(name) == mime


# ---------------------------------------------------------------------------
# The plan: everything checkable without a request
# ---------------------------------------------------------------------------


class TestPlanImages:
    def test_a_draft_without_images_has_an_empty_plan(self, tmp_path):
        plan = wp.plan_images("# T\n\nJust text.", tmp_path)
        assert plan.refs == [] and plan.problems == [] and plan.block_images == []

    def test_a_local_image_is_to_be_uploaded_and_a_url_is_only_linked(self, tmp_path):
        (tmp_path / "a.png").write_bytes(PNG)
        plan = wp.plan_images("![A](a.png)\n\n![B](https://e.com/b.png)", tmp_path)
        assert plan.problems == []
        assert list(plan.uploads) == ["a.png"]
        assert [r.src for r in plan.linked] == ["https://e.com/b.png"]

    def test_every_problem_is_reported_at_once_and_numbered(self, tmp_path):
        (tmp_path / "ok.png").write_bytes(PNG)
        md = "![a](ok.png)\n\n![b](gone.png)\n\n![c](https://e.com/c.png)\n\n![d](also-gone.png)"
        plan = wp.plan_images(md, tmp_path)
        assert len(plan.problems) == 2
        assert plan.problems[0].startswith('Image 2 of 4, "gone.png": file not found')
        assert plan.problems[1].startswith(
            'Image 4 of 4, "also-gone.png": file not found'
        )

    def test_an_inline_local_image_is_a_problem(self, tmp_path):
        (tmp_path / "i.png").write_bytes(PNG)
        plan = wp.plan_images("Text ![i](i.png) text.", tmp_path)
        assert len(plan.problems) == 1
        assert "inside other content" in plan.problems[0]

    def test_a_missing_alt_is_a_warning_and_not_a_problem(self, tmp_path):
        (tmp_path / "a.png").write_bytes(PNG)
        plan = wp.plan_images("![](a.png)", tmp_path)
        assert plan.problems == []
        assert len(plan.warnings) == 1 and "no alt text" in plan.warnings[0]

    def test_whitespace_alone_is_no_alt_text(self, tmp_path):
        (tmp_path / "a.png").write_bytes(PNG)
        assert wp.plan_images("![   ](a.png)", tmp_path).warnings

    def test_an_image_with_alt_text_has_no_warning(self, tmp_path):
        (tmp_path / "a.png").write_bytes(PNG)
        assert wp.plan_images("![Grid](a.png)", tmp_path).warnings == []

    def test_planning_sends_nothing(self, tmp_path):
        """No network at all: the socket guard would fail this test otherwise."""
        (tmp_path / "a.png").write_bytes(PNG)
        with patch("ci_article_review.adapters.cms.wordpress.requests.post") as post:
            wp.plan_images("![A](a.png)", tmp_path)
        assert not post.called


# ---------------------------------------------------------------------------
# Publishing them
# ---------------------------------------------------------------------------

DRAFT = '# Grid Report\n\nIntro.\n\n![Grid load by county](maps/grid.png "Figure 1")\n\nBody.\n'


class TestPushUploadsImages:
    def test_the_file_is_uploaded_to_the_media_endpoint(self, tmp_path):
        result, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert result["success"] is True
        (upload,) = fake.uploads
        assert fake.media_requests == [
            ("POST", "https://example.com/wp-json/wp/v2/media")
        ]
        assert upload["name"] == "grid.png"
        assert upload["mime"] == "image/png"
        # The same picture, re-saved without its metadata (TestStrippingMetadata),
        # so it is the pixels and not the bytes that have to match.
        assert _pixels(upload["bytes"]) == _pixels(PNG)

    def test_the_upload_is_authenticated_and_is_not_labelled_json(self, tmp_path):
        """A JSON Content-Type on a multipart body breaks the boundary, so the
        auth-only headers are what an upload takes."""
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        headers = fake.uploads[0]["headers"]
        assert headers["Authorization"].startswith("Basic ")
        assert "Content-Type" not in headers

    def test_the_upload_has_its_own_longer_timeout(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert fake.uploads[0]["timeout"] > 60

    def test_the_images_go_up_before_the_post_is_created(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        urls = [u for _, u in fake.requests]
        media = next(i for i, u in enumerate(urls) if u.endswith("/media"))
        created = next(i for i, u in enumerate(urls) if u.endswith("/pages"))
        assert media < created

    def test_the_alt_text_travels_with_the_file(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert fake.uploads[0]["data"] == {"alt_text": "Grid load by county"}

    def test_the_post_holds_a_native_image_block_for_the_attachment(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        content = fake.created[0]["content"]
        assert (
            '<!-- wp:image {"id":500,"sizeSlug":"large","linkDestination":"none"} -->'
            in content
        )
        assert 'alt="Grid load by county"' in content
        assert 'class="wp-image-500"' in content
        assert ">Figure 1</figcaption>" in content

    def test_the_blocks_url_is_the_hosted_one_not_the_local_path(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        content = fake.created[0]["content"]
        assert (
            "https://example.com/wp-content/uploads/2026/09/grid-1024x576.png"
            in content
        )
        assert "maps/grid.png" not in content

    def test_large_is_used_when_wordpress_made_one_and_full_when_it_did_not(
        self, tmp_path
    ):
        fake = FakeWordPress()
        fake.large = False
        _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        content = fake.created[0]["content"]
        assert '"sizeSlug":"full"' in content
        assert (
            'src="https://example.com/wp-content/uploads/2026/09/grid.png"' in content
        )

    def test_the_result_reports_what_was_uploaded(self, tmp_path):
        result, _ = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert result["images_uploaded"] == 1
        assert result["images_linked"] == 0
        assert result["image_uploads"] == [
            {
                "src": "maps/grid.png",
                "id": 500,
                "url": "https://example.com/wp-content/uploads/2026/09/grid-1024x576.png",
                "stripped": True,
            }
        ]
        assert result["images_stripped"] == 1

    def test_a_hosted_url_is_linked_and_not_uploaded(self, tmp_path):
        md = "# T\n\n![Logo](https://cdn.example.com/logo.png)\n"
        result, fake = _push(tmp_path, md)
        assert fake.media_requests == []
        assert result["images_uploaded"] == 0 and result["images_linked"] == 1
        content = fake.created[0]["content"]
        assert 'src="https://cdn.example.com/logo.png"' in content
        assert "wp:image -->" in content  # a bare URL has no attachment id

    def test_a_draft_without_images_sends_no_media_request_and_no_new_result_keys(
        self, tmp_path
    ):
        """What publishing was before this, unchanged."""
        result, fake = _push(tmp_path, "# T\n\nJust words.\n")
        assert fake.media_requests == []
        assert not [k for k in result if k.startswith("image")]

    def test_several_images_are_uploaded_in_document_order(self, tmp_path):
        md = "# T\n\n![one](1.png)\n\n![two](2.png)\n\n![three](3.png)\n"
        result, fake = _push(tmp_path, md, files={f"{n}.png": PNG for n in (1, 2, 3)})
        assert [u["name"] for u in fake.uploads] == ["1.png", "2.png", "3.png"]
        assert [u["id"] for u in result["image_uploads"]] == [500, 501, 502]

    def test_the_same_file_twice_is_uploaded_once(self, tmp_path):
        md = '# T\n\n![First alt](a.png "First")\n\nText.\n\n![Second alt](./a.png "Second")\n'
        result, fake = _push(tmp_path, md, files={"a.png": PNG})
        assert result["success"] is True
        assert len(fake.uploads) == 1
        content = fake.created[0]["content"]
        assert content.count("wp:image {") == 2
        assert content.count('class="wp-image-500"') == 2

    def test_the_same_file_twice_keeps_each_blocks_own_alt_and_caption(self, tmp_path):
        md = '# T\n\n![First alt](a.png "First")\n\n![Second alt](a.png "Second")\n'
        _, fake = _push(tmp_path, md, files={"a.png": PNG})
        content = fake.created[0]["content"]
        assert 'alt="First alt"' in content and 'alt="Second alt"' in content
        assert ">First</figcaption>" in content and ">Second</figcaption>" in content

    def test_the_attachment_takes_the_first_alt_that_is_not_empty(self, tmp_path):
        md = "# T\n\n![](a.png)\n\n![Real alt](a.png)\n"
        _, fake = _push(tmp_path, md, files={"a.png": PNG})
        assert fake.uploads[0]["data"] == {"alt_text": "Real alt"}

    def test_a_decorative_image_sends_no_alt_field(self, tmp_path):
        result, fake = _push(tmp_path, "# T\n\n![](a.png)\n", files={"a.png": PNG})
        assert fake.uploads[0]["data"] == {}
        assert "image_warnings" not in result, (
            "no alt was asked for, so none is missing"
        )

    def test_the_post_content_has_no_paragraph_wrapped_img(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert "<p><img" not in fake.created[0]["content"]

    def test_a_live_publish_uploads_the_same_way(self, tmp_path):
        result, fake = _push(
            tmp_path, DRAFT, files={"maps/grid.png": PNG}, publish_live=True
        )
        assert result["success"] is True
        assert fake.created[0]["status"] == "publish"
        assert len(fake.uploads) == 1


class TestAltTextOnTheMediaItem:
    """Both places matter: the block, and the attachment's own alt-text field.

    The second is the one that goes missing quietly: the library item has no alt
    text on the file itself if only the block was set, and WordPress reports
    ``200`` either way.
    """

    def test_it_is_checked_in_the_response_not_assumed(self, tmp_path):
        fake = FakeWordPress()
        fake.echo_alt = False  # the field did not land
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert fake.alt_updates == [{"alt_text": "Grid load by county"}]
        assert "image_warnings" not in result, "the retry landed"

    def test_a_retry_that_also_fails_warns_and_still_publishes(self, tmp_path):
        fake = FakeWordPress()
        fake.echo_alt = False
        fake.alt_update_response = Resp(500, {"code": "internal", "message": "boom"})
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is True
        (warning,) = result["image_warnings"]
        assert '"maps/grid.png"' in warning and "media-library alt text" in warning
        assert "block has it" in warning

    def test_a_retry_that_returns_nothing_kept_still_warns(self, tmp_path):
        fake = FakeWordPress()
        fake.echo_alt = False
        fake.alt_update_response = Resp(200, {"id": 1, "alt_text": ""})
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert "did not keep the alt text" in result["image_warnings"][0]

    def test_no_retry_when_it_landed(self, tmp_path):
        _, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert fake.alt_updates == []

    def test_wordpress_sanitising_it_is_not_a_difference(self, tmp_path):
        """``sanitize_text_field`` turns a bare ``<`` into ``&lt;``."""
        fake = FakeWordPress()
        fake.upload_body_override = {
            "id": 9,
            "source_url": "https://example.com/wp-content/uploads/a.png",
            "alt_text": "Load &lt; 5 MW",
            "media_details": {},
        }
        result, _ = _push(
            tmp_path, "# T\n\n![Load < 5 MW](a.png)\n", fake=fake, files={"a.png": PNG}
        )
        assert fake.alt_updates == []
        assert "image_warnings" not in result

    def test_something_different_that_was_kept_is_reported_not_retried(self, tmp_path):
        fake = FakeWordPress()
        fake.upload_body_override = {
            "id": 9,
            "source_url": "https://example.com/wp-content/uploads/a.png",
            "alt_text": "grid",
            "media_details": {},
        }
        result, _ = _push(
            tmp_path,
            "# T\n\n![Grid <b>map</b>](a.png)\n",
            fake=fake,
            files={"a.png": PNG},
        )
        assert fake.alt_updates == []
        assert (
            "stored the media-library alt text as 'grid'" in result["image_warnings"][0]
        )


class TestFailingLoudly:
    """An image that cannot be published stops the publish, before the post."""

    def _assert_nothing_created(self, fake):
        assert fake.post_requests == [], "the post must not exist"

    def test_a_missing_file_fails_before_any_request_at_all(self, tmp_path):
        result, fake = _push(tmp_path, DRAFT)
        assert result["success"] is False
        assert fake.requests == [], "not even a term lookup"
        assert 'Image 1 of 1, "maps/grid.png"' in result["error"]
        assert "file not found" in result["error"]
        assert str(tmp_path / "maps" / "grid.png") in result["error"]

    def test_a_file_that_is_not_an_image_fails_before_any_request(self, tmp_path):
        md = "# T\n\n![a](report.pdf)\n"
        result, fake = _push(tmp_path, md, files={"report.pdf": b"%PDF-1.7"})
        assert result["success"] is False and fake.requests == []
        assert '"report.pdf"' in result["error"]

    def test_an_inline_local_image_fails_before_any_request(self, tmp_path):
        result, fake = _push(
            tmp_path, "# T\n\nSee ![i](i.png) here.\n", files={"i.png": PNG}
        )
        assert result["success"] is False and fake.requests == []
        assert "inside other content" in result["error"]

    def test_every_bad_image_is_named_in_one_failure(self, tmp_path):
        md = "# T\n\n![a](gone1.png)\n\n![b](gone2.png)\n"
        result, _ = _push(tmp_path, md)
        assert '"gone1.png"' in result["error"] and '"gone2.png"' in result["error"]

    @pytest.mark.parametrize(
        "status,body,expect",
        [
            (
                401,
                {
                    "code": "rest_cannot_create",
                    "message": "Sorry, you must be logged in.",
                },
                "wordpress.application_password",
            ),
            (
                403,
                {
                    "code": "rest_cannot_create",
                    "message": "Sorry, you are not allowed to upload media.",
                },
                "upload_files",
            ),
            (
                413,
                {"code": "rest_upload_limit", "message": "Request Entity Too Large"},
                "upload_max_filesize",
            ),
            (
                500,
                {
                    "code": "rest_upload_unknown_error",
                    "message": "Sorry, you are not allowed to upload this file type.",
                },
                "not allowed to upload this file type",
            ),
        ],
    )
    def test_a_refused_upload_names_the_image_the_status_and_the_reason(
        self, tmp_path, status, body, expect
    ):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(status, body)
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert 'Image 1 of 1, "maps/grid.png"' in result["error"]
        assert f"HTTP {status}" in result["error"]
        assert body["message"] in result["error"]
        assert expect in result["error"]
        self._assert_nothing_created(fake)

    def test_a_refusal_without_a_json_body_still_says_something(self, tmp_path):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(502, None)
        fake.fail_upload[1].text = "<html>Bad gateway</html>"
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert "HTTP 502" in result["error"] and "Bad gateway" in result["error"]
        self._assert_nothing_created(fake)

    @pytest.mark.parametrize(
        "exc",
        [
            requests.ConnectionError("Connection refused"),
            requests.Timeout("Read timed out"),
        ],
        ids=["refused", "timeout"],
    )
    def test_a_network_failure_names_the_image(self, tmp_path, exc):
        fake = FakeWordPress()
        fake.raise_on_upload[1] = exc
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert '"maps/grid.png"' in result["error"]
        assert "did not complete" in result["error"]
        self._assert_nothing_created(fake)

    def test_an_answer_that_names_no_attachment_fails(self, tmp_path):
        fake = FakeWordPress()
        fake.upload_body_override = {"unexpected": True}
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert "did not name the new attachment" in result["error"]
        self._assert_nothing_created(fake)

    def test_an_answer_that_is_not_json_fails(self, tmp_path):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(200, None)
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert "did not name the new attachment" in result["error"]

    def test_a_later_failure_names_what_was_already_uploaded(self, tmp_path):
        """The first image is already in the media library when the second
        fails. It is not deleted (the post may yet be wanted), and it is not
        silent either: the author is told which item to remove."""
        md = "# T\n\n![one](1.png)\n\n![two](2.png)\n"
        fake = FakeWordPress()
        fake.fail_upload[2] = Resp(500, {"code": "x", "message": "disk full"})
        result, _ = _push(tmp_path, md, fake=fake, files={"1.png": PNG, "2.png": PNG})
        assert result["success"] is False
        assert 'Image 2 of 2, "2.png"' in result["error"]
        assert "disk full" in result["error"]
        assert "ID 500 (1.png)" in result["error"]
        assert "nothing was deleted" in result["error"]
        self._assert_nothing_created(fake)

    def test_nothing_is_ever_deleted(self, tmp_path):
        md = "# T\n\n![one](1.png)\n\n![two](2.png)\n"
        fake = FakeWordPress()
        fake.fail_upload[2] = Resp(500, {"code": "x", "message": "disk full"})
        with patch(
            "ci_article_review.adapters.cms.wordpress.requests.delete"
        ) as delete:
            _push(tmp_path, md, fake=fake, files={"1.png": PNG, "2.png": PNG})
        assert not delete.called

    def test_a_conversion_failure_after_the_uploads_still_names_them(self, tmp_path):
        """Not reachable while the plan and the converter agree about the draft.
        The images are up by then, though, and the error is the one place that
        says so."""
        with patch(
            "ci_article_review.adapters.cms.wordpress.blocks.to_blocks",
            side_effect=ImageError('Image 1 of 1, "maps/grid.png": disagreement'),
        ):
            result, fake = _push(tmp_path, DRAFT, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert "disagreement" in result["error"]
        assert "ID 500 (maps/grid.png)" in result["error"]
        self._assert_nothing_created(fake)

    def test_a_failed_post_after_the_uploads_still_names_them(self, tmp_path):
        fake = FakeWordPress()
        fake.post_response = Resp(
            500, {"code": "internal", "message": "database error"}
        )
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        assert result["success"] is False
        assert "ID 500 (maps/grid.png)" in result["error"]

    def test_a_live_publish_refused_for_its_terms_uploads_nothing(self, tmp_path):
        """The term check comes first, so a publish it refuses leaves the media
        library as it found it."""

        class NoTerms(FakeWordPress):
            def get(self, url, **kw):
                self.requests.append(("GET", url))
                return Resp(200, [])

        fake = NoTerms()
        (tmp_path / "maps").mkdir()
        (tmp_path / "maps" / "grid.png").write_bytes(PNG)
        pub_params = {
            "title": "T",
            "post_type": "post",
            "wordpress_category": "nope",
            "tags": [],
        }
        with (
            patch("ci_article_review.adapters.cms.wordpress.requests.post", fake.post),
            patch("ci_article_review.adapters.cms.wordpress.requests.get", fake.get),
        ):
            result = wp.push(
                DRAFT,
                pub_params,
                WP_CONFIG,
                RANK_MATH,
                publish_live=True,
                image_base_dir=tmp_path,
            )
        assert result["success"] is False
        assert "unresolved taxonomy terms" in result["error"]
        assert fake.media_requests == [] and fake.uploads == []

    def test_no_credential_reaches_the_error_text(self, tmp_path):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(401, {"code": "rest_cannot_create", "message": "no"})
        result, _ = _push(tmp_path, DRAFT, fake=fake, files={"maps/grid.png": PNG})
        token = base64.b64encode(b"editor:pass word here").decode()
        assert "pass word here" not in result["error"]
        assert token not in result["error"]


# ---------------------------------------------------------------------------
# What the author sees
# ---------------------------------------------------------------------------


class TestWhatThePublishSays:
    def _plan(self, tmp_path, md, **files):
        for name, data in files.items():
            (tmp_path / name).write_bytes(data)
        return wp.plan_images(md, tmp_path)

    def test_the_plan_lists_each_file_with_its_resolved_path(self, tmp_path, capsys):
        plan = self._plan(tmp_path, '![Grid](a.png "Cap")', **{"a.png": PNG})
        wp.print_image_plan(plan)
        out = capsys.readouterr().out
        assert "IMAGES" in out
        assert f"UPLOAD  {tmp_path / 'a.png'}" in out
        assert "alt:     Grid" in out and "caption: Cap" in out

    def test_a_hosted_image_is_marked_as_not_uploaded(self, tmp_path, capsys):
        wp.print_image_plan(self._plan(tmp_path, "![Logo](https://e.com/l.png)"))
        out = capsys.readouterr().out
        assert "LINK    https://e.com/l.png" in out and "not uploaded" in out

    def test_a_missing_alt_is_flagged_in_the_plan(self, tmp_path, capsys):
        wp.print_image_plan(self._plan(tmp_path, "![](a.png)", **{"a.png": PNG}))
        out = capsys.readouterr().out
        assert "alt:     (none)" in out and "no alt text" in out

    def test_the_public_warning_is_shown_only_when_something_is_uploaded(
        self, tmp_path, capsys
    ):
        wp.print_image_plan(self._plan(tmp_path, "![Logo](https://e.com/l.png)"))
        assert "public" not in capsys.readouterr().out
        wp.print_image_plan(self._plan(tmp_path, "![A](a.png)", **{"a.png": PNG}))
        assert "public from the moment" in capsys.readouterr().out

    def test_no_images_prints_nothing(self, tmp_path, capsys):
        wp.print_image_plan(wp.plan_images("# T\n\nText.", tmp_path))
        assert capsys.readouterr().out == ""

    def test_the_result_says_how_many_went_up(self, capsys):
        wp.print_image_result(
            {
                "images_uploaded": 2,
                "images_linked": 1,
                "image_uploads": [
                    {"src": "a.png", "id": 5, "url": "u"},
                    {"src": "b.png", "id": 6, "url": "u"},
                ],
            }
        )
        out = capsys.readouterr().out
        assert "2 uploaded to the media library, 1 linked" in out
        assert "a.png -> media ID 5" in out and "b.png -> media ID 6" in out

    def test_warnings_are_printed_as_warnings(self, capsys):
        wp.print_image_result({"image_warnings": ["Image 1: something"]})
        assert "WARNING: Image 1: something" in capsys.readouterr().out

    def test_a_result_with_no_images_prints_nothing(self, capsys):
        wp.print_image_result({"success": True, "post_id": 1})
        assert capsys.readouterr().out == ""

    def test_the_checklist_no_longer_leaves_alt_text_as_the_only_signal(self):
        assert "listed under IMAGES above" in wp.CHECKLIST


# ---------------------------------------------------------------------------
# Through the real publish command
# ---------------------------------------------------------------------------

_HANDOFF = (
    "PUBLICATION HANDOFF\n"
    "Article: Grid Report\n"
    "Publication: testpub\n\n"
    "PUBLICATION PARAMETERS\n"
    "Status: draft\n"
    "Post type: page\n\n"
    "SEO METADATA\n"
    "Focus keyword: grid\n\n"
    "FINAL DRAFT\n"
    "# Grid Report\n\nIntro.\n\n{image}\n\nBody.\n"
)


def _handoff_text(image_line, params="", post_type="page"):
    """``_HANDOFF`` with ``params`` (whole lines) added to PUBLICATION PARAMETERS."""
    return _HANDOFF.format(image=image_line).replace(
        "Post type: page\n", f"Post type: {post_type}\n{params}", 1
    )


def _run_publish(
    tmp_path,
    image_line,
    fake=None,
    files=None,
    handoff_dir=None,
    params="",
    post_type="page",
    on_confirm=None,
):
    """``run_publish_pipeline`` on a handoff in ``handoff_dir``, network faked.

    ``params`` is whole lines for PUBLICATION PARAMETERS, each ending in a
    newline. ``on_confirm`` is called in place of the checklist prompt, to see
    what had been printed by then.

    Returns ``(exit code or None, fake, steps)``.
    """
    from ci_article_review.pipeline import run_publish_pipeline

    fake = fake or FakeWordPress()
    handoff_dir = handoff_dir or tmp_path
    handoff_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (files or {}).items():
        target = handoff_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    path = handoff_dir / "grid-publication.md"
    path.write_text(_handoff_text(image_line, params, post_type), encoding="utf-8")
    confirm_answer = (
        {"side_effect": on_confirm} if on_confirm else {"return_value": True}
    )
    config = {
        "publication": {"wordpress": dict(WP_CONFIG), "rank_math": dict(RANK_MATH)},
        "api_keys": {},
    }
    wp_module = "ci_article_review.adapters.cms.wordpress"
    with (
        patch("ci_article_review.pipeline.load_user_config", return_value={}),
        patch("ci_article_review.pipeline.load_publication_config", return_value={}),
        patch("ci_article_review.pipeline.merge_configs", return_value=config),
        patch("ci_article_review.pipeline._suggest_seo_for_publish") as suggest,
        patch(f"{wp_module}.print_checklist_and_confirm", **confirm_answer) as confirm,
        patch(f"{wp_module}.requests.post", fake.post),
        patch(f"{wp_module}.requests.get", fake.get),
    ):
        try:
            run_publish_pipeline(str(path), "testpub")
        except SystemExit as e:
            code = e.code
        else:
            code = None
    return code, fake, {"suggest": suggest, "confirm": confirm}


class TestPublishCommand:
    def test_a_relative_path_is_relative_to_the_handoff_not_the_shell(
        self, tmp_path, monkeypatch
    ):
        """The same handoff must find the same files whatever directory the
        command is run from."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        code, fake, _ = _run_publish(
            tmp_path,
            '![Grid](images/grid.png "Fig")',
            files={"images/grid.png": PNG},
            handoff_dir=tmp_path / "handoff",
        )
        assert code is None
        assert _pixels(fake.uploads[0]["bytes"]) == _pixels(PNG)

    def test_the_uploaded_image_reaches_the_post_as_a_block(self, tmp_path):
        code, fake, _ = _run_publish(
            tmp_path, "![Grid](grid.png)", files={"grid.png": PNG}
        )
        assert code is None
        assert '<!-- wp:image {"id":500' in fake.created[0]["content"]

    def test_the_output_reports_the_upload_next_to_the_post(self, tmp_path, capsys):
        _run_publish(tmp_path, "![Grid](grid.png)", files={"grid.png": PNG})
        out = capsys.readouterr().out
        assert "WordPress push successful." in out
        assert "Images:   1 uploaded to the media library, 0 linked" in out
        assert "grid.png -> media ID 500" in out

    def test_the_image_list_comes_before_the_checklist(self, tmp_path, capsys):
        from ci_article_review.pipeline import run_publish_pipeline

        order = []
        wp_module = "ci_article_review.adapters.cms.wordpress"
        fake = FakeWordPress()
        (tmp_path / "grid.png").write_bytes(PNG)
        path = tmp_path / "h.md"
        path.write_text(_HANDOFF.format(image="![Grid](grid.png)"), encoding="utf-8")
        config = {
            "publication": {"wordpress": dict(WP_CONFIG), "rank_math": dict(RANK_MATH)},
            "api_keys": {},
        }

        def checklist():
            order.append(capsys.readouterr().out)
            return True

        with (
            patch("ci_article_review.pipeline.load_user_config", return_value={}),
            patch(
                "ci_article_review.pipeline.load_publication_config", return_value={}
            ),
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch(f"{wp_module}.print_checklist_and_confirm", side_effect=checklist),
            patch(f"{wp_module}.requests.post", fake.post),
            patch(f"{wp_module}.requests.get", fake.get),
        ):
            run_publish_pipeline(str(path), "testpub", seo_suggestions=False)
        assert "UPLOAD" in order[0], "the author sees what will go up before saying yes"

    def test_a_missing_image_stops_everything_and_names_it(self, tmp_path, caplog):
        with caplog.at_level(logging.ERROR):
            code, fake, steps = _run_publish(tmp_path, "![Grid](nowhere/grid.png)")
        assert code == 1
        assert 'Image 1 of 1, "nowhere/grid.png"' in caplog.text
        assert "file not found" in caplog.text
        assert fake.requests == []
        assert not steps["suggest"].called, "before the SEO call is paid for"
        assert not steps["confirm"].called, "before the checklist asks for a yes"

    def test_a_draft_without_images_prints_no_image_section(self, tmp_path, capsys):
        code, fake, _ = _run_publish(tmp_path, "Just a paragraph.")
        out = capsys.readouterr().out
        assert code is None
        assert "IMAGES" not in out and "Images:" not in out
        assert fake.media_requests == []

    def test_a_failed_upload_exits_1_and_names_the_image(self, tmp_path, capsys):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(
            403, {"code": "rest_cannot_create", "message": "not allowed"}
        )
        code, _, _ = _run_publish(
            tmp_path, "![Grid](grid.png)", fake=fake, files={"grid.png": PNG}
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "WordPress push FAILED" in out
        assert 'Image 1 of 1, "grid.png"' in out and "not allowed" in out
        assert fake.post_requests == []


# ---------------------------------------------------------------------------
# The convention is written down where the next person looks
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Metadata (#288)
#
# A media-library file is public the moment it is uploaded, so an untouched phone
# photo publishes where it was taken before anyone has chosen to publish the
# post. These assert on the bytes that left the process, because that is the only
# place the question is settled.
# ---------------------------------------------------------------------------

GPS_TAGS = {
    ExifTags.GPS.GPSLatitudeRef: "N",
    ExifTags.GPS.GPSLatitude: (IFDRational(41), IFDRational(52), IFDRational(55)),
    ExifTags.GPS.GPSLongitudeRef: "W",
    ExifTags.GPS.GPSLongitude: (IFDRational(87), IFDRational(37), IFDRational(40)),
    ExifTags.GPS.GPSAltitudeRef: 0,
    ExifTags.GPS.GPSAltitude: IFDRational(181),
    ExifTags.GPS.GPSTimeStamp: (IFDRational(16), IFDRational(22), IFDRational(33)),
    ExifTags.GPS.GPSDateStamp: "2026:07:04",
}


def phone_exif(orientation=1):
    """The EXIF a phone writes: where, when, and on what."""
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "ACME"
    exif[ExifTags.Base.Model] = "Phone X"
    exif[ExifTags.Base.Software] = "Phone OS 9"
    exif[ExifTags.Base.Orientation] = orientation
    exif[ExifTags.Base.DateTime] = "2026:07:04 11:22:33"
    # A dict under the sub-IFD's own tag is how Pillow is given one to write:
    # ``get_ifd`` returns a throwaway dict when the tag is absent, so mutating it
    # writes nothing. The capture time belongs here and not in IFD0, which is
    # what a phone does and what inspect_metadata has to read.
    exif[ExifTags.Base.ExifOffset] = {
        ExifTags.Base.DateTimeOriginal: "2026:07:04 11:22:33"
    }
    exif[ExifTags.Base.GPSInfo] = dict(GPS_TAGS)
    return exif


def phone_photo(orientation=1, fmt="JPEG", size=(24, 16), **kw):
    """Bytes of an image carrying that EXIF, the way a camera hands one over."""
    im = Image.new("RGB", size)
    for x in range(size[0]):  # not flat, so a quality change would show
        for y in range(size[1]):
            im.putpixel((x, y), (x * 9 % 256, y * 15 % 256, (x + y) * 7 % 256))
    out = io.BytesIO()
    im.save(out, fmt, exif=phone_exif(orientation), **kw)
    return out.getvalue()


def read_exif(data):
    """``(ifd0 tag names, gps tag names)`` of an image's bytes."""
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        ifd0 = {ExifTags.TAGS.get(t, t) for t in exif}
        ifd0 |= {ExifTags.TAGS.get(t, t) for t in exif.get_ifd(ExifTags.IFD.Exif) or {}}
        gps = {
            ExifTags.GPSTAGS.get(t, t)
            for t in (exif.get_ifd(ExifTags.IFD.GPSInfo) or {})
        }
    return ifd0, gps


#: A draft whose only image is one photograph.
DAM = "# T\n\n![Dam](dam.jpg)\n"


class TestTheFixtureReallyCarriesGPS:
    """If the sample had no GPS block, every test below would pass vacuously."""

    def test_the_jpeg_has_a_gps_block_and_a_device(self):
        ifd0, gps = read_exif(phone_photo())
        assert "GPSLatitude" in gps and "GPSLongitude" in gps
        assert len(gps) == len(GPS_TAGS)
        assert {"Make", "Model", "Software"} <= ifd0
        assert b"Exif\x00\x00" in phone_photo()

    def test_inspect_reads_it_back(self, tmp_path):
        path = tmp_path / "shot.jpg"
        path.write_bytes(phone_photo(orientation=6))
        meta = img.inspect_metadata(path)
        assert len(meta.location) == len(GPS_TAGS)
        assert meta.identity == ("Make", "Model", "Software")
        assert "DateTime" in meta.times and "DateTimeOriginal" in meta.times
        assert meta.orientation == 6
        assert meta.found is True
        assert "GPS location" in meta.summary()

    def test_a_plain_image_carries_nothing(self, tmp_path):
        path = tmp_path / "chart.png"
        path.write_bytes(PNG)
        meta = img.inspect_metadata(path)
        assert meta.found is False
        assert meta.summary() == "none"
        assert meta.location == () and meta.orientation == 1


class TestStrippingMetadata:
    def test_the_uploaded_jpeg_has_no_gps_and_no_device(self, tmp_path):
        result, fake = _push(
            tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": phone_photo()}
        )
        assert result["success"] is True
        sent = fake.uploads[0]["bytes"]
        ifd0, gps = read_exif(sent)
        assert gps == set()
        assert ifd0 == set()
        assert b"Exif" not in sent
        assert b"ACME" not in sent and b"Phone X" not in sent

    def test_the_original_on_disk_is_not_touched(self, tmp_path):
        before = phone_photo()
        _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": before})
        assert (tmp_path / "dam.jpg").read_bytes() == before

    def test_the_picture_is_the_same_one(self, tmp_path):
        raw = phone_photo()
        _, fake = _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": raw})
        fmt, size, _ = _pixels(fake.uploads[0]["bytes"])
        assert (fmt, size) == ("JPEG", (24, 16))

    def test_a_rotated_photo_is_rotated_in_the_pixels_not_a_tag(self, tmp_path):
        """Orientation 6 means "turn it 90 degrees to show it". Dropping the tag
        without turning the pixels is how a stripped phone photo ends up on its
        side, which is the case the lossless segment drop could not handle."""
        raw = phone_photo(orientation=6, size=(24, 16))
        _, fake = _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": raw})
        sent = fake.uploads[0]["bytes"]
        _, size, _ = _pixels(sent)
        assert size == (16, 24)  # turned, not merely re-labelled
        assert read_exif(sent) == (set(), set())

    def test_an_upright_photo_is_not_turned(self, tmp_path):
        raw = phone_photo(orientation=1, size=(24, 16))
        _, fake = _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": raw})
        assert _pixels(fake.uploads[0]["bytes"])[1] == (24, 16)

    def test_the_icc_profile_survives(self, tmp_path):
        """Colour, not a person."""
        icc = b"\x00\x00\x02\x0cmntrRGB XYZ " + b"\x00" * 512
        raw = phone_photo(icc_profile=icc)
        _, fake = _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": raw})
        with Image.open(io.BytesIO(fake.uploads[0]["bytes"])) as im:
            assert im.info.get("icc_profile") == icc

    def test_a_jpeg_comment_goes_too(self, tmp_path):
        """Pillow's JPEG writer takes the COM segment from ``im.info`` when the
        save does not name one, so this is the block that survives a naive
        re-save."""
        raw = phone_photo(comment=b"shot at home")
        _, fake = _push(tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": raw})
        assert b"shot at home" not in fake.uploads[0]["bytes"]

    def test_png_text_chunks_and_its_exif_go(self, tmp_path):
        from PIL import PngImagePlugin

        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "taken at 41.88, -87.63")
        out = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(
            out, "PNG", pnginfo=info, exif=phone_exif()
        )
        raw = out.getvalue()
        # A PNG keeps EXIF in an ``eXIf`` chunk, whose payload is a bare TIFF
        # header -- no "Exif\0\0" prefix to look for, unlike a JPEG's APP1.
        assert b"41.88" in raw and b"eXIf" in raw  # the fixture really has them
        _, fake = _push(tmp_path, "# T\n\n![C](c.png)\n", files={"c.png": raw})
        sent = fake.uploads[0]["bytes"]
        assert b"41.88" not in sent and b"eXIf" not in sent
        assert read_exif(sent) == (set(), set())

    def test_a_webp_loses_its_exif(self, tmp_path):
        raw = phone_photo(fmt="WEBP", lossless=True)
        assert read_exif(raw)[1]  # the fixture really has a GPS block
        _, fake = _push(tmp_path, "# T\n\n![W](w.webp)\n", files={"w.webp": raw})
        sent = fake.uploads[0]["bytes"]
        assert read_exif(sent) == (set(), set())
        assert _pixels(sent) == _pixels(raw)  # lossless in, lossless out

    def test_a_jpeg_is_re_encoded_at_the_quality_it_already_was(self, tmp_path):
        """The quantisation tables come from the source, so the strip does not
        quietly cost a quality step or a size increase."""
        raw = phone_photo(size=(64, 64), quality=40)
        _, fake = _push(tmp_path, "# T\n\n![D](d.jpg)\n", files={"d.jpg": raw})
        sent = fake.uploads[0]["bytes"]
        with Image.open(io.BytesIO(raw)) as a, Image.open(io.BytesIO(sent)) as b:
            a.load()
            b.load()
            assert b.quantization == a.quantization
        assert len(sent) <= len(raw)

    def test_pillow_is_not_trusted_to_drop_exif_on_its_own(self):
        """The assertion that dates this module: none of the three writers copies
        EXIF out of ``im.info``, which is why a plain re-save already loses it.
        A Pillow that changed that would fail here rather than quietly publish a
        GPS block, and ``strip_metadata`` passes ``exif=b""`` regardless."""
        for fmt in ("JPEG", "PNG", "WEBP"):
            out = io.BytesIO()
            with Image.open(io.BytesIO(phone_photo())) as im:
                im.save(out, fmt)
            assert read_exif(out.getvalue()) == (set(), set()), fmt


class TestWhatTheAuthorIsTold:
    def test_the_listing_names_what_each_file_carries(self, tmp_path, capsys):
        (tmp_path / "dam.jpg").write_bytes(phone_photo())
        plan = wp.plan_images("# T\n\n![Dam](dam.jpg)\n", tmp_path)
        wp.print_image_plan(plan)
        out = capsys.readouterr().out
        assert "GPS location (8 tags)" in out
        assert "Make, Model, Software" in out
        assert "capture time" in out
        assert "stripped before upload" in out

    def test_a_rotated_photo_says_the_rotation_is_applied(self, tmp_path, capsys):
        """The one thing the strip changes about the picture, said where the
        author can see it before confirming."""
        (tmp_path / "dam.jpg").write_bytes(phone_photo(orientation=6))
        wp.print_image_plan(wp.plan_images(DAM, tmp_path))
        assert "the EXIF rotation is applied to the pixels" in capsys.readouterr().out

    def test_a_file_with_nothing_says_so(self, tmp_path, capsys):
        (tmp_path / "c.png").write_bytes(PNG)
        wp.print_image_plan(wp.plan_images("# T\n\n![C](c.png)\n", tmp_path))
        assert "metadata: none found" in capsys.readouterr().out

    def test_the_result_counts_what_was_stripped(self, tmp_path):
        result, _ = _push(
            tmp_path, "# T\n\n![Dam](dam.jpg)\n", files={"dam.jpg": phone_photo()}
        )
        assert result["images_stripped"] == 1
        assert result["image_uploads"][0]["stripped"] is True


class TestKeepImageMetadata:
    def test_the_file_goes_up_byte_for_byte(self, tmp_path):
        raw = phone_photo()
        result, fake = _push(
            tmp_path,
            "# T\n\n![Dam](dam.jpg)\n",
            files={"dam.jpg": raw},
            strip_image_metadata=False,
        )
        assert result["success"] is True
        assert fake.uploads[0]["bytes"] == raw
        assert result["images_stripped"] == 0
        assert result["image_uploads"][0]["stripped"] is False

    def test_the_listing_says_the_location_is_going_public(self, tmp_path, capsys):
        (tmp_path / "dam.jpg").write_bytes(phone_photo())
        plan = wp.plan_images(
            "# T\n\n![Dam](dam.jpg)\n", tmp_path, strip_metadata=False
        )
        wp.print_image_plan(plan)
        out = capsys.readouterr().out
        assert "KEPT (--keep-image-metadata)" in out
        assert "records WHERE IT WAS TAKEN" in out
        assert "nothing is stripped" in out

    def test_the_flag_exists_and_stripping_is_the_default(self):
        """A rename or a store_true flipped the wrong way would leave the strip
        silently off, which looks exactly like a working publish."""
        from ci_article_review import pipeline

        parser = pipeline.build_parser()
        base = ["--publish", "x", "--publication", "p"]
        assert parser.parse_args(base).keep_image_metadata is False
        assert parser.parse_args(base + ["--keep-image-metadata"]).keep_image_metadata
        signature = inspect.signature(pipeline.run_publish_pipeline)
        assert signature.parameters["strip_image_metadata"].default is True
        assert (
            inspect.signature(wp.push).parameters["strip_image_metadata"].default
            is True
        )


class TestTypesThatAreNotStripped:
    def test_an_unstripped_type_is_uploaded_as_it_is_and_said_so(
        self, tmp_path, capsys
    ):
        """A TIFF carries the same GPS block and is not re-encoded. The author is
        told, rather than the file being quietly published either way."""
        out = io.BytesIO()
        # ``.tobytes()``: Pillow's TIFF writer cannot take an ``Image.Exif``
        # object (it reaches for a file handle the object does not have).
        Image.new("RGB", (8, 8)).save(out, "TIFF", exif=phone_exif().tobytes())
        raw = out.getvalue()
        assert read_exif(raw)[1]  # the fixture really has a GPS block
        (tmp_path / "s.tif").write_bytes(raw)
        plan = wp.plan_images("# T\n\n![S](s.tif)\n", tmp_path)
        wp.print_image_plan(plan)
        printed = capsys.readouterr().out
        assert "NOT stripped (.tif is uploaded as it is)" in printed
        assert "records WHERE IT WAS TAKEN" in printed
        _, fake = _push(tmp_path, "# T\n\n![S](s.tif)\n", files={"s.tif": raw})
        assert fake.uploads[0]["bytes"] == raw

    def test_an_animation_is_not_re_encoded(self, tmp_path, capsys):
        """An APNG. Re-saving one means rebuilding every frame's timing and
        disposal, and a still image where an animation used to be is a worse
        outcome than the metadata the strip would have removed."""
        out = io.BytesIO()
        frames = [Image.new("RGB", (8, 8), c) for c in ((1, 0, 0), (0, 1, 0))]
        frames[0].save(
            out, "PNG", save_all=True, append_images=frames[1:], duration=100
        )
        raw = out.getvalue()
        (tmp_path / "a.png").write_bytes(raw)
        plan = wp.plan_images("# T\n\n![A](a.png)\n", tmp_path)
        assert plan.metadata["a.png"].animated is True
        wp.print_image_plan(plan)
        assert "NOT stripped (an animation" in capsys.readouterr().out
        _, fake = _push(tmp_path, "# T\n\n![A](a.png)\n", files={"a.png": raw})
        assert fake.uploads[0]["bytes"] == raw

    def test_an_svg_is_not_reported_as_unreadable(self, tmp_path, capsys):
        """Pillow cannot open one, which is not a problem to report: it is simply
        not a type the strip handles."""
        raw = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        (tmp_path / "d.svg").write_bytes(raw)
        plan = wp.plan_images("# T\n\n![D](d.svg)\n", tmp_path)
        assert plan.problems == []
        wp.print_image_plan(plan)
        printed = capsys.readouterr().out
        assert "NOT stripped (.svg is uploaded as it is)" in printed
        assert "could not be read" not in printed


class TestFailingClosed:
    def test_a_jpeg_that_cannot_be_opened_stops_the_publish(self, tmp_path):
        """The one answer that is never right is uploading the original because
        the scrubber choked: the publish would look like it worked."""
        result, fake = _push(
            tmp_path,
            "# T\n\n![Broken](broken.jpg)\n",
            files={"broken.jpg": b"\xff\xd8\xff not really a jpeg"},
        )
        assert result["success"] is False
        assert fake.media_requests == []
        assert "cannot be opened as an image" in result["error"]
        assert "--keep-image-metadata" in result["error"]

    def test_that_same_file_publishes_with_the_opt_out(self, tmp_path):
        raw = b"\xff\xd8\xff not really a jpeg"
        result, fake = _push(
            tmp_path,
            "# T\n\n![Broken](broken.jpg)\n",
            files={"broken.jpg": raw},
            strip_image_metadata=False,
        )
        assert result["success"] is True
        assert fake.uploads[0]["bytes"] == raw

    def test_a_strip_that_fails_at_upload_time_names_what_went_up(self, tmp_path):
        """``strip_metadata`` raising after the plan passed must not be answered
        by sending the original either, and must say what is already public."""
        md = "# T\n\n![One](1.jpg)\n\n![Two](2.jpg)\n"
        calls = []
        real = img.strip_metadata

        def fail_on_second(path, out_dir):
            calls.append(path)
            if len(calls) == 2:
                raise ImageError("the metadata could not be stripped (boom)")
            return real(path, out_dir)

        with patch.object(img, "strip_metadata", fail_on_second):
            result, fake = _push(
                tmp_path, md, files={f"{n}.jpg": phone_photo() for n in (1, 2)}
            )
        assert result["success"] is False
        assert "could not be stripped" in result["error"]
        assert "still in the media library" in result["error"]
        assert len(fake.uploads) == 1  # the first one, and no post


class TestSameNameDifferentFolders:
    def test_two_files_called_the_same_thing_do_not_collide(self, tmp_path):
        md = "# T\n\n![A](a/shot.jpg)\n\n![B](b/shot.jpg)\n"
        a, b = phone_photo(size=(24, 16)), phone_photo(size=(32, 8))
        result, fake = _push(tmp_path, md, files={"a/shot.jpg": a, "b/shot.jpg": b})
        assert result["success"] is True
        assert [u["name"] for u in fake.uploads] == ["shot.jpg", "shot.jpg"]
        assert [_pixels(u["bytes"])[1] for u in fake.uploads] == [(24, 16), (32, 8)]
        assert all(read_exif(u["bytes"]) == (set(), set()) for u in fake.uploads)


# ---------------------------------------------------------------------------
# AVIF and HEIC (#299)
#
# Two formats that were uploaded untouched after #288. They end up in opposite
# places, and the line between them is not "can it be stripped" but "can it be
# read": Pillow opens an AVIF, so it is stripped like the other four; nothing
# here opens a HEIC, so there is no informed choice to offer and it is refused
# rather than uploaded blind.
# ---------------------------------------------------------------------------

#: An ISO-BMFF ftyp box declaring a HEIC brand. Enough to be the file a draft
#: names and nothing more: every assertion below is about a file that is refused
#: or passed through untouched, so none of them decodes it.
HEIC = (
    b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"
    + b"\x00\x00\x00\x08mdat"
    + b"\x00" * 64
)

avif_only = pytest.mark.skipif(
    not features.check("avif"),
    reason="this Pillow build has no AVIF codec (its wheels cover every platform "
    "here, but not Windows ARM64 or iOS)",
)


@avif_only
class TestAvifIsStripped:
    def test_its_gps_block_does_not_reach_the_upload(self, tmp_path):
        raw = phone_photo(fmt="AVIF")
        assert read_exif(raw)[1]  # the fixture really has a GPS block
        result, fake = _push(tmp_path, "# T\n\n![A](a.avif)\n", files={"a.avif": raw})
        assert result["success"] is True
        sent = fake.uploads[0]["bytes"]
        assert read_exif(sent) == (set(), set())
        assert b"ACME" not in sent and b"Phone X" not in sent
        assert result["images_stripped"] == 1

    def test_a_rotated_one_is_rotated_in_the_pixels(self, tmp_path):
        raw = phone_photo(fmt="AVIF", orientation=6, size=(24, 16))
        _, fake = _push(tmp_path, "# T\n\n![A](a.avif)\n", files={"a.avif": raw})
        fmt, size, _ = _pixels(fake.uploads[0]["bytes"])
        assert (fmt, size) == ("AVIF", (16, 24))

    def test_the_icc_profile_survives(self, tmp_path):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        raw = phone_photo(fmt="AVIF", icc_profile=icc)
        _, fake = _push(tmp_path, "# T\n\n![A](a.avif)\n", files={"a.avif": raw})
        with Image.open(io.BytesIO(fake.uploads[0]["bytes"])) as im:
            assert im.info.get("icc_profile") == icc

    def test_the_listing_says_what_it_carried(self, tmp_path, capsys):
        (tmp_path / "a.avif").write_bytes(phone_photo(fmt="AVIF"))
        wp.print_image_plan(wp.plan_images("# T\n\n![A](a.avif)\n", tmp_path))
        out = capsys.readouterr().out
        assert "GPS location" in out and "stripped before upload" in out


class TestHeicIsRefused:
    """Not stripped and not sent. #288's own failure was uploading the format an
    iPhone writes by default without being able to say what was in it."""

    @pytest.mark.parametrize("name", ["shot.heic", "shot.heif", "SHOT.HEIC"])
    def test_it_stops_the_publish_before_anything_is_sent(self, tmp_path, name):
        result, fake = _push(tmp_path, f"# T\n\n![S]({name})\n", files={name: HEIC})
        assert result["success"] is False
        assert fake.requests == []  # not one request, let alone an upload
        assert "Export it as a JPEG" in result["error"]
        assert "--keep-image-metadata" in result["error"]

    def test_the_reason_says_why_rather_than_that_it_is_broken(self, tmp_path):
        """It is a sound file. Saying "cannot be opened as an image" -- what the
        unreadable path would say -- would send the author looking for damage."""
        (tmp_path / "s.heic").write_bytes(HEIC)
        plan = wp.plan_images("# T\n\n![S](s.heic)\n", tmp_path)
        (problem,) = plan.problems
        assert "no lossless way to re-save one" in problem
        assert "do not display a .heic" in problem
        assert "cannot be opened as an image" not in problem

    def test_the_opt_out_uploads_it_byte_for_byte_and_says_what_is_unknown(
        self, tmp_path, capsys
    ):
        (tmp_path / "s.heic").write_bytes(HEIC)
        plan = wp.plan_images("# T\n\n![S](s.heic)\n", tmp_path, strip_metadata=False)
        assert plan.problems == []
        wp.print_image_plan(plan)
        printed = capsys.readouterr().out
        assert "NOT stripped, and NOT readable either (.heic)" in printed
        assert "assume it records WHERE IT WAS TAKEN" in printed
        result, fake = _push(
            tmp_path,
            "# T\n\n![S](s.heic)\n",
            files={"s.heic": HEIC},
            strip_image_metadata=False,
        )
        assert result["success"] is True
        assert fake.uploads[0]["bytes"] == HEIC
        assert fake.uploads[0]["mime"] == "image/heic"
        assert result["images_stripped"] == 0

    def test_pillow_still_cannot_open_one(self):
        """The premise of the refusal. If a Pillow release (or a plugin arriving
        through another dependency) starts opening these, the refusal is no
        longer the honest answer and #299 is worth reopening."""
        with pytest.raises(Exception):
            Image.open(io.BytesIO(HEIC)).load()
        assert ".heic" not in Image.registered_extensions()


# ---------------------------------------------------------------------------
# Slug, excerpt and featured image (#315)
#
# Template C had no field for the three things a post needs to look right once
# it is shared or listed, so each was set by hand in WordPress after the push.
# Slug and excerpt are text: what matters is that they are sent only when the
# handoff set them, and that what WordPress kept is read back and not assumed.
# The featured image is a file that gets uploaded, so it is public from that
# moment, has to clear every check an image in the draft does before anything is
# sent, and then has to become the post's ``featured_media``.
# ---------------------------------------------------------------------------

#: Where the featured image is, in most of what follows.
HERO = "images/hero.jpg"

#: A post, which is the type WordPress gives an excerpt to. ``_push`` publishes
#: a page unless told otherwise.
POST = {"post_type": "post"}


def _push_featured(tmp_path, featured=HERO, files=None, md="# T\n\nBody.\n", **kw):
    """``wp.push`` of a post whose handoff has a ``Featured image:``.

    The file is a phone photo unless ``files`` says otherwise, so what leaves
    the process is something to inspect.
    """
    files = {HERO: phone_photo()} if files is None else files
    return _push(
        tmp_path,
        md,
        files=files,
        params={**POST, "featured_image": featured},
        **kw,
    )


class TestSlugAndExcerptAreSent:
    def test_both_are_in_the_create_payload(self, tmp_path):
        result, fake = _push(
            tmp_path,
            "# T\n\nBody.\n",
            params={
                **POST,
                "slug": "grid-report-2026",
                "excerpt": "Load peaked in August.",
            },
        )
        assert result["success"] is True
        assert fake.created[0]["slug"] == "grid-report-2026"
        assert fake.created[0]["excerpt"] == "Load peaked in August."

    def test_left_out_they_are_absent_and_not_empty(self, tmp_path):
        """An empty ``excerpt`` is a value WordPress takes literally, where an
        absent key lets it derive one as it always has."""
        _, fake = _push(
            tmp_path, "# T\n\nBody.\n", params={**POST, "slug": "", "excerpt": "   "}
        )
        assert not {"slug", "excerpt", "featured_media"} & set(fake.created[0])

    def test_a_handoff_that_sets_none_of_them_sends_what_it_always_did(self, tmp_path):
        _, fake = _push(tmp_path, "# T\n\nBody.\n", params=POST)
        assert set(fake.created[0]) == {
            "title",
            "content",
            "status",
            "categories",
            "tags",
        }

    def test_stray_whitespace_is_not_sent(self, tmp_path):
        _, fake = _push(
            tmp_path,
            "# T\n\nBody.\n",
            params={**POST, "slug": "  my-slug ", "excerpt": "  Words.  "},
        )
        assert fake.created[0]["slug"] == "my-slug"
        assert fake.created[0]["excerpt"] == "Words."

    def test_a_page_is_sent_them_too(self, tmp_path):
        """A page has a slug, and has an excerpt only if the site adds one.
        Which of those is true is WordPress's to say and not this adapter's: it
        is sent, and the answer is read (TestWhatWordPressKept)."""
        _, fake = _push(
            tmp_path, "# T\n\nBody.\n", params={"slug": "about", "excerpt": "Words."}
        )
        assert fake.created[0]["slug"] == "about"
        assert fake.created[0]["excerpt"] == "Words."
        assert "categories" not in fake.created[0], "still a page"


class TestWhatWordPressKept:
    """The answer to the create is read, and not the ``200``: a field the post
    type has not registered is dropped without an error, which is how this
    repo's Rank Math fields once vanished."""

    def _sent(self, tmp_path, fake=None, **params):
        return _push(tmp_path, "# T\n\nBody.\n", fake=fake, params={**POST, **params})

    def _answering(self, **body):
        fake = FakeWordPress()
        fake.post_response = Resp(
            200, {"id": 7, "link": "https://example.com/p/", **body}
        )
        return fake

    def test_a_slug_that_landed_is_reported_without_a_warning(self, tmp_path):
        result, _ = self._sent(tmp_path, slug="grid-report")
        assert result["post_fields"]["slug"] == {
            "requested": "grid-report",
            "stored": "grid-report",
        }
        assert "post_field_warnings" not in result

    def test_a_slug_wordpress_cleans_is_reported_both_ways(self, tmp_path):
        """The colon in a time is what ``sanitize_title`` drops: the case this
        field was added for. Not a warning, because the slug did land, but the
        one in the handoff is no longer the one on the site."""
        result, _ = self._sent(tmp_path, slug="Stuck at 0:00? Here")
        assert result["post_fields"]["slug"] == {
            "requested": "Stuck at 0:00? Here",
            "stored": "stuck-at-000-here",
        }
        assert "post_field_warnings" not in result

    def test_a_published_slug_that_is_taken_comes_back_with_a_suffix(self, tmp_path):
        fake = FakeWordPress()
        fake.taken_slugs = {"grid-report"}
        result, _ = _push(
            tmp_path,
            "# T\n\nBody.\n",
            fake=fake,
            params={**POST, "slug": "grid-report"},
            publish_live=True,
        )
        assert result["post_fields"]["slug"]["stored"] == "grid-report-2"

    def test_a_draft_slug_is_not_made_unique_until_it_is_published(self, tmp_path):
        """``wp_unique_post_slug`` returns early for a draft, so the ``-2`` a
        draft will get is not in this answer. The docs say so, rather than let
        a printed slug read as final."""
        fake = FakeWordPress()
        fake.taken_slugs = {"grid-report"}
        result, _ = self._sent(tmp_path, fake=fake, slug="grid-report")
        assert result["post_fields"]["slug"]["stored"] == "grid-report"

    def test_a_slug_wordpress_did_not_keep_warns(self, tmp_path):
        result, _ = self._sent(tmp_path, fake=self._answering(slug=""), slug="grid")
        assert result["success"] is True
        (warning,) = result["post_field_warnings"]
        assert "did not keep the slug" in warning

    def test_an_excerpt_that_landed_is_reported_without_a_warning(self, tmp_path):
        result, _ = self._sent(tmp_path, excerpt="Load peaked in August.")
        assert result["post_fields"]["excerpt"] == {
            "requested": "Load peaked in August.",
            "stored": "Load peaked in August.",
        }
        assert "post_field_warnings" not in result

    def test_a_page_has_no_excerpt_by_default_and_the_push_says_so(self, tmp_path):
        """Core's page type does not list ``excerpt`` in what it supports, so
        WordPress ignores the field and answers without one."""
        result, _ = _push(tmp_path, "# T\n\nBody.\n", params={"excerpt": "Words."})
        assert result["success"] is True
        (warning,) = result["post_field_warnings"]
        assert "did not keep the excerpt" in warning and "page" in warning
        assert result["post_fields"]["excerpt"]["stored"] is None

    def test_a_site_that_gives_pages_excerpts_keeps_it(self, tmp_path):
        fake = FakeWordPress()
        fake.pages_have_excerpts = True
        result, _ = _push(
            tmp_path, "# T\n\nBody.\n", fake=fake, params={"excerpt": "Words."}
        )
        assert "post_field_warnings" not in result

    def test_an_excerpt_that_came_back_different_is_reported(self, tmp_path):
        fake = self._answering(
            excerpt={"raw": "Something else.", "rendered": "<p>Something else.</p>"}
        )
        result, _ = self._sent(tmp_path, fake=fake, excerpt="Words.")
        (warning,) = result["post_field_warnings"]
        assert "'Something else.'" in warning and "not as written" in warning

    def test_wordpress_escaping_is_not_a_difference(self, tmp_path):
        """``kses`` stores an ampersand as ``&amp;``."""
        fake = self._answering(
            excerpt={"raw": "Load &amp; supply", "rendered": "<p>Load &amp; supply</p>"}
        )
        result, _ = self._sent(tmp_path, fake=fake, excerpt="Load & supply")
        assert "post_field_warnings" not in result

    def test_the_rendered_form_stands_in_when_there_is_no_raw(self, tmp_path):
        fake = self._answering(excerpt={"rendered": "<p>Words.</p>\n"})
        result, _ = self._sent(tmp_path, fake=fake, excerpt="Words.")
        assert "post_field_warnings" not in result

    def test_a_featured_image_that_landed_is_reported(self, tmp_path):
        result, _ = _push_featured(tmp_path)
        assert result["post_fields"]["featured_media"] == {
            "requested": 500,
            "stored": 500,
            "src": HERO,
        }
        assert "post_field_warnings" not in result

    def test_a_theme_without_thumbnails_drops_it_and_the_warning_names_the_upload(
        self, tmp_path
    ):
        fake = FakeWordPress()
        fake.thumbnails_supported = False
        result, _ = _push_featured(tmp_path, fake=fake)
        assert result["success"] is True
        (warning,) = result["post_field_warnings"]
        assert "did not set the featured image" in warning
        assert "media ID 500" in warning

    def test_a_handoff_that_asked_for_none_of_them_reports_none(self, tmp_path):
        """And a response with none of the fields in it is not a problem then."""
        fake = self._answering()
        result, _ = self._sent(tmp_path, fake=fake)
        assert "post_fields" not in result and "post_field_warnings" not in result


class TestFeaturedImage:
    def test_it_is_uploaded_and_becomes_the_posts_featured_media(self, tmp_path):
        result, fake = _push_featured(tmp_path)
        assert result["success"] is True
        (upload,) = fake.uploads
        assert upload["name"] == "hero.jpg" and upload["mime"] == "image/jpeg"
        assert fake.created[0]["featured_media"] == 500

    def test_it_is_in_the_media_library_before_the_post_exists(self, tmp_path):
        _, fake = _push_featured(tmp_path)
        urls = [u for _, u in fake.requests]
        media = next(i for i, u in enumerate(urls) if u.endswith("/media"))
        created = next(i for i, u in enumerate(urls) if u.endswith("/posts"))
        assert media < created

    def test_it_is_scrubbed_like_any_image_in_the_draft(self, tmp_path):
        raw = phone_photo(orientation=6)
        assert read_exif(raw)[1], "the fixture really has a GPS block"
        result, fake = _push_featured(tmp_path, files={HERO: raw})
        sent = fake.uploads[0]["bytes"]
        assert read_exif(sent) == (set(), set())
        assert b"ACME" not in sent and b"Phone X" not in sent
        assert _pixels(sent)[1] == (16, 24), "the rotation is in the pixels"
        assert (tmp_path / HERO).read_bytes() == raw, "the file on disk is untouched"
        assert result["images_stripped"] == 1
        assert result["image_uploads"][0]["stripped"] is True

    def test_keep_image_metadata_uploads_it_as_it_is(self, tmp_path):
        raw = phone_photo()
        _, fake = _push_featured(
            tmp_path, files={HERO: raw}, strip_image_metadata=False
        )
        assert fake.uploads[0]["bytes"] == raw

    def test_a_markdown_image_gives_the_media_item_its_alt_text(self, tmp_path):
        _, fake = _push_featured(
            tmp_path, featured=f"![A lineworker on a pole at dusk]({HERO})"
        )
        assert fake.uploads[0]["data"] == {"alt_text": "A lineworker on a pole at dusk"}

    def test_a_bare_path_sends_no_alt_text(self, tmp_path):
        _, fake = _push_featured(tmp_path)
        assert fake.uploads[0]["data"] == {}

    def test_it_goes_up_first_and_the_drafts_images_follow(self, tmp_path):
        result, fake = _push_featured(
            tmp_path,
            md="# T\n\n![Grid](grid.png)\n",
            files={HERO: phone_photo(), "grid.png": PNG},
        )
        assert [u["name"] for u in fake.uploads] == ["hero.jpg", "grid.png"]
        assert fake.created[0]["featured_media"] == 500
        assert 'class="wp-image-501"' in fake.created[0]["content"]
        featured, body = result["image_uploads"]
        assert featured["featured"] is True and featured["id"] == 500
        assert "featured" not in body, "a draft's own entries are as they were"

    def test_a_file_that_is_also_in_the_draft_is_uploaded_once(self, tmp_path):
        """Spelled two ways, which is one file."""
        result, fake = _push_featured(
            tmp_path,
            featured=f"./{HERO}",
            md=f"# T\n\n![Hero in the text]({HERO})\n",
        )
        assert len(fake.uploads) == 1
        assert fake.created[0]["featured_media"] == 500
        assert 'class="wp-image-500"' in fake.created[0]["content"]
        (entry,) = result["image_uploads"]
        assert entry["featured"] is True

    def test_the_featured_alt_text_is_the_one_the_media_item_keeps(self, tmp_path):
        """A media item has one alt text, and a theme reads the featured image's
        alt text from it. The block in the draft keeps its own."""
        _, fake = _push_featured(
            tmp_path,
            featured=f"![Featured alt]({HERO})",
            md=f"# T\n\n![Body alt]({HERO})\n",
        )
        assert fake.uploads[0]["data"] == {"alt_text": "Featured alt"}
        assert 'alt="Body alt"' in fake.created[0]["content"]

    def test_a_post_with_only_a_featured_image_still_reports_the_upload(self, tmp_path):
        result, _ = _push_featured(tmp_path)
        assert result["images_uploaded"] == 1 and result["images_linked"] == 0
        assert [u["src"] for u in result["image_uploads"]] == [HERO]

    def test_a_page_can_have_one(self, tmp_path):
        result, fake = _push(
            tmp_path,
            "# T\n\nBody.\n",
            files={HERO: phone_photo()},
            params={"featured_image": HERO},
        )
        assert result["success"] is True
        assert fake.created[0]["featured_media"] == 500

    def test_without_one_nothing_new_is_sent_or_reported(self, tmp_path):
        result, fake = _push(tmp_path, "# T\n\nBody.\n", params=POST)
        assert fake.media_requests == []
        assert "featured_media" not in fake.created[0]
        assert not [k for k in result if k.startswith(("image", "post_field"))]


class TestAFeaturedImageThatCannotBePublished:
    """The same rule as an image in the draft: nothing is sent, not even a term
    lookup, and the error names the image."""

    @pytest.mark.parametrize(
        "featured,files,expect",
        [
            (HERO, {}, "file not found"),
            ("report.pdf", {"report.pdf": b"%PDF-1.7"}, "not an image type"),
            ("images", {"images/x.png": PNG}, "folder"),
            ("shot.heic", {"shot.heic": HEIC}, "Export it as a JPEG"),
            ("https://cdn.example.com/hero.jpg", {}, "media library"),
            ("![alt]()", {}, "no source"),
            ("![a](data:image/png;base64,AAAA)", {}, "not a path to a file"),
            (f"![a]({HERO}) and some words", {HERO: PNG}, "single image"),
            (f"![a]({HERO}) ![b]({HERO})", {HERO: PNG}, "single image"),
        ],
        ids=[
            "missing",
            "not-an-image",
            "folder",
            "heic",
            "url",
            "no-source",
            "data-uri",
            "words-beside-it",
            "two-images",
        ],
    )
    def test_it_stops_the_publish_before_anything_is_sent(
        self, tmp_path, featured, files, expect
    ):
        result, fake = _push_featured(tmp_path, featured=featured, files=files)
        assert result["success"] is False
        assert fake.requests == [], "not even a term lookup"
        assert result["error"].startswith(
            "The handoff has image(s) that cannot be published, so nothing was sent"
        )
        assert "Featured image" in result["error"] and expect in result["error"]

    def test_a_url_is_refused_with_the_reason_and_the_way_out(self, tmp_path):
        result, _ = _push_featured(tmp_path, featured="https://cdn.example.com/h.jpg")
        error = " ".join(result["error"].split())
        assert "attachment ID" in error
        assert "give that path" in error

    def test_the_draft_is_not_blamed_for_it(self, tmp_path):
        """The old wording named the FINAL DRAFT, which is not where the line is."""
        result, _ = _push_featured(tmp_path, featured="missing.jpg")
        assert "Featured image:" in result["error"].rsplit("\n", 1)[-1]

    def test_a_refused_upload_stops_before_the_post_and_the_rest(self, tmp_path):
        fake = FakeWordPress()
        fake.fail_upload[1] = Resp(
            403, {"code": "rest_cannot_create", "message": "not allowed"}
        )
        result, _ = _push_featured(
            tmp_path,
            fake=fake,
            md="# T\n\n![Grid](grid.png)\n",
            files={HERO: phone_photo(), "grid.png": PNG},
        )
        assert result["success"] is False
        assert 'Featured image "images/hero.jpg"' in result["error"]
        assert "HTTP 403" in result["error"] and "upload_files" in result["error"]
        assert fake.post_requests == []
        assert fake.upload_attempts == 1, "the draft's image was never sent after it"

    def test_a_later_failure_names_it_among_what_was_left_behind(self, tmp_path):
        fake = FakeWordPress()
        fake.fail_upload[2] = Resp(500, {"code": "x", "message": "disk full"})
        result, _ = _push_featured(
            tmp_path,
            fake=fake,
            md="# T\n\n![Grid](grid.png)\n",
            files={HERO: phone_photo(), "grid.png": PNG},
        )
        assert result["success"] is False
        assert 'Image 1 of 1, "grid.png"' in result["error"]
        assert "ID 500 (images/hero.jpg)" in result["error"]
        assert "nothing was deleted" in result["error"]
        assert fake.post_requests == []

    def test_a_failed_post_still_names_the_upload(self, tmp_path):
        fake = FakeWordPress()
        fake.post_response = Resp(500, {"code": "internal", "message": "db error"})
        result, _ = _push_featured(tmp_path, fake=fake)
        assert result["success"] is False
        assert "ID 500 (images/hero.jpg)" in result["error"]

    def test_a_live_publish_refused_for_its_terms_uploads_nothing(self, tmp_path):
        class NoTerms(FakeWordPress):
            def get(self, url, **kw):
                self.requests.append(("GET", url))
                return Resp(200, [])

        fake = NoTerms()
        result, _ = _push(
            tmp_path,
            "# T\n\nBody.\n",
            fake=fake,
            files={HERO: phone_photo()},
            params={**POST, "wordpress_category": "nope", "featured_image": HERO},
            publish_live=True,
        )
        assert result["success"] is False
        assert "unresolved taxonomy terms" in result["error"]
        assert fake.media_requests == [] and fake.uploads == []


class TestPlanningTheFeaturedImage:
    def _file(self, tmp_path, name="hero.jpg", data=None):
        (tmp_path / name).write_bytes(phone_photo() if data is None else data)

    def test_it_is_held_apart_from_the_drafts_own_images(self, tmp_path):
        self._file(tmp_path)
        self._file(tmp_path, "a.png", PNG)
        plan = wp.plan_images("![A](a.png)", tmp_path, featured_image="hero.jpg")
        assert plan.problems == []
        assert plan.featured.src == "hero.jpg"
        assert [r.src for r in plan.refs] == ["a.png"], "the numbering does not move"
        assert set(plan.uploads) == {"a.png", "hero.jpg"}

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_a_blank_value_is_no_featured_image(self, tmp_path, value):
        plan = wp.plan_images("# T", tmp_path, featured_image=value)
        assert plan.featured is None and plan.problems == [] and plan.uploads == {}

    def test_planning_sends_nothing(self, tmp_path):
        self._file(tmp_path)
        with (
            patch("ci_article_review.adapters.cms.wordpress.requests.post") as post,
            patch("ci_article_review.adapters.cms.wordpress.requests.get") as get,
        ):
            wp.plan_images("# T", tmp_path, featured_image="hero.jpg")
        assert not post.called and not get.called

    def test_its_problems_come_first_and_are_named_apart_from_the_drafts(
        self, tmp_path
    ):
        plan = wp.plan_images("![b](gone.png)", tmp_path, featured_image="missing.jpg")
        assert plan.problems[0].startswith(
            'Featured image "missing.jpg": file not found'
        )
        assert plan.problems[1].startswith('Image 1 of 1, "gone.png"')

    def test_a_url_is_not_planned_as_an_upload_or_a_link(self, tmp_path):
        plan = wp.plan_images(
            "# T", tmp_path, featured_image="https://cdn.example.com/hero.jpg"
        )
        (problem,) = plan.problems
        assert "attachment ID" in problem
        assert plan.featured is None and plan.uploads == {}

    def test_a_caption_is_not_used_and_says_so(self, tmp_path):
        self._file(tmp_path)
        plan = wp.plan_images(
            "# T", tmp_path, featured_image='![Alt](hero.jpg "Photo: someone")'
        )
        assert plan.problems == []
        (warning,) = plan.warnings
        assert (
            warning.startswith('Featured image "hero.jpg"') and "no caption" in warning
        )

    def test_no_alt_text_is_a_warning_and_not_a_problem(self, tmp_path):
        self._file(tmp_path)
        plan = wp.plan_images("# T", tmp_path, featured_image="hero.jpg")
        assert plan.problems == []
        (warning,) = plan.warnings
        assert warning.startswith('Featured image "hero.jpg"')
        assert "no alt text" in warning and "![alt text](images/hero.jpg)" in warning

    def test_alt_text_means_no_warning(self, tmp_path):
        self._file(tmp_path)
        plan = wp.plan_images(
            "# T", tmp_path, featured_image="![Dam at dusk](hero.jpg)"
        )
        assert plan.warnings == []

    def test_a_location_it_will_keep_is_called_out_by_name(self, tmp_path):
        self._file(tmp_path)
        plan = wp.plan_images(
            "# T", tmp_path, strip_metadata=False, featured_image="hero.jpg"
        )
        (warning,) = [w for w in plan.warnings if "WHERE IT WAS TAKEN" in w]
        assert warning.startswith('Featured image "hero.jpg"')

    def test_a_path_with_spaces_needs_no_escaping_when_it_is_bare(self, tmp_path):
        self._file(tmp_path, "my photo.jpg")
        plan = wp.plan_images("# T", tmp_path, featured_image="my photo.jpg")
        assert plan.problems == [] and plan.featured.src == "my photo.jpg"

    def test_the_markdown_form_takes_angle_brackets_like_the_drafts(self, tmp_path):
        self._file(tmp_path, "my photo.jpg")
        plan = wp.plan_images("# T", tmp_path, featured_image="![Dam](<my photo.jpg>)")
        assert plan.problems == [] and plan.featured.src == "my photo.jpg"
        assert plan.featured.alt == "Dam"

    def test_an_absolute_path_is_taken_as_the_system_writes_it(self, tmp_path):
        """On Windows that has backslashes in it, which Markdown would read as
        escapes: the reason a bare value is not run through Markdown."""
        self._file(tmp_path)
        plan = wp.plan_images(
            "# T", tmp_path / "elsewhere", featured_image=str(tmp_path / "hero.jpg")
        )
        assert plan.problems == [] and plan.featured is not None


class TestWhatTheAuthorSeesOfThem:
    def test_the_list_leads_with_the_featured_image(self, tmp_path, capsys):
        (tmp_path / "hero.jpg").write_bytes(phone_photo())
        (tmp_path / "a.png").write_bytes(PNG)
        plan = wp.plan_images(
            "![A](a.png)", tmp_path, featured_image="![Dam at dusk](hero.jpg)"
        )
        wp.print_image_plan(plan)
        out = capsys.readouterr().out
        assert out.index("Featured image:") < out.index("In the post:")
        assert out.index("In the post:") < out.index("1. UPLOAD")
        assert f"UPLOAD  {tmp_path / 'hero.jpg'}" in out
        assert "alt:     Dam at dusk" in out
        assert "GPS location (8 tags)" in out and "stripped before upload" in out
        assert "public from the moment" in out

    def test_a_featured_image_alone_still_prints_the_list(self, tmp_path, capsys):
        (tmp_path / "hero.jpg").write_bytes(phone_photo())
        wp.print_image_plan(
            wp.plan_images("# T\n\nText.", tmp_path, featured_image="hero.jpg")
        )
        out = capsys.readouterr().out
        assert "IMAGES" in out and "Featured image:" in out
        assert "In the post:" not in out
        assert "public from the moment" in out
        assert "alt:     (none)" in out

    def test_with_none_the_list_reads_as_it_did(self, tmp_path, capsys):
        (tmp_path / "a.png").write_bytes(PNG)
        wp.print_image_plan(wp.plan_images("![A](a.png)", tmp_path))
        out = capsys.readouterr().out
        assert "Featured image" not in out and "In the post" not in out

    def test_keep_image_metadata_is_said_to_cover_it_too(self, tmp_path, capsys):
        (tmp_path / "hero.jpg").write_bytes(phone_photo())
        plan = wp.plan_images(
            "# T", tmp_path, strip_metadata=False, featured_image="hero.jpg"
        )
        wp.print_image_plan(plan)
        out = capsys.readouterr().out
        assert "KEPT (--keep-image-metadata)" in out
        assert "records WHERE IT WAS TAKEN" in out and "nothing is stripped" in out

    def test_the_result_marks_the_featured_upload(self, capsys):
        wp.print_image_result(
            {
                "images_uploaded": 3,
                "images_linked": 0,
                "image_uploads": [
                    {
                        "src": "h.jpg",
                        "id": 5,
                        "url": "u",
                        "stripped": True,
                        "featured": True,
                    },
                    {"src": "a.png", "id": 6, "url": "u", "stripped": False},
                    {
                        "src": "k.jpg",
                        "id": 7,
                        "url": "u",
                        "stripped": False,
                        "featured": True,
                    },
                ],
            }
        )
        out = capsys.readouterr().out
        assert "h.jpg -> media ID 5  (featured image)" in out
        assert "a.png -> media ID 6  (metadata NOT stripped)" in out
        assert "k.jpg -> media ID 7  (featured image; metadata NOT stripped)" in out

    def test_the_checklist_asks_about_all_three(self):
        checklist = " ".join(wp.CHECKLIST.split())
        assert 'the handoff\'s "Featured image:" line' in checklist
        assert 'the handoff\'s "Slug:" and "Excerpt:" lines' in checklist
        assert "public from the moment it is uploaded" in checklist
        assert "listed under IMAGES above" in checklist


class TestPrintingWhatLanded:
    def _print(self, capsys, **result):
        wp.print_post_fields_result({"success": True, **result})
        return capsys.readouterr().out

    def test_a_handoff_that_set_none_prints_nothing(self, capsys):
        assert self._print(capsys) == ""

    def test_the_slug_is_the_one_wordpress_answered_with(self, capsys):
        slug = {"requested": "grid-report", "stored": "grid-report"}
        assert (
            self._print(capsys, post_fields={"slug": slug}) == "Slug:     grid-report\n"
        )

    def test_a_changed_slug_shows_what_the_handoff_asked_for(self, capsys):
        slug = {"requested": "Stuck at 0:00?", "stored": "stuck-at-000"}
        out = self._print(capsys, post_fields={"slug": slug})
        assert (
            out == "Slug:     stuck-at-000  (the handoff asked for 'Stuck at 0:00?')\n"
        )

    def test_a_slug_that_did_not_land_prints_none_and_the_warning(self, capsys):
        out = self._print(
            capsys,
            post_fields={"slug": {"requested": "x", "stored": ""}},
            post_field_warnings=["WordPress did not keep the slug."],
        )
        assert "Slug:     (none)" in out
        assert "WARNING: WordPress did not keep the slug." in out

    def test_the_excerpt_is_shown_and_a_long_one_is_cut(self, capsys):
        short = self._print(
            capsys, post_fields={"excerpt": {"requested": "Words.", "stored": "Words."}}
        )
        assert short == "Excerpt:  Words.\n"
        long = "word " * 80
        cut = self._print(
            capsys, post_fields={"excerpt": {"requested": long, "stored": long}}
        )
        assert cut.endswith("...\n") and len(cut) < 230

    def test_a_dropped_excerpt_prints_no_line_of_its_own(self, capsys):
        out = self._print(
            capsys,
            post_fields={"excerpt": {"requested": "Words.", "stored": None}},
            post_field_warnings=["WordPress did not keep the excerpt."],
        )
        assert not out.startswith("Excerpt:") and "Excerpt:  " not in out
        assert "WARNING: WordPress did not keep the excerpt." in out

    def test_the_featured_line_names_the_media_id_and_the_source(self, capsys):
        featured = {"requested": 500, "stored": 500, "src": "images/hero.jpg"}
        out = self._print(capsys, post_fields={"featured_media": featured})
        assert out == "Featured: media ID 500 (images/hero.jpg)\n"

    def test_a_featured_image_that_did_not_land_prints_only_the_warning(self, capsys):
        featured = {"requested": 500, "stored": 0, "src": "images/hero.jpg"}
        out = self._print(
            capsys,
            post_fields={"featured_media": featured},
            post_field_warnings=["WordPress did not set the featured image."],
        )
        assert "Featured:" not in out
        assert "WARNING: WordPress did not set the featured image." in out


class TestPublishCommandPostFields:
    PARAMS = (
        "Slug: grid-report-2026\n"
        "Excerpt: Load peaked in August.\n"
        f"Featured image: ![Dam at dusk]({HERO})\n"
    )

    def test_all_three_reach_the_post_and_are_printed_next_to_it(
        self, tmp_path, capsys
    ):
        code, fake, _ = _run_publish(
            tmp_path,
            "Just words.",
            files={HERO: phone_photo()},
            params=self.PARAMS,
            post_type="post",
        )
        out = capsys.readouterr().out
        created = fake.created[0]
        assert code is None
        assert created["slug"] == "grid-report-2026"
        assert created["excerpt"] == "Load peaked in August."
        assert created["featured_media"] == 500
        assert fake.uploads[0]["data"] == {"alt_text": "Dam at dusk"}
        assert "Slug:     grid-report-2026" in out
        assert "Excerpt:  Load peaked in August." in out
        assert "Featured: media ID 500 (images/hero.jpg)" in out
        assert "images/hero.jpg -> media ID 500  (featured image)" in out
        assert out.index("Post ID:") < out.index("Slug:") < out.index("Images:")

    def test_the_featured_image_is_listed_before_the_checklist_asks(
        self, tmp_path, capsys
    ):
        seen = []

        def confirm():
            seen.append(capsys.readouterr().out)
            return True

        _run_publish(
            tmp_path,
            "Just words.",
            files={HERO: phone_photo()},
            params=self.PARAMS,
            post_type="post",
            on_confirm=confirm,
        )
        assert "Featured image:" in seen[0] and "UPLOAD" in seen[0]
        assert "public from the moment" in seen[0]

    def test_a_bad_featured_image_stops_before_the_seo_call_and_the_checklist(
        self, tmp_path, caplog
    ):
        with caplog.at_level(logging.ERROR):
            code, fake, steps = _run_publish(
                tmp_path,
                "Just words.",
                params="Featured image: images/nope.jpg\n",
                post_type="post",
            )
        assert code == 1
        assert 'Featured image "images/nope.jpg"' in caplog.text
        assert "file not found" in caplog.text
        assert fake.requests == []
        assert not steps["suggest"].called, "before the SEO call is paid for"
        assert not steps["confirm"].called, "before the checklist asks for a yes"

    def test_an_excerpt_the_site_drops_is_warned_about_next_to_the_post(
        self, tmp_path, capsys
    ):
        """A page, which has no excerpt unless the site adds one."""
        code, _, _ = _run_publish(tmp_path, "Just words.", params="Excerpt: Words.\n")
        out = capsys.readouterr().out
        assert code is None
        assert "WARNING: WordPress did not keep the excerpt" in out
        assert not [x for x in out.splitlines() if x.startswith("Excerpt:")]

    def test_a_handoff_without_them_prints_none_of_the_new_lines(
        self, tmp_path, capsys
    ):
        _run_publish(tmp_path, "Just words.", post_type="post")
        out = capsys.readouterr().out
        assert "WordPress push successful." in out
        assert not [
            x
            for x in out.splitlines()
            if x.startswith(("Slug:", "Excerpt:", "Featured"))
        ]

    def test_an_unfilled_placeholder_sets_none_of_them(self, tmp_path):
        """As the template ships: bracketed, and wrapped onto further lines."""
        params = (
            "Slug: [optional. The last part of the web address:\n"
            "joined by hyphens.]\n"
            "Excerpt: [optional]\n"
            "Featured image: [optional. A path to an image file,\n"
            "relative to THIS handoff.]\n"
        )
        code, fake, _ = _run_publish(
            tmp_path, "Just words.", params=params, post_type="post"
        )
        assert code is None
        assert not {"slug", "excerpt", "featured_media"} & set(fake.created[0])
        assert fake.media_requests == []


TEMPLATE = Path(handoff_parser.__file__).parent / "handoff_templates" / "publication.md"


def _images_section():
    text = TEMPLATE.read_text(encoding="utf-8")
    return text.split("\nIMAGES AND ALT TEXT\n", 1)[1].split("\nDISPOSITION LOG\n", 1)[
        0
    ]


def _parameters_section():
    text = TEMPLATE.read_text(encoding="utf-8")
    return text.split("\nPUBLICATION PARAMETERS\n", 1)[1].split("\nSEO METADATA\n", 1)[
        0
    ]


class TestTheTemplateOffersSlugExcerptAndFeaturedImage:
    @pytest.mark.parametrize("label", ["Slug:", "Excerpt:", "Featured image:"])
    def test_each_is_offered_once_as_a_placeholder(self, label):
        """Once, so the first line with the label is the field and a note never
        is; a placeholder, so a copy left unfilled reads as unset and publishes
        as such posts always have."""
        lines = [x for x in _parameters_section().splitlines() if x.startswith(label)]
        assert len(lines) == 1
        assert lines[0][len(label) :].strip().startswith("[")

    def test_an_unfilled_copy_sets_none_of_them(self):
        parsed = parse_publication_handoff(TEMPLATE.read_text(encoding="utf-8"))
        params = parsed["publication_parameters"]
        assert (params["slug"], params["excerpt"], params["featured_image"]) == (
            "",
            "",
            "",
        )

    def test_the_markdown_form_it_shows_is_one_the_push_reads(self, tmp_path):
        """A documented syntax that does not parse is worse than none. The
        example is taken from the template itself, so rewording it there is
        checked here."""
        line = next(x for x in _parameters_section().splitlines() if "![" in x)
        example = line[line.index("![") :]
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "hero.jpg").write_bytes(phone_photo())
        plan = wp.plan_images("# T", tmp_path, featured_image=example)
        assert plan.problems == []
        assert plan.featured.alt == "Alt text: what it shows"

    def test_it_says_a_url_is_refused_and_that_the_upload_is_public(self):
        section = " ".join(_parameters_section().split())
        assert "an https:// URL is refused" in section
        assert "public the moment it is uploaded" in section

    def test_the_images_note_covers_the_featured_image_too(self):
        note = " ".join(_images_section().split())
        assert "The Featured image: line" in note
        assert "public as soon as it is uploaded" in note
        assert "an https:// URL is refused" in note


class TestTheTemplateDocumentsIt:
    def test_the_section_exists(self):
        assert "IMAGES AND ALT TEXT" in TEMPLATE.read_text(encoding="utf-8")

    def test_it_says_metadata_is_stripped_and_that_pixels_are_not(self):
        """#288: the strip is a default the author should know about, and a photo
        of a screen can show a location that no strip touches."""
        section = _images_section()
        assert "scrubbed copy" in section
        assert "GPS" in section
        assert "photo of a screen" in section
        assert "coordinates" in section
        assert "IMAGES AND ALT TEXT" in handoff_parser.PUB_HEADERS

    def test_its_example_is_an_image_the_publish_actually_reads(self):
        """A documented syntax that does not parse is worse than none. The
        example is taken from the template itself, so rewording it there is
        checked here."""
        example = next(
            line for line in _images_section().splitlines() if line.startswith("![")
        )
        (ref,) = blocks.find_images(example)
        assert ref.placement == img.BLOCK
        assert ref.kind == img.LOCAL
        assert ref.alt and ref.caption

    def test_the_angle_bracket_form_it_offers_works(self):
        line = next(line for line in _images_section().splitlines() if "(<" in line)
        start = line.index("![")
        (ref,) = blocks.find_images(line[start:])
        assert ref.src == "my photo.png"

    def test_the_note_stays_out_of_the_embeds_section(self):
        """The section above it must end where it does. Without the header in
        PUB_HEADERS, EMBEDS would carry the whole note along. (It does mention
        the header once, deliberately: that line points the reader here.)"""
        text = TEMPLATE.read_text(encoding="utf-8")
        embeds = parse_publication_handoff(text)["embeds"]
        assert "A note, not a form" not in embeds
        assert "![" not in embeds
        assert "\nIMAGES AND ALT TEXT\n" not in f"\n{embeds}\n"

    def test_the_embeds_section_says_images_are_not_embeds(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        embeds = text.split("\nEMBEDS AND SPECIAL ELEMENTS\n", 1)[1].split(
            "\nIMAGES AND ALT TEXT\n", 1
        )[0]
        assert "Images are NOT embeds" in embeds

    def test_the_note_never_reaches_the_published_article(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        parsed = parse_publication_handoff(text)
        assert "IMAGES AND ALT TEXT" not in parsed["final_draft"]
        assert "alt text" not in parsed["final_draft"].lower()

    def test_a_handoff_with_an_image_line_keeps_it_in_the_final_draft(self):
        handoff = _HANDOFF.format(image="![Grid](grid.png)")
        assert "![Grid](grid.png)" in parse_publication_handoff(handoff)["final_draft"]

    def test_an_older_handoff_without_the_section_parses_as_before(self):
        parsed = parse_publication_handoff(_HANDOFF.format(image="Text."))
        assert parsed["title"] == "Grid Report"
        assert parsed["final_draft"].startswith("# Grid Report")

    def test_the_documented_backslash_warning_is_true(self):
        """The template says C:\\photos\\_map.png reaches the script as
        C:\\photos_map.png. If Markdown ever stops doing that, so should this."""
        assert "reaches the script as C:\\photos_map.png" in _images_section()
        (ref,) = blocks.find_images(r"![a](C:\photos\_map.png)")
        assert ref.src == "C:\\photos_map.png"

    def test_ci_setup_copies_the_template_unchanged_in_kind(self):
        """The section is a bracketed note, like the others, so a filled-in
        handoff that leaves it in place is harmless."""
        section = _images_section().strip()
        assert section.startswith("[") and section.endswith("]")
