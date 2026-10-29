import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import tempfile
import zipfile
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yt_dlp
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, stream_with_context
from ytmusicapi import YTMusic

from converter_playlist_fixed import YTDirect, get_spotify_playlist
from privacy_monitor_routes import register_privacy_monitor

APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads")).resolve()
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = DOWNLOAD_ROOT / "jobs.sqlite3"
MAX_PLAYLIST_ITEMS = max(1, min(int(os.environ.get("MAX_PLAYLIST_ITEMS", "100")), 100))
MAX_OUTSTANDING_JOBS = 6

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
JOB_QUEUE: queue.Queue[str] = queue.Queue()
CANCEL_EVENTS: dict[str, threading.Event] = {}
CANCEL_LOCK = threading.RLock()
STREAMS: dict[str, dict] = {}
STREAM_LOCK = threading.RLock()
MAX_ACTIVE_STREAMS = 2
STREAM_TTL_SECONDS = 1800
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/tmp/playlist-bridge-archives")).resolve()
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_TTL_SECONDS = 3600
ARCHIVE_FILES: dict[str, dict] = {}
ARCHIVE_LOCK = threading.RLock()


class JobCancelled(Exception):
    """Raised when the user cancels a queued or active job."""

JOB_FIELDS = {
    "status", "phase", "message", "percent", "current_item", "total_items",
    "matched", "skipped", "title", "folder_title", "result_url", "files_json", "error", "updated_at", "quality"
}


class SafeJobError(Exception):
    """An error that is safe to show in the UI."""


@contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                message TEXT NOT NULL,
                percent REAL NOT NULL DEFAULT 0,
                current_item INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                matched INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                folder_title TEXT NOT NULL DEFAULT '',
                result_url TEXT NOT NULL DEFAULT '',
                files_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "quality" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN quality TEXT NOT NULL DEFAULT '128'")
        if "folder_title" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN folder_title TEXT NOT NULL DEFAULT ''")
        # A process restart cannot resume an interrupted yt-dlp invocation safely.
        now = time.time()
        conn.execute(
            "UPDATE jobs SET status='failed', phase='Interrupted', "
            "message='Server restarted during this job; start it again.', "
            "error='Server restarted during this job.', updated_at=? WHERE status='running'",
            (now,),
        )


def get_job(job_id):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def update_job(job_id, **fields):
    unknown = set(fields) - JOB_FIELDS
    if unknown:
        raise ValueError(f"Unsupported job fields: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [job_id]
    with db_connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)


def serialize_job(row):
    files = json.loads(row.get("files_json") or "[]")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "mode": row["mode"],
        "quality": row.get("quality", "128"),
        "url": row["url"],
        "status": row["status"],
        "can_cancel": row["status"] in ("queued", "running"),
        "phase": row["phase"],
        "message": row["message"],
        "percent": round(float(row["percent"] or 0), 1),
        "current_item": int(row["current_item"] or 0),
        "total_items": int(row["total_items"] or 0),
        "matched": int(row["matched"] or 0),
        "skipped": int(row["skipped"] or 0),
        "title": row["title"],
        "result_url": row["result_url"],
        "error": row["error"],
        "created_at": row["created_at"],
        "files": [
            {"name": name, "url": f"/api/jobs/{row['id']}/files/{quote(name, safe='')}"}
            for name in files
        ],
    }


def create_job(kind, url, mode="", quality="128"):
    job_id = uuid.uuid4().hex
    now = time.time()
    with db_connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if count >= MAX_OUTSTANDING_JOBS:
            raise SafeJobError("The job queue is full. Wait for a job to finish, then try again.")
        conn.execute("""
            INSERT INTO jobs (
                id, kind, url, mode, quality, status, phase, message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', 'Queued', 'Waiting for the next worker slot.', ?, ?)
        """, (job_id, kind, url, mode, quality, now, now))
    with CANCEL_LOCK:
        CANCEL_EVENTS[job_id] = threading.Event()
    JOB_QUEUE.put(job_id)
    created = get_job(job_id)
    return serialize_job(created)


def job_cancel_event(job_id):
    with CANCEL_LOCK:
        return CANCEL_EVENTS.get(job_id)


def check_job_cancelled(job_id):
    event = job_cancel_event(job_id)
    if event is not None and event.is_set():
        raise JobCancelled("Cancelled by user")


def cleanup_media_files(job_id):
    job_dir = (DOWNLOAD_ROOT / job_id).resolve()
    if DOWNLOAD_ROOT in job_dir.parents and job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def mark_job_cancelled(job_id, kind):
    if kind == "media_download":
        cleanup_media_files(job_id)
        message = "Cancelled. Partial server files were removed."
        update_job(job_id, status="cancelled", phase="Cancelled", message=message,
                   percent=0, files_json="[]", error="", result_url="")
    else:
        message = "Cancelled. If a YouTube Music playlist was already created, it may be incomplete."
        update_job(job_id, status="cancelled", phase="Cancelled", message=message,
                   error="")


