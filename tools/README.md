# tools/

Scripts for checking things the test suite cannot. Not part of any package, not
imported by anything, not shipped in a wheel: each one is run directly and each
says in its own docstring what it is for and why it exists.

| Script | What it is for |
|---|---|
| `publish_rehearsal.py` | Run the real `ci-review --publish` with every socket blocked and WordPress faked. The only safe way to exercise the one path with irreversible side effects. |
| `image_metadata.py` | `report` what an image carries (read with exifread, not Pillow), or `make` one that carries a phone's GPS block, orientation and ICC profile. |
| `media_exif_audit.py` | Report which images already in a WordPress media library carry EXIF or a GPS block. GETs only; deletes nothing. |
| `tidy.py` | List merged branches, removable worktrees and orphan directories left by finished sessions, and print the commands to clear them. Read-only; deletes nothing. See `docs/LANDING-A-PR.md`, "After it lands". |

The procedure these belong to is in
[`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md), under "Verifying a publish
without the live site" — that is the single source, and this table is only an
index.

`exifread` is in the `dev` dependency group, so `uv run` finds it in a
development checkout. A worktree has no `configs/` of its own, so pass
`--config-dir` pointing at the main checkout's.
