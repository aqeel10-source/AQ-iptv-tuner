"""E2E tests for HDHomeRun endpoints: discover, lineup, device XML."""
import pytest
import main


class TestDiscoverJSON:
    """GET /discover.json — HDHR device discovery."""

    def test_returns_200(self, app_client):
        resp = app_client.get("/discover.json")
        assert resp.status_code == 200

    def test_has_required_fields(self, app_client):
        data = app_client.get("/discover.json").json()
        assert "FriendlyName" in data
        assert "Manufacturer" in data
        assert "ModelNumber" in data
        assert "TunerCount" in data
        assert "DeviceID" in data
        assert "BaseURL" in data
        assert "LineupURL" in data

    def test_friendly_name(self, app_client):
        data = app_client.get("/discover.json").json()
        assert data["FriendlyName"] == "IPTV HDHomeRun Tuner"

    def test_manufacturer(self, app_client):
        data = app_client.get("/discover.json").json()
        assert data["Manufacturer"] == "Silicondust"

    def test_lineup_url_contains_base(self, app_client):
        data = app_client.get("/discover.json").json()
        assert "/lineup.json" in data["LineupURL"]


class TestDeviceXML:
    """GET /device.xml — UPnP device descriptor."""

    def test_returns_xml(self, app_client):
        resp = app_client.get("/device.xml")
        assert resp.status_code == 200
        assert "xml" in resp.headers.get("content-type", "")

    def test_contains_device_info(self, app_client):
        resp = app_client.get("/device.xml")
        body = resp.text
        assert "IPTV HDHomeRun Tuner" in body
        assert "Silicondust" in body
        assert "HDTC-900" in body


class TestLineupStatus:
    """GET /lineup_status.json — scan status."""

    def test_returns_200(self, app_client):
        resp = app_client.get("/lineup_status.json")
        assert resp.status_code == 200

    def test_scan_not_in_progress(self, app_client):
        data = app_client.get("/lineup_status.json").json()
        assert data["ScanInProgress"] == 0
        assert data["ScanPossible"] == 1


class TestLineupJSON:
    """GET /lineup.json — channel listing."""

    def test_empty_when_no_channels(self, app_client):
        data = app_client.get("/lineup.json").json()
        assert data == []

    def test_lists_channels(self, app_client, make_channel):
        make_channel(name="ESPN", number="100", group="Sports")
        make_channel(name="CNN", number="200", group="News")
        data = app_client.get("/lineup.json").json()
        assert len(data) == 2

    def test_channel_fields(self, app_client, make_channel):
        make_channel(name="ESPN", number="100", group="Sports", quality="HD")
        data = app_client.get("/lineup.json").json()
        ch = data[0]
        assert ch["GuideName"] == "ESPN"
        assert ch["GuideNumber"] == "100"
        assert "URL" in ch
        assert ch["HD"] == 1
        assert "Tags" in ch

    def test_sd_channel_not_hd(self, app_client, make_channel):
        make_channel(name="TBS", number="300", group="General", quality="SD")
        data = app_client.get("/lineup.json").json()
        assert data[0]["HD"] == 0

    def test_excludes_dead_channels(self, app_client, make_channel):
        import time
        ch = make_channel(name="ESPN", number="100", group="Sports")
        main._DEAD_CHANNELS[ch.id] = {"count": 10, "last_ts": time.time()}
        data = app_client.get("/lineup.json").json()
        assert len(data) == 0
