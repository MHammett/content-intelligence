"""Where a Gemini model is served on Vertex AI, and how to reach it.

The two Gemini generations this repo has run are served from different places.
The 2.5 models are at every US region and at ``global``. The 3.x models are at
``global`` and at the ``us`` and ``eu`` multi-region endpoints, and at no
regional one: Google's endpoint tables ("Deployments and endpoints", read
2026-09-19) tick no US region for any of them, and the 3.5 Flash model card
lists Standard PayGo as "Global: global, Multi-region: us, eu". A 3.x request
sent to ``us-central1`` is a 404, "Publisher model ... was not found or your
project does not have access to it" (BerriAI/litellm#37989). litellm does not
route around it: it swaps a location only for a model whose map entry lists
``supported_regions``, and no gemini entry does (checked 2026-09-19 against
1.96.2's bundled map and the live one).

So the location depends on the model. A config that names one region for every
model it might run is wrong for one generation or the other, and a cost preset
that moves gemini from 2.5 to 3.x keeps user.yaml's ``location`` (it is an
infrastructure key). ``location_for`` picks the endpoint for the model being
called, and ``us`` means "keep it in the United States" for both generations.

This module imports no litellm, so ci-check can use it.
"""

import logging
import os
import re
import threading

log = logging.getLogger(__name__)

#: The location a 3.x model goes to when nothing names one: the US multi-region
#: endpoint, which keeps machine-learning processing in the United States. It
#: costs 10% more than ``global`` (Vertex's pricing page lists Gemini 3 and
#: later at a "Non-global" rate from 2026-07-01), and ``global`` is the choice
#: for whoever has no reason to keep processing in the US.
DEFAULT_LOCATION = "us"

#: The variable litellm reads when the request names no location.
_LOCATION_ENV = "VERTEXAI_LOCATION"

_GENERATION = re.compile(r"(?:[a-z_]+/)*gemini-(\d+)", re.IGNORECASE)

_said: set[tuple[str, str]] = set()
_said_lock = threading.Lock()


def generation(model):
    """The Gemini generation ``model`` belongs to (2, 3, ...), or None.

    Read off the id, ``gemini-3.5-flash-lite`` being generation 3, with any
    route prefix (``vertex_ai/``, ``models/``) ignored. None for an id that does
    not start ``gemini-<number>`` (``gemini-live-2.5-flash``, a tuned model's
    number), which is then treated as an older model: sent where it is told.
    """
    found = _GENERATION.match(str(model or ""))
    return int(found.group(1)) if found else None


def is_modern(model):
    """Whether ``model`` is a Gemini 3 or later, which thinks by level."""
    gen = generation(model)
    return gen is not None and gen >= 3


def location_for(model, location=None):
    """The Vertex location a request for ``model`` goes to, or None.

    ``location`` is what the config says (``models.gemini.location``), or None.
    An unset one falls to ``VERTEXAI_LOCATION``, litellm's own variable, before
    anything is decided. The rules, by generation:

    * 3.x: ``global``, ``us`` and ``eu`` are used as written. Nothing set means
      ``us``. A US region (``us-central1``) is not served for this model, so it
      becomes ``us``, and says so once. Any other region is used as written:
      Google lists some outside the US for these models, and whoever names one
      meant it.
    * Older: ``us`` becomes ``us-central1``, the US region litellm defaults to,
      since 2.5 has no multi-region endpoint. Everything else is used as
      written, and nothing set stays None, which leaves it to litellm.

    Lowercased, because litellm refuses anything else.
    """
    where = (location or os.environ.get(_LOCATION_ENV) or "").strip().lower()
    if is_modern(model):
        if not where:
            return DEFAULT_LOCATION
        if where.startswith("us-"):
            _say_once(model, where)
            return DEFAULT_LOCATION
        return where
    if where == "us":
        return "us-central1"
    return where or None


def host(location):
    """The API host for a Vertex ``location``.

    ``global`` has its own; the multi-regions (``us``, ``eu``) are served from
    ``aiplatform.<location>.rep.googleapis.com``; a region is a prefix. Google's
    "Deployments and endpoints" page gives all three, and litellm builds the
    same (``get_vertex_base_url``).
    """
    if location == "global":
        return "aiplatform.googleapis.com"
    if "-" not in location:
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


def _say_once(model, where):
    key = (str(model), where)
    with _said_lock:
        if key in _said:
            return
        _said.add(key)
    log.warning(_correction(model, where))


def _correction(model, where):
    return (
        f"models.gemini.location is {where!r}, but {model} is not served at any "
        f"US region on Vertex AI, only at 'global' and the 'us' and 'eu' "
        f"multi-regions. Sending it to 'us'. Set location: us to say so, or "
        f"location: global if processing outside the US is acceptable."
    )


def location_warnings(model_configs):
    """A warning for each gemini config whose location its model is not served at.

    The same correction ``location_for`` makes at call time, said at config load
    instead, before a run spends anything. Only the Vertex route is judged: on
    AI Studio a location is never sent. Nothing here changes the config; the
    request is corrected where it is built.
    """
    if not isinstance(model_configs, dict):
        return []
    cfg = model_configs.get("gemini")
    if (
        not isinstance(cfg, dict)
        or cfg.get("enabled", True) is False
        or cfg.get("provider") != "vertex_ai"
    ):
        return []
    from ci_core.llm import client  # lazy: client imports this module

    model = client._resolve_model("gemini", None, cfg)
    where = str(cfg.get("location") or "").strip().lower()
    if is_modern(model) and where.startswith("us-"):
        return [_correction(model, where)]
    return []
