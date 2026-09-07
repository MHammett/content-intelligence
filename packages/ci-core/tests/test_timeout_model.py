"""Tests for the sliding-scale timeout model."""

import ci_core.llm.timeout_model as tm

CEILING = 1200  # task_timeout_seconds headroom; formula clamps to CEILING - 15
ANCHOR = 62000  # calibration doc size (the 1.0 size bucket)


class TestComputeTimeout:
    def test_gpt55_xhigh_is_clamped_by_the_task_ceiling(self):
        # gpt-5.5 xhigh: base 60 × model 1.3 × effort 10.5 × variance 2.5 ≈ 2047s,
        # which the task ceiling cuts to CEILING - 15. This cell is the reason the
        # variance_margin note in timeouts.yaml warns that raising the margin does
        # nothing here: it was already at the clamp at 1.25 (≈1024s vs a 1085s clamp
        # under the shipped 1100s task_timeout_seconds), and it is further past it
        # now. Raising task_timeout_seconds is the only knob that moves this cell.
        t = tm.compute_timeout(ANCHOR, "gpt-5.5", "xhigh", CEILING)
        assert t == CEILING - 15, t

    def test_variance_margin_applied(self):
        # The configured variance_margin must scale the result above the bare
        # base × model × effort product.
        import copy

        cfg = copy.deepcopy(tm._CONFIG)
        cfg["variance_margin"] = 1.0
        # gpt-5.4 high rather than gpt-5.5 xhigh: the latter clamps at the task
        # ceiling once the margin is applied, which would hide the ratio being
        # tested here (see test_gpt55_xhigh_is_clamped_by_the_task_ceiling).
        bare = tm.compute_timeout(ANCHOR, "gpt-5.4", "high", CEILING, config=cfg)
        withmargin = tm.compute_timeout(ANCHOR, "gpt-5.4", "high", CEILING)
        assert withmargin > bare
        assert round(withmargin / bare, 2) == tm._CONFIG["variance_margin"]
        assert withmargin < CEILING - 15, "cell must not clamp, or the ratio is moot"

    def test_effort_is_steep(self):
        # Same model: high should be several times none (calibration showed ~5x).
        # Threshold is 2.5x rather than the tighter historical 3x because
        # floor_seconds was raised 60->90 (for Grok's thin timeout headroom),
        # which also lifts the floor-clamped "none" baseline for gpt-5.4 — the
        # ratio compresses even though the underlying "high" value (unfloored,
        # effort-driven) hasn't changed.
        none = tm.compute_timeout(ANCHOR, "gpt-5.4", "none", CEILING)
        high = tm.compute_timeout(ANCHOR, "gpt-5.4", "high", CEILING)
        assert high >= 2.5 * none, (none, high)

    def test_floor_enforced(self):
        # A small doc on the cheapest model computes well under the floor and is
        # clamped up. This used to be asserted with Grok, on the premise that
        # "Grok is fast" — a 130k-char run in Aug 2026 disproved that (it used
        # 100% of its budget and timed out), and its multiplier was raised to 2.0,
        # which lifts it clear of the floor at every size. See timeouts.yaml.
        t = tm.compute_timeout(3000, "mistral-small-latest", "none", CEILING)
        assert t == tm._CONFIG["floor_seconds"]

    def test_grok_budget_covers_its_observed_worst_case(self):
        """Grok's budget must clear what it actually took on a large draft.

        Its old 1.2 multiplier produced 126s at 130k chars, which grok:completeness
        hit exactly (timed out) while two other domains finished within 14s of it.
        The true completeness time is unknown — it was cut off, not measured — so
        this guards the calls that *did* complete, with margin.
        """
        t = tm.compute_timeout(130190, "grok-4.20-0309-reasoning", "none", CEILING)
        slowest_completed = 112.83
        assert t >= 180, t
        assert t >= 1.5 * slowest_completed, (t, slowest_completed)

    def test_ceiling_enforced_on_huge_doc(self):
        t = tm.compute_timeout(500000, "gpt-5.5", "xhigh", CEILING)
        assert t == CEILING - 15

    def test_low_ceiling_clamps_xhigh(self):
        # With a tight ceiling, even gpt-5.5 xhigh is clamped down to ceiling - 15.
        t = tm.compute_timeout(ANCHOR, "gpt-5.5", "xhigh", 500)
        assert t == 500 - 15

    def test_size_scales_monotonically(self):
        sizes = [3000, 15000, 40000, 62000, 120000]
        vals = [tm.compute_timeout(c, "gpt-5.5", "xhigh", CEILING) for c in sizes]
        assert vals == sorted(vals), vals

    def test_grounded_model_high_even_without_effort(self):
        # Gemini has no reasoning_effort knob, but grounded latency is captured by
        # its model multiplier — it must exceed a fast model's timeout.
        #
        # The comparison used to be against grok, on the assumption that grok was
        # the fast one. It is not: measured 2026-09-03 in a single run, grok-4.6
        # took up to 377.61s against gemini-2.5-pro's 28.65s. gpt-5.4 at
        # multiplier 1.0 is the actual baseline.
        gem = tm.compute_timeout(ANCHOR, "gemini-2.5-pro", None, CEILING)
        fast = tm.compute_timeout(ANCHOR, "gpt-5.4", None, CEILING)
        assert gem > fast

    def test_grok_is_budgeted_as_the_slow_model_it_measures_as(self):
        """Its cell was a placeholder set from a ceiling a call had just hit;
        the note beside it said the true time was 'cut off, not measured'. Five
        uncensored completions at 2,182 chars — 17.58s to 377.61s — are what it
        is set from now."""
        grok = tm.compute_timeout(2182, "grok-4.6", None, CEILING)
        assert grok > 377.61, "the slowest measured grok call must fit"
        gem = tm.compute_timeout(2182, "gemini-2.5-pro", None, CEILING)
        assert grok > gem

    def test_model_prefix_longest_match(self):
        # "mistral-medium-3-5" must match its own entry, not a shorter prefix.
        med = tm.compute_timeout(ANCHOR, "mistral-medium-3-5", "none", CEILING)
        small = tm.compute_timeout(ANCHOR, "mistral-small-latest", "none", CEILING)
        # medium-3-5 multiplier (1.3) > small (0.8), so medium >= small
        assert med >= small

    def test_unknown_model_uses_default(self):
        t = tm.compute_timeout(ANCHOR, "some-future-model-x", "none", CEILING)
        assert t >= tm._CONFIG["floor_seconds"]

    def test_unknown_effort_falls_back_to_default(self):
        # An unrecognized effort string uses the 'default' multiplier, not a crash.
        t = tm.compute_timeout(ANCHOR, "gpt-5.4", "ultra-mega", CEILING)
        assert t >= tm._CONFIG["floor_seconds"]