def terminate_process(process, wait_seconds=1.0):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            pass


def stream_status_view(stream):
    return {"id": stream["id"], "status": stream["status"], "message": stream["message"]}


def purge_old_streams():
    now = time.time()
    with STREAM_LOCK:
        expired = [
            stream_id for stream_id, stream in STREAMS.items()
            if (
                stream["status"] == "ready" and now - stream["created_at"] > 120
            ) or (
                stream["status"] in ("complete", "cancelled", "failed")
                and now - stream["updated_at"] > STREAM_TTL_SECONDS
            )
        ]
        for stream_id in expired:
            STREAMS.pop(stream_id, None)


def kill_stream(stream):
    stream["event"].set()
    with STREAM_LOCK:
        processes = list(stream.get("processes", []))
    for process in processes:
        terminate_process(process)


def drain_stderr(pipe, stream, label):
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                with STREAM_LOCK:
                    stream["stderr"].append(f"{label}: {line[:300]}")
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def is_youtube_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
        valid_host = (
            hostname == "youtube.com"
            or hostname.endswith(".youtube.com")
            or hostname == "youtu.be"
            or hostname == "youtube-nocookie.com"
            or hostname.endswith(".youtube-nocookie.com")
        )
        return (
            parsed.scheme == "https"
            and valid_host
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def is_single_youtube_video_url(url):
    if not is_youtube_url(url):
        return False
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query)
    if hostname == "youtu.be":
        return bool(path.strip("/"))
    if path == "/watch":
        return bool(query.get("v"))
    parts = [part for part in path.split("/") if part]
    return len(parts) >= 2 and parts[0] in ("shorts", "embed", "live") and bool(parts[1])


def is_spotify_playlist_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlparse(url.strip())
        return (
            parsed.scheme == "https"
            and parsed.hostname == "open.spotify.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and re.fullmatch(r"/playlist/[A-Za-z0-9]{22}/?", parsed.path) is not None
        )
    except ValueError:
        return False


def require_job(job_id):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return job


def on_media_progress(job_id, started_at):
    last_write = [0.0]
    last_folder_title = [""]

    def hook(data):
        check_job_cancelled(job_id)
        try:
            info = data.get("info_dict") or {}
            title = str(info.get("title") or "media")[:200]
            folder_title = str(info.get("playlist_title") or "").strip()[:200]
            if folder_title and folder_title != last_folder_title[0]:
                update_job(job_id, folder_title=folder_title)
                last_folder_title[0] = folder_title
            item_index = int(info.get("playlist_index") or 1)
            playlist_count = int(info.get("playlist_count") or 0)
            total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            file_percent = min(100.0, (100.0 * downloaded / total_bytes)) if total_bytes else 0.0
            if playlist_count > 0:
                overall_percent = ((item_index - 1 + file_percent / 100.0) / playlist_count) * 100.0
                overall_percent = min(99.5, overall_percent)
            else:
                overall_percent = min(99.5, file_percent)

            current_time = time.monotonic()
            status = data.get("status")
            if status == "downloading":
                if current_time - last_write[0] >= 0.7:
                    message = (
                        f"Downloading item {item_index}/{playlist_count}: {title}"
                        if playlist_count else f"Downloading: {title}"
                    )
                    update_job(
                        job_id,
                        phase="Downloading",
                        message=message,
                        title=title,
                        percent=overall_percent,
                        current_item=item_index,
                        total_items=playlist_count or 1,
                    )
                    last_write[0] = current_time
            elif status == "finished":
                update_job(
                    job_id,
                    phase="Converting to MP3",
                    message=f"Downloaded {title}; converting audio to MP3…",
                    percent=overall_percent,
                    current_item=item_index,
                    total_items=playlist_count or 1,
                )
        except Exception:
            logging.exception("Progress hook failed for job %s", job_id)

    return hook


def on_postprocessor(job_id):
    def hook(data):
        check_job_cancelled(job_id)
        try:
            if data.get("status") in ("started", "processing"):
                info = data.get("info_dict") or {}
                title = str(info.get("title") or "media")[:200]
                update_job(
                    job_id,
                    phase="Converting to MP3",
                    message=f"Converting to MP3: {title}",
                    title=title,
                )
        except Exception:
            logging.exception("Postprocessor hook failed for job %s", job_id)
    return hook


class YTDLPLogger:
    def debug(self, message):
        # Keep the UI and container logs focused; errors and warnings are kept.
        if str(message).startswith("[debug]"):
            return

    def info(self, message):
        return

    def warning(self, message):
        logging.warning("yt-dlp: %s", str(message)[:500])

    def error(self, message):
        logging.error("yt-dlp: %s", str(message)[:800])


