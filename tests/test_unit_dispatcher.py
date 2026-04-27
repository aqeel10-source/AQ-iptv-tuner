"""Unit tests for the StreamDispatcher: scoring, filtering, caching, and failure tracking."""
import time
import pytest
import main


class TestScoreCandidate:
    """_score_candidate() produces a composite score from health, load, priority, and penalties."""

    def test_healthy_sub_scores_high(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5, priority=1)
        make_sub_health(name="S1", fetch_ok=True, tcp_ok=True)
        cand = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score, breakdown = main.dispatcher._score_candidate(cand, sub)
        assert score > 0
        assert "S1:" in breakdown

    def test_high_load_lowers_score(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=2, priority=1)
        sub.active_streams = 1  # 50% load
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score_half, _ = main.dispatcher._score_candidate(cand, sub)

        sub2 = make_subscription(name="S2", max_streams=2, priority=1)
        sub2.active_streams = 0  # 0% load
        make_sub_health(name="S2")
        cand2 = main.ChannelCandidate(sub_name="S2", stream_id="2")
        score_empty, _ = main.dispatcher._score_candidate(cand2, sub2)

        assert score_empty > score_half

    def test_lower_priority_number_scores_higher(self, make_subscription, make_sub_health):
        sub1 = make_subscription(name="S1", max_streams=5, priority=1)
        make_sub_health(name="S1")
        cand1 = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score1, _ = main.dispatcher._score_candidate(cand1, sub1)

        sub2 = make_subscription(name="S2", max_streams=5, priority=3)
        make_sub_health(name="S2")
        cand2 = main.ChannelCandidate(sub_name="S2", stream_id="2")
        score2, _ = main.dispatcher._score_candidate(cand2, sub2)

        assert score1 > score2

    def test_recent_fail_60s_penalty(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5, priority=1)
        make_sub_health(name="S1")
        cand_ok = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score_ok, _ = main.dispatcher._score_candidate(cand_ok, sub)

        cand_fail = main.ChannelCandidate(sub_name="S1", stream_id="2",
                                          last_fail_ts=time.time() - 30)
        score_fail, _ = main.dispatcher._score_candidate(cand_fail, sub)

        assert score_ok > score_fail
        assert (score_ok - score_fail) >= 40  # penalty is ~50

    def test_recent_fail_300s_penalty(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5, priority=1)
        make_sub_health(name="S1")
        cand_ok = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score_ok, _ = main.dispatcher._score_candidate(cand_ok, sub)

        cand_fail = main.ChannelCandidate(sub_name="S1", stream_id="2",
                                          last_fail_ts=time.time() - 120)
        score_fail, _ = main.dispatcher._score_candidate(cand_fail, sub)

        assert score_ok > score_fail
        assert (score_ok - score_fail) >= 15  # penalty is ~20

    def test_dispatch_weight_multiplier(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5, priority=1)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="1")

        score_normal, _ = main.dispatcher._score_candidate(cand, sub, dispatch_weight=1.0)
        score_boosted, _ = main.dispatcher._score_candidate(cand, sub, dispatch_weight=2.0)

        assert score_boosted > score_normal

    def test_zero_max_streams_treats_as_full(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=0, priority=1)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="1")
        score, _ = main.dispatcher._score_candidate(cand, sub)
        # load_pts should be 0 since max_streams is 0
        assert score >= 0


