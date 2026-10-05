import base64
import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

import requests

from ci_core import redact

from . import blocks, images

log = logging.getLogger(__name__)

#: Read timeout for one image upload. The post itself gets 60s; a photograph over
#: a slow uplink is a bigger body than any post, and a timeout here fails the
#: publish over an upload that was merely slow.
_UPLOAD_TIMEOUT = 120

CHECKLIST = """
PRE-PUBLICATION CHECKLIST
==========================
CONTENT
[ ] All consensus flags addressed or explicitly dismissed with reasoning
[ ] All factual claims have primary source citations
[ ] All citations have SHA-256 checksums recorded in pipeline_history/
[ ] Pre-draft analysis counterargument dispositions reflected in final draft

TECHNICAL
[ ] All embedded components tested (charts, maps, interactive elements)
[ ] Data in visualizations matches claims in prose
[ ] Structured data schema validates (https://validator.schema.org)
[ ] All images have alt text (any the script found without are listed under IMAGES above)
[ ] Internal links reviewed and functional
[ ] Canonical URL set correctly in WordPress

SEO
[ ] Focus keyword set in Rank Math
[ ] Meta description under 155 characters
[ ] OG tags set
[ ] Featured image set: it is the share card (og:image) and the archive thumbnail
    (the handoff's "Featured image:" line; listed under IMAGES above when there is
    one, and public from the moment it is uploaded)
[ ] Schema type correct for content type (set in Rank Math, not by this script)

PUBLICATION
[ ] WordPress category correct
[ ] Tags applied
[ ] Slug and excerpt are what you want (the handoff's "Slug:" and "Excerpt:"
    lines; WordPress makes both up when they are left out)
[ ] Status is draft (default) -- confirm before switching to live
[ ] UpdraftPlus backup is current
"""