def run_media_download(job):
    if not is_youtube_url(job["url"]):
        raise SafeJobError("Use an HTTPS YouTube video or playlist URL.")
    if shutil.disk_usage(DOWNLOAD_ROOT).free < 512 * 1024 * 1024:
        raise SafeJobError("Less than 512 MB of free disk space remains on the server.")

    job_dir = (DOWNLOAD_ROOT / job["id"]).resolve()
    if DOWNLOAD_ROOT not in job_dir.parents:
        raise SafeJobError("Invalid download path.")
    job_dir.mkdir(parents=True, exist_ok=True)
    mode = job["mode"]
    quality = str(job.get("quality") or "128")
    # Prefer a source audio stream at or below the selected target bitrate when
    # yt-dlp exposes an ABR value; fall back to the best available audio if not.
    source_format = {
        "128": "bestaudio[abr<=128]/bestaudio/best",
        "192": "bestaudio[abr<=192]/bestaudio/best",
        "320": "bestaudio/best",
    }.get(quality, "bestaudio/best")
    update_job(
        job["id"],
        phase="Preparing download",
        message="Resolving the YouTube page and available audio formats…",
        percent=0,
    )

    options = {
        "format": source_format,
        "js_runtimes": {"node": {}},
        "outtmpl": str(job_dir / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": mode == "video",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "ignoreerrors": mode == "playlist",
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 18,
        "continuedl": True,
        "match_filter": lambda info, *, incomplete=False: check_job_cancelled(job["id"]),
        "windowsfilenames": True,
        "noplaylist": mode == "video",
        "quiet": True,
        "no_warnings": False,
        "logger": YTDLPLogger(),
        "progress_hooks": [on_media_progress(job["id"], time.monotonic())],
        "postprocessor_hooks": [on_postprocessor(job["id"])],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }],
    }

    update_job(
        job["id"],
        phase="Downloading",
        message=(f"Downloading playlist (first {MAX_PLAYLIST_ITEMS} items maximum)…"
                 if mode == "playlist" else "Downloading video audio…"),
    )
    check_job_cancelled(job["id"])
    with yt_dlp.YoutubeDL(options) as downloader:
        return_code = downloader.download([job["url"]])
    check_job_cancelled(job["id"])

    mp3s = sorted(
        p.name for p in job_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".mp3"
    )
    if not mp3s:
        if return_code:
            raise SafeJobError("yt-dlp could not download this item. Check the server log for details.")
        raise SafeJobError("No MP3 files were produced. The source may be unavailable or unsupported.")

    total = max(1, int(get_job(job["id"])['total_items'] or len(mp3s)))
    update_job(
        job["id"],
        status="complete",
        phase="Complete",
        message=f"Finished: {len(mp3s)} MP3 file(s) ready to download.",
        percent=100,
        current_item=len(mp3s),
        total_items=total,
        files_json=json.dumps(mp3s),
        error="",
    )


def ytmusic_from_existing_session(yt):
    # The existing converter builds Authorization from the SAPISID cookie.
    # Pass those authenticated request headers into ytmusicapi; never expose them.
    headers = {str(key).lower(): str(value) for key, value in yt.session.headers.items()}
    headers["origin"] = "https://music.youtube.com"
    headers["x-origin"] = "https://music.youtube.com"
    headers["x-goog-authuser"] = headers.get("x-goog-authuser", "0")
    if "cookie" not in headers or "authorization" not in headers:
        raise SafeJobError("YouTube Music authentication headers are missing. Refresh browser.json on your PC.")
    return YTMusic(headers)


