"""The publication's writing rules, as a block for the SEO prompts.

``seo_suggest`` and ``seo_content`` write text an author may publish: a meta
description, an OG title and description, replacement headings and openings.
The voice_style review is the only prompt that was handed the rules that text
has to meet, so a suggestion could carry exactly the habit the voice check
flags in the draft (issue #343).

The same three sources the review prompts read in ``pipeline._run_domain``:
``style_profile`` (``voice_profile`` is the older key), and ``style_rules``
with its ``banned_words``, ``banned_phrases`` and ``positive_rules``.

This only asks. A model can still ignore it, and the observed failure, an em
dash, is free text inside a voice profile rather than an entry in a list this
module could match. A mechanical check on the output would need a structured
rule to check against.
"""

HEADING = "PUBLICATION STYLE RULES"

#: Added to a system prompt that writes text. Says what the user prompt's block
#: is for; the block itself is absent when the publication configures nothing.
SYSTEM_RULE = (
    f"- When the prompt includes a {HEADING} section, everything you write "
    "(descriptions, titles, headings, openings, rationales) must follow it. "
    "Those rules are the author's, and they override your own phrasing habits."
)


def style_block(pub_config):
    """The rules block for a user prompt, or ``""`` when none are configured."""
    pub_config = pub_config or {}
    style = pub_config.get("style_rules") or {}

    sections = []
    profile = pub_config.get("style_profile") or pub_config.get("voice_profile")
    if profile:
        sections.append(f"VOICE PROFILE:\n{profile}")
    if style.get("banned_words"):
        sections.append("BANNED WORDS: " + ", ".join(style["banned_words"]))
    if style.get("banned_phrases"):
        sections.append("BANNED PHRASES: " + ", ".join(style["banned_phrases"]))
    if style.get("positive_rules"):
        sections.append(
            "RULES TO FOLLOW:\n" + "\n".join(f"- {r}" for r in style["positive_rules"])
        )
    if not sections:
        return ""
    return f"{HEADING} (text you write must follow these):\n\n" + "\n\n".join(sections)
