#!/usr/bin/env python3
"""
YT Audio Extractor — Cloud Run backend
- Uses /tmp for ephemeral audio storage (Cloud Run gives 512 MB in-memory tmpfs)
- Optional: set GCS_BUCKET env var to persist files to Google Cloud Storage
- Designed for gunicorn multi-threaded workers
"""

import os
import re
import json
import uuid
import threading
import subprocess
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ── Storage ────────────────────────────────────────────────────
# Cloud Run: /tmp is an in-memory tmpfs (up to 512 MB by default).
# For larger files or persistence, set GCS_BUCKET to auto-upload to GCS.
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/ytaudio"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")  # optional

# ── In-memory job store ────────────────────────────────────────
# NOTE: in multi-instance Cloud Run deployments, jobs won't be shared
# across instances. For production scale, replace with Firestore/Redis.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────

def sanitize_video_id(raw: str) -> str | None:
    patterns = [
        r"(?:v=|/embed/|/v/|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, raw.strip())
        if m:
            return m.group(1)
    return None


def update_job(job_id: str, **kw):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(**kw)


def upload_to_gcs(local_path: Path, destination: str) -> str:
    """Upload file to GCS and return the gs:// URI. Requires google-cloud-storage."""
    try:
        from google.cloud import storage  # type: ignore
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(destination)
        blob.upload_from_filename(str(local_path))
        log.info("Uploaded %s to gs://%s/%s", local_path.name, GCS_BUCKET, destination)
        return f"gs://{GCS_BUCKET}/{destination}"
    except Exception as e:
        log.warning("GCS upload failed: %s", e)
        return ""


def run_extraction(job_id: str, video_id: str, fmt: str):
    """Background thread: download + extract audio via yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_tpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    codec_map = {"mp3": "mp3", "m4a": "m4a", "wav": "wav", "opus": "opus"}
    codec = codec_map.get(fmt, "mp3")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--extract-audio",
        "--audio-format", codec,
        "--audio-quality", "0",
        "--output", out_tpl,
        "--print-json",
        "--no-progress",
        url,
    ]

    update_job(job_id, status="downloading", progress=10)
    log.info("[%s] Starting extraction: %s → %s", job_id[:8], video_id, codec)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = proc.communicate(timeout=300)

        if proc.returncode != 0:
            err_lines = [l for l in stderr.splitlines() if l.strip()]
            msg = err_lines[-1] if err_lines else "yt-dlp failed"
            log.error("[%s] yt-dlp error: %s", job_id[:8], msg)
            update_job(job_id, status="error", error=msg)
            return

        # Parse metadata
        meta = {}
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    meta = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

        produced = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not produced:
            update_job(job_id, status="error", error="Audio file was not created.")
            return

        filepath = produced[0]
        filename = filepath.name
        file_size = filepath.stat().st_size
        log.info("[%s] Done: %s (%.1f MB)", job_id[:8], filename, file_size / 1e6)

        # Optional GCS upload
        gcs_uri = ""
        if GCS_BUCKET:
            gcs_uri = upload_to_gcs(filepath, f"ytaudio/{filename}")

        update_job(
            job_id,
            status="done",
            progress=100,
            filename=filename,
            file_size=file_size,
            title=meta.get("title") or meta.get("fulltitle") or "Unknown",
            channel=meta.get("channel") or meta.get("uploader") or "",
            duration=meta.get("duration") or 0,
            thumbnail=meta.get("thumbnail") or "",
            gcs_uri=gcs_uri,
        )

    except subprocess.TimeoutExpired:
        proc.kill()
        update_job(job_id, status="error", error="Download timed out.")
    except Exception as exc:
        log.exception("[%s] Unexpected error", job_id[:8])
        update_job(job_id, status="error", error=str(exc))


# ── Health check ───────────────────────────────────────────────

@app.route("/healthz")
def health():
    """Cloud Run health check endpoint."""
    return jsonify(status="ok"), 200


# ── Static frontend ────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")


# ── API ────────────────────────────────────────────────────────

@app.route("/api/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True) or {}
    raw_url = data.get("url", "").strip()
    fmt = data.get("format", "mp3").lower()

    video_id = sanitize_video_id(raw_url)
    if not video_id:
        return jsonify(error="Could not parse a valid YouTube video ID."), 400

    if fmt not in ("mp3", "m4a", "wav", "opus"):
        fmt = "mp3"

    job_id = uuid.uuid4().hex[:16]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "format": fmt}

    thread = threading.Thread(
        target=run_extraction, args=(job_id, video_id, fmt), daemon=True
    )
    thread.start()
    log.info("Job %s queued for video %s (%s)", job_id[:8], video_id, fmt)

    return jsonify(job_id=job_id), 202


@app.route("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job:
        return jsonify(error="Unknown job ID."), 404
    # Don't expose internal fields
    job.pop("filename", None) if job.get("status") != "done" else None
    return jsonify(job)


@app.route("/api/stream/<job_id>")
def stream_audio(job_id):
    """Range-request-aware audio streaming for browser seek support."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="File not ready."), 404

    filepath = DOWNLOAD_DIR / job["filename"]
    if not filepath.exists():
        return jsonify(error="File not found on disk."), 404

    file_size = filepath.stat().st_size
    range_hdr = request.headers.get("Range")
    ext = filepath.suffix.lower()
    mime_map = {".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".wav": "audio/wav", ".opus": "audio/ogg"}
    mime = mime_map.get(ext, "audio/mpeg")

    if range_hdr:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if not m:
            return Response(status=416)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def generate():
            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": mime,
        }
        return Response(stream_with_context(generate()), 206, headers=headers)

    return send_file(filepath, mimetype=mime)


@app.route("/api/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="File not ready."), 404

    filepath = DOWNLOAD_DIR / job["filename"]
    if not filepath.exists():
        return jsonify(error="File not found on disk."), 404

    return send_file(filepath, as_attachment=True, download_name=job["filename"])


@app.route("/api/cleanup/<job_id>", methods=["DELETE"])
def cleanup(job_id):
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job and job.get("filename"):
        f = DOWNLOAD_DIR / job["filename"]
        f.unlink(missing_ok=True)
    return jsonify(ok=True)


# ── Dev entrypoint ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("Dev server starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
