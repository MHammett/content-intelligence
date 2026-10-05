# CMS Adapter Interface

Each CMS adapter must expose:

- `push(content, pub_params, cms_config, seo_config, publish_live=False)` → dict with `success`, `post_id`, `post_url`, `error`
- `print_checklist_and_confirm()` → bool (True if user confirmed, False if aborted)

An adapter that uploads media also takes `image_base_dir` (what a relative image
path is relative to) and reports `images_uploaded`, `images_linked` and
`image_uploads` on its result. `images.py` holds what any such adapter needs: the
vocabulary for an image's source, and the checks on a local file. `blocks.py` turns
an image alone on a line into an image block, given where each one now lives.

`pub_params` may also carry `slug`, `excerpt` and `featured_image` (the handoff's
three optional lines). The first two are sent when they are not blank. The
featured image is a file that is uploaded, through the same checks and scrub as an
image in the draft and ahead of them, and its attachment ID is what the post
points at: a URL cannot be one, so it is refused. What the CMS kept of all three is
read back from its answer, not assumed, and reported as `post_fields` with a
`post_field_warnings` entry for each that did not land.

To add a new CMS adapter:
1. Create a new file in adapters/cms/
2. Implement the interface above
3. In your publication config, change the `wordpress` block to `cms` with an `adapter` field
