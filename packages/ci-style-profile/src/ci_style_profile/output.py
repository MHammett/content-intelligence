"""Output formatters and file writing for synthesized style profiles."""

from __future__ import annotations

import difflib
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import LiteralScalarString

    _RUAMEL_AVAILABLE = True
except ImportError:
    log.warning(
        "ruamel.yaml not installed; falling back to PyYAML (comments in existing YAML will be lost)"
    )
    import yaml as _pyyaml

    _RUAMEL_AVAILABLE = False


def _make_ruamel():
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.preserve_quotes = True
    yaml.width = 120
    return yaml


def _literal(text: str) -> "LiteralScalarString":
    """Wrap a multiline string as a YAML literal block scalar."""
    return LiteralScalarString(text)


class Formatter(ABC):
    @abstractmethod
    def format(self, profile: dict, mode: str = "canonical") -> str: ...


class PublicationYamlFormatter(Formatter):
    """Formats the synthesized profile for embedding in publication.yaml."""

    def format(self, profile: dict, mode: str = "canonical") -> str:
        if _RUAMEL_AVAILABLE:
            return self._format_ruamel(profile, mode)
        return self._format_pyyaml(profile, mode)

    def _build_style_block(self, profile: dict, mode: str) -> dict:
        """Build the style section dict ready for YAML serialization."""
        canonical = (
            profile if mode == "canonical" else profile.get("canonical", profile)
        )

        style_profile_text = canonical.get("style_profile", "")
        audience_primary = canonical.get("audience_primary", "")
        audience_secondary = canonical.get("audience_secondary")
        banned_words = canonical.get("banned_words", [])
        banned_phrases = canonical.get("banned_phrases", [])
        positive_rules = canonical.get("positive_rules", [])

        block: dict = {}

        if _RUAMEL_AVAILABLE:
            block["style_profile"] = (
                _literal(style_profile_text + "\n") if style_profile_text else ""
            )
            block["audience"] = {}
            if audience_primary:
                block["audience"]["primary"] = audience_primary
            if audience_secondary:
                block["audience"]["secondary"] = audience_secondary
        else:
            block["style_profile"] = style_profile_text
            block["audience"] = {}
            if audience_primary:
                block["audience"]["primary"] = audience_primary
            if audience_secondary:
                block["audience"]["secondary"] = audience_secondary

        block["style_rules"] = {
            "banned_words": banned_words,
            "banned_phrases": banned_phrases,
            "positive_rules": positive_rules,
        }

        # Add style_profiles block for detect/per-source modes
        # INTERNAL KEY: detected_styles → EMITTED KEY: style_profiles
        if mode in ("detect", "per-source"):
            detected = profile.get("detected_styles", {})
            style_profiles: dict = {}
            for label, vdata in detected.items():
                vp_text = vdata.get("style_profile", "")
                entry: dict = {}
                if _RUAMEL_AVAILABLE:
                    entry["style_profile"] = _literal(vp_text + "\n") if vp_text else ""
                else:
                    entry["style_profile"] = vp_text
                if vdata.get("additional_banned_words"):
                    entry["additional_banned_words"] = vdata["additional_banned_words"]
                if vdata.get("additional_positive_rules"):
                    entry["additional_positive_rules"] = vdata[
                        "additional_positive_rules"
                    ]
                if vdata.get("style_notes"):
                    entry["style_notes"] = vdata["style_notes"]
                if vdata.get("source_distribution"):
                    entry["source_distribution"] = vdata["source_distribution"]
                if vdata.get("doc_count") is not None:
                    entry["doc_count"] = vdata["doc_count"]
                if vdata.get("confidence"):
                    entry["confidence"] = vdata["confidence"]
                style_profiles[label] = entry

            if style_profiles:
                block["style_profiles"] = style_profiles

        return block

    def _format_ruamel(self, profile: dict, mode: str) -> str:
        import io

        yaml = _make_ruamel()
        block = self._build_style_block(profile, mode)
        buf = io.StringIO()
        yaml.dump(block, buf)
        return buf.getvalue()

    def _format_pyyaml(self, profile: dict, mode: str) -> str:
        block = self._build_style_block(profile, mode)
        return _pyyaml.dump(
            block, default_flow_style=False, allow_unicode=True, sort_keys=False
        )

    def merge_into_existing(
        self, existing_path: str, profile: dict, mode: str = "canonical"
    ) -> str:
        """Load existing YAML, replace style sections, preserve all other keys."""
        existing = Path(existing_path)
        new_style_block = self._build_style_block(profile, mode)

        if not existing.exists():
            # Create fresh file with style block
            return self.format(profile, mode)

        if _RUAMEL_AVAILABLE:
            return self._merge_ruamel(existing, new_style_block)
        return self._merge_pyyaml(existing, new_style_block)

    def _merge_ruamel(self, existing_path: Path, new_style_block: dict) -> str:
        import io

        yaml = _make_ruamel()
        with open(existing_path, encoding="utf-8") as f:
            data = yaml.load(f) or {}

        style_keys = ["style_profile", "audience", "style_rules", "style_profiles"]
        for key in style_keys:
            if key in data:
                del data[key]

        for key, value in new_style_block.items():
            data[key] = value

        buf = io.StringIO()
        yaml.dump(data, buf)
        return buf.getvalue()

    def _merge_pyyaml(self, existing_path: Path, new_style_block: dict) -> str:
        with open(existing_path, encoding="utf-8") as f:
            data = _pyyaml.safe_load(f) or {}

        style_keys = ["style_profile", "audience", "style_rules", "style_profiles"]
        for key in style_keys:
            data.pop(key, None)

        data.update(new_style_block)
        return _pyyaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False
        )

    def diff_style_sections(self, existing_path: str, new_yaml: str) -> str:
        """Return unified diff of style sections between existing file and new YAML."""
        existing = Path(existing_path)
        if not existing.exists():
            return ""
        old_lines = existing.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = new_yaml.splitlines(keepends=True)

        def _extract_style_lines(lines: list[str]) -> list[str]:
            style_keys = {
                "style_profile:",
                "audience:",
                "style_rules:",
                "style_profiles:",
            }
            result = []
            in_style = False
            for line in lines:
                stripped = line.lstrip()
                if any(stripped.startswith(k) for k in style_keys):
                    in_style = True
                elif (
                    not line.startswith(" ")
                    and not line.startswith("\t")
                    and stripped
                    and not stripped.startswith("#")
                ):
                    in_style = False
                if in_style:
                    result.append(line)
            return result

        old_style = _extract_style_lines(old_lines)
        new_style = _extract_style_lines(new_lines)
        return "".join(
            difflib.unified_diff(
                old_style,
                new_style,
                fromfile=f"{existing_path} (existing)",
                tofile="new profile",
            )
        )