def run_spotify_conversion(job):
    if not is_spotify_playlist_url(job["url"]):
        raise SafeJobError("Use a standard public Spotify playlist URL.")

    check_job_cancelled(job["id"])
    update_job(job["id"], phase="Reading Spotify", message="Loading public Spotify playlist metadata…")
    title, tracks = get_spotify_playlist(job["url"])
    check_job_cancelled(job["id"])
    title = re.sub(r"[<>]", "", str(title or "")).strip() or "Spotify Playlist Import"
    if not tracks:
        raise SafeJobError("No tracks were found. The playlist may be private or temporarily unavailable.")

    total = len(tracks)
    update_job(
        job["id"], phase="Checking YouTube Music login", message=f"Found {total} tracks. Checking your session…",
        title=title, total_items=total, current_item=0, percent=0,
    )
    yt = YTDirect("browser.json")
    if not yt.test_auth():
        raise SafeJobError("Your YouTube Music session is invalid or expired. Refresh browser.json securely and try again.")
    ytm = ytmusic_from_existing_session(yt)

    video_ids = []
    matched = 0
    skipped = 0
    for index, track in enumerate(tracks, 1):
        check_job_cancelled(job["id"])
        name = str(track.get("title") or "Unknown track")
        artist = str(track.get("artist") or "")
        query = str(track.get("query") or f"{name} {artist}")
        update_job(
            job["id"], phase="Matching tracks",
            message=f"Searching {index}/{total}: {name} — {artist}",
            current_item=index, total_items=total,
            percent=(index - 1) / max(total, 1) * 70,
            matched=matched, skipped=skipped, title=title,
        )
        try:
            video_id = yt.search_song(query, artist)
        except Exception:
            check_job_cancelled(job["id"])
            logging.exception("YouTube Music search failed for item %s in job %s", index, job["id"])
            video_id = None
        check_job_cancelled(job["id"])
        if video_id:
            video_ids.append(video_id)
            matched += 1
        else:
            skipped += 1
        update_job(
            job["id"], current_item=index, total_items=total,
            matched=matched, skipped=skipped,
            percent=index / max(total, 1) * 70,
            message=(f"Matched {index}/{total}: {name}" if video_id
                     else f"No confident match {index}/{total}: {name}"),
        )
        if index < total:
            event = job_cancel_event(job["id"])
            if event and event.wait(0.5):
                raise JobCancelled("Cancelled by user")

    check_job_cancelled(job["id"])
    unique_ids = list(dict.fromkeys(video_ids))
    if not unique_ids:
        raise SafeJobError("No confident YouTube Music matches were found; no playlist was created.")

    update_job(
        job["id"], phase="Creating playlist", message=f"Creating a private playlist for {len(unique_ids)} matched tracks…",
        percent=75, matched=len(unique_ids), skipped=skipped,
    )
    check_job_cancelled(job["id"])
    created = ytm.create_playlist(title, "Imported from Spotify with Playlist Bridge", privacy_status="PRIVATE")
    if isinstance(created, str):
        playlist_id = created
    elif isinstance(created, dict):
        playlist_id = created.get("playlistId") or created.get("playlist_id")
    else:
        playlist_id = None
    if not playlist_id:
        raise SafeJobError("YouTube Music rejected playlist creation. Check the server log; no playlist link was returned.")

    result_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    update_job(
        job["id"], phase="Adding tracks", result_url=result_url,
        message=f"Adding {len(unique_ids)} matched tracks…", percent=80,
    )
    for start in range(0, len(unique_ids), 20):
        check_job_cancelled(job["id"])
        batch = unique_ids[start:start + 20]
        response = ytm.add_playlist_items(playlist_id, batch, duplicates=False)
        if isinstance(response, dict) and response.get("status") not in (None, "STATUS_SUCCEEDED"):
            raise SafeJobError("YouTube Music created the playlist but rejected a batch of tracks. Open the partial playlist link.")
        if isinstance(response, str) and response != "STATUS_SUCCEEDED":
            raise SafeJobError("YouTube Music created the playlist but rejected a batch of tracks. Open the partial playlist link.")
        done = min(start + len(batch), len(unique_ids))
        update_job(
            job["id"], percent=80 + (20 * done / len(unique_ids)),
            message=f"Added {done}/{len(unique_ids)} matched tracks…",
        )
        if done < len(unique_ids):
            time.sleep(0.5)

    update_job(
        job["id"], status="complete", phase="Complete", message=f"Playlist created: {len(unique_ids)} matched, {skipped} skipped.",
        percent=100, current_item=total, total_items=total, matched=len(unique_ids), skipped=skipped,
        title=title, result_url=result_url, error="",
    )


def worker_loop():
    while True:
        job_id = JOB_QUEUE.get()
        try:
            # Serialize the queued -> running transition with cancellation so a
            # just-cancelled queued job cannot start after the API responds.
            with CANCEL_LOCK:
                job = get_job(job_id)
                if not job or job["status"] != "queued":
                    continue
                event = CANCEL_EVENTS.setdefault(job_id, threading.Event())
                if event.is_set():
                    cancelled_job = job
                else:
                    cancelled_job = None
                    update_job(job_id, status="running", phase="Starting", message="Worker started.", error="")

            if cancelled_job:
                mark_job_cancelled(job_id, cancelled_job["kind"])
                continue

            job = get_job(job_id)
            try:
                if job["kind"] == "media_download":
                    run_media_download(job)
                elif job["kind"] == "spotify_convert":
                    run_spotify_conversion(job)
                else:
                    raise SafeJobError("Unknown job type.")
                if event.is_set():
                    mark_job_cancelled(job_id, job["kind"])
            except Exception as exc:
                if event.is_set() or isinstance(exc, JobCancelled):
                    logging.info("Job %s cancelled by user", job_id)
                    mark_job_cancelled(job_id, job["kind"])
                elif isinstance(exc, SafeJobError):
                    logging.warning("Job %s stopped: %s", job_id, str(exc)[:300])
                    update_job(job_id, status="failed", phase="Failed", message=str(exc)[:300], error=str(exc)[:300])
                else:
                    logging.exception("Background job %s failed", job_id)
                    update_job(
                        job_id, status="failed", phase="Failed",
                        message="Job failed. Check server logs for the technical details.",
                        error="Job failed. Check server logs.",
                    )
        finally:
            with CANCEL_LOCK:
                CANCEL_EVENTS.pop(job_id, None)
            JOB_QUEUE.task_done()