def _auth_header(username, application_password):
    token = base64.b64encode(f"{username}:{application_password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _require_https(site_url, wp_config):
    """Refuse to send an application password over cleartext HTTP.

    Basic auth is base64, which is encoding, not encryption — anyone on the path
    reads the credential verbatim. A WordPress application password grants
    post-creation rights on a live site, and it is sent on every category lookup
    as well as the publish itself.

    ``allow_insecure_http: true`` exists for a local WordPress on the loopback
    interface, which is a real development case. It has to be set deliberately.
    """
    if site_url.lower().startswith("https://"):
        return
    if wp_config.get("allow_insecure_http"):
        log.warning(
            "WordPress site_url is not HTTPS and allow_insecure_http is set — "
            "the application password will be sent in cleartext."
        )
        return
    raise ValueError(
        f"Refusing to send WordPress credentials over an insecure URL: {site_url}\n"
        "Basic auth is base64-encoded, not encrypted, so an application password "
        "sent over http:// is readable by anything on the network path.\n"
        "Fix wordpress.site_url to use https://, or set "
        "wordpress.allow_insecure_http: true if this is a local test site."
    )


def _lookup_term_ids(api_base, headers, taxonomy, items):
    """Convert category/tag slugs (strings) or IDs (ints) to WP term IDs.

    WordPress REST API requires integer IDs when creating/updating posts.
    String slugs are resolved via a GET request to the taxonomy endpoint.
    Integer IDs are passed through unchanged.

    Args:
        api_base:  Base URL for WP REST API, e.g. ``https://site.com/wp-json/wp/v2``
        headers:   Auth headers dict (already built by caller)
        taxonomy:  ``"categories"`` or ``"tags"``
        items:     List of slugs (str) or IDs (int), or a single slug string

    Returns:
        ``(resolved_ids, unresolved_slugs)``. Unresolved slugs are returned
        rather than only logged: a post created with an empty categories array
        is an editorial failure, and it used to be reported as a clean success
        with the explanation buried in a warning above the success output.
    """
    if not items:
        return [], []

    # Accept a single string or a list
    if isinstance(items, (str, int)):
        items = [items]

    resolved = []
    unresolved = []
    for item in items:
        if isinstance(item, int):
            resolved.append(item)
            continue
        if not isinstance(item, str) or not item.strip():
            continue
        slug = item.strip()
        try:
            resp = requests.get(
                f"{api_base}/{taxonomy}",
                params={"slug": slug},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            terms = resp.json()
            if terms:
                resolved.append(terms[0]["id"])
                log.debug(f"Resolved {taxonomy} slug '{slug}' → ID {terms[0]['id']}")
            else:
                unresolved.append(slug)
                log.warning(
                    f"WordPress {taxonomy} slug '{slug}' not found — "
                    f"create it in WP admin first, or use its integer ID."
                )
        except Exception as e:
            unresolved.append(slug)
            log.warning(f"WordPress {taxonomy} lookup failed for '{slug}': {e}")

    return resolved, unresolved


#: WordPress core content types this adapter can create, mapped to their REST
#: route. Both are "posts" in WordPress's internal vocabulary, but they are
#: different endpoints with different fields: only ``post`` carries categories
#: and tags. Anything outside this map is rejected rather than passed through —
#: a typo ("pages", "Post ") would otherwise POST the article to a URL that
#: does not exist, and the failure would surface as a bare 404 from the REST
#: API rather than as the handoff error it actually is.
POST_TYPE_ROUTES = {"post": "posts", "page": "pages"}

#: Fields the ``page`` type does not accept. WordPress pages are not in the
#: category/tag taxonomies at all, so sending these is not merely redundant —
#: the REST API rejects unregistered fields on a route that does not declare
#: them.
_TAXONOMY_FIELDS = ("categories", "tags")


def resolve_post_type(raw):
    """Return the normalised post type for a handoff's ``Post type:`` value.

    Empty or missing means ``post``: every publication handoff written before
    this field existed is an article, and must keep publishing as one.
    """
    value = (raw or "").strip().lower() or "post"
    if value not in POST_TYPE_ROUTES:
        raise ValueError(
            f"Unknown post type {value!r} in the publication handoff. "
            f"Supported: {', '.join(sorted(POST_TYPE_ROUTES))}."
        )
    return value


def _build_post_payload(
    pub_params,
    wp_config,
    rank_math_config,
    content,
    category_ids,
    tag_ids,
    post_type="post",
    featured_media=None,
):
    payload = {
        "title": pub_params.get("title", ""),
        "content": content,
        "status": "draft",  # always draft unless --publish-live
        "categories": category_ids,
        "tags": tag_ids,
    }

    # A page has no taxonomy. Build the common payload first and strip, rather
    # than branching the whole dict, so a field added for posts later cannot be
    # silently missing from pages.
    if post_type == "page":
        for field in _TAXONOMY_FIELDS:
            payload.pop(field, None)

    author = pub_params.get("author")
    if author:
        payload["author"] = author

    # Slug, excerpt and featured image are optional and are left out of the
    # payload, not sent empty, when the handoff did not set them: an absent key
    # is what lets WordPress derive the slug from the title and the excerpt from
    # the opening words of the body, as it always has, and a handoff written
    # before these fields existed publishes exactly the post it did.
    #
    # They go on pages as well. A page has a slug and a featured image by
    # default but no excerpt unless the site adds one, and WordPress drops an
    # unregistered field without an error, so what it kept is read back from
    # its answer (_confirm_post_fields) rather than decided here for a site this
    # cannot see.
    slug = (pub_params.get("slug") or "").strip()
    if slug:
        payload["slug"] = slug
    excerpt = (pub_params.get("excerpt") or "").strip()
    if excerpt:
        payload["excerpt"] = excerpt
    # An attachment ID, which is the only thing ``featured_media`` takes: the
    # image has to be in this site's media library already, which is why it is
    # uploaded first (upload_images).
    if featured_media:
        payload["featured_media"] = featured_media

    # Rank Math SEO meta fields
    meta = {}
    # Rank Math's fields are deliberately NOT set here. They were, via
    # payload["meta"], and WordPress discarded every one of them: Rank Math
    # does not register its meta keys with the core REST API, and core drops
    # unregistered keys from a meta payload without erroring. A live page
    # pushed that way came back with meta == ["footnotes"] and no SEO fields
    # at all, while the publish reported success. They are set after creation
    # instead, against Rank Math's own endpoint — see rank_math_meta() and
    # _apply_rank_math_meta().
    if meta:
        payload["meta"] = meta

    return payload


def rank_math_meta(pub_params, rank_math_config):
    """Return the Rank Math meta fields for a handoff's SEO METADATA block.

    Keys are Rank Math's own post-meta names, verified against a live install.
    ``rank_math_title`` is the one that sets the ``<title>`` tag, which is what
    makes a short post title (an About page's "About") publishable without
    tripping a search-snippet length check: the SEO title is a separate field
    from the title the theme renders.

    Schema type is absent on purpose. Rank Math stores schema through its
    ``updateSchemas`` endpoint rather than a meta key; the plausible-looking
    ``rank_math_rich_snippet`` was tried against a live install and did not
    change the rendered schema, so setting it would only look like it worked.
    Set schema in Rank Math's Schema tab.
    """
    seo = pub_params.get("seo", {}) or {}
    title = pub_params.get("title", "")
    focus_keyword = seo.get("focus_keyword")
    meta_description = seo.get("meta_description")
    og_title = seo.get("og_title") or title
    og_description = seo.get("og_description") or meta_description
    # An explicit SEO title wins; otherwise the OG title does, since a handoff
    # that bothered to write one meant it to be the search-facing title.
    seo_title = seo.get("seo_title") or og_title

    meta = {}
    if focus_keyword:
        meta["rank_math_focus_keyword"] = focus_keyword
    if seo_title:
        meta["rank_math_title"] = seo_title
    if meta_description:
        meta["rank_math_description"] = meta_description
    if rank_math_config.get("auto_set_og_tags"):
        if og_title:
            meta["rank_math_facebook_title"] = og_title
            meta["rank_math_twitter_title"] = og_title
        if og_description:
            meta["rank_math_facebook_description"] = og_description
            meta["rank_math_twitter_description"] = og_description
    return meta


def _apply_rank_math_meta(site_url, headers, post_id, meta):
    """POST the Rank Math fields to its own endpoint. Never fails the publish.

    The post already exists by the time this runs, so a failure here costs the
    SEO fields, not the article. It is reported rather than raised, and the
    caller surfaces it next to the success line — a silently SEO-less post is
    the failure this whole function exists to stop repeating.
    """
    if not meta:
        return None
    try:
        resp = requests.post(
            f"{site_url}/wp-json/rankmath/v1/updateMeta",
            headers=headers,
            json={"objectType": "post", "objectID": post_id, "meta": meta},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        detail = redact.capture_error_body(e) or redact.redact_url_keys(str(e))
        log.warning(f"Rank Math meta not applied: {detail}")
        return str(detail)
    except Exception as e:  # network, DNS, timeout
        log.warning(f"Rank Math meta not applied: {e}")
        return str(e)
    log.info(f"Rank Math meta applied: {', '.join(sorted(meta))}")
    return None


# ---------------------------------------------------------------------------
# Images
#
# An image alone on a line of the draft becomes a core/image block. One with an
# http(s) source is already hosted, and the block points at it. One with a path
# is a file on the author's disk, and is uploaded to the media library first,
# because the block has to point at where the image now lives. All of it that
# can be checked without a request is checked first (plan_images), so a mistyped
# path fails before anything has been sent rather than after an earlier image
# has already gone up.
#
# The handoff's "Featured image:" line goes through all of that with them: it is
# planned before anything is sent, scrubbed, listed before the author says yes,
# and uploaded. It differs in one way. A post's featured image is its
# ``featured_media``, which WordPress takes as an attachment ID, so there is no
# "already hosted" form to link to: it has to be a file, and it is uploaded.
# ---------------------------------------------------------------------------


@dataclass
class ImagePlan:
    """What the draft's images need, worked out without a single request."""

    #: Every image the draft contains, in document order.
    refs: list = field(default_factory=list)
    #: The image the handoff's ``Featured image:`` line names, once it has passed
    #: every check, else None. It is not in ``refs``: those are numbered in
    #: document order in every message ("Image 2 of 4") and this one is not in the
    #: document. Its file is in ``uploads`` and ``metadata`` under its source as
    #: written, like theirs, so the strip, the listing and the upload all treat
    #: it the way they treat the others.
    featured: images.ImageRef | None = None
    #: Source as written -> the file on disk, for each local image that becomes
    #: a block, and for the featured image.
    uploads: dict = field(default_factory=dict)
    #: Source as written -> what that file carries besides pixels, for each one
    #: that was readable. Read without the network, like everything else here.
    metadata: dict = field(default_factory=dict)
    #: Whether the metadata is removed before upload. False is
    #: ``--keep-image-metadata``, and means every file goes up exactly as it is.
    strip_metadata: bool = True
    #: What stops the publish: one entry per image, each naming it.
    problems: list = field(default_factory=list)
    #: What does not stop it, but the author should see.
    warnings: list = field(default_factory=list)

    @property
    def block_images(self):
        """The images that will become image blocks."""
        return [r for r in self.refs if r.placement == images.BLOCK]

    @property
    def linked(self):
        """Those already hosted, which are pointed at and never uploaded."""
        return [r for r in self.block_images if r.kind == images.URL]


def plan_images(content, base_dir=None, strip_metadata=True, featured_image=None):
    """Find the draft's images and check every local file, sending nothing.

    ``base_dir`` is what a relative path is relative to: the directory of the
    handoff file, so the same handoff resolves the same way wherever the command
    is run from. Left unset it is the working directory.

    ``featured_image`` is the handoff's ``Featured image:`` value, when it has
    one. It is checked here, with the draft's own images, because it is a file
    that gets uploaded and so fails for the same reasons, and has to fail at the
    same point: before anything is sent.

    Each local file is also read for what it carries besides pixels, so that a
    photograph that records where it was taken can be named on the page the
    author confirms rather than after it is already public.
    """
    plan = ImagePlan(refs=blocks.find_images(content), strip_metadata=strip_metadata)
    # First, so its problems lead: the handoff's parameters come before its draft.
    if (featured_image or "").strip():
        _plan_featured_image(plan, featured_image, base_dir)
    structural = blocks.image_problems(plan.refs)
    for i, ref in enumerate(plan.refs):
        label = images.describe(ref, i + 1, len(plan.refs))
        if i in structural:
            plan.problems.append(f"{label}: {structural[i]}")
            continue
        if ref.placement != images.BLOCK:
            continue
        if ref.kind == images.LOCAL:
            try:
                path = images.resolve_local_image(ref.src, base_dir)
            except images.ImageError as e:
                plan.problems.append(f"{label}: {e}")
                continue
            plan.uploads[ref.src] = path
            problem = _read_metadata(plan, ref.src, path, label)
            if problem:
                plan.problems.append(problem)
                continue
            warning = _location_warning(plan, ref.src, label)
            if warning:
                plan.warnings.append(warning)
        if not ref.alt.strip():
            plan.warnings.append(
                f"{label}: no alt text. It goes between the square brackets: "
                "![alt text](...)."
            )
    return plan


#: How a message names the featured image. The draft's own are "Image 2 of 4",
#: numbered in document order, and this one is not in the document.
_FEATURED = "Featured image"


def _featured_label(ref):
    return f'{_FEATURED} "{images.shorten(ref.src.strip())}"'


def _featured_ref(raw):
    """The ``ImageRef`` a ``Featured image:`` value names, or an ``ImageError``.

    Two spellings, and an author who has read the IMAGES note will try both. A
    bare source (``images/hero.jpg``) is taken as it stands, with no Markdown
    reading it, so a space or a Windows backslash needs no escaping. A Markdown
    image (``![Alt text](images/hero.jpg)``) is read by the parser the draft's
    own images go through, and is the only way to give a featured image alt text
    on one line. Anything else that opens ``![`` is refused, not guessed at.
    """
    value = raw.strip()
    if not value.startswith("!["):
        return images.ImageRef(src=value)
    found = blocks.find_images(value)
    if len(found) != 1 or found[0].placement != images.BLOCK:
        raise images.ImageError(
            "the line could not be read as a single image. Give a path, or one "
            "Markdown image: ![alt text](images/hero.jpg)."
        )
    return found[0]


def _plan_featured_image(plan, raw, base_dir):
    """Check the handoff's featured image the way the draft's own are checked.

    Everything that stops a body image stops this one, for the same reasons: a
    missing file, one that is not an image, one whose metadata cannot be
    stripped, a ``.heic``. What differs is the source. A URL is refused, where a
    body image would be linked: ``featured_media`` is an attachment ID, so a post
    cannot point at an image hosted elsewhere, and the only way to give it one is
    to upload the file. (WordPress 7.1 can fetch a URL into the media library
    itself, through a ``url`` parameter on the media endpoint. That route is not
    used. The fetch happens on the server, so the metadata strip never sees the
    file, it needs a WordPress that has it, and its path was reported to skip
    the fields a normal upload carries, alt text among them:
    WordPress/gutenberg#82034.)
    """
    try:
        ref = _featured_ref(raw)
    except images.ImageError as e:
        plan.problems.append(f"{_FEATURED}: {e}")
        return
    kind = ref.kind
    if kind == images.EMPTY:
        plan.problems.append(
            f"{_FEATURED}: it has no source. Give a path to an image file."
        )
        return
    label = _featured_label(ref)
    if kind == images.URL:
        plan.problems.append(
            f"{label}: it is a URL, and a post's featured image has to be an item "
            "in this site's media library (WordPress sets it by attachment ID), "
            "so it cannot point at an image hosted elsewhere. Save the image as a "
            "file and give that path: it is uploaded the way an image in the "
            "draft is."
        )
        return
    if kind == images.UNSUPPORTED:
        plan.problems.append(
            f"{label}: its source is not a path to a file. Save the image as a "
            "file and give that path."
        )
        return
    try:
        path = images.resolve_local_image(ref.src, base_dir)
    except images.ImageError as e:
        plan.problems.append(f"{label}: {e}")
        return
    plan.uploads[ref.src] = path
    problem = _read_metadata(plan, ref.src, path, label)
    if problem:
        plan.problems.append(problem)
        return
    plan.featured = ref
    warning = _location_warning(plan, ref.src, label)
    if warning:
        plan.warnings.append(warning)
    if not ref.alt.strip():
        plan.warnings.append(
            f"{label}: no alt text. Write the line as a Markdown image to give it "
            "some: ![alt text](images/hero.jpg)."
        )
    if ref.caption.strip():
        plan.warnings.append(
            f"{label}: a featured image has no caption, so the one written here "
            "is not used."
        )


def _read_metadata(plan, src, path, label):
    """Record what ``path`` carries. Returns a problem, or None.

    A file whose type is stripped but which Pillow cannot open is a problem and
    not a warning: the strip would fail at upload time instead, after the term
    lookups and after the author said yes, and the one answer that is never
    right is to send the original anyway.
    """
    if plan.strip_metadata:
        refusal = images.refusal(path)
        if refusal:
            # Before inspect_metadata, which cannot open one of these and would
            # report "could not be read" as if the file were damaged.
            return f"{label}: {refusal}"
    try:
        meta = plan.metadata[src] = images.inspect_metadata(path)
    except images.ImageError as e:  # Pillow missing from the environment
        if plan.strip_metadata:
            return f"{label}: {e}"
        # Nothing is being stripped, so this costs only the warning below.
        plan.warnings.append(f"{label}: its metadata could not be read ({e})")
        return None
    if plan.strip_metadata and meta.unreadable and images.strippable(path):
        return (
            f"{label}: {path.name} is a {path.suffix.lower()} file that cannot be "
            f"opened as an image ({meta.unreadable}), so the metadata it may "
            "carry cannot be stripped. Fix or convert the file, or pass "
            "--keep-image-metadata to upload it as it is."
        )
    return None


def _will_strip(plan, src):
    """Whether this image's metadata is removed before it is uploaded."""
    path, meta = plan.uploads.get(src), plan.metadata.get(src)
    if not plan.strip_metadata or path is None or meta is None:
        return False
    # An animation is left alone: re-encoding it would mean rebuilding every
    # frame's timing and disposal, and getting that wrong is a worse outcome
    # than the metadata it would remove.
    return images.strippable(path) and not meta.animated and not meta.unreadable


def _location_warning(plan, src, label):
    """A warning when a file that records where it was taken will keep it.

    Also when that cannot be established: a kept ``.heic`` gets the warning on
    the strength of what the format usually carries, because nothing here can
    open one to check, and silence would read as "nothing found".
    """
    path = plan.uploads.get(src)
    if path is not None and path.suffix.lower() in images.REFUSED:
        return (
            f"{label}: this is a {path.suffix.lower()} file being uploaded as "
            "it is, and nothing here can read it. A phone writes a GPS "
            "position into this format by default, so assume it records WHERE "
            "IT WAS TAKEN. A media-library file is public the moment it is "
            "uploaded."
        )
    meta = plan.metadata.get(src)
    if not meta or not meta.location or _will_strip(plan, src):
        return None
    why = (
        "--keep-image-metadata was passed"
        if not plan.strip_metadata
        else f"{plan.uploads[src].suffix.lower()} is not stripped"
        if not images.strippable(plan.uploads[src])
        else "an animation is not re-encoded"
    )
    return (
        f"{label}: this file records WHERE IT WAS TAKEN (GPS, "
        f"{len(meta.location)} tags) and will be uploaded with it, because "
        f"{why}. A media-library file is public the moment it is uploaded."
    )


def describe_problems(plan):
    return (
        "The handoff has image(s) that cannot be published, so nothing was "
        "sent:\n  - "
        + "\n  - ".join(plan.problems)
        + "\nFix the image line(s) in the handoff (in the FINAL DRAFT, or the "
        "Featured image: line) and re-run."
    )


#: What each status an upload commonly fails with usually means, since the
#: server's own message for these says what happened and not what to do.
_UPLOAD_HINTS = {
    401: (
        "Check wordpress.username and wordpress.application_password: "
        "WordPress answers a wrong username exactly as it answers no "
        "credentials at all."
    ),
    403: (
        "This WordPress user may not be allowed to upload files (the "
        "upload_files capability, which Author and above have)."
    ),
    413: (
        "The file is larger than the server accepts. Shrink it, or raise "
        "upload_max_filesize and post_max_size on the server."
    ),
}


def _upload_error(exc):
    """A WordPress REST error as one line: status, message, code, and what to do."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    message = code = None
    try:
        body = resp.json()
    except Exception:  # no response, or a body that is not JSON
        body = None
    if isinstance(body, dict):
        message, code = body.get("message"), body.get("code")
    if isinstance(message, str) and message:
        detail = f"{message} ({code})" if code else message
    else:
        detail = redact.capture_error_body(exc) or redact.redact_url_keys(str(exc))
    line = f"HTTP {status}: {detail}" if status else str(detail)
    hint = _UPLOAD_HINTS.get(status)
    return f"{line} {hint}" if hint else line


def _plain(text):
    """Alt text as WordPress will have kept it: unescaped, whitespace collapsed.

    WordPress runs alt text through ``sanitize_text_field``, which turns a bare
    ``<`` into ``&lt;`` and collapses runs of whitespace. Comparing raw strings
    would report a difference for an alt text that arrived exactly as written.
    """
    return " ".join(html.unescape(text or "").split())


def _set_alt_text(api_base, headers, attachment_id, alt, stored):
    """Second chance for the media item's own alt text. A warning, or None if it landed.

    Never fails the publish. The block carries the alt text that matters for
    accessibility whatever happens here; this is the copy on the media-library
    item, which is what the next post that reuses the image will offer.
    """
    if stored:
        # WordPress kept something, so the field works and it edited what it
        # was given (it strips tags). A second attempt would be refused the same way.
        return (
            f"WordPress stored the media-library alt text as {stored!r}, not "
            f"{alt!r}. The block has it as written."
        )
    try:
        resp = requests.post(
            f"{api_base}/media/{attachment_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"alt_text": alt},
            timeout=60,
        )
        resp.raise_for_status()
        stored = resp.json().get("alt_text")
    except Exception as e:
        return (
            f"the media-library alt text could not be set ({redact.redact_url_keys(str(e))}). "
            "The block has it."
        )
    if _plain(stored) != _plain(alt):
        return (
            "WordPress did not keep the alt text on the media-library item, "
            "although the block has it."
        )
    return None


def _upload_one(api_base, headers, path, alt):
    """Upload one file to the media library: ``(UploadedImage, warning or None)``.

    Multipart, so the alt text travels in the same request as the file. A
    raw-body upload carries only the file, and naming it would take a second
    request. Its landing is checked all the same, in the response: this
    repo's last WordPress field that vanished (Rank Math's) came back
    ``200`` with nothing set.
    """
    try:
        with path.open("rb") as fh:
            resp = requests.post(
                f"{api_base}/media",
                headers=headers,
                files={"file": (path.name, fh, images.mime_for(path))},
                data={"alt_text": alt} if alt else None,
                timeout=_UPLOAD_TIMEOUT,
            )
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise images.ImageError(
            f"WordPress refused the upload. {_upload_error(e)}"
        ) from e
    except requests.RequestException as e:  # timeout, refused connection, DNS
        raise images.ImageError(
            f"the upload did not complete: {redact.redact_url_keys(str(e))}"
        ) from e
    except OSError as e:
        raise images.ImageError(f"the file could not be read: {e.strerror or e}") from e

    try:
        body = resp.json()
        attachment_id = int(body["id"])
        source_url = body["source_url"]
    except (ValueError, KeyError, TypeError) as e:
        raise images.ImageError(
            "WordPress accepted the upload, but its answer did not name the new "
            "attachment (no id or source_url), so there is nothing to point the "
            "block at."
        ) from e

    # The size the block editor picks by default: "large" when WordPress made
    # one, which it does only for an image wider than that size, else the file
    # as uploaded.
    large = ((body.get("media_details") or {}).get("sizes") or {}).get("large") or {}
    if large.get("source_url"):
        url, size_slug = large["source_url"], "large"
    else:
        url, size_slug = source_url, "full"

    warning = None
    if alt and _plain(body.get("alt_text")) != _plain(alt):
        warning = _set_alt_text(
            api_base, headers, attachment_id, alt, body.get("alt_text")
        )
    return images.UploadedImage(attachment_id, url, size_slug), warning


def _file_key(path):
    """One identity for a file however it was spelled (case, ``./``, symlinks)."""
    return os.path.normcase(os.path.realpath(path))


def _left_behind(uploads):
    """A note on what is already in the media library when a later step fails."""
    if not uploads:
        return ""
    listing = ", ".join(f"ID {u['id']} ({u['src']})" for u in uploads)
    return (
        "\nUploaded before this failed, and still in the media library (nothing "
        f"was deleted): {listing}. Remove them under Media > Library if you do "
        "not want them."
    )


def upload_images(plan, api_base, headers):
    """Upload each distinct local file once: ``(resolved, uploads, warnings)``.

    ``resolved`` is what ``blocks.to_blocks`` takes, source -> ``UploadedImage``.
    ``uploads`` and ``warnings`` are for the result. The featured image, when
    there is one, is in all three: ``resolved`` is how ``push`` finds its
    attachment ID (no block uses that entry), and its ``uploads`` entry says
    ``"featured": True``.

    A file the draft uses twice is uploaded once and both blocks point at the
    one attachment. Each block keeps its own alt text; the attachment can hold
    only one, so it gets the first that is not empty. The featured image goes
    first, so when it is also a picture in the draft the attachment keeps its
    alt text, which is where a theme reads the featured image's alt text from.
    That also makes it the first upload: when it fails, nothing else went up,
    and when a later one fails, it is named with the rest.

    What goes up is a scrubbed copy in a temporary directory, not the file on the
    author's disk: a media-library item is public the moment it exists, and the
    original carries whatever the camera wrote into it. The author's file is
    never modified, and the copy keeps its name so the attachment still has the
    name they chose.

    Raises ``ImageError`` naming the image, and what was already uploaded.
    """
    queue = [
        (ref, images.describe(ref, i + 1, len(plan.refs)), False)
        for i, ref in enumerate(plan.refs)
    ]
    if plan.featured is not None:
        queue.insert(0, (plan.featured, _featured_label(plan.featured), True))

    first_alt = {}
    for ref, _, _ in queue:
        path = plan.uploads.get(ref.src)
        if path is not None and ref.alt.strip():
            first_alt.setdefault(_file_key(path), ref.alt)

    by_file, resolved, uploads, warnings = {}, {}, [], []
    with tempfile.TemporaryDirectory(prefix="ci-image-scrub-") as scratch:
        for ref, label, featured in queue:
            path = plan.uploads.get(ref.src)
            if path is None:
                continue
            key = _file_key(path)
            if key not in by_file:
                send, stripped = path, _will_strip(plan, ref.src)
                if stripped:
                    try:
                        send = images.strip_metadata(
                            path, _scrub_dir(scratch, len(by_file))
                        )
                    except images.ImageError as e:
                        raise images.ImageError(
                            f"{label}: {e}{_left_behind(uploads)}"
                        ) from e
                try:
                    by_file[key], warning = _upload_one(
                        api_base, headers, send, first_alt.get(key, "")
                    )
                except images.ImageError as e:
                    raise images.ImageError(
                        f"{label}: {e}{_left_behind(uploads)}"
                    ) from e
                log.info(
                    "Uploaded %s: media ID %s, %s size, metadata %s",
                    path.name,
                    by_file[key].attachment_id,
                    by_file[key].size_slug,
                    "stripped" if stripped else "NOT stripped",
                )
                uploads.append(
                    {
                        "src": ref.src,
                        "id": by_file[key].attachment_id,
                        "url": by_file[key].url,
                        "stripped": stripped,
                        # Only ever present, so a draft's own entries stay as
                        # they were.
                        **({"featured": True} if featured else {}),
                    }
                )
                if warning:
                    warnings.append(f"{label}: {warning}")
            resolved[ref.src] = by_file[key]
    return resolved, uploads, warnings


def _scrub_dir(root, n):
    """One directory per file, so two images with the same name do not collide."""
    out = os.path.join(root, str(n))
    os.mkdir(out)
    return out


def _size(path):
    try:
        return f"{path.stat().st_size / 1024:.0f} KB"
    except OSError:
        return "size unknown"


def print_image_plan(plan):
    """List the draft's images before the checklist asks for a yes.

    It shows the resolved path of every file that will be uploaded, which is the
    one thing on this page the author has not already read in the draft: a
    handoff can name any file the machine can read, and this is the last point
    at which someone can see which.

    The featured image is listed first, under its own heading, and counts as an
    upload for the closing line: it is public from the moment it is uploaded
    like any other, and is the one most likely to be a photograph.
    """
    if not plan.block_images and plan.featured is None:
        return
    print("\nIMAGES")
    print("======")
    if plan.featured is not None:
        path = plan.uploads[plan.featured.src]
        print("Featured image:")
        print(f"  UPLOAD  {path}  ({_size(path)})")
        print(f"     alt:     {plan.featured.alt.strip() or '(none)'}")
        print(f"     metadata: {_metadata_line(plan, plan.featured.src)}")
        if plan.block_images:
            print("In the post:")
    for i, ref in enumerate(plan.refs, 1):
        if ref.placement != images.BLOCK:
            continue
        if ref.src in plan.uploads:
            path = plan.uploads[ref.src]
            print(f"{i}. UPLOAD  {path}  ({_size(path)})")
        else:
            print(f"{i}. LINK    {ref.src}  (already hosted; not uploaded)")
        print(f"     alt:     {ref.alt.strip() or '(none)'}")
        if ref.caption.strip():
            print(f"     caption: {ref.caption.strip()}")
        if ref.src in plan.uploads:
            print(f"     metadata: {_metadata_line(plan, ref.src)}")
    for warning in plan.warnings:
        print(f"  ! {warning}")
    if plan.uploads:
        print(
            "Uploaded files are public from the moment they are uploaded, even "
            "though the post stays a draft."
        )
        if not plan.strip_metadata:
            print(
                "--keep-image-metadata: nothing is stripped. Every file goes up "
                "with whatever its camera wrote into it."
            )


def _metadata_line(plan, src):
    """What this file carries and what becomes of it: one line for the listing.

    The author has read the draft, so the resolved path is one of the two things
    on that page they have not already seen. What the file says about them is
    the other.
    """
    meta = plan.metadata.get(src)
    path = plan.uploads[src]
    if meta is None:
        return "not read"
    if _will_strip(plan, src):
        notes = []
        if meta.orientation != 1:
            # Said because it is the one thing the strip changes about the
            # picture: dropping the tag without turning the pixels is how a
            # stripped phone photo ends up on its side.
            notes.append("the EXIF rotation is applied to the pixels")
        if meta.icc_profile:
            notes.append("the ICC colour profile is kept")
        tail = f" ({'; '.join(notes)})" if notes else ""
        found = meta.summary()
        if found == "none":
            return f"none found; stripped anyway before upload{tail}"
        return f"{found} -> stripped before upload{tail}"
    if path.suffix.lower() in images.REFUSED:
        return (
            f"NOT stripped, and NOT readable either ({path.suffix.lower()}): "
            "whether this file records where it was taken is unknown"
        )
    if not images.strippable(path):
        return f"NOT stripped ({path.suffix.lower()} is uploaded as it is)"
    if meta.animated:
        return "NOT stripped (an animation is not re-encoded)"
    return f"{meta.summary()} -> KEPT (--keep-image-metadata)"


def print_image_result(result):
    """Say what the images did, next to the post URL, the way terms are."""
    if "images_uploaded" in result:
        print(
            f"Images:   {result['images_uploaded']} uploaded to the media "
            f"library, {result['images_linked']} linked from an existing URL"
        )
        for up in result["image_uploads"]:
            notes = [
                note
                for note, applies in (
                    ("featured image", up.get("featured")),
                    ("metadata NOT stripped", not up.get("stripped")),
                )
                if applies
            ]
            tail = f"  ({'; '.join(notes)})" if notes else ""
            print(f"  - {up['src']} -> media ID {up['id']}{tail}")
    for warning in result.get("image_warnings", []):
        print(f"WARNING: {warning}")


def _stored_excerpt(data):
    """The excerpt WordPress answered with, as plain text, or None if it sent none.

    ``raw`` is what is in the database, and it comes back to the caller who
    created the post. ``rendered`` is the fallback, with the paragraph tags the
    ``the_excerpt`` filter wraps it in taken off.
    """
    excerpt = data.get("excerpt")
    if isinstance(excerpt, dict):
        raw = excerpt.get("raw")
        if raw is None:
            raw = re.sub(r"<[^>]+>", "", excerpt.get("rendered") or "")
        return raw
    return excerpt if isinstance(excerpt, str) else None


def _confirm_post_fields(payload, data, featured_src=None):
    """What WordPress kept of the slug, excerpt and featured image: ``(fields, warnings)``.

    Read from the answer to the create and not taken from its ``200``. A page has
    no excerpt unless the site adds one, a theme without post-thumbnail support
    has no featured image, and WordPress drops a field the post type has not
    registered without saying so: this repo's last fields to vanish that way,
    Rank Math's, came back ``200`` with nothing set.

    ``fields`` has an entry for each of the three the handoff asked for, with what
    was sent and what came back, and nothing for one it did not. A slug WordPress
    changed is not a warning (it cleans one with ``sanitize_title``, and adds
    ``-2`` to a published one that is taken) but the author is shown both, since
    the one in the handoff is no longer the one on the site. ``warnings`` is for
    what did not land at all.
    """
    fields, warnings = {}, []
    if "slug" in payload:
        stored = data.get("slug") or ""
        fields["slug"] = {"requested": payload["slug"], "stored": stored}
        if not stored:
            warnings.append(
                "WordPress did not keep the slug (it answered with none), so the "
                "post will be named from its title."
            )
    if "excerpt" in payload:
        stored = _stored_excerpt(data)
        fields["excerpt"] = {"requested": payload["excerpt"], "stored": stored}
        if stored is None:
            warnings.append(
                "WordPress did not keep the excerpt (its answer has none). A page "
                "has no excerpt unless the site adds that support to pages."
            )
        elif _plain(stored) != _plain(payload["excerpt"]):
            warnings.append(
                f"WordPress stored the excerpt as {stored!r}, not as written."
            )
    if "featured_media" in payload:
        requested = payload["featured_media"]
        try:
            stored = int(data.get("featured_media"))
        except (TypeError, ValueError):
            stored = None
        fields["featured_media"] = {
            "requested": requested,
            "stored": stored,
            "src": featured_src,
        }
        if stored != requested:
            warnings.append(
                "WordPress did not set the featured image: it answered "
                f"featured_media {data.get('featured_media')!r}, not {requested}. "
                f"The image is in the media library as media ID {requested}; set "
                "it as the featured image in the post's sidebar. (A theme with no "
                "post-thumbnail support has none to set.)"
            )
    return fields, warnings


def print_post_fields_result(result):
    """Say what became of the slug, excerpt and featured image, next to the post URL.

    A field the handoff did not set prints nothing, so a handoff without them
    reads as it always has. The slug is the one WordPress answered with: it can
    differ from the handoff's, and the author would otherwise find out from the
    permalink.
    """
    fields = result.get("post_fields") or {}
    slug = fields.get("slug")
    if slug:
        line = f"Slug:     {slug['stored'] or '(none)'}"
        if slug["stored"] and slug["stored"] != slug["requested"]:
            line += f"  (the handoff asked for {slug['requested']!r})"
        print(line)
    excerpt = fields.get("excerpt")
    if excerpt and excerpt["stored"] is not None:
        text = " ".join(excerpt["stored"].split())
        print(f"Excerpt:  {text if len(text) <= 200 else text[:197] + '...'}")
    featured = fields.get("featured_media")
    if featured and featured["stored"] == featured["requested"]:
        print(f"Featured: media ID {featured['stored']} ({featured['src']})")
    for warning in result.get("post_field_warnings", []):
        print(f"WARNING: {warning}")


def push(
    content,
    pub_params,
    wp_config,
    rank_math_config,
    publish_live=False,
    allow_missing_terms=False,
    image_base_dir=None,
    strip_image_metadata=True,
):
    """Push article to WordPress.  Always saves as draft unless publish_live=True.

    Resolves category and tag slugs to integer IDs via the WP REST API before
    creating the post.

    An image alone on a line of the draft becomes a native image block. A local
    file (a path relative to ``image_base_dir``, or absolute) is uploaded to the
    media library first; an http(s) URL is pointed at and never uploaded. Every
    local file is checked before the first request, and if an upload fails the
    post is not created. Uploads are not undone when a later step fails: they are
    named in the error, since a media-library item is public the moment it exists.

    A JPEG, PNG or WebP goes up as a scrubbed copy: EXIF (the GPS position, the
    device, the capture time), XMP, IPTC and any comment removed, the EXIF
    orientation applied to the pixels, the ICC colour profile kept. The original
    on disk is untouched. ``strip_image_metadata=False`` is
    ``--keep-image-metadata`` and uploads every file exactly as it is.

    ``pub_params["post_type"]`` selects the REST route: ``post`` (default) or
    ``page``. A page carries no categories or tags, so for that type the term
    lookups are skipped entirely and any terms named in the handoff are
    reported back on the result rather than dropped — naming a category on a
    page is an authoring mistake, and a silent drop is how it stays one.

    ``pub_params["slug"]``, ``["excerpt"]`` and ``["featured_image"]`` are
    optional. The first two are sent as ``slug`` and ``excerpt``. The featured
    image is a path (or a Markdown image, for its alt text) that goes through the
    same checks, scrub and upload as an image in the draft, first, and its
    attachment ID is sent as ``featured_media``; a URL is refused, because a
    post's featured image has to be in this site's media library. What
    WordPress kept of all three is read back from its answer into
    ``post_fields``, with a ``post_field_warnings`` entry for each that did not
    land.

    Returns dict with keys: success (bool), post_id, post_url, error (if failed).
    """
    try:
        post_type = resolve_post_type(pub_params.get("post_type"))
    except ValueError as e:
        log.error(str(e))
        return {"success": False, "error": str(e)}
    route = POST_TYPE_ROUTES[post_type]
    site_url = wp_config["site_url"].rstrip("/")
    _require_https(site_url, wp_config)
    endpoint = wp_config.get("rest_api_endpoint", "/wp-json/wp/v2")
    api_base = f"{site_url}{endpoint}"
    username = wp_config["username"]
    app_password = wp_config["application_password"]

    headers = _auth_header(username, app_password)
    headers["Content-Type"] = "application/json"
    auth_headers = _auth_header(username, app_password)  # without Content-Type for GETs

    # Every local image is checked here, before the first request of any kind. A
    # mistyped path costs nothing now; found after an earlier image had gone up,
    # it would cost an orphaned upload as well.
    image_plan = plan_images(
        content,
        image_base_dir,
        strip_image_metadata,
        featured_image=pub_params.get("featured_image"),
    )
    if image_plan.problems:
        log.error(describe_problems(image_plan))
        return {"success": False, "error": describe_problems(image_plan)}

    # Resolve slugs → IDs before building the post payload. Pages are not in
    # either taxonomy, so the two GETs are skipped rather than issued and
    # discarded.
    requested_terms = []
    if post_type == "page":
        category_ids, unresolved_categories = [], []
        tag_ids, unresolved_tags = [], []
        raw_category = pub_params.get("wordpress_category")
        if raw_category:
            requested_terms.append(str(raw_category))
        requested_terms.extend(str(t) for t in pub_params.get("tags", []) or [])
    else:
        category_ids, unresolved_categories = _lookup_term_ids(
            api_base,
            auth_headers,
            "categories",
            pub_params.get("wordpress_category"),
        )
        tag_ids, unresolved_tags = _lookup_term_ids(
            api_base,
            auth_headers,
            "tags",
            pub_params.get("tags", []),
        )

    # Fail closed before an irreversible publish. Going live into no category,
    # with none of its tags, is a real editorial failure, and the checklist item
    # the user already ticked ("WordPress category correct") was confirmed before
    # this lookup ran — so the one check that could have caught it happened at
    # the one moment it could not.
    unresolved = unresolved_categories + unresolved_tags
    if unresolved and publish_live and not allow_missing_terms:
        return {
            "success": False,
            "error": (
                "Refusing to publish live with unresolved taxonomy terms: "
                + ", ".join(sorted(unresolved))
                + ". Create them in WP admin (or use integer IDs), or pass "
                "allow_missing_terms=True to publish without them."
            ),
            "unresolved_terms": sorted(unresolved),
        }

    # WordPress stores HTML; the pipeline carries Markdown. Converting here
    # rather than at the call site means every publish path gets it. The images
    # go up first, after the term check above so that a publish refused for its
    # terms uploads nothing, because each block has to point at where its image
    # now lives. Either step failing means the post is never created.
    try:
        hosted, image_uploads, image_warnings = upload_images(
            image_plan, api_base, auth_headers
        )
    except images.ImageError as e:  # names the image, and what already went up
        log.error(f"WordPress image upload failed: {e}")
        return {"success": False, "error": str(e)}
    try:
        content = blocks.to_blocks(content, resolved=hosted)
    except images.ImageError as e:
        # Not reachable while the plan and the converter read the draft the same
        # way, which they do by sharing find_images. Handled anyway, because the
        # images are already up by now and this is the one place that says so.
        error = f"{e}{_left_behind(image_uploads)}"
        log.error(f"WordPress image conversion failed: {error}")
        return {"success": False, "error": error}

    # Found by source, as a body image is: upload_images keys everything it put
    # up by the source as written. The featured image is always in it, because a
    # plan with a featured image has no problems and so uploaded it.
    featured_media = (
        hosted[image_plan.featured.src].attachment_id
        if image_plan.featured is not None
        else None
    )

    payload = _build_post_payload(
        pub_params,
        wp_config,
        rank_math_config,
        content,
        category_ids=category_ids,
        tag_ids=tag_ids,
        post_type=post_type,
        featured_media=featured_media,
    )

    if publish_live:
        payload["status"] = "publish"

    try:
        resp = requests.post(
            f"{api_base}/{route}", headers=headers, json=payload, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        post_id = data.get("id")
        post_url = data.get("link")
        log.info(
            f"WordPress push successful: type={post_type} post_id={post_id} "
            f"url={post_url} categories={category_ids} tags={tag_ids}"
        )
        result = {
            "success": True,
            "post_id": post_id,
            "post_url": post_url,
            "post_type": post_type,
        }
        rank_math_error = _apply_rank_math_meta(
            site_url, headers, post_id, rank_math_meta(pub_params, rank_math_config)
        )
        if rank_math_error:
            result["rank_math_error"] = rank_math_error
        if requested_terms:
            # Not an error: the push succeeded and the page is correct. It is
            # reported so the author learns the terms they wrote were never
            # applied, instead of assuming a page can be categorised.
            result["ignored_terms"] = sorted(set(requested_terms))
        if unresolved:
            # Surfaced on the result, not just in a log line, so the caller can
            # print it next to the success message instead of it scrolling past.
            result["unresolved_terms"] = sorted(unresolved)
        post_fields, field_warnings = _confirm_post_fields(
            payload,
            data,
            featured_src=(
                image_plan.featured.src if image_plan.featured is not None else None
            ),
        )
        if post_fields:
            result["post_fields"] = post_fields
        if field_warnings:
            result["post_field_warnings"] = field_warnings
        if image_plan.block_images or image_plan.featured is not None:
            result["images_stripped"] = sum(
                1 for up in image_uploads if up.get("stripped")
            )
            result["images_uploaded"] = len(image_uploads)
            result["images_linked"] = len(image_plan.linked)
            result["image_uploads"] = image_uploads
        if image_warnings:
            result["image_warnings"] = image_warnings
        return result
    except requests.HTTPError as e:
        # Redacted and bounded like every other adapter's error path. A WordPress
        # error body can echo the submitted post, and this string is both logged
        # and returned to the caller for display.
        error_body = redact.capture_error_body(e) or redact.redact_url_keys(str(e))
        log.error(f"WordPress push failed: {error_body}")
        return {
            "success": False,
            "error": str(error_body) + _left_behind(image_uploads),
        }
    except Exception as e:
        log.error(f"WordPress push failed: {e}")
        return {"success": False, "error": str(e) + _left_behind(image_uploads)}


def print_checklist_and_confirm():
    print(CHECKLIST)
    answer = (
        input("Have you reviewed the checklist and confirmed all items? [yes/no]: ")
        .strip()
        .lower()
    )
    if answer not in ("yes", "y"):
        print("Publication aborted. Complete the checklist and re-run.")
        return False
    return True
