/*
 * What the WordPress block editor writes for the blocks that
 * ci_article_review.adapters.cms.blocks emits, and a check of markup against
 * the editor's own validator.
 *
 * Paste this into the browser console of any block editor (Posts > Add New is
 * enough, and so is a disposable WordPress Playground) and run it. It reads the
 * editor's block registry, saves nothing and touches no post. The only requests
 * it makes are a GET for each of three editor script files, to record which
 * build the editor was. README.md in this directory says how to open an editor
 * and how to carry the result out of the console.
 *
 *   await wpEditorBlocks.capture()
 *     Returns { sha256, json }. `json` is the text of blocks.json, and `sha256`
 *     is its hash, so that a copy can be checked with sha256sum. For each case
 *     below it builds the blocks the Markdown means (createBlock), has the
 *     editor write them down (serialize), reads what it wrote back (parse) and
 *     records whether every block parsed was valid and whether serialising the
 *     parse gives the same text again.
 *
 *   await wpEditorBlocks.check([{ name, markup }, ...])
 *     Returns, for each, whether the editor accepts the markup: the name and
 *     validity of every top-level block, and the first validation issue of an
 *     invalid one. This is how to check what to_blocks writes.
 *
 * Two kinds of case. A case with `blocks` is built from the blocks it means, so
 * the expected text is whatever the editor writes for them, not anyone's reading
 * of its source. A case with `markup` is text written out in full (what the
 * converter sends as raw HTML, or a `>` in a paragraph, which the editor writes
 * differently when it is typed and when it is read back): the editor parses it
 * and the record shows whether it accepted it and wrote it back unchanged.
 */