class TestComputeAll:
    def test_respects_explicit_override(self):
        cfgs = {
            "openai": {
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 999,
            }
        }
        out = tm.compute_all(ANCHOR, cfgs, CEILING)
        assert out["openai"] == 999  # explicit wins, formula skipped

    def test_computes_when_unset(self):
        cfgs = {"openai": {"model": "gpt-5.5", "reasoning_effort": "xhigh"}}
        out = tm.compute_all(ANCHOR, cfgs, CEILING)
        # Clamped by the task ceiling — see test_gpt55_xhigh_is_clamped_by_the_task_ceiling.
        assert out["openai"] == CEILING - 15

    def test_skips_disabled_models(self):
        cfgs = {
            "openai": {"model": "gpt-5.4", "enabled": False},
            "grok": {"model": "grok-4.20-0309-reasoning"},
        }
        out = tm.compute_all(ANCHOR, cfgs, CEILING)
        assert "openai" not in out
        assert "grok" in out

    def test_reads_effort_from_either_key(self):
        # reasoning_effort (openai/mistral/grok) and effort (claude) both recognized.
        a = tm.compute_all(
            ANCHOR, {"x": {"model": "gpt-5.4", "reasoning_effort": "high"}}, CEILING
        )
        b = tm.compute_all(
            ANCHOR, {"x": {"model": "gpt-5.4", "effort": "high"}}, CEILING
        )
        assert a["x"] == b["x"]