class TestDispatcherPick:
    """dispatcher.pick() returns an ordered Decision or None."""

    def test_returns_none_for_no_candidates(self, make_subscription):
        make_subscription(name="S1")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="", candidates=[])
        result = main.dispatcher.pick(ch)
        assert result is None

    def test_returns_decision_with_candidates(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        result = main.dispatcher.pick(ch)
        assert result is not None
        assert len(result.ordered) == 1
        assert result.ordered[0].sub_name == "S1"
        assert "score:" in result.reason

    def test_orders_by_score_descending(self, make_subscription, make_sub_health):
        sub1 = make_subscription(name="S1", max_streams=5, priority=1)
        sub2 = make_subscription(name="S2", max_streams=5, priority=3)
        make_sub_health(name="S1")
        make_sub_health(name="S2")
        cands = [
            main.ChannelCandidate(sub_name="S2", stream_id="200", priority=3),
            main.ChannelCandidate(sub_name="S1", stream_id="100", priority=1),
        ]
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=cands)
        result = main.dispatcher.pick(ch)
        assert result is not None
        assert result.ordered[0].sub_name == "S1"  # higher priority = first

    def test_skips_disabled_subscription(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5, enabled=False)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        result = main.dispatcher.pick(ch)
        assert result is None

    def test_skips_full_subscription(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=1)
        sub.active_streams = 1
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        result = main.dispatcher.pick(ch)
        assert result is None

    def test_skips_drained_subscription(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        main._DRAINED_SUBS.add("S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        result = main.dispatcher.pick(ch)
        assert result is None

    def test_user_allowed_subscriptions_filter(self, make_subscription, make_sub_health, make_user):
        make_subscription(name="S1", max_streams=5)
        make_subscription(name="S2", max_streams=5)
        make_sub_health(name="S1")
        make_sub_health(name="S2")
        user = make_user(username="viewer", allowed_subscriptions=["S2"])
        cands = [
            main.ChannelCandidate(sub_name="S1", stream_id="100"),
            main.ChannelCandidate(sub_name="S2", stream_id="200"),
        ]
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=cands)
        result = main.dispatcher.pick(ch, user=user)
        assert result is not None
        assert all(c.sub_name == "S2" for c in result.ordered)

    def test_sticky_cache_returns_same_decision(self, make_subscription, make_sub_health):
        sub = make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        d1 = main.dispatcher.pick(ch)
        d2 = main.dispatcher.pick(ch)
        assert d1 is d2  # same cached object


class TestDispatcherFailure:
    """mark_failure(), invalidate(), and cache management."""

    def test_mark_failure_bumps_candidate(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        decision = main.dispatcher.pick(ch)
        assert cand.fail_count == 0
        main.dispatcher.mark_failure(decision, 0, "http_error")
        assert cand.fail_count == 1
        assert cand.last_fail_ts > 0

    def test_mark_failure_tracks_dispatch_stats(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_subscription(name="S2", max_streams=5)
        make_sub_health(name="S1")
        make_sub_health(name="S2")
        cands = [
            main.ChannelCandidate(sub_name="S1", stream_id="100"),
            main.ChannelCandidate(sub_name="S2", stream_id="200"),
        ]
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=cands)
        decision = main.dispatcher.pick(ch)
        main.dispatcher.mark_failure(decision, 0, "error")
        assert main._DISPATCH_STATS["S1"]["failovers_from"] == 1
        assert main._DISPATCH_STATS["S2"]["failovers_to"] == 1

    def test_mark_failure_last_candidate_clears_cache(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        decision = main.dispatcher.pick(ch)
        assert main.dispatcher.cache_size() == 1
        main.dispatcher.mark_failure(decision, 0, "error")
        assert main.dispatcher.cache_size() == 0

    def test_invalidate_by_channel(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        main.dispatcher.pick(ch)
        assert main.dispatcher.cache_size() == 1
        dropped = main.dispatcher.invalidate("ch1")
        assert dropped == 1
        assert main.dispatcher.cache_size() == 0

    def test_invalidate_all(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        for i in range(3):
            cand = main.ChannelCandidate(sub_name="S1", stream_id=str(i))
            ch = main.Channel(id=f"ch{i}", name=f"Ch{i}", number=str(i),
                              logo="", group="G", stream_id=str(i),
                              candidates=[cand])
            main.state.channels[ch.id] = ch
            main.dispatcher.pick(ch)
        assert main.dispatcher.cache_size() == 3
        dropped = main.dispatcher.invalidate_all()
        assert dropped == 3
        assert main.dispatcher.cache_size() == 0


class TestDrainedSubs:
    """_DRAINED_SUBS controls which subscriptions are excluded from dispatch."""

    def test_is_sub_drained_false_by_default(self):
        assert not main._is_sub_drained("S1")

    def test_is_sub_drained_true_after_add(self):
        main._DRAINED_SUBS.add("S1")
        assert main._is_sub_drained("S1")

    def test_drain_cleared_on_remove(self):
        main._DRAINED_SUBS.add("S1")
        main._DRAINED_SUBS.discard("S1")
        assert not main._is_sub_drained("S1")
