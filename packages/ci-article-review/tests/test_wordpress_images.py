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
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

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
        self.post_response = Resp(200, {"id": 7, "link": "https://example.com/p/"})

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
        return self.post_response

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


def _push(tmp_path, markdown, fake=None, files=None, **kw):
    """Run ``wp.push`` against the fake, with ``files`` (name -> bytes) on disk."""
    fake = fake or FakeWordPress()
    for name, data in (files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pub_params = {"title": "Grid Report", "post_type": "page", "tags": []}
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
        assert upload["bytes"] == PNG

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
            }
        ]

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


def _run_publish(tmp_path, image_line, fake=None, files=None, handoff_dir=None):
    """``run_publish_pipeline`` on a handoff in ``handoff_dir``, network faked.

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
    path.write_text(_HANDOFF.format(image=image_line), encoding="utf-8")
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
        patch(f"{wp_module}.print_checklist_and_confirm", return_value=True) as confirm,
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
        assert fake.uploads[0]["bytes"] == PNG

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

TEMPLATE = Path(handoff_parser.__file__).parent / "handoff_templates" / "publication.md"


def _images_section():
    text = TEMPLATE.read_text(encoding="utf-8")
    return text.split("\nIMAGES AND ALT TEXT\n", 1)[1].split("\nDISPOSITION LOG\n", 1)[
        0
    ]


class TestTheTemplateDocumentsIt:
    def test_the_section_exists(self):
        assert "IMAGES AND ALT TEXT" in TEMPLATE.read_text(encoding="utf-8")
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