class MarkdownReportFormatter(Formatter):
    """Human-readable Markdown summary of the synthesized profile."""

    def format(self, profile: dict, mode: str = "canonical") -> str:
        canonical = (
            profile if mode == "canonical" else profile.get("canonical", profile)
        )
        lines = ["# Style Profile Report", ""]

        vp = canonical.get("style_profile", "")
        if vp:
            lines += ["## Style Profile", "", vp, ""]

        audience = canonical.get("audience") or {}
        if not audience:
            primary = canonical.get("audience_primary")
            secondary = canonical.get("audience_secondary")
        else:
            primary = audience.get("primary")
            secondary = audience.get("secondary")

        if primary or secondary:
            lines += ["## Audience", ""]
            if primary:
                lines += [f"**Primary:** {primary}", ""]
            if secondary:
                lines += [f"**Secondary:** {secondary}", ""]

        style = canonical.get("style_rules") or {}
        banned_words = style.get("banned_words") or canonical.get("banned_words", [])
        banned_phrases = style.get("banned_phrases") or canonical.get(
            "banned_phrases", []
        )
        positive_rules = style.get("positive_rules") or canonical.get(
            "positive_rules", []
        )

        if banned_words or banned_phrases:
            lines += ["## Banned Words & Phrases", ""]
            if banned_words:
                lines += ["| Banned Word |", "|-------------|"]
                for w in banned_words:
                    lines += [f"| {w} |"]
                lines += [""]
            if banned_phrases:
                lines += ["| Banned Phrase |", "|---------------|"]
                for p in banned_phrases:
                    lines += [f"| {p} |"]
                lines += [""]

        if positive_rules:
            lines += ["## Style Rules", ""]
            for i, rule in enumerate(positive_rules, 1):
                lines += [f"{i}. {rule}"]
            lines += [""]

        # Per-style section
        if mode in ("detect", "per-source"):
            detected = profile.get("detected_styles") or profile.get(
                "style_profiles", {}
            )
            if detected:
                lines += ["## Detected Styles", ""]
                for label, vdata in detected.items():
                    lines += [f"### {label}", ""]
                    if vdata.get("style_profile"):
                        lines += [vdata["style_profile"], ""]
                    if vdata.get("source_distribution"):
                        lines += ["**Source distribution:**", ""]
                        lines += ["| Source | % |", "|--------|---|"]
                        for src, pct in sorted(
                            vdata["source_distribution"].items(), key=lambda x: -x[1]
                        ):
                            lines += [f"| {src} | {pct * 100:.0f}% |"]
                        lines += [""]
                    if vdata.get("style_notes"):
                        lines += [f"*{vdata['style_notes']}*", ""]

        return "\n".join(lines)


class JsonFormatter(Formatter):
    """Raw JSON dump of the profile dict."""

    def format(self, profile: dict, mode: str = "canonical") -> str:
        return json.dumps(profile, indent=2, ensure_ascii=False)


def write_atomic(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """Write content atomically using a temp file + os.replace()."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(output_path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise
    log.info("Wrote profile to %s", output_path)


#: Every profile's snapshot history. It sits beside this module, so inside the
#: installed package (gitignored in a checkout). A module constant so the test
#: suite can redirect it: see ``isolate_package_state`` in tests/conftest.py.
_PROFILES_DIR = Path(__file__).parent / "profiles"


def save_versioned_snapshot(
    content: str, publication: str | None, output_yaml: str | None
) -> Path:
    """Save a timestamped copy of the profile."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    profiles_dir = _PROFILES_DIR

    if publication:
        snap_dir = profiles_dir / publication
    elif output_yaml:
        stem = Path(output_yaml).stem
        snap_dir = profiles_dir / "_output" / stem
    else:
        snap_dir = profiles_dir / "_unknown"

    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snap_dir / f"{ts}.yaml"
    write_atomic(snap_path, content)
    log.info("Versioned snapshot: %s", snap_path)
    return snap_path
