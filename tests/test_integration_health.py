"""Integration tests for health monitoring: SubHealth scoring, dead channels, event bus."""
import time

import pytest
import main


class TestSubHealth:
    """SubHealth tracks per-subscription health metrics and computes a 0-100 score."""

    def test_initial_score_is_low(self):
        h = main.SubHealth(name="test")
        sub = main.Subscription(name="test", url="http://x", username="u",
                                password="p", max_streams=5)
        score = h.score(sub)
        assert score >= 0
        assert score <= 100

    def test_record_fetch_success(self):
        h = main.SubHealth(name="test")
        h.record_fetch(True, 500, channels=100)
        assert h.last_fetch_ok is True
        assert h.last_fetch_ms == 500
        assert h.channels_loaded == 100
        assert len(h.fetch_history) >= 1

    def test_record_fetch_failure(self):
        h = main.SubHealth(name="test")
        h.record_fetch(False, 0, err_class="network", err_msg="timeout")
        assert h.last_fetch_ok is False
        assert h.last_error_class == "network"
        assert h.last_error_msg == "timeout"

    def test_record_tcp_probe(self):
        h = main.SubHealth(name="test")
        h.record_tcp(True, 50)
        assert h.tcp_probe_ok is True
        assert h.tcp_probe_ms == 50

    def test_success_rate_all_ok(self):
        h = main.SubHealth(name="test")
        for _ in range(12):
            h.fetch_history.append(True)
        assert h.success_rate(12) == 1.0

    def test_success_rate_half(self):
        h = main.SubHealth(name="test")
        for i in range(12):
            h.fetch_history.append(i % 2 == 0)
        rate = h.success_rate(12)
        assert 0.4 <= rate <= 0.6

    def test_success_rate_empty(self):
        h = main.SubHealth(name="test")
        assert h.success_rate() == 0.0

    def test_uptime_pct_all_ok(self):
        h = main.SubHealth(name="test")
        for _ in range(240):
            h.probe_history.append(True)
        assert h.uptime_pct() == 100.0

    def test_uptime_pct_empty(self):
        h = main.SubHealth(name="test")
        assert h.uptime_pct() == 0.0

    def test_fetch_history_capped_at_48(self):
        h = main.SubHealth(name="test")
        for _ in range(60):
            h.record_fetch(True, 100, channels=10)
        assert len(h.fetch_history) <= 48

    def test_probe_history_capped_at_240(self):
        h = main.SubHealth(name="test")
        for _ in range(300):
            h.record_tcp(True, 50)
        assert len(h.probe_history) <= 240


class TestSubHealthScore:
    """Score composition: fetch_rate + tcp + recency + account + load."""

    def test_perfect_score(self, make_subscription):
        sub = make_subscription(name="S1", max_streams=5)
        h = main.SubHealth(name="S1")
        # (a) 12/12 fetches OK -> 40 pts
        for _ in range(12):
            h.record_fetch(True, 100, channels=50)
        # (b) TCP OK -> 20 pts
        h.record_tcp(True, 50)
        # (c) last fetch < 24h -> 20 pts (already set)
        # (d) account not expiring: 5 pts (unknown = 5)
        # (e) load < 90%: sub.active_streams=0/5 -> 10 pts
        score = h.score(sub)
        assert score >= 90  # should be 95

    def test_dead_sub_scores_low(self, make_subscription):
        sub = make_subscription(name="S1", max_streams=5)
        h = main.SubHealth(name="S1")
        for _ in range(12):
            h.record_fetch(False, 0, err_class="network")
        h.record_tcp(False, 0)
        score = h.score(sub)
        assert score <= 20

    def test_high_load_loses_points(self, make_subscription):
        sub = make_subscription(name="S1", max_streams=5)
        sub.active_streams = 5  # 100% load
        h = main.SubHealth(name="S1")
        for _ in range(12):
            h.record_fetch(True, 100, channels=50)
        h.record_tcp(True, 50)
        score_full = h.score(sub)

        sub2 = make_subscription(name="S2", max_streams=5)
        sub2.active_streams = 0
        h2 = main.SubHealth(name="S2")
        for _ in range(12):
            h2.record_fetch(True, 100, channels=50)
        h2.record_tcp(True, 50)
        score_empty = h2.score(sub2)

        assert score_empty > score_full

    def test_to_dict(self, make_subscription):
        sub = make_subscription(name="S1", max_streams=5)
        h = main.SubHealth(name="S1")
        h.record_fetch(True, 100, channels=50)
        h.record_tcp(True, 50)
        d = h.to_dict(sub)
        assert d["name"] == "S1"
        assert "score" in d
        assert "success_rate_1h" in d
        assert "uptime_pct_24h" in d


