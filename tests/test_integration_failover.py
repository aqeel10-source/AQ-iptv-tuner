"""Integration tests for dispatch failover logic: poisoned candidates, exhaustion, limits."""
import time

import pytest
import main


class TestPreDispatchFailover:
    """When top candidates are poisoned (recent failures), dispatcher picks the next."""

    def test_skips_recently_failed_candidate(self, make_subscription, make_sub_health):
        """Poison first candidate with a recent failure; dispatcher should pick second."""
        make_subscription(name="S1", max_streams=5, priority=1)
        make_subscription(name="S2", max_streams=5, priority=1)
        make_sub_health(name="S1")
        make_sub_health(name="S2")

        cands = [
            main.ChannelCandidate(sub_name="S1", stream_id="100",
                                  last_fail_ts=time.time() - 10, fail_count=3),
            main.ChannelCandidate(sub_name="S2", stream_id="200"),
        ]
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=cands)
        decision = main.dispatcher.pick(ch)
        assert decision is not None
        # S2 should be first because S1 has a recent failure penalty
        assert decision.ordered[0].sub_name == "S2"

    def test_multiple_poisoned_picks_last_healthy(self, make_subscription, make_sub_health):
        """Poison first two, third should be picked."""
        make_subscription(name="S1", max_streams=5, priority=1)
        make_subscription(name="S2", max_streams=5, priority=1)
        make_subscription(name="S3", max_streams=5, priority=1)
        make_sub_health(name="S1")
        make_sub_health(name="S2")
        make_sub_health(name="S3")

        now = time.time()
        cands = [
            main.ChannelCandidate(sub_name="S1", stream_id="100",
                                  last_fail_ts=now - 5, fail_count=5),
            main.ChannelCandidate(sub_name="S2", stream_id="200",
                                  last_fail_ts=now - 5, fail_count=5),
            main.ChannelCandidate(sub_name="S3", stream_id="300"),
        ]
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=cands)
        decision = main.dispatcher.pick(ch)
        assert decision is not None
        assert decision.ordered[0].sub_name == "S3"


class TestFailoverExhaustion:
    """When ALL candidates fail, dispatcher returns None."""

    def test_all_candidates_fail_returns_none(self, make_subscription, make_sub_health):
        """All subs disabled or full → no candidates."""
        make_subscription(name="S1", max_streams=1, enabled=False)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        decision = main.dispatcher.pick(ch)
        assert decision is None

    def test_mark_failure_exhausts_decision(self, make_subscription, make_sub_health):
        """After marking failure on the only candidate, cache is cleared."""
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        decision = main.dispatcher.pick(ch)
        main.dispatcher.mark_failure(decision, 0, "all_fail")
        assert main.dispatcher.cache_size() == 0


class TestFailoverLimit:
    """max_failovers_per_session bounds failover attempts."""

    def test_config_has_max_failovers(self):
        cfg = main._dispatch_cfg()
        assert "max_failovers_per_session" in cfg
        assert cfg["max_failovers_per_session"] >= 1

    def test_decision_tracks_failover_count(self, make_subscription, make_sub_health):
        """Decision.failover_count can be incremented."""
        make_subscription(name="S1", max_streams=5)
        make_sub_health(name="S1")
        cand = main.ChannelCandidate(sub_name="S1", stream_id="100")
        ch = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                          group="G", stream_id="100", candidates=[cand])
        decision = main.dispatcher.pick(ch)
        assert decision.failover_count == 0
        decision.failover_count += 1
        assert decision.failover_count == 1


class TestDispatchFailoverStats:
    """Per-sub failover stats tracking for observability."""

    def test_stats_track_failover_from(self, make_subscription, make_sub_health):
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

    def test_stats_accumulate(self, make_subscription, make_sub_health):
        make_subscription(name="S1", max_streams=5)
        make_subscription(name="S2", max_streams=5)
        make_sub_health(name="S1")
        make_sub_health(name="S2")
        # First pick + failure
        cands1 = [
            main.ChannelCandidate(sub_name="S1", stream_id="100"),
            main.ChannelCandidate(sub_name="S2", stream_id="200"),
        ]
        ch1 = main.Channel(id="ch1", name="Ch1", number="1", logo="",
                           group="G", stream_id="100", candidates=cands1)
        main.state.channels["ch1"] = ch1
        d1 = main.dispatcher.pick(ch1)
        main.dispatcher.mark_failure(d1, 0, "error")
        main.dispatcher.invalidate_all()
        # Second pick + failure with fresh candidates
        cands2 = [
            main.ChannelCandidate(sub_name="S1", stream_id="100"),
            main.ChannelCandidate(sub_name="S2", stream_id="200"),
        ]
        ch2 = main.Channel(id="ch2", name="Ch2", number="2", logo="",
                           group="G", stream_id="100", candidates=cands2)
        main.state.channels["ch2"] = ch2
        d2 = main.dispatcher.pick(ch2)
        main.dispatcher.mark_failure(d2, 0, "error")
        assert main._DISPATCH_STATS["S1"]["failovers_from"] == 2


class TestNullPacketConstants:
    """Null MPEG-TS packet used during mid-stream failover."""

    def test_packet_size(self):
        assert len(main._TS_NULL_PACKET) == 188

    def test_packet_sync_byte(self):
        assert main._TS_NULL_PACKET[0:1] == b"\x47"

    def test_packet_pid_1fff(self):
        # PID is in bytes 1-2, bits: 5 bits of byte[1] + 8 bits of byte[2]
        pid = ((main._TS_NULL_PACKET[1] & 0x1F) << 8) | main._TS_NULL_PACKET[2]
        assert pid == 0x1FFF

    def test_burst_is_multiple_of_packet(self):
        assert len(main._TS_NULL_BURST) == 188 * 14
        assert len(main._TS_NULL_BURST) % 188 == 0


class TestTunerStatePick:
    """TunerState.pick_subscription and pick_subscription_for_failover."""

    def test_pick_subscription_by_priority(self, make_subscription):
        make_subscription(name="Backup", priority=2)
        make_subscription(name="Primary", priority=1)
        result = main.state.pick_subscription()
        assert result.name == "Primary"

    def test_pick_subscription_excludes(self, make_subscription):
        make_subscription(name="S1", priority=1)
        make_subscription(name="S2", priority=2)
        result = main.state.pick_subscription(exclude=["S1"])
        assert result.name == "S2"

    def test_pick_subscription_none_available(self, make_subscription):
        make_subscription(name="S1", max_streams=1, enabled=False)
        result = main.state.pick_subscription()
        assert result is None

    def test_pick_subscription_for_failover(self, make_subscription):
        make_subscription(name="S1", priority=1)
        make_subscription(name="S2", priority=2)
        result = main.state.pick_subscription_for_failover("S1")
        assert result.name == "S2"
