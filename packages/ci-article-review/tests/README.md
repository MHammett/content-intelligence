# Tests

Run all tests:
```
pytest tests/
```

`test_adapters.py` — unit tests for LanguageTool, OpenAI, Mistral, and Gemini adapters.
All external HTTP calls are mocked; no API keys required.

`test_consolidation.py` — unit tests for consensus detection, delta computation, and report building.

`test_docs_current.py` — documentation drift guards. Reads repo files (README.md,
docs/, the .md tree) and fails when a CLI flag, source module, or citation adapter
exists in code but not in the docs, or when a relative markdown link points at a
path that no longer exists, or at a `#anchor` that no heading in the target file
produces. If one of these fails, the fix is almost always to update the doc it
names — not the test.
