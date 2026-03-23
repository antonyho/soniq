"""
Unit tests for server.py
Run with: pytest tests/ -v
"""
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ── URL / video ID parsing ────────────────────────────────────────────────

class TestSanitizeVideoId:
    def test_standard_watch_url(self):
        assert server.sanitize_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_www_watch_url(self):
        assert server.sanitize_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert server.sanitize_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        assert server.sanitize_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert server.sanitize_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_bare_video_id(self):
        assert server.sanitize_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        assert server.sanitize_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == "dQw4w9WgXcQ"

    def test_invalid_url_returns_none(self):
        assert server.sanitize_video_id("notaurl") is None

    def test_empty_string_returns_none(self):
        assert server.sanitize_video_id("") is None

    def test_random_url_returns_none(self):
        assert server.sanitize_video_id("https://example.com/video") is None


# ── Health check ──────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_healthz_returns_200(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_returns_json(self, client):
        resp = client.get("/healthz")
        data = json.loads(resp.data)
        assert data["status"] == "ok"


# ── /api/extract ──────────────────────────────────────────────────────────

class TestExtractEndpoint:
    def test_missing_url_returns_400(self, client):
        resp = client.post("/api/extract", json={})
        assert resp.status_code == 400

    def test_invalid_url_returns_400(self, client):
        resp = client.post("/api/extract", json={"url": "notaurl"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_valid_url_returns_job_id(self, client):
        resp = client.post("/api/extract", json={"url": "https://youtube.com/watch?v=dQw4w9WgXcQ", "format": "mp3"})
        assert resp.status_code == 202
        data = json.loads(resp.data)
        assert "job_id" in data
        assert len(data["job_id"]) == 16

    def test_invalid_format_falls_back_to_mp3(self, client):
        resp = client.post("/api/extract", json={"url": "https://youtu.be/dQw4w9WgXcQ", "format": "xyz"})
        assert resp.status_code == 202

    def test_all_valid_formats_accepted(self, client):
        for fmt in ("mp3", "m4a", "wav", "opus"):
            resp = client.post("/api/extract", json={"url": "https://youtu.be/dQw4w9WgXcQ", "format": fmt})
            assert resp.status_code == 202, f"Format {fmt} failed"

    def test_no_json_body_returns_400(self, client):
        resp = client.post("/api/extract", data="not json", content_type="text/plain")
        assert resp.status_code == 400


# ── /api/status ───────────────────────────────────────────────────────────

class TestStatusEndpoint:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/status/doesnotexist")
        assert resp.status_code == 404

    def test_queued_job_returns_status(self, client):
        r = client.post("/api/extract", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        job_id = json.loads(r.data)["job_id"]
        resp = client.get(f"/api/status/{job_id}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] in ("queued", "downloading", "done", "error")


# ── /api/stream and /api/download ─────────────────────────────────────────

class TestStreamDownloadEndpoints:
    def test_stream_unknown_job_returns_404(self, client):
        resp = client.get("/api/stream/doesnotexist")
        assert resp.status_code == 404

    def test_download_unknown_job_returns_404(self, client):
        resp = client.get("/api/download/doesnotexist")
        assert resp.status_code == 404


# ── /api/cleanup ──────────────────────────────────────────────────────────

class TestCleanupEndpoint:
    def test_cleanup_unknown_job_still_returns_200(self, client):
        resp = client.delete("/api/cleanup/nonexistent")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True

    def test_cleanup_removes_job(self, client):
        r = client.post("/api/extract", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        job_id = json.loads(r.data)["job_id"]
        client.delete(f"/api/cleanup/{job_id}")
        resp = client.get(f"/api/status/{job_id}")
        assert resp.status_code == 404


# ── Static frontend ───────────────────────────────────────────────────────

class TestStaticFrontend:
    def test_root_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()
