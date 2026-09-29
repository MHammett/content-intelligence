# CMS Adapter Interface

Each CMS adapter must expose:

- `push(content, pub_params, cms_config, seo_config, publish_live=False)` → dict with `success`, `post_id`, `post_url`, `error`
- `print_checklist_and_confirm()` → bool (True if user confirmed, False if aborted)

An adapter that uploads media also takes `image_base_dir` (what a relative image
path is relative to) and reports `images_uploaded`, `images_linked` and
`image_uploads` on its result. `images.py` holds what any such adapter needs: the
vocabulary for an image's source, and the checks on a local file. `blocks.py` turns
an image alone on a line into an image block, given where each one now lives.

To add a new CMS adapter:
1. Create a new file in adapters/cms/
2. Implement the interface above
3. In your publication config, change the `wordpress` block to `cms` with an `adapter` field