class TestFlagStaleOverrides:
    """Catches the perplexity/claude failure mode: an explicit timeout_seconds
    left over from a lighter preset, silently undercutting what the formula
    would now compute for the model's current effort."""

    def test_flags_an_override_far_below_the_formula(self):
        # claude at effort=high, ANCHOR size: formula computes well above 240s
        # (base 60 x model 1.0 x effort-high 3.5 x variance 2.5 = 525s at this
        # anchor size's 1.0 multiplier) -- the actual 2026-08-18 bug.
        cfgs = {
            "claude": {
                "model": "claude-opus-4-8",
                "effort": "high",
                "timeout_seconds": 240,
            }
        }
        flagged = tm.flag_stale_overrides(ANCHOR, cfgs, CEILING)
        assert len(flagged) == 1
        provider, override, formula = flagged[0]
        assert provider == "claude"
        assert override == 240
        assert formula > 240 * 2  # comfortably below the 0.5 ratio, not a near-miss

    def test_silent_when_no_override_present(self):
        cfgs = {"claude": {"model": "claude-opus-4-8", "effort": "high"}}
        assert tm.flag_stale_overrides(ANCHOR, cfgs, CEILING) == []

    def test_silent_when_override_is_generous(self):
        # An override comfortably above the formula is a deliberate ceiling,
        # not a stale one -- must not be flagged.
        cfgs = {"gemini": {"model": "gemini-2.5-pro", "timeout_seconds": 100000}}
        assert tm.flag_stale_overrides(ANCHOR, cfgs, CEILING) == []

    def test_silent_for_disabled_models(self):
        cfgs = {
            "claude": {
                "model": "claude-opus-4-8",
                "effort": "high",
                "timeout_seconds": 240,
                "enabled": False,
            }
        }
        assert tm.flag_stale_overrides(ANCHOR, cfgs, CEILING) == []

    def test_ratio_is_configurable(self):
        # A 240s override against a formula of ~525s is 0.46x -- flagged at the
        # default 0.5 ratio, not flagged at a looser 0.4 ratio.
        cfgs = {
            "claude": {
                "model": "claude-opus-4-8",
                "effort": "high",
                "timeout_seconds": 240,
            }
        }
        assert tm.flag_stale_overrides(ANCHOR, cfgs, CEILING, ratio=0.5) != []
        assert tm.flag_stale_overrides(ANCHOR, cfgs, CEILING, ratio=0.1) == []


class TestGlobalCeiling:
    """The parallel batch's outer wall-clock bound (pipeline._global_ceiling)."""

    def test_exceeds_slowest_task_with_retry_room(self):
        from ci_article_review.pipeline import _global_ceiling

        # Must sit above the slowest task + a retry_delay + slack, so a transient
        # retry or clean timeout isn't masked as a global-ceiling cancellation.
        c = _global_ceiling([819, 273, 240, 60], retry_delay=10)
        assert c == 819 + 10 + 30  # slowest + retry_delay + 30s slack

    def test_margin_above_slowest_is_more_than_old_plus_10(self):
        from ci_article_review.pipeline import _global_ceiling

        # Regression: the old +10 margin let a retry collide with the ceiling.
        c = _global_ceiling([647], retry_delay=10)
        assert c - 647 > 10

    def test_empty_list_safe(self):
        from ci_article_review.pipeline import _global_ceiling

        assert _global_ceiling([], retry_delay=10) == 40