class TestGetSubHealth:
    """_get_sub_health lazily creates SubHealth instances."""

    def test_creates_on_first_access(self):
        h = main._get_sub_health("new_sub")
        assert h.name == "new_sub"
        assert "new_sub" in main._SUB_HEALTH

    def test_returns_same_instance(self):
        h1 = main._get_sub_health("same_sub")
        h2 = main._get_sub_health("same_sub")
        assert h1 is h2


class TestDeadChannels:
    """Dead channel tracking: _record_channel_failure and _is_channel_dead."""

    def test_not_dead_initially(self):
        assert main._is_channel_dead("ch1") is False

    def test_dead_after_threshold(self):
        for _ in range(5):
            main._record_channel_failure("ch1")
        assert main._is_channel_dead("ch1") is True

    def test_not_dead_below_threshold(self):
        for _ in range(3):
            main._record_channel_failure("ch1")
        assert main._is_channel_dead("ch1") is False

    def test_window_reset(self):
        """Failures outside the window reset the counter."""
        main._DEAD_CHANNELS["ch1"] = {"count": 4, "last_ts": time.time() - 7200}
        # Next failure is outside the window, should reset
        main._record_channel_failure("ch1")
        assert main._DEAD_CHANNELS["ch1"]["count"] == 1
        assert main._is_channel_dead("ch1") is False


class TestLiveChannels:
    """_live_channels filters out dead channels."""

    def test_returns_all_when_none_dead(self, make_channel):
        make_channel(name="ESPN", group="Sports")
        make_channel(name="CNN", number="200", group="News")
        live = main._live_channels()
        assert len(live) == 2

    def test_excludes_dead(self, make_channel):
        ch = make_channel(name="ESPN", group="Sports")
        make_channel(name="CNN", number="200", group="News")
        main._DEAD_CHANNELS[ch.id] = {"count": 10, "last_ts": time.time()}
        live = main._live_channels()
        assert len(live) == 1
        assert ch.id not in live


class TestEventBus:
    """EventBus pub/sub for SSE clients."""

    def test_subscribe_and_publish(self):
        import asyncio
        loop = asyncio.get_event_loop()
        q = loop.run_until_complete(main.bus.subscribe())
        main.bus.publish("test", {"msg": "hello"})
        msg = q.get_nowait()
        assert msg["topic"] == "test"
        assert msg["data"]["msg"] == "hello"
        main.bus.unsubscribe(q)

    def test_unsubscribe(self):
        import asyncio
        loop = asyncio.get_event_loop()
        q = loop.run_until_complete(main.bus.subscribe())
        count_before = main.bus.subscriber_count
        main.bus.unsubscribe(q)
        assert main.bus.subscriber_count == count_before - 1

    def test_full_queue_drops_message(self):
        import asyncio
        loop = asyncio.get_event_loop()
        q = loop.run_until_complete(main.bus.subscribe())
        # Fill the queue (maxsize=64)
        for i in range(70):
            main.bus.publish("spam", {"i": i})
        # Should not raise — drops on full
        assert q.qsize() == 64
        main.bus.unsubscribe(q)

    def test_subscriber_count(self):
        import asyncio
        loop = asyncio.get_event_loop()
        initial = main.bus.subscriber_count
        q1 = loop.run_until_complete(main.bus.subscribe())
        q2 = loop.run_until_complete(main.bus.subscribe())
        assert main.bus.subscriber_count == initial + 2
        main.bus.unsubscribe(q1)
        main.bus.unsubscribe(q2)
