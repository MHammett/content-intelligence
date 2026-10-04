"""Report which images in a WordPress media library carry EXIF, GPS first.

Read-only. It issues GETs and nothing else: no DELETE, no PUT, no POST. That is
deliberate and not an oversight -- on the site this was written for, a media
DELETE without ``force`` is refused (``501 rest_trash_not_supported``, because
there is no MEDIA_TRASH), so the only REST delete for an attachment is
``force=true``, a permanent one. Removing anything is a decision for a person,
under Media > Library.

Two things make this harder than reading the API:

* ``media_details.image_meta`` cannot answer the question. WordPress builds it
  with ``wp_read_image_metadata``, which keeps aperture, camera,
  ``created_timestamp``, a caption and a few others -- and **no GPS fields at
  all**. So the only way to know is to fetch each file and read its own bytes.
* The file the API points at is often not the one carrying the metadata.
  WordPress serves a ``-scaled`` copy of anything wider than
  ``big_image_size_threshold`` (2560px by default) and regenerates it *without*
  EXIF, while the untouched original stays public in the same folder under the
  name in ``media_details.original_image``. An audit that reads only
  ``source_url`` will report a clean library that is not clean.

So every candidate URL for each attachment is fetched and read.

    uv run --with exifread python tools/media_exif_audit.py \\
        --publication NAME --config-dir ../../configs

The site URL and credentials come from the publication config, the same loader
the publish path uses -- so ``${WP_USER}``-style placeholders resolve from
``.env`` exactly as they do for a real publish, and nothing about a particular
site is written down here. A worktree has no ``configs/`` of its own, so point
``--config-dir`` at the main checkout's.
"""

import argparse
import base64
import datetime as dt
import io
import json
import pathlib
import sys

import requests

USER_AGENT = "content-intelligence/media-exif-audit"


def _session_and_base(publication, config_dir):
    """``(session, api_base, username)`` from the publication config."""
    from ci_article_review.config_loader import (
        load_publication_config,
        load_user_config,
        merge_configs,
    )

    config = merge_configs(
        load_user_config(config_dir), load_publication_config(publication, config_dir)
    )
    wp = (config.get("publication") or {}).get("wordpress") or {}
    site_url = (wp.get("site_url") or "").rstrip("/")
    if not site_url:
        raise SystemExit(f"publication {publication!r} has no wordpress.site_url")
    username, password = wp.get("username"), wp.get("application_password")
    if not username or not password:
        raise SystemExit(
            "wordpress.username / wordpress.application_password are unset. They "
            "usually come from .env; a worktree's .env is a placeholder stub, so "
            "use the main checkout's config-dir."
        )
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Basic {token}", "User-Agent": USER_AGENT}
    )
    endpoint = wp.get("rest_api_endpoint", "/wp-json/wp/v2")
    return session, f"{site_url}{endpoint}", username


def list_media(session, api_base):
    """Every media item, following the pagination to the end."""
    items, page = [], 1
    while True:
        resp = session.get(
            f"{api_base}/media",
            params={"per_page": 100, "page": page, "orderby": "id", "order": "asc"},
            timeout=60,
        )
        if resp.status_code == 400 and "rest_post_invalid_page_number" in resp.text:
            break
        if resp.status_code == 401:
            raise SystemExit(
                f"HTTP 401 {resp.text[:200]}\n"
                "rest_not_logged_in also means a wrong username, not only a wrong "
                "password: the WordPress login is not necessarily your own name."
            )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        total = resp.headers.get("X-WP-TotalPages")
        print(f"  page {page}: {len(batch)} items (X-WP-TotalPages={total})")
        page += 1
        if total and page > int(total):
            break
    return items


def candidate_urls(item):
    """Every public URL for one attachment that could carry its metadata."""
    details = item.get("media_details") or {}
    source = item.get("source_url")
    urls = {}
    if source:
        urls["attachment"] = source
    served = ((details.get("sizes") or {}).get("full") or {}).get("source_url")
    if served and served != source:
        urls["full rendition"] = served
    original = details.get("original_image")
    if original and source:
        urls["pre-scale original"] = f"{source.rsplit('/', 1)[0]}/{original}"
    return urls


def exif_of(session, url):
    """``{exif_tags, gps_tags, bytes, note}`` for one file's own bytes."""
    import exifread

    try:
        resp = session.get(url, headers={"Authorization": ""}, timeout=120)
    except requests.RequestException as e:
        return {
            "exif_tags": None,
            "gps_tags": [],
            "bytes": 0,
            "note": f"fetch failed: {e}",
        }
    if resp.status_code != 200:
        return {
            "exif_tags": None,
            "gps_tags": [],
            "bytes": 0,
            "note": f"HTTP {resp.status_code}",
        }
    tags = exifread.process_file(io.BytesIO(resp.content), details=False)
    return {
        "exif_tags": len(tags),
        "gps_tags": sorted(k[4:] for k in tags if k.startswith("GPS ")),
        "bytes": len(resp.content),
        "note": "",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--publication", required=True)
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument("--since", help="only flag items uploaded on/after YYYY-MM-DD")
    parser.add_argument("--out", help="write the full report as JSON here")
    args = parser.parse_args(argv)

    since = (
        dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
        if args.since
        else None
    )
    session, api_base, username = _session_and_base(args.publication, args.config_dir)
    print(f"{api_base} as {username!r} -- GETs only, nothing is deleted or changed")
    items = list_media(session, api_base)
    print(f"{len(items)} media items\n")

    rows = []
    for item in items:
        if not (item.get("mime_type") or "").startswith("image/"):
            continue
        uploaded = item.get("date_gmt")
        row = {
            "id": item["id"],
            "slug": item.get("slug"),
            "date_gmt": uploaded,
            "mime": item["mime_type"],
            "files": {
                label: dict(exif_of(session, url), url=url)
                for label, url in candidate_urls(item).items()
            },
        }
        row["has_gps"] = any(f["gps_tags"] for f in row["files"].values())
        row["max_exif"] = max((f["exif_tags"] or 0) for f in row["files"].values())
        row["after_since"] = bool(
            since
            and uploaded
            and dt.datetime.fromisoformat(uploaded).replace(tzinfo=dt.timezone.utc)
            >= since
        )
        rows.append(row)
        flag = "GPS" if row["has_gps"] else "-- "
        mark = " *" if row["after_since"] else ""
        print(
            f"  {flag} id={row['id']:<5} {uploaded}  {row['mime']:<12} "
            f"exif={row['max_exif']:<4} {(row['slug'] or '')[:44]}{mark}"
        )

    gps_rows = [r for r in rows if r["has_gps"]]
    exif_rows = [r for r in rows if r["max_exif"]]
    print(f"\n{len(rows)} images checked")
    print(f"  carrying a GPS block:  {len(gps_rows)}")
    print(f"  carrying any EXIF:     {len(exif_rows)}")
    for row in gps_rows or exif_rows:
        print(f"\n  id={row['id']}  {row['slug']}  uploaded {row['date_gmt']}")
        for label, info in row["files"].items():
            if info["exif_tags"]:
                print(
                    f"    {label}: {info['exif_tags']} EXIF tags, "
                    f"{len(info['gps_tags'])} GPS -> {info['url']}"
                )
                if info["gps_tags"]:
                    print(f"      {', '.join(info['gps_tags'])}")
    if gps_rows:
        print(
            "\nNothing was deleted. A media DELETE without force is refused here "
            "(501) and force=true is permanent, so removal is a decision for a "
            "person, under Media > Library."
        )
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nfull report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
