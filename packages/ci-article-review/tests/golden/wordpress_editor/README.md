# What the WordPress block editor writes

`ci_article_review.adapters.cms.blocks` turns a draft into block markup. REST
stores whatever it is sent and answers `200`, so nothing at publish time says
whether the **block editor** will accept it, and a block whose stored markup is not
what its `save()` writes opens as "Block contains unexpected or invalid content"
(issue #325: a quote, a table, a code block and a separator all did). This
directory is what a real editor wrote for each construct the converter emits, so
the tests can hold the converter to the editor's own output.

| File | What it is |
|---|---|
| `capture.js` | The constructs, each as the Markdown it comes from and the blocks it means, and the code that asks an editor to write them. Also `check()`, which asks the editor whether markup is valid. |
| `blocks.json` | What the editor wrote (`expected`), the editor's verdict on it (`valid`, `round_trip`) and the build it came from. Machine-written: do not edit it by hand. |
| `every_construct.md` | One of every construct the converter writes, as a document to put in front of an editor. |
| `../../test_wordpress_editor_blocks.py` | The tests. |

`valid` is that every block the editor read back from `expected` was valid.
`round_trip` is that writing those blocks out again gave `expected` unchanged, so
the string is what the editor itself would store.

## How `blocks.json` was captured

On 2026-10-05, from the **WordPress 7.1.2** block editor, in a disposable
WordPress Playground (PHP 8.3). No site was involved, and nothing was saved.

The build is what decides validity, so it was compared with the one the site
runs. `blocks.min.js` (the parser, validator and serialiser) and
`block-library.min.js` (every core block's `save()`) are byte for byte the 7.1.2
release's, and the production site served the same two files that day
(`4b5f1f4b23be` and `42d66f582f11`). The third file, `block-editor.min.js`, is
`72cbf44f5f43` on the release and `dc8506ba37fc` in Playground: Playground
prepends a short snippet and swaps one `iframe` in the editor canvas for its own
component, which has nothing to do with how a block is written or validated.

To take the captures again, or to check what `to_blocks` writes against an
editor, follow `docs/DEVELOPMENT.md`, "Checking block markup in the real editor".
The procedure is there once, and this file only records what was done.

## What the editor does that the issue did not say

Each of these is pinned by a case in `blocks.json`.

- **Code.** There is no language, so the `language-*` class goes. Every `[` is
  written `&#91;` and the `//` of the first URL on a line of its own is written
  `&#47;&#47;`, so that WordPress does not run a `[gallery]` in a sample or turn a
  URL into an embed (only the first such URL: a second one stays as written). A
  `>` is written bare when it is typed and `&gt;` when the block is read back from
  stored markup. The converter writes `&gt;`, the form that survives the editor
  saving the post.
- **Table.** `has-fixed-layout` is the block's default. A cell's alignment is
  `class="has-text-align-right" data-align="right"` (it reads neither
  `style=` nor `align=`). A table with no body rows has no `tbody`, but
  Python-Markdown gives a table with only a header one empty body row.
- **Quote.** Inner paragraphs are joined by a blank line. A citation is a
  `<cite>` straight after the last inner block, which the converter never writes
  because Markdown has no citation. A quote holding anything other than
  paragraphs (a list, a heading, another quote) has no faithful form, so it is
  sent as a `wp:html` block.
- **`wp:html`.** Valid whatever it holds, and written back unchanged. It is where
  everything with no native form goes, including a `figure` written as raw HTML.

The editor also accepts, without writing them back unchanged, a heading (it adds
`class="wp-block-heading"`) and a list (it spaces the items differently). The
converter's headings and lists are valid, and are not part of this capture.
