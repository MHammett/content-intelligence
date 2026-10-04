---
name: verifying-a-publish
description: Use before committing any change to handoff parsing, run_publish_pipeline or adapters/cms/ — and whenever a check needs a real ci-review --publish run, which would otherwise POST to a live site and leave public uploads behind. Also use to read what an image carries besides pixels, to build a test photo carrying a GPS block and an EXIF orientation, or to find out which images already in a WordPress media library expose EXIF or a location. Covers tools/publish_rehearsal.py, tools/image_metadata.py and tools/media_exif_audit.py, and what a rehearsal does and does not prove.
---

The procedure lives in `docs/DEVELOPMENT.md`, under "Verifying a publish without the live site"; read it and follow it. It is the single source, so this file stays short.

The short version:

- `--replay` cannot touch `--publish`. `tools/publish_rehearsal.py` runs the real CLI with every socket blocked and WordPress faked, and forces dummy credentials, so the real ones are never read. Flags after a bare `--` reach `ci-review`.
- A worktree has no `configs/` of its own: pass `--config-dir` pointing at the main checkout's.
- Check an image with `tools/image_metadata.py`, which reads with exifread. Checking a Pillow-based strip with Pillow is Pillow agreeing with itself. `make` builds a photo with an 11-tag GPS block and orientation 6; `report` with two paths diffs before against after.
- `tools/media_exif_audit.py` reports what is already public. It GETs only. `media_details.image_meta` holds no GPS fields, and WordPress keeps the pre-scale original public with its EXIF while serving a `-scaled` copy without it — so read each file's bytes, and read `original_image` too.
- A rehearsal proves this project's side of the conversation and nothing about what WordPress does with it. Do one live run after it is clean, and expect the attachments to survive it: a media `DELETE` without `force` is refused here, and `force=true` is permanent.
