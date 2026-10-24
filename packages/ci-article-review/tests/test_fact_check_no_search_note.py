"""A model with no search tool must be told so, or it may answer with nothing.

grok runs ``fact_check`` with no web search in every preset, and the prompt
opens "verify ... using your search capability". Measured 2026-10-23 on one
2,183-character draft at ``reasoning_effort: high`` with the strict schema: 3 of
4 calls (and the one pipeline-path call) came back as the all-empty answer, in
8-16 s with 500-1,200 completion tokens, finish_reason "stop". The reasoning of
those calls planned searches; the same request with ``json_object`` produced
``{"type": "tool_call", "name": "search", ...}`` as its answer, and with no
response_format at all produced a sentence announcing a search and then no JSON.
The strict schema only decided what the announcement looked like: the one
schema-valid "I will be back" is seven empty arrays. With a paragraph saying
what to do without a search tool, 8 of 8 calls answered (4 probes, 4 through the
pipeline), 13-22 entries each.

This pins the paragraph, not the model's behaviour.
"""

from ci_article_review import pipeline


def _fact_check_prompt():
    return pipeline._load_prompt(pipeline._DOMAIN_PROMPTS["fact_check"])


class TestNoSearchToolNote:
    def test_fact_check_says_what_to_do_without_a_search_tool(self):
        text = _fact_check_prompt()
        assert "IF YOU HAVE NO SEARCH TOOL" in text
        assert "do not write a tool call" in text
        assert "do not end your turn before the JSON" in text

    def test_the_note_precedes_the_task_so_it_reads_before_the_buckets(self):
        text = _fact_check_prompt()
        assert text.index("IF YOU HAVE NO SEARCH TOOL") < text.index("YOUR TASK:")

    def test_it_still_routes_unsupported_claims_to_the_honest_buckets(self):
        text = _fact_check_prompt()
        note = text[text.index("IF YOU HAVE NO SEARCH TOOL") : text.index("YOUR TASK:")]
        assert '"unverifiable"' in note
        assert '"primary_source_needed"' in note

    def test_the_other_domains_are_unchanged(self):
        for domain, name in pipeline._DOMAIN_PROMPTS.items():
            if domain == "fact_check":
                continue
            assert "NO SEARCH TOOL" not in pipeline._load_prompt(name)

    def test_rendering_leaves_no_placeholder_behind(self):
        rendered = pipeline._render_prompt(
            _fact_check_prompt(),
            publication_description="p",
            audience="a",
            voice_profile="v",
            out_of_scope_passages="",
        )
        assert "{" not in rendered.split("RETURN FORMAT")[0]