(() => {
  const { createBlock, serialize, parse, rawHandler } = wp.blocks;
  const { RichTextData } = wp.richText;

  // Content as the editor models what a person types: plain text, escaped when it is written.
  const text = (raw) => RichTextData.fromPlainText(raw);
  const para = (content) => ['core/paragraph', { content }];
  const quote = (...paragraphs) => ['core/quote', {}, paragraphs];
  const th = (content, align) => ({ content, tag: 'th', ...(align ? { align } : {}) });
  const td = (content, align) => ({ content, tag: 'td', ...(align ? { align } : {}) });
  const table = (head, body) => ['core/table', { head: [{ cells: head }], body: body.map((cells) => ({ cells })) }];
  const separator = () => ['core/separator'];
  // Code the way the editor reads it off a page: the text goes into a <pre><code> through the DOM, which
  // escapes it as a browser does, and the editor's own HTML import makes the block. That is the form it
  // writes back after reading stored markup; typed text differs from it only in keeping a bare ">".
  const code = (raw) => () => {
    const pre = document.createElement('pre');
    const el = document.createElement('code');
    el.textContent = raw;
    pre.append(el);
    return rawHandler({ HTML: pre.outerHTML });
  };
  const build = ([name, attributes = {}, inner = []]) => createBlock(name, attributes, inner.map(build));
  // An item of `blocks` is a tree for createBlock, or a function that returns blocks.
  const blocksOf = (item) => (typeof item === 'function' ? item() : [build(item)]);

  const CASES = [
    // core/quote: one core/paragraph per paragraph.
    { name: 'quote-one-paragraph', markdown: '> A quoted paragraph.', blocks: [quote(para('A quoted paragraph.'))] },
    {
      name: 'quote-two-paragraphs',
      markdown: '> First paragraph.\n>\n> Second paragraph.',
      blocks: [quote(para('First paragraph.'), para('Second paragraph.'))],
    },
    {
      name: 'quote-inline-formatting',
      markdown: '> **Bold**, *italic*, `code` and [a link](https://example.com/x).',
      blocks: [quote(para('<strong>Bold</strong>, <em>italic</em>, <code>code</code> and <a href="https://example.com/x">a link</a>.'))],
    },
    {
      name: 'quote-special-characters',
      markdown: '> Fish & chips cost < 5 pounds.',
      blocks: [quote(para(text('Fish & chips cost < 5 pounds.')))],
    },
    {
      name: 'quote-greater-than',
      markdown: '> 5 > 3 and 3 < 5.',
      markup: '<!-- wp:quote -->\n<blockquote class="wp-block-quote"><!-- wp:paragraph -->\n<p>5 &gt; 3 and 3 &lt; 5.</p>\n<!-- /wp:paragraph --></blockquote>\n<!-- /wp:quote -->',
    },

    // core/table: a figure around the table, no whitespace between tags, alignment on the cells.
    { name: 'table-basic', markdown: '| A |\n|---|\n| b |', blocks: [table([th('A')], [[td('b')]])] },
    {
      name: 'table-bold-cell',
      markdown: '| Name | Count |\n|---|---|\n| **Bold cell** | 2 |\n| Plain | 30 |',
      blocks: [table([th('Name'), th('Count')], [[td('<strong>Bold cell</strong>'), td('2')], [td('Plain'), td('30')]])],
    },
    {
      name: 'table-column-alignment',
      markdown: '| Left | Centre | Right | None |\n|:--|:-:|--:|---|\n| a | b | c | d |',
      blocks: [table([th('Left', 'left'), th('Centre', 'center'), th('Right', 'right'), th('None')], [[td('a', 'left'), td('b', 'center'), td('c', 'right'), td('d')]])],
    },
    {
      name: 'table-empty-cells',
      markdown: '| | B |\n|---|---|\n| | d |',
      blocks: [table([th(''), th('B')], [[td(''), td('d')]])],
    },
    {
      // Python-Markdown gives a table with only a header and a separator one empty body row.
      name: 'table-header-only',
      markdown: '| A | B |\n|---|---|',
      blocks: [table([th('A'), th('B')], [[td(''), td('')]])],
    },
    {
      // A table with no body rows has no tbody at all in the editor's output.
      name: 'table-raw-html-without-body-rows',
      markdown: '<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody></tbody></table>',
      blocks: [table([th('A'), th('B')], [])],
    },
    {
      name: 'table-special-characters',
      markdown: '| R&D | a < b |\n|---|---|\n| "q" | it\'s |',
      blocks: [table([th(text('R&D')), th(text('a < b'))], [[td(text('"q"')), td(text("it's"))]])],
    },
    {
      name: 'table-greater-than',
      markdown: '| x > y |\n|---|\n| b |',
      markup: '<!-- wp:table -->\n<figure class="wp-block-table"><table class="has-fixed-layout"><thead><tr><th>x &gt; y</th></tr></thead><tbody><tr><td>b</td></tr></tbody></table></figure>\n<!-- /wp:table -->',
    },

    // core/code: no language, and the escapes the block's save() applies.
    { name: 'code-fenced-with-language', markdown: '```python\nprint("hi")\n```', blocks: [code('print("hi")\n')] },
    { name: 'code-fenced-plain', markdown: '```\nplain text\n```', blocks: [code('plain text\n')] },
    {
      name: 'code-indented',
      markdown: 'Intro.\n\n    first line\n    second line',
      blocks: [para('Intro.'), code('first line\nsecond line\n')],
    },
    {
      name: 'code-blank-line-and-indent',
      markdown: '```\ndef f(x):\n\n    return x\n```',
      blocks: [code('def f(x):\n\n    return x\n')],
    },
    {
      name: 'code-special-characters',
      markdown: '```\nif a < b && c > d:\n    print("it\'s")\n```',
      blocks: [code('if a < b && c > d:\n    print("it\'s")\n')],
    },
    {
      name: 'code-shortcode-brackets',
      markdown: '```\n[caption id="a"]\nx[0] = y[1]\n```',
      blocks: [code('[caption id="a"]\nx[0] = y[1]\n')],
    },
    {
      // Only the first URL on a line of its own is escaped.
      name: 'code-isolated-urls',
      markdown: '```\nhttps://example.com/a\nhttps://example.com/b\n```',
      blocks: [code('https://example.com/a\nhttps://example.com/b\n')],
    },
    {
      name: 'code-url-in-a-sentence',
      markdown: '```\nsee https://example.com/a here\n```',
      blocks: [code('see https://example.com/a here\n')],
    },
    { name: 'code-non-ascii', markdown: '```\ncafé — “q” ✓\n```', blocks: [code('café — “q” ✓\n')] },
    { name: 'code-empty', markdown: '```\n```', blocks: [code('')] },

    // core/separator
    { name: 'separator-dashes', markdown: '---', blocks: [separator()] },
    { name: 'separator-asterisks', markdown: '* * *', blocks: [separator()] },

    // What has no native form goes out as raw HTML, which the editor must accept as it is.
    {
      name: 'quote-with-a-list-is-html',
      markdown: '> - one\n> - two',
      markup: '<!-- wp:html -->\n<blockquote>\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n</blockquote>\n<!-- /wp:html -->',
    },
    {
      name: 'nested-quote-is-html',
      markdown: '> outer\n>\n> > inner',
      markup: '<!-- wp:html -->\n<blockquote>\n<p>outer</p>\n<blockquote>\n<p>inner</p>\n</blockquote>\n</blockquote>\n<!-- /wp:html -->',
    },
    {
      name: 'table-with-colspan-is-html',
      markdown: '<table><tr><td colspan="2">x</td></tr></table>',
      markup: '<!-- wp:html -->\n<table><tr><td colspan="2">x</td></tr></table>\n<!-- /wp:html -->',
    },
    {
      name: 'code-with-a-class-is-html',
      markdown: '<pre class="x"><code>raw</code></pre>',
      markup: '<!-- wp:html -->\n<pre class="x"><code>raw</code></pre>\n<!-- /wp:html -->',
    },
    {
      name: 'figure-is-html',
      markdown: '<figure><img src="https://example.com/a.png" alt="A" /><figcaption>Cap</figcaption></figure>',
      markup: '<!-- wp:html -->\n<figure><img src="https://example.com/a.png" alt="A"><figcaption>Cap</figcaption></figure>\n<!-- /wp:html -->',
    },
    {
      name: 'rule-with-a-class-is-html',
      markdown: '<hr class="x" />',
      markup: '<!-- wp:html -->\n<hr class="x">\n<!-- /wp:html -->',
    },
  ];

  // The native blocks side by side in one document, in an order Markdown cannot merge (two quotes in
  // a row would become one quote).
  const TOGETHER = [
    'quote-one-paragraph', 'table-bold-cell', 'code-fenced-with-language', 'separator-dashes',
    'quote-two-paragraphs', 'table-column-alignment', 'code-special-characters',
  ];
  const byName = Object.fromEntries(CASES.map((c) => [c.name, c]));
  CASES.push({
    name: 'every-native-block-together',
    markdown: TOGETHER.map((n) => byName[n].markdown).join('\n\n'),
    blocks: TOGETHER.flatMap((n) => byName[n].blocks),
  });

  // A validation issue is { log, args }, args being a printf-style format and its values.
  const format = (args) => {
    const [message, ...values] = args;
    let i = 0;
    return String(message).replace(/%[sodO]/g, () => {
      const value = values[i++];
      return typeof value === 'string' ? value : JSON.stringify(value);
    });
  };
  const allValid = (block) => block.isValid && block.innerBlocks.every(allValid);
  const firstIssue = (block) => {
    if (!block.isValid) {
      const issue = (block.validationIssues || [])[0];
      return format(issue ? issue.args : ['(the editor gave no detail)']).split('\n\nContent generated')[0].slice(0, 200);
    }
    for (const inner of block.innerBlocks) {
      const found = firstIssue(inner);
      if (found) return found;
    }
    return null;
  };

  const sha = async (input) => {
    if (!(window.crypto && crypto.subtle)) return null;
    const bytes = typeof input === 'string' ? new TextEncoder().encode(input) : input;
    return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((b) => b.toString(16).padStart(2, '0')).join('');
  };

  const provenance = async () => {
    const footer = (document.querySelector('#footer-upgrade') || {}).textContent || '';
    const base = location.pathname.split('/wp-admin')[0];
    const bundles = {};
    for (const name of ['blocks', 'block-library', 'block-editor']) {
      try {
        const bytes = await (await fetch(`${base}/wp-includes/js/dist/${name}.min.js`)).arrayBuffer();
        bundles[`${name}.min.js`] = (await sha(bytes) || '').slice(0, 12) || null;
      } catch (error) {
        bundles[`${name}.min.js`] = null;
      }
    }
    return {
      captured_on: new Date().toISOString().slice(0, 10),
      wordpress: (footer.match(/\d+\.\d+(?:\.\d+)?/) || [null])[0],
      bundles_sha256_12: bundles,
      script: 'tests/golden/wordpress_editor/capture.js',
    };
  };

  const capture = async () => {
    const cases = CASES.map((c) => {
      const expected = c.markup !== undefined ? serialize(parse(c.markup)) : serialize(c.blocks.flatMap(blocksOf));
      const parsed = parse(expected);
      return {
        name: c.name,
        markdown: c.markdown,
        expected,
        names: parsed.map((b) => b.name),
        valid: parsed.length > 0 && parsed.every(allValid),
        round_trip: serialize(parsed) === expected,
      };
    });
    const json = JSON.stringify({ provenance: await provenance(), cases }, null, 2) + '\n';
    return { sha256: await sha(json), json };
  };

  const check = async (inputs) =>
    inputs.map(({ name, markup }) => {
      const blocks = parse(markup);
      return {
        name,
        blocks: blocks.map((b) => ({ name: b.name, valid: allValid(b), ...(allValid(b) ? {} : { issue: firstIssue(b) }) })),
        // False is not a failure: the editor writes a heading with a class the converter does not,
        // and spaces a list differently, and accepts both. It means "not byte for byte what I write".
        round_trip: serialize(blocks) === markup,
      };
    });

  window.wpEditorBlocks = { capture, check, cases: CASES.map((c) => c.name) };
  return `wpEditorBlocks ready: ${CASES.length} cases`;
})();