def start_worker():
    init_db()
    with db_connect() as conn:
        queued = [row[0] for row in conn.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at"
        ).fetchall()]
    with CANCEL_LOCK:
        for job_id in queued:
            CANCEL_EVENTS.setdefault(job_id, threading.Event())
    thread = threading.Thread(target=worker_loop, name="playlist-bridge-worker", daemon=True)
    thread.start()
    for job_id in queued:
        JOB_QUEUE.put(job_id)


@app.before_request
def require_login():
    if request.endpoint == "health":
        return None
    expected_user = os.environ.get("BRIDGE_USER")
    expected_password = os.environ.get("BRIDGE_PASSWORD")
    if not expected_user or not expected_password:
        return "Server login is not configured.", 503
    auth = request.authorization
    if not auth or not (
        secrets.compare_digest(auth.username or "", expected_user)
        and secrets.compare_digest(auth.password or "", expected_password)
    ):
        return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="Playlist Bridge"'})
    return None


register_privacy_monitor(app)

@app.get("/")
def index():
    return render_template("dashboard.html", max_playlist_items=MAX_PLAYLIST_ITEMS)


@app.get("/media")
def media_studio():
    return render_template("index.html", max_playlist_items=MAX_PLAYLIST_ITEMS)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    # Only summarize the authenticated user's own job metadata and MP3 storage.
    with db_connect() as conn:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        recent_rows = conn.execute(
            "SELECT id, kind, status, phase, message, percent, current_item, "
            "total_items, title, files_json, updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT 8"
        ).fetchall()
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    counts = {"queued": 0, "running": 0, "complete": 0, "failed": 0, "cancelled": 0}
    for row in status_rows:
        counts[str(row["status"])] = int(row["n"])

    mp3_count = 0
    mp3_bytes = 0
    try:
        for item in DOWNLOAD_ROOT.rglob("*"):
            if item.is_file() and item.suffix.lower() == ".mp3":
                try:
                    mp3_count += 1
                    mp3_bytes += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass

    try:
        disk = shutil.disk_usage(DOWNLOAD_ROOT)
        disk_info = {"total_bytes": disk.total, "free_bytes": disk.free, "used_bytes": disk.used}
    except OSError:
        disk_info = {"total_bytes": 0, "free_bytes": 0, "used_bytes": 0}

    recent = []
    for row in recent_rows:
        try:
            files = json.loads(row["files_json"] or "[]")
        except (TypeError, ValueError):
            files = []
        recent.append({
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "phase": row["phase"],
            "message": row["message"],
            "percent": round(float(row["percent"] or 0), 1),
            "current_item": int(row["current_item"] or 0),
            "total_items": int(row["total_items"] or 0),
            "title": row["title"] or "",
            "files_count": len(files),
            "updated_at": row["updated_at"],
        })

    return jsonify({
        "status": "ok",
        "stats": {
            "total_jobs": int(total_jobs),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "complete": counts.get("complete", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "mp3_count": mp3_count,
            "mp3_bytes": mp3_bytes,
            "disk": disk_info,
        },
        "recent": recent,
        "updated_at": time.time(),
    })


@app.post("/api/streams")
def api_create_stream():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not is_single_youtube_video_url(url):
        return jsonify(error="Use an HTTPS YouTube single-video URL, not a playlist URL."), 400
    if data.get("authorized") is not True:
        return jsonify(error="Confirm you own this media or have permission to download it."), 400
    quality = str(data.get("quality", "128"))
    if quality not in ("128", "192", "320"):
        return jsonify(error="Choose 128, 192, or 320 kbps."), 400
    purge_old_streams()
    with STREAM_LOCK:
        active = sum(1 for item in STREAMS.values() if item["status"] in ("ready", "running", "cancelling"))
        if active >= MAX_ACTIVE_STREAMS:
            return jsonify(error="The direct-stream limit is busy. Cancel or finish a stream first."), 429
        stream_id = uuid.uuid4().hex
        STREAMS[stream_id] = {
            "id": stream_id,
            "url": url.strip(),
            "quality": quality,
            "event": threading.Event(),
            "processes": [],
            "stderr": deque(maxlen=40),
            "status": "ready",
            "message": "Ready to stream MP3 to your phone.",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    return jsonify({
        "id": stream_id,
        "download_url": f"/api/streams/{stream_id}/download",
        "status_url": f"/api/streams/{stream_id}",
        "cancel_url": f"/api/streams/{stream_id}/cancel",
    }), 201


@app.get("/api/streams/<stream_id>")
def api_stream_status(stream_id):
    with STREAM_LOCK:
        stream = STREAMS.get(stream_id)
        if not stream:
            abort(404)
        return jsonify(stream_status_view(stream))


@app.post("/api/streams/<stream_id>/cancel")
def api_cancel_stream(stream_id):
    with STREAM_LOCK:
        stream = STREAMS.get(stream_id)
        if not stream:
            abort(404)
        if stream["status"] in ("complete", "cancelled", "failed"):
            return jsonify(error="This stream has already finished."), 409
        stream["event"].set()
        stream["status"] = "cancelling"
        stream["message"] = "Cancellation requested; stopping the media pipeline…"
        stream["updated_at"] = time.time()
    kill_stream(stream)
    with STREAM_LOCK:
        stream["status"] = "cancelled"
        stream["message"] = "Direct stream cancelled."
        stream["updated_at"] = time.time()
    return jsonify(stream_status_view(stream))


@app.get("/api/streams/<stream_id>/download")
def api_stream_download(stream_id):
    with STREAM_LOCK:
        stream = STREAMS.get(stream_id)
        if not stream:
            abort(404)
        if stream["event"].is_set() or stream["status"] != "ready":
            return jsonify(error="This direct stream is no longer available."), 410
        stream["status"] = "running"
        stream["message"] = "Streaming MP3 to your phone as audio is converted…"
        stream["updated_at"] = time.time()

    downloader = None
    transcoder = None
    try:
        quality = str(stream["quality"])
        source_format = {
            "128": "bestaudio[abr<=128]/bestaudio/best",
            "192": "bestaudio[abr<=192]/bestaudio/best",
            "320": "bestaudio/best",
        }.get(quality, "bestaudio/best")
        downloader_command = [
            sys.executable, "-m", "yt_dlp",
            "--no-warnings", "--quiet", "--no-progress", "--no-playlist",
            "--js-runtimes", "node",
            "--format", source_format,
            "--output", "-",
            stream["url"],
        ]
        downloader = subprocess.Popen(
            downloader_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True,
        )
        with STREAM_LOCK:
            stream["processes"].append(downloader)
            cancelled = stream["event"].is_set()
        if cancelled:
            terminate_process(downloader)
            return jsonify(error="The stream was cancelled."), 410

        transcoder_command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-vn", "-codec:a", "libmp3lame",
            "-b:a", f"{stream['quality']}k", "-f", "mp3", "pipe:1",
        ]
        transcoder = subprocess.Popen(
            transcoder_command, stdin=downloader.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
        )
        if downloader.stdout:
            downloader.stdout.close()
        with STREAM_LOCK:
            stream["processes"].append(transcoder)
            cancelled = stream["event"].is_set()
        if cancelled:
            terminate_process(transcoder)
            terminate_process(downloader)
            return jsonify(error="The stream was cancelled."), 410

        threading.Thread(target=drain_stderr, args=(downloader.stderr, stream, "yt-dlp"), daemon=True).start()
        threading.Thread(target=drain_stderr, args=(transcoder.stderr, stream, "ffmpeg"), daemon=True).start()

    except Exception:
        logging.exception("Could not start direct stream %s", stream_id)
        if transcoder:
            terminate_process(transcoder)
        if downloader:
            terminate_process(downloader)
        with STREAM_LOCK:
            stream["status"] = "failed"
            stream["message"] = "Could not start the stream. Check server logs."
            stream["updated_at"] = time.time()
        return jsonify(error="Could not start the direct stream. Check server logs."), 500

    @stream_with_context
    def body():
        delivered = 0
        disconnected = False
        try:
            while not stream["event"].is_set():
                chunk = transcoder.stdout.read(64 * 1024)
                if not chunk:
                    break
                delivered += len(chunk)
                yield chunk
            if stream["event"].is_set():
                with STREAM_LOCK:
                    stream["status"] = "cancelled"
                    stream["message"] = "Direct stream cancelled; partial audio was not kept on the server."
                    stream["updated_at"] = time.time()
                return
            transcoder.wait(timeout=10)
            downloader.wait(timeout=10)
            if transcoder.returncode == 0 and downloader.returncode == 0 and delivered > 0:
                with STREAM_LOCK:
                    stream["status"] = "complete"
                    stream["message"] = "MP3 stream finished. No completed MP3 was saved on the server."
                    stream["updated_at"] = time.time()
            else:
                with STREAM_LOCK:
                    stream["status"] = "failed"
                    stream["message"] = "The stream ended early. Check container logs and stream status."
                    stream["updated_at"] = time.time()
                logging.warning(
                    "Direct stream %s failed: yt-dlp=%s ffmpeg=%s details=%s",
                    stream_id, downloader.returncode, transcoder.returncode,
                    " | ".join(stream["stderr"])[-2000:],
                )
        except GeneratorExit:
            disconnected = True
            stream["event"].set()
            with STREAM_LOCK:
                stream["status"] = "cancelled"
                stream["message"] = "Phone disconnected; the stream was stopped."
                stream["updated_at"] = time.time()
            raise
        except Exception:
            logging.exception("Direct stream %s failed while sending bytes", stream_id)
            with STREAM_LOCK:
                stream["status"] = "failed"
                stream["message"] = "The stream failed. Check server logs."
                stream["updated_at"] = time.time()
        finally:
            if stream["event"].is_set() or disconnected:
                kill_stream(stream)
            else:
                terminate_process(transcoder)
                terminate_process(downloader)
            for pipe in (getattr(transcoder, "stdout", None), getattr(downloader, "stdout", None)):
                if pipe:
                    try:
                        pipe.close()
                    except Exception:
                        pass

    return Response(
        body(),
        mimetype="audio/mpeg",
        headers={
            "Content-Disposition": 'attachment; filename="Playlist-Bridge-Audio.mp3"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        direct_passthrough=True,
    )


@app.get("/api/jobs")
def api_jobs():
    purge_expired_archives()
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30").fetchall()
    return jsonify([serialize_job(dict(row)) for row in rows])


@app.post("/api/jobs/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    mode = data.get("mode", "video")
    quality = str(data.get("quality", "128"))
    if quality not in ("128", "192", "320"):
        return jsonify(error="Choose 128, 192, or 320 kbps."), 400
    if not is_youtube_url(url):
        return jsonify(error="Use an HTTPS YouTube video or playlist URL."), 400
    if mode not in ("video", "playlist"):
        return jsonify(error="Choose video or playlist mode."), 400
    if data.get("authorized") is not True:
        return jsonify(error="Confirm that you own this media or have permission to download it."), 400
    try:
        job = create_job("media_download", url.strip(), mode, quality)
    except SafeJobError as exc:
        return jsonify(error=str(exc)), 429
    return jsonify(job), 202


@app.post("/api/jobs/spotify")
def api_spotify():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not is_spotify_playlist_url(url):
        return jsonify(error="Use a standard public https://open.spotify.com/playlist/... URL."), 400
    try:
        job = create_job("spotify_convert", url.strip())
    except SafeJobError as exc:
        return jsonify(error=str(exc)), 429
    return jsonify(job), 202


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel_job(job_id):
    with CANCEL_LOCK:
        job = get_job(job_id)
        if job is None:
            abort(404)
        if job["status"] not in ("queued", "running"):
            return jsonify(error="This job is no longer active."), 409
        event = CANCEL_EVENTS.setdefault(job_id, threading.Event())
        event.set()
        if job["status"] == "queued":
            # Mark queued jobs terminal while holding the same lock used by the
            # worker's queued -> running transition.
            update_job(
                job_id, status="cancelled", phase="Cancelled",
                message=("Cancelled before it started. No media was downloaded." if job["kind"] == "media_download"
                         else "Cancelled before it started."), error="", files_json="[]",
            )
            cancel_queued = True
        else:
            update_job(job_id, phase="Cancelling", message="Cancellation requested; stopping at the next safe checkpoint.")
            cancel_queued = False
    if cancel_queued and job["kind"] == "media_download":
        cleanup_media_files(job_id)
    return jsonify({
        "id": job_id,
        "status": "cancelled" if cancel_queued else "cancelling",
        "message": "Cancellation accepted.",
    }), 202


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = require_job(job_id)
    return jsonify(serialize_job(job))


def safe_archive_folder(value):
    """Turn a display title into one safe, portable directory name for a ZIP."""
    value = str(value or "").replace("\\", "-").replace("/", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-_\t")
    return value[:90] or "Playlist Bridge Downloads"


def archive_folder_for_job(job):
    if job.get("mode") == "playlist":
        title = str(job.get("folder_title") or "").strip()
        if not title:
            parsed = urlparse(job.get("url", ""))
            playlist_id = (parse_qs(parsed.query).get("list") or [""])[0]
            title = f"YouTube Playlist {playlist_id}" if playlist_id else "YouTube Playlist"
    else:
        title = job.get("title") or "YouTube Audio"
    return safe_archive_folder(title)


def validated_archive_items(items):
    """Validate all selected files against the completed-job database records."""
    validated = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise SafeJobError("Invalid selected-file entry.")
        job_id = item.get("job_id")
        filename = item.get("filename")
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise SafeJobError("Invalid job ID in selected files.")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
            raise SafeJobError("Invalid filename in selected files.")
        key = (job_id, filename)
        if key in seen:
            continue
        seen.add(key)
        job = get_job(job_id)
        if not job or job.get("kind") != "media_download" or job.get("status") != "complete":
            raise SafeJobError("One selected download is no longer available. Refresh the page and select files again.")
        files = json.loads(job.get("files_json") or "[]")
        if filename not in files:
            raise SafeJobError("A selected file is not in the completed job's file list.")
        job_dir = (DOWNLOAD_ROOT / job_id).resolve()
        if DOWNLOAD_ROOT not in job_dir.parents:
            raise SafeJobError("Invalid job storage path.")
        source = (job_dir / filename).resolve()
        if source.parent != job_dir or not source.is_file() or source.suffix.lower() != ".mp3":
            raise SafeJobError(f"A selected MP3 is missing: {filename}")
        arcname = f"{archive_folder_for_job(job)}/{filename}"
        validated.append({"path": source, "arcname": arcname})
    if not validated:
        raise SafeJobError("Select at least one completed MP3 file.")
    if len(validated) > 500:
        raise SafeJobError("Select 500 files or fewer per archive.")
    return validated


def build_archive_file(items):
    """Build a fast, uncompressed ZIP: MP3 files are already compressed."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="playlist-bridge-", suffix=".zip", dir=ARCHIVE_DIR, delete=False)
    archive_path = Path(handle.name)
    handle.close()
    used_names = set()
    try:
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for item in items:
                name = item["arcname"]
                if name in used_names:
                    stem, suffix = os.path.splitext(name)
                    index = 2
                    while f"{stem} ({index}){suffix}" in used_names:
                        index += 1
                    name = f"{stem} ({index}){suffix}"
                used_names.add(name)
                archive.write(item["path"], arcname=name)
        if archive_path.stat().st_size <= 22:
            raise SafeJobError("The ZIP archive is empty.")
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def purge_expired_archives():
    now = time.time()
    with ARCHIVE_LOCK:
        expired = [token for token, entry in ARCHIVE_FILES.items()
                   if now - entry["created_at"] > ARCHIVE_TTL_SECONDS]
        for token in expired:
            entry = ARCHIVE_FILES.pop(token, None)
            if entry:
                Path(entry["path"]).unlink(missing_ok=True)


def register_archive(archive_path, filename):
    purge_expired_archives()
    token = secrets.token_urlsafe(24)
    with ARCHIVE_LOCK:
        ARCHIVE_FILES[token] = {
            "path": str(archive_path),
            "filename": filename,
            "created_at": time.time(),
        }
    return token


def archive_name(value):
    name = safe_archive_folder(value).replace(" ", "-")
    return f"{name[:80]}.zip"


@app.get("/api/jobs/<job_id>/archive")
def api_job_archive(job_id):
    job = require_job(job_id)
    if job.get("kind") != "media_download" or job.get("status") != "complete":
        return jsonify(error="Only completed media-download jobs can be archived."), 409
    files = json.loads(job.get("files_json") or "[]")
    try:
        items = validated_archive_items([{"job_id": job_id, "filename": name} for name in files])
        archive_path = build_archive_file(items)
        display_name = archive_name(archive_folder_for_job(job))
        token = register_archive(archive_path, display_name)
        return redirect(f"/api/archives/{token}", code=302)
    except SafeJobError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        logging.exception("Could not build archive for job %s", job_id)
        return jsonify(error="Could not create the ZIP. Check available disk space and server logs."), 500


@app.post("/api/archive")
def api_selected_archive():
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or len(items) > 500:
        return jsonify(error="Send a list of up to 500 selected MP3 files."), 400
    try:
        validated = validated_archive_items(items)
        archive_path = build_archive_file(validated)
        token = register_archive(archive_path, "Playlist-Bridge-Selected-MP3s.zip")
        return jsonify({"download_url": f"/api/archives/{token}", "count": len(validated)}), 201
    except SafeJobError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        logging.exception("Could not build selected MP3 archive")
        return jsonify(error="Could not create the ZIP. Check available disk space and server logs."), 500


@app.get("/api/archives/<token>")
def api_download_archive(token):
    purge_expired_archives()
    with ARCHIVE_LOCK:
        entry = ARCHIVE_FILES.get(token)
    if not entry:
        abort(404)
    archive_path = Path(entry["path"])
    if not archive_path.is_file():
        with ARCHIVE_LOCK:
            ARCHIVE_FILES.pop(token, None)
        abort(404)
    response = send_file(
        archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=entry["filename"],
        conditional=False,
    )
    def cleanup_archive():
        with ARCHIVE_LOCK:
            ARCHIVE_FILES.pop(token, None)
        archive_path.unlink(missing_ok=True)
    response.call_on_close(cleanup_archive)
    return response


@app.get("/api/jobs/<job_id>/files/<path:filename>")
def download_file(job_id, filename):
    job = require_job(job_id)
    files = json.loads(job.get("files_json") or "[]")
    # Only allow exact file names recorded at completion; disallow directory traversal.
    if job["kind"] != "media_download" or filename not in files or Path(filename).name != filename:
        abort(404)
    job_dir = (DOWNLOAD_ROOT / job_id).resolve()
    if DOWNLOAD_ROOT not in job_dir.parents:
        abort(404)
    return send_from_directory(job_dir, filename, as_attachment=True, download_name=filename, mimetype="audio/mpeg")


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    job = require_job(job_id)
    if job["status"] in ("queued", "running"):
        return jsonify(error="Wait for the job to finish before removing it."), 409
    if job["kind"] == "media_download":
        job_dir = (DOWNLOAD_ROOT / job_id).resolve()
        if DOWNLOAD_ROOT in job_dir.parents and job_dir.exists():
            shutil.rmtree(job_dir)
    with db_connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return jsonify({"deleted": True})


start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088, debug=False, threaded=True)
