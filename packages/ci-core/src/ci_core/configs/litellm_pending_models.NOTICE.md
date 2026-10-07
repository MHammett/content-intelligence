# Notice for litellm_pending_models.json

`litellm_pending_models.json` is not original to this project. Each entry in it
(`gpt-6-astra`, `gpt-6-luna`, `gpt-6-sol`, `gpt-6.1-sol`) is copied verbatim from
`model_prices_and_context_window.json` in
[BerriAI/litellm](https://github.com/BerriAI/litellm), at commit
`b370996b9d2fc9aaec356013a698711ee3e127cc`. Why it is here, and when it goes, is
in `ci_core.llm.client._register_pending_models`.

litellm is available under the MIT license, reproduced below as that license
asks. Everything outside litellm's `enterprise/` directory is MIT; this file is
not from that directory.

```
MIT License

Copyright (c) 2023 Berri AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Deleting `litellm_pending_models.json` (the shim's own exit condition) deletes
this notice with it.
