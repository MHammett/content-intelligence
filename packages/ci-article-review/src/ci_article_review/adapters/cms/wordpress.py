import base64
import logging

import requests

from ci_core import redact

from . import blocks

log = logging.getLogger(__name__)

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
[ ] All images have alt text
[ ] Internal links reviewed and functional
[ ] Canonical URL set correctly in WordPress

SEO
[ ] Focus keyword set in Rank Math
[ ] Meta description under 155 characters
[ ] OG tags set
[ ] Schema type correct for content type (set in Rank Math, not by this script)

PUBLICATION
[ ] WordPress category correct
[ ] Tags applied
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


def push(
    content,
    pub_params,
    wp_config,
    rank_math_config,
    publish_live=False,
    allow_missing_terms=False,
):
    """Push article to WordPress.  Always saves as draft unless publish_live=True.

    Resolves category and tag slugs to integer IDs via the WP REST API before
    creating the post.

    ``pub_params["post_type"]`` selects the REST route: ``post`` (default) or
    ``page``. A page carries no categories or tags, so for that type the term
    lookups are skipped entirely and any terms named in the handoff are
    reported back on the result rather than dropped — naming a category on a
    page is an authoring mistake, and a silent drop is how it stays one.

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
    # rather than at the call site means every publish path gets it.
    content = blocks.to_blocks(content)

    payload = _build_post_payload(
        pub_params,
        wp_config,
        rank_math_config,
        content,
        category_ids=category_ids,
        tag_ids=tag_ids,
        post_type=post_type,
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
        return result
    except requests.HTTPError as e:
        # Redacted and bounded like every other adapter's error path. A WordPress
        # error body can echo the submitted post, and this string is both logged
        # and returned to the caller for display.
        error_body = redact.capture_error_body(e) or redact.redact_url_keys(str(e))
        log.error(f"WordPress push failed: {error_body}")
        return {"success": False, "error": str(error_body)}
    except Exception as e:
        log.error(f"WordPress push failed: {e}")
        return {"success": False, "error": str(e)}


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
