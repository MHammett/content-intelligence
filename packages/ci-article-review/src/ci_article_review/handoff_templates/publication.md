PUBLICATION HANDOFF
Generated: [timestamp]
Article: [title]
Publication: [publication config name]

PUBLICATION PARAMETERS
Status: draft [change to: publish-live only with explicit intent]
Post type: [post (default) | page -- use page for standing pages such as
About or Contact: no date, no category, not in the blog feed. A page
ignores the category and tag fields below.]
WordPress category: [category slug]
Tags: [comma-separated slugs]
WordPress author: [WordPress *login username*, if a multi-author site. Not a
display name and not a byline — this is the account the post is filed under.
The draft handoff's own "Author:" line is a different field entirely: that
one names who "I" is for citation verification. "Author:" is still accepted
here for existing documents.]

SEO METADATA
Focus keyword: [keyword or phrase | or: derive from primary claim]
SEO title: [the search-facing title, 20-60 chars | or: use OG title.
Separate from the post title, so a short title such as "About" can stay
short on the page and still be descriptive in search results.]
Meta description: [under 155 characters | or: derive from opening paragraph]
OG title: [or: use article title]
OG description: [or: use meta description]
[No schema field: the script does not set schema. Rank Math applies its own
default for each post type, set under Rank Math SEO > Titles & Meta > Posts
(or Pages) > Schema Type. To change it for one post, use the Schema tab in
the editor after the push. AboutPage and ContactPage are site-wide instead:
Titles & Meta > Local SEO.]

EMBEDS AND SPECIAL ELEMENTS
[List any shortcodes, React components, charts, iframes, or interactive elements]
[Note: these are preserved verbatim -- the script does not modify embedded content]
[Images are NOT embeds. Do not list them here: see IMAGES AND ALT TEXT below.]

IMAGES AND ALT TEXT
[A note, not a form: write each image in FINAL DRAFT as ordinary Markdown,
alone on its own line, with a blank line above and below it:

![Alt text: what the image shows](images/grid-map.png "Optional caption")

Alt text is the text in the square brackets. A screen reader reads it in place
of the image, so describe what the image shows, not what the file is called.
It is set on the image block and on the media-library item.
Caption is the optional text in quotes after the path. It shows under the
image. Leave the quotes out for no caption. Plain text only.
Source is what is between the parentheses:
  - A file on your disk: a path relative to THIS handoff file, or an absolute
    path. It is uploaded to the WordPress media library when you publish and
    becomes an Image block you can edit in the block editor.
  - An https:// URL of an image that is already hosted. The block points at it;
    nothing is uploaded.
Use forward slashes in paths (C:/photos/map.png). Markdown treats a backslash
as an escape, so C:\photos\_map.png reaches the script as C:\photos_map.png.
Wrap a path that has spaces in angle brackets: ![alt](<my photo.png>)
If a file is missing or is not an image, or an image is inside a sentence or a
list, publishing stops before anything is sent and names the image. Uploaded
files are public as soon as they are uploaded, even though the post stays a draft.

A JPEG, PNG or WebP is uploaded as a scrubbed copy: the EXIF a camera writes --
the GPS position, the device make and model, the capture time -- is removed, the
orientation is applied to the pixels so the photo is not sideways, and the ICC
colour profile is kept. The file on your disk is not changed. The IMAGES list
before the checklist says what each file carried and what happened to it.
Other types (.heic, .tif, .gif, .avif, .svg) are uploaded as they are, and an
image of one of those that records a location is called out in that list.

Stripping metadata does not make a photo anonymous. A photo of a screen can show
a location in plain text that no metadata strip touches: a navigation unit's
coordinates, a map, a monitoring dashboard, a terminal with a hostname, a
visible street sign or door number. Open each photo at full size and read what
is actually in the frame before you publish it.]

DISPOSITION LOG
Consensus flags addressed: [count]
Consensus flags dismissed: [count with brief reasoning]
Voice flags accepted: [count]
Voice flags rejected: [count]
Argument flags accepted: [count]
Red team findings addressed: [yes/no for each of the three]
Manual overrides: [any place you kept something every model flagged, and why]

FINAL DRAFT
[Full article text, clean and approved for publication]
