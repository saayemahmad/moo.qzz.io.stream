import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
import re
import sys
import uuid
import json
import base64
import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, HTTPException, Depends, BackgroundTasks, Form
from pydantic import BaseModel
from typing import Optional, Any, List
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pathlib import Path
import aiofiles
import static_ffmpeg
static_ffmpeg.add_paths()

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

    # Fix: suppress ConnectionResetError (WinError 10054) in ProactorEventLoop.
    # This is a known Windows asyncio bug where the remote host closes the
    # connection and _call_connection_lost() tries to shutdown the socket,
    # raising a noisy but harmless ConnectionResetError in the callback.
    import socket
    from asyncio.proactor_events import _ProactorBasePipeTransport

    _original_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

    def _patched_call_connection_lost(self, exc):
        try:
            _original_call_connection_lost(self, exc)
        except ConnectionResetError:
            pass  # harmless: remote closed connection before we could shutdown

    _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost

async def run_cmd_async(cmd: list, cwd: Optional[Any] = None):
    """
    Run an external command asynchronously using a thread pool.
    This avoids NotImplementedError on Windows when using SelectorEventLoop,
    and runs non-blocking in asyncio.
    """
    def _run():
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    return await asyncio.to_thread(_run)

from .database import get_db

app = FastAPI()

# Rate Limiter setup
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_PATHS_PREFIXES = (
    "/uploads",
    "/media",
    "/b2-media",
    "/log-client-error",
    "/log-watch",
    "/api",
    "/analytics",
)
ALLOWED_EXACT_PATHS = {
    "/login",
    "/logout",
    "/analytics",
    "/deleted_messages",
    "/api/health",
}

class StrictWhitelistMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path not in ALLOWED_EXACT_PATHS and not any(path.startswith(prefix) for prefix in ALLOWED_PATHS_PREFIXES):
                response = Response(content="Not Found", status_code=404)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

app.add_middleware(StrictWhitelistMiddleware)

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
# Render frontend origin + API subdomain itself
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "https://moo.qzz.io,https://api.moo.qzz.io"
).split(",")

if ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Upload-Offset", "Location", "Upload-Length", "Tus-Resumable", "Tus-Version", "Tus-Extension", "Tus-Max-Size"]
)

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD_DIR = BASE_DIR / "uploads_raw"
MEDIA_DIR = BASE_DIR / "media"
os.makedirs(UPLOAD_DIR, exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)



TUS_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# TEMPORARY FILTER: when True, the grid will only show entries whose folder
# physically exists inside /media. Set to False to disable this filter.
# DB is NEVER modified by this flag — only the API response is filtered.
# ---------------------------------------------------------------------------
FILTER_MISSING_MEDIA: bool = True

# Local metadata caching for ultra-fast non-blocking TUS operations
upload_sessions_cache: dict[str, dict] = {}

def get_upload_metadata(upload_id: str, db=None) -> Optional[dict]:
    if upload_id in upload_sessions_cache:
        return upload_sessions_cache[upload_id]
    meta_file = UPLOAD_DIR / f"{upload_id}.meta.json"
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            upload_sessions_cache[upload_id] = data
            return data
        except Exception:
            pass
    if db is not None:
        try:
            data = db.reference(f'uploads/{upload_id}').get()
            if data:
                upload_sessions_cache[upload_id] = dict(data)
                try:
                    meta_file.write_text(json.dumps(data), encoding="utf-8")
                except Exception:
                    pass
                return data
        except Exception as e:
            print(f"Error reading Firebase upload metadata: {e}")
    return None

def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        clean = dt_str.strip().rstrip('Z').replace(' ', 'T')
        if not clean:
            return None
        return datetime.fromisoformat(clean).replace(tzinfo=None)
    except Exception:
        return None

# --- TUS Protocol Implementation ---

@app.options("/uploads")
@app.options("/uploads/{upload_id}")
def tus_options():
    response = Response(status_code=204)
    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Tus-Version"] = "1.0.0"
    response.headers["Tus-Extension"] = "creation,termination"
    return response

@app.post("/uploads")
@limiter.limit("10/minute")
def tus_create(request: Request, response: Response, db=Depends(get_db)):
    upload_length = request.headers.get("Upload-Length")
    upload_metadata = request.headers.get("Upload-Metadata")
    
    if not upload_length:
        raise HTTPException(status_code=400, detail="Missing Upload-Length header")
    
    filename = "unknown"
    filetype = "application/octet-stream"
    duration = "never"
    video_duration_sec = 0
    category = ""
    if upload_metadata:
        parts = upload_metadata.split(",")
        for part in parts:
            kv = part.strip().split(" ")
            if len(kv) == 2:
                key, val = kv
                decoded = base64.b64decode(val).decode("utf-8")
                if key == "filename":
                    filename = decoded
                elif key == "filetype":
                    filetype = decoded
                elif key == "duration":
                    duration = decoded
                elif key == "video_duration":
                    try:
                        video_duration_sec = int(decoded)
                    except ValueError:
                        pass
                elif key == "category":
                    category = decoded
    
    prefix = "VID" if filetype.startswith("video/") else "IMG"
    max_num = 99
    pattern = re.compile(rf"^{prefix}(\d+)")
    
    # Check DB
    uploads_ref = db.reference('uploads').get()
    if uploads_ref:
        for k in uploads_ref.keys():
            if k.startswith(prefix):
                match = pattern.match(k)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num
                        
    # Check UPLOAD_DIR
    if os.path.exists(UPLOAD_DIR):
        for item in os.listdir(UPLOAD_DIR):
            match = pattern.match(item)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num

    # Check MEDIA_DIR
    if os.path.exists(MEDIA_DIR):
        for item in os.listdir(MEDIA_DIR):
            match = pattern.match(item)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
                    
    next_num = max_num + 1
    upload_id = f"{prefix}{next_num}"
    
    ext = ""
    if "." in filename:
        ext = filename[filename.rfind("."):]
    filename = f"{upload_id}{ext}"
    expires_at = None
    if duration == "30m":
        # legacy shortcut
        expires_at = datetime.utcnow() + timedelta(minutes=30)
    elif duration == "4h":
        # legacy shortcut
        expires_at = datetime.utcnow() + timedelta(hours=4)
    elif duration and duration != "never":
        # Custom picker sends a full ISO 8601 timestamp (e.g. "2026-09-29T15:30:00.000Z")
        try:
            parsed_dt = parse_iso_datetime(duration)
            if parsed_dt and parsed_dt > datetime.utcnow():
                expires_at = parsed_dt
        except Exception:
            pass
    
    session_data = {
        "id": upload_id,
        "filename": filename,
        "mimetype": filetype,
        "size": int(upload_length),
        "offset": 0,
        "status": "pending",
        "expires_at": expires_at.isoformat() + "Z" if expires_at else None,
        "duration": video_duration_sec,
        "category": category,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    upload_sessions_cache[upload_id] = session_data
    try:
        (UPLOAD_DIR / f"{upload_id}.meta.json").write_text(json.dumps(session_data), encoding="utf-8")
    except Exception:
        pass

    db.reference(f'uploads/{upload_id}').set(session_data)
    
    # Create empty file
    file_path = UPLOAD_DIR / upload_id
    file_path.touch()
    
    response.status_code = 201
    response.headers["Location"] = f"/uploads/{upload_id}"
    response.headers["Tus-Resumable"] = TUS_VERSION
    return response

@app.head("/uploads/{upload_id}")
def tus_head(upload_id: str, db=Depends(get_db)):
    file_path = UPLOAD_DIR / upload_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")
    
    actual_offset = file_path.stat().st_size
    session = get_upload_metadata(upload_id, db)
    total_size = session.get('size', 0) if session else 0
        
    response = Response(status_code=200)
    response.headers["Upload-Offset"] = str(actual_offset)
    response.headers["Upload-Length"] = str(total_size)
    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Cache-Control"] = "no-store"
    return response

@app.patch("/uploads/{upload_id}")
async def tus_patch(upload_id: str, request: Request, db=Depends(get_db)):
    file_path = UPLOAD_DIR / upload_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")
        
    upload_offset = request.headers.get("Upload-Offset")
    if upload_offset is None:
        raise HTTPException(status_code=400, detail="Missing Upload-Offset header")
    
    content_type = request.headers.get("Content-Type")
    if content_type != "application/offset+octet-stream":
        raise HTTPException(status_code=415, detail="Unsupported Media Type")
    
    actual_offset = file_path.stat().st_size
    client_offset = int(upload_offset)
    
    if client_offset != actual_offset:
        raise HTTPException(status_code=409, detail=f"Conflict: client offset {client_offset} != actual file size {actual_offset}")
        
    body = await request.body()
    if body:
        def _write():
            with open(file_path, 'ab') as f:
                f.write(body)
                f.flush()
        await asyncio.to_thread(_write)
    
    new_offset = file_path.stat().st_size
    session = get_upload_metadata(upload_id, db)
    total_size = session.get('size', 0) if session else 0
    
    if total_size > 0 and new_offset >= total_size:
        def _mark_uploaded():
            try:
                db.reference(f'uploads/{upload_id}').update({'offset': new_offset, 'status': 'uploaded'})
            except Exception as e:
                print(f"Firebase status update error for {upload_id}: {e}")
        asyncio.create_task(asyncio.to_thread(_mark_uploaded))
        
    response = Response(status_code=204)
    response.headers["Upload-Offset"] = str(new_offset)
    response.headers["Tus-Resumable"] = TUS_VERSION
    return response

async def package_hls(upload_id: str, filename: str):
    print(f"Starting packaging for {upload_id}")
    from .database import get_db
    db_conn = get_db()
    session_ref = db_conn.reference(f'uploads/{upload_id}')

    try:
        input_file = str(UPLOAD_DIR / upload_id)
        if not os.path.exists(input_file):
            print(f"Error: input file {input_file} does not exist")
            session_ref.update({'status': 'failed'})
            return

        output_dir = MEDIA_DIR / upload_id
        os.makedirs(output_dir, exist_ok=True)
        
        thumb_cmd = [
            "ffmpeg", "-y", "-i", input_file,
            "-ss", "00:00:00.000",
            "-vframes", "1",
            "-vf", "scale=300:-1",
            str(output_dir / "thumb.jpg")
        ]
        await run_cmd_async(thumb_cmd)
        
        cmd = [
            "ffmpeg", "-y", "-i", input_file, 
            "-c", "copy", 
            "-f", "hls", 
            "-hls_time", "6", 
            "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "seg_%05d.m4s",
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts",
            "-max_muxing_queue_size", "1024",
            "master.m3u8"
        ]
        
        process = await run_cmd_async(cmd, cwd=output_dir)
        session = session_ref.get()
        
        if session:
            if process.returncode == 0:
                updates = {'status': 'ready'}
                try:
                    probe_cmd = [
                        "ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", input_file
                    ]
                    process_probe = await run_cmd_async(probe_cmd)
                    p_out = process_probe.stdout.decode(errors='replace').strip()
                    if process_probe.returncode == 0 and p_out:
                        updates['duration'] = int(float(p_out))
                except Exception as e:
                    print(f"Error getting duration: {e}")
                session_ref.update(updates)
            else:
                session_ref.update({'status': 'failed'})
                err_msg = process.stderr.decode(errors='replace')
                print(f"FFmpeg Error: {err_msg}")
    except Exception as e:
        print(f"Exception in package_hls for {upload_id}: {e}")
        try:
            session_ref.update({'status': 'failed'})
        except Exception:
            pass
            
    print(f"Finished packaging for {upload_id}")

async def package_image(upload_id: str, filename: str):
    print(f"Starting image processing for {upload_id}")
    from .database import get_db
    db_conn = get_db()
    session_ref = db_conn.reference(f'uploads/{upload_id}')

    try:
        input_file = str(UPLOAD_DIR / upload_id)
        if not os.path.exists(input_file):
            print(f"Error: input file {input_file} does not exist")
            session_ref.update({'status': 'failed'})
            return

        output_dir = MEDIA_DIR / upload_id
        os.makedirs(output_dir, exist_ok=True)
        
        shutil.copy(input_file, output_dir / filename)
        
        thumb_cmd = [
            "ffmpeg", "-y", "-i", input_file,
            "-vframes", "1",
            "-vf", "scale=300:-1",
            str(output_dir / "thumb.jpg")
        ]
        await run_cmd_async(thumb_cmd)
        
        # Fallback if ffmpeg didn't produce thumb.jpg
        if not (output_dir / "thumb.jpg").exists() and os.path.exists(input_file):
            shutil.copy(input_file, output_dir / "thumb.jpg")
        
        if session_ref.get():
            session_ref.update({'status': 'ready'})
            
        print(f"Finished image processing for {upload_id}")
    except Exception as e:
        print(f"Exception in package_image for {upload_id}: {e}")
        try:
            if session_ref.get():
                session_ref.update({'status': 'failed'})
        except Exception:
            pass

@app.post("/uploads/{upload_id}/finalize")
async def finalize_upload(upload_id: str, db=Depends(get_db)):
    session_ref = db.reference(f'uploads/{upload_id}')
    session = session_ref.get()
    
    if not session:
        session = get_upload_metadata(upload_id, db)
        if not session:
            raise HTTPException(status_code=404, detail="Upload not found")
            
    file_path = UPLOAD_DIR / upload_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Upload raw file not found")
        
    actual_size = file_path.stat().st_size
    expected_size = session.get('size', 0)
    
    # Verify file has arrived
    if expected_size > 0 and actual_size < expected_size:
        raise HTTPException(status_code=400, detail=f"Cannot finalize: file incomplete ({actual_size}/{expected_size} bytes)")
        
    session_ref.update({'status': 'packaging', 'offset': actual_size})
    
    if session.get('mimetype', '').startswith("video/"):
        asyncio.create_task(package_hls(upload_id, session.get('filename')))
    else:
        asyncio.create_task(package_image(upload_id, session.get('filename')))
        
    return JSONResponse({"status": "packaging"}, status_code=202)

@app.get("/uploads/{upload_id}/status")
def get_status(upload_id: str, db=Depends(get_db)):
    session = db.reference(f'uploads/{upload_id}').get()
    if not session:
        raise HTTPException(status_code=404, detail="Not found")
    return {"status": session.get('status'), "filename": session.get('filename'), "mimetype": session.get('mimetype')}

@app.get("/uploads")
def list_uploads(db=Depends(get_db)):
    print("[API LOG] Client requested /uploads")
    uploads_ref = db.reference('uploads').get()
    if not uploads_ref:
        return []
        
    now = datetime.utcnow()
    sessions = []
    
    for uid, s in uploads_ref.items():
        if s.get('expires_at'):
            exp = parse_iso_datetime(s.get('expires_at'))
            if exp and exp < now:
                db.reference(f'uploads/{uid}').delete()
                continue
                
        if s.get('status') in ["ready", "packaging"]:
            # --- FILTER_MISSING_MEDIA ---
            # When enabled, skip entries whose /media folder doesn't exist.
            # This is a read-only check; the DB is never touched.
            if FILTER_MISSING_MEDIA and not (MEDIA_DIR / uid).is_dir():
                if not s.get('b2_direct'):
                    continue
            # ----------------------------
            s_dict = dict(s)
            s_dict['id'] = s.get('id') or uid
            sessions.append(s_dict)
            
    sessions.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return sessions

@app.delete("/uploads/bulk")
async def delete_uploads_bulk(request: Request, db=Depends(get_db)):
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"status": "ok"}

    def _delete_sync():
        for uid in ids:
            upload_sessions_cache.pop(uid, None)
            meta_file = UPLOAD_DIR / f"{uid}.meta.json"
            if meta_file.exists():
                try:
                    meta_file.unlink()
                except Exception:
                    pass
            try:
                db.reference(f'uploads/{uid}').delete()
            except Exception as e:
                print(f"Error deleting upload {uid} from DB: {e}")

    await asyncio.to_thread(_delete_sync)
    return {"status": "ok", "deleted": len(ids)}

@app.get("/media/{upload_id}/{filename}")
async def get_media_file(upload_id: str, filename: str, response: Response):
    folder = MEDIA_DIR / upload_id
    
    if folder.exists() and folder.is_dir():
        target_file = folder / filename
        if target_file.exists() and target_file.is_file():
            return FileResponse(target_file, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        
        # Smart Fallback for old files:
        candidates = [
            f for f in folder.iterdir() 
            if f.is_file() and not f.name.lower().startswith("thumb.") and not f.name.startswith("seg_") and f.name not in ("master.m3u8", "init.mp4")
        ]
        if candidates:
            candidate = candidates[0]
            try:
                candidate.rename(target_file)
                return FileResponse(target_file, headers={"Cache-Control": "public, max-age=31536000, immutable"})
            except Exception:
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    
    # NEW: If absolutely not found locally, it must be a Direct B2 Upload!
    # Redirect seamlessly to B2 presigned URL.
    try:
        bucket = os.getenv("B2_BUCKET")
        client = get_b2_client()
        url = client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': f"{upload_id}/{filename}"},
            ExpiresIn=3600
        )
        return RedirectResponse(url)
    except Exception as e:
        print(f"B2 Fallback Error: {e}")
        response.headers["Cache-Control"] = "no-store"
        raise HTTPException(status_code=404, detail="File not found locally or on B2")

app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

class ClientErrorLog(BaseModel):
    message: str
    source: Optional[str] = None
    lineno: Optional[int] = None
    colno: Optional[int] = None
    error: Optional[Any] = None
    context: Optional[str] = None
    timestamp: Optional[str] = None

ClientErrorLog.model_rebuild()

class WatchLog(BaseModel):
    action: str
    filename: str
    media_type: str
    seconds: Optional[int] = 0
    session_id: Optional[str] = None
    username: Optional[str] = "anonymous"
    seek_position: Optional[float] = 0
    skipped_seconds: Optional[int] = 0
    skip_count: Optional[int] = 0
    skips: Optional[List[Any]] = None
    watched_ranges: Optional[List[Any]] = None

WatchLog.model_rebuild()

def merge_ranges_helper(ranges):
    if not ranges:
        return []
    valid = []
    for r in ranges:
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            s, e = int(r[0]), int(r[1])
            if e > s:
                valid.append([s, e])
    if not valid:
        return []
    valid.sort(key=lambda x: x[0])
    merged = [valid[0]]
    for cur in valid[1:]:
        prev = merged[-1]
        # Bridge micro-gaps (<= 2s) caused by rapid play-pause clicks
        if cur[0] <= prev[1] + 2:
            prev[1] = max(prev[1], cur[1])
        else:
            merged.append(cur)
    return merged

@app.post("/log-watch")
def log_watch(data: WatchLog, db=Depends(get_db)):
    RESET = "\033[0m"
    seek_disp = f" (Seek: {int(data.seek_position or 0)}s)" if data.seek_position is not None else ""
    if data.action == "start":
        color = "\033[46;97m" # Cyan bg
        print(f"\n{color} [Started] {data.media_type}: {data.filename}{seek_disp} {RESET}")
    elif data.action == "watching":
        color = "\033[45;97m" # Magenta bg
        skip_info = f" (Skipped: {data.skipped_seconds}s)" if (data.skipped_seconds and data.skipped_seconds > 0) else ""
        print(f"{color} [Watching] {data.media_type}: {data.filename} - {data.seconds}s{skip_info}{seek_disp} {RESET}")
    elif data.action == "skip":
        color = "\033[41;97m" # Red bg for seek skipping
        last_skip = data.skips[-1] if (data.skips and len(data.skips) > 0) else None
        if last_skip and isinstance(last_skip, dict):
            s_from = last_skip.get('from', '?')
            s_to = last_skip.get('to', '?')
            print(f"{color} [Seek Skip] {data.media_type}: {data.filename} - Skipped from {s_from}s to {s_to}s (Jumped to {int(data.seek_position or 0)}s, Total Skipped: {data.skipped_seconds}s) {RESET}")
        else:
            print(f"{color} [Seek Skip] {data.media_type}: {data.filename} - Skipped +{data.skipped_seconds}s (Jumped to {int(data.seek_position or 0)}s, Total Skipped: {data.skipped_seconds}s) {RESET}")
    elif data.action == "stop":
        color = "\033[42;97m" # Green bg
        skip_info = f" | Skipped: {data.skipped_seconds}s" if (data.skipped_seconds and data.skipped_seconds > 0) else ""
        print(f"{color} [Finished] {data.media_type}: {data.filename} - Watched: {data.seconds}s{skip_info}{seek_disp} {RESET}\n")
    elif data.action == "upload_start":
        color = "\033[43;97m" # Yellow bg
        print(f"\n{color} [Started Uploading] {data.media_type}: {data.filename} {RESET}")
    elif data.action == "uploading":
        color = "\033[44;97m" # Blue bg
        print(f"{color} [Uploading] {data.media_type}: {data.filename} - {data.seconds}% {RESET}")
    elif data.action == "upload_stop":
        color = "\033[42;97m" # Green bg
        print(f"{color} [Finished Uploading] {data.media_type}: {data.filename} {RESET}\n")

    # Authoritative persistence to Firebase Realtime Database
    if data.session_id and data.action in ("start", "watching", "skip", "stop"):
        try:
            now_iso = datetime.utcnow().isoformat() + "Z"
            session_ref = db.reference(f'watchTimeSessions/{data.session_id}')
            existing = session_ref.get()
            w_sec = int(data.seconds or 0)
            sk_sec = int(data.skipped_seconds or 0)
            sk_cnt = int(data.skip_count or 0)
            last_seek = float(data.seek_position or 0)
            s_list = data.skips if data.skips is not None else []
            w_ranges = data.watched_ranges if data.watched_ranges is not None else []

            if not existing:
                session_ref.set({
                    "id": data.session_id,
                    "username": data.username or "anonymous",
                    "filename": data.filename,
                    "media_type": data.media_type,
                    "watch_seconds": w_sec,
                    "skipped_seconds": sk_sec,
                    "skip_count": sk_cnt,
                    "skips": s_list,
                    "watched_ranges": w_ranges,
                    "last_watched_at": now_iso,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "last_seek_position": last_seek
                })
            else:
                session_ref.update({
                    "watch_seconds": max(int(existing.get('watch_seconds', 0) or 0), w_sec),
                    "skipped_seconds": max(int(existing.get('skipped_seconds', 0) or 0), sk_sec),
                    "skip_count": max(int(existing.get('skip_count', 0) or 0), sk_cnt),
                    "skips": s_list,
                    "watched_ranges": w_ranges,
                    "last_watched_at": now_iso,
                    "updated_at": now_iso,
                    "last_seek_position": last_seek
                })
        except Exception as e:
            print(f"[Firebase DB Log Error]: {e}")

    return {"status": "logged"}

@app.get("/api/seek-position")
def get_seek_position(filename: str, username: str, db=Depends(get_db)):
    print(f"[API LOG] Client requested seek position for {username} - {filename}")
    sessions_ref = db.reference('watchTimeSessions').get()
    if not sessions_ref:
        return {
            "session_id": None,
            "seek_position": 0,
            "watch_seconds": 0,
            "skipped_seconds": 0,
            "skip_count": 0,
            "skips": [],
            "watched_ranges": []
        }
        
    match = None
    for sid, s in sessions_ref.items():
        if s.get('username') == username and s.get('filename') == filename:
            if not match or s.get('last_watched_at', '') > match.get('last_watched_at', ''):
                match = s
                
    if not match:
        return {
            "session_id": None,
            "seek_position": 0,
            "watch_seconds": 0,
            "skipped_seconds": 0,
            "skip_count": 0,
            "skips": [],
            "watched_ranges": []
        }
    return {
        "session_id": match.get('id'),
        "seek_position": match.get('last_seek_position', 0),
        "watch_seconds": match.get('watch_seconds', 0),
        "skipped_seconds": match.get('skipped_seconds', 0),
        "skip_count": match.get('skip_count', 0),
        "skips": match.get('skips', []),
        "watched_ranges": match.get('watched_ranges', [])
    }

@app.get("/api/analytics/watch-time")
def get_watch_time(request: Request, db=Depends(get_db)):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    print("[API LOG] Client requested /api/analytics/watch-time")
    
    last_window = datetime.utcnow() - timedelta(days=90)
    sessions_ref = db.reference('watchTimeSessions').get()
    if not sessions_ref:
        return {"data": [], "meta": {}}
        
    uploads_ref = db.reference('uploads').get() or {}
    
    sessions = []
    for sid, s in sessions_ref.items():
        upd = parse_iso_datetime(s.get('updated_at'))
        if upd and upd >= last_window:
            sessions.append(s)
            
    valid_updated = [s.get('updated_at') for s in sessions if s.get('updated_at')]


    valid_created = [s.get('created_at') for s in sessions if s.get('created_at')]
    earliest_updated = min(valid_updated) if valid_updated else None
    earliest_created = min(valid_created) if valid_created else None
    
    next_reset = None
    if earliest_updated:
        eu_dt = parse_iso_datetime(earliest_updated)
        if eu_dt:
            next_reset = (eu_dt + timedelta(days=90)).isoformat() + "Z"
            
    last_start = earliest_created
    
    user_data = {}
    for s in sessions:
        u = s.get('username', 'anonymous')
        if u not in user_data:
            user_data[u] = {"videos_by_file": {}, "max_per_video": {}, "max_skipped_per_video": {}}
            
        fname = s.get('filename', '')
        w_sec = int(s.get('watch_seconds', 0) or 0)
        sk_sec = int(s.get('skipped_seconds', 0) or 0)
        sk_cnt = int(s.get('skip_count', 0) or 0)
        
        us = next((v for k, v in uploads_ref.items() if v.get('filename') == fname or k == fname.rsplit('.', 1)[0]), None)
        vid_id = (us.get('id') if us else None) or fname.rsplit('.', 1)[0]
        duration = us.get('duration', 0) if us else 0

        # Cap legacy inflated watch_seconds if greater than duration
        if duration > 0 and w_sec > duration:
            w_sec = duration

        # Do NOT merge sessions per video file; instead use session_id to keep them separate in watch history
        sid = s.get('session_id') or (fname + "_" + str(s.get('last_watched_at', '')))
        
        entry = {
            "filename": fname,
            "watch_seconds": w_sec,
            "skipped_seconds": sk_sec,
            "skip_count": sk_cnt,
            "skips": list(s.get('skips', []) or []),
            "watched_ranges": list(s.get('watched_ranges', []) or []),
            "id": vid_id,
            "duration": duration,
            "media_type": s.get('media_type', ''),
            "last_watched_at": s.get('last_watched_at')
        }
        
        if entry["watched_ranges"]:
            range_watch = sum(max(0, r[1] - r[0]) for r in entry["watched_ranges"] if isinstance(r, (list, tuple)) and len(r) >= 2)
            if range_watch > 0:
                entry["watch_seconds"] = min(duration, range_watch) if duration > 0 else range_watch
                
        user_data[u]["videos_by_file"][sid] = entry
        
        # Track max per video for total leaderboard time computation
        user_data[u]["max_per_video"][fname] = max(user_data[u]["max_per_video"].get(fname, 0), entry["watch_seconds"])
        user_data[u]["max_skipped_per_video"][fname] = max(user_data[u]["max_skipped_per_video"].get(fname, 0), entry["skipped_seconds"])
        
    result = []
    for username, data in user_data.items():
        videos_list = list(data["videos_by_file"].values())
        
        overall_ranges_by_file = {}
        for v in videos_list:
            fname = v["filename"]
            if fname not in overall_ranges_by_file:
                overall_ranges_by_file[fname] = []
            overall_ranges_by_file[fname].extend(v.get("watched_ranges") or [])
            
        overall_watch_by_file = {}
        for fname, ranges in overall_ranges_by_file.items():
            merged = merge_ranges_helper(ranges)
            overall_ranges_by_file[fname] = merged
            dur = sum(max(0, r[1] - r[0]) for r in merged if isinstance(r, (list, tuple)) and len(r) >= 2)
            vid_duration = next((v["duration"] for v in videos_list if v["filename"] == fname and v.get("duration")), 0)
            overall_watch_by_file[fname] = min(vid_duration, dur) if vid_duration > 0 else dur
            
        for v in videos_list:
            fname = v["filename"]
            v["overall_watch_seconds"] = overall_watch_by_file[fname]
            v["overall_watched_ranges"] = overall_ranges_by_file[fname]
            
        total_watch = sum(overall_watch_by_file.values())
        total_skipped = sum(data["max_skipped_per_video"].values())
        
        videos_list.sort(key=lambda x: (x.get("last_watched_at") or ""), reverse=True)
        result.append({
            "username": username,
            "watch_seconds": total_watch,
            "skipped_seconds": total_skipped,
            "videos": videos_list
        })
        
    return {"data": result, "meta": {"next_reset": next_reset, "last_start": last_start}}

@app.get("/api/analytics/videos")
def get_videos_list(request: Request, db=Depends(get_db)):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    print("[API LOG] Client requested /api/analytics/videos")
    uploads_ref = db.reference('uploads').get()
    videos = []
    if uploads_ref:
        for uid, v in uploads_ref.items():
            if v.get('mimetype', '').startswith('video/') and v.get('status') == 'ready':
                v_dict = dict(v)
                v_dict['id'] = v.get('id') or uid
                videos.append(v_dict)
    videos.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return {"data": [{"id": v.get('id'), "filename": v.get('filename'), "duration": v.get('duration')} for v in videos]}

@app.post("/api/analytics/reset-watch-time")
def reset_watch_time(request: Request, db=Depends(get_db)):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    print("[API LOG] Client triggered reset-watch-time")
    last_90d = datetime.utcnow() - timedelta(days=90)
    sessions_ref = db.reference('watchTimeSessions').get()
    
    if sessions_ref:
        for sid, s in sessions_ref.items():
            upd = parse_iso_datetime(s.get('updated_at'))
            if upd and upd >= last_90d:
                new_time = upd - timedelta(days=90, minutes=1)
                db.reference(f'watchTimeSessions/{sid}').update({
                    'updated_at': new_time.isoformat() + "Z"
                })
                
    return {"status": "success", "message": "Leaderboard hidden, data kept in DB"}

def send_pushover_sync(message: str):
    import urllib.request
    import urllib.parse
    
    print(f"[DEBUG Pushover] Sending message: '{message}'")
    url = "https://api.pushover.net/1/messages.json"
    data = urllib.parse.urlencode({
        "token": "ah14gf1m63ay2nthixx1xdpf3wzhhr",
        "user": "ub9dgcg7x68jzuf71zeozh465pun3r",
        "message": message,
        "title": "Signal Deck Presence"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        response = urllib.request.urlopen(req, timeout=10)
        resp_body = response.read().decode('utf-8')
        print(f"[DEBUG Pushover] Success! HTTP Status: {response.status}, Body: {resp_body}")
    except Exception as e:
        print(f"[DEBUG Pushover] Error: {e}")

class NotifyRequest(BaseModel):
    message: str

NotifyRequest.model_rebuild()

@app.post("/api/analytics/notify-pushover")
def notify_pushover(req: NotifyRequest, request: Request, background_tasks: BackgroundTasks):
    print(f"[DEBUG API] Received request to /api/analytics/notify-pushover with message: '{req.message}'")
    background_tasks.add_task(send_pushover_sync, req.message)
    return {"status": "queued"}

RENDER_FRONTEND = "https://moo.qzz.io"
COOKIE_DOMAIN = ".moo.qzz.io"  # shared across moo.qzz.io + api.moo.qzz.io

@app.post("/login")
def login_post(password: str = Form(...)):
    if password == "198110":
        response = JSONResponse(content={"status": "ok", "redirect": f"{RENDER_FRONTEND}/analytics"})
        response.set_cookie(
            key="auth_token",
            value="198110",
            httponly=True,
            domain=COOKIE_DOMAIN,
            samesite="none",
            secure=True
        )
        return response
    return JSONResponse(content={"status": "error", "message": "Invalid password"}, status_code=401)

@app.get("/logout")
def logout():
    response = JSONResponse(content={"status": "ok", "redirect": f"{RENDER_FRONTEND}/login"})
    response.delete_cookie(key="auth_token", domain=COOKIE_DOMAIN, samesite="none", secure=True)
    return response

@app.get("/analytics")
def check_analytics_auth(request: Request):
    """Auth-check endpoint — Render page calls this to verify cookie."""
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "ok"}

@app.get("/analytics/{page}")
def check_analytics_page_auth(page: str, request: Request):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "ok"}

@app.get("/deleted_messages")
def check_deleted_messages_auth(request: Request):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "ok"}

@app.post("/log-client-error")
@limiter.limit("30/minute")
async def log_client_error(request: Request, error_data: ClientErrorLog):
    print("\n" + "="*50)
    print("🚨 CLIENT-SIDE UPLOAD/SYSTEM ERROR 🚨")
    print(f"Time: {error_data.timestamp}")
    print(f"Context: {error_data.context}")
    print(f"Message: {error_data.message}")
    print("="*50 + "\n")
    return {"status": "logged"}

# Static assets are now served from Render (moo.qzz.io) — no file routes needed on PC

def get_dir_size(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
    return total

@app.get("/api/storage")
def get_storage():
    try:
        used_bytes = 0
        if os.path.exists(UPLOAD_DIR):
            used_bytes += get_dir_size(UPLOAD_DIR)
        if os.path.exists(MEDIA_DIR):
            used_bytes += get_dir_size(MEDIA_DIR)
        
        # Get total drive capacity based on where UPLOAD_DIR is
        total_bytes = shutil.disk_usage(UPLOAD_DIR).total
        return {"used_bytes": used_bytes, "total_bytes": total_bytes}
    except Exception as e:
        return {"error": str(e), "used_bytes": 0, "total_bytes": 0}

@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "uptime": "active",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

@app.get("/")
def read_root():
    return RedirectResponse(url=RENDER_FRONTEND, status_code=302)

@app.on_event("startup")
async def startup_event():
    print("Checking for stuck uploads in Firebase RTDB...")
    from .database import get_db
    db_conn = get_db()
    uploads_ref = db_conn.reference('uploads').get()
    
    if uploads_ref:
        for uid, session in uploads_ref.items():
            status = session.get('status')
            file_path = UPLOAD_DIR / uid
            if not file_path.exists():
                continue
                
            needs_processing = False
            if status in ["uploaded", "packaging", "failed"]:
                if status == "uploaded" and session.get('offset', 0) < session.get('size', 0):
                    continue
                needs_processing = True
            elif status == "pending":
                # pending means the TUS upload never completed — skip entirely,
                # there is nothing to package yet
                continue
            elif status == "ready":
                thumb_path = MEDIA_DIR / uid / "thumb.jpg"
                if not thumb_path.exists():
                    needs_processing = True
                    
            if needs_processing:
                print(f"Packaging missing media/thumbnail for {uid}")
                db_conn.reference(f'uploads/{uid}').update({'status': 'packaging'})
                if session.get('mimetype', '').startswith("video/"):
                    asyncio.create_task(package_hls(uid, session.get('filename')))
                else:
                    asyncio.create_task(package_image(uid, session.get('filename')))

# --- Direct B2 Upload Endpoints (Multi-Account) ---

import boto3
from botocore.config import Config

# Per-account boto3 client cache: account_id -> client
_b2_clients: dict = {}

def _make_b2_client(endpoint: str, key_id: str, app_key: str):
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=app_key,
        config=Config(signature_version='s3v4')
    )

def get_b2_client_for(account: dict):
    """Return a cached boto3 client for the given account dict."""
    aid = account.get("id") or account.get("endpoint", "default")
    if aid not in _b2_clients:
        _b2_clients[aid] = _make_b2_client(
            account["endpoint"], account["key_id"], account["app_key"]
        )
    return _b2_clients[aid]

def _load_all_b2_accounts(db) -> list:
    """
    Load all B2 accounts. Always includes the .env fallback account (id='default').
    Firebase DB node: b2_accounts/{id} = {id, label, endpoint, key_id, app_key, bucket, enabled}
    """
    accounts = []
    # Firebase accounts
    try:
        fb_accounts = db.reference('b2_accounts').get() or {}
        for aid, acc in fb_accounts.items():
            if acc.get('enabled', True):
                if not acc.get('endpoint') or not acc.get('key_id') or not acc.get('app_key'):
                    # Skip incomplete Firebase entries so .env fallback can apply
                    continue
                acc['id'] = aid
                accounts.append(acc)
    except Exception as e:
        print(f"[B2] Failed to load accounts from Firebase: {e}")

    # Always include .env fallback if configured
    env_endpoint = os.getenv("B2_ENDPOINT")
    env_key_id = os.getenv("B2_KEY_ID")
    env_app_key = os.getenv("B2_APP_KEY")
    env_bucket = os.getenv("B2_BUCKET")
    if env_endpoint and env_key_id and env_app_key and env_bucket:
        # Only add if no firebase account has id='default'
        if not any(a.get('id') == 'default' for a in accounts):
            accounts.append({
                "id": "default",
                "label": "Default (.env)",
                "endpoint": env_endpoint,
                "key_id": env_key_id,
                "app_key": env_app_key,
                "bucket": env_bucket,
                "enabled": True
            })
    return accounts

def _get_account_used_bytes(client, bucket: str) -> int:
    """Get total bytes used in a B2 bucket via list_objects_v2."""
    total = 0
    try:
        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get('Contents', []):
                total += obj.get('Size', 0)
    except Exception as e:
        print(f"[B2] Could not get bucket size: {e}")
        total = -1  # unknown
    return total

def _select_best_b2_account(db) -> dict:
    """
    Select the B2 account with the most free space.

    Strategy (fast path):
      - Read cached 'used_bytes' stored in Firebase under b2_accounts/{id}/used_bytes_cache.
      - This avoids expensive per-upload bucket scans.
      - The cache is updated by _update_account_usage_cache() which is called:
          * After every successful B2 finalize (incremental += file size)
          * By the /api/b2/accounts/usage endpoint (full rescan on demand)

    If NO cache exists for an account, fall back to a live scan ONCE and store the result.
    If ALL accounts are over the threshold, raise 507 so the frontend can warn the user.
    """
    B2_LIMIT = 10 * 1024 * 1024 * 1024       # 10 GB Backblaze free tier
    WARN_THRESHOLD = 9.5 * 1024 * 1024 * 1024 # warn at 9.5 GB (95%)

    accounts = _load_all_b2_accounts(db)
    if not accounts:
        raise HTTPException(status_code=503, detail="No B2 accounts configured")

    candidates = []   # (free_bytes, account)
    all_full   = True

    for acc in accounts:
        acc_id = acc['id']

        # 1. Try cached used_bytes from Firebase
        cached_used = None
        try:
            fb_used = db.reference(f'b2_accounts/{acc_id}/used_bytes_cache').get()
            if fb_used is not None:
                cached_used = int(fb_used)
        except Exception:
            pass

        # 2. Fallback: live scan (only if no cache exists)
        if cached_used is None:
            print(f"[B2] No cache for {acc_id}, running live scan...")
            try:
                client = get_b2_client_for(acc)
                cached_used = _get_account_used_bytes(client, acc['bucket'])
                if cached_used >= 0:
                    # Store for next time
                    try:
                        db.reference(f'b2_accounts/{acc_id}/used_bytes_cache').set(cached_used)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[B2] Live scan failed for {acc_id}: {e}")
                cached_used = -1

        used = cached_used if cached_used is not None else -1
        free = B2_LIMIT - used if used >= 0 else B2_LIMIT  # assume empty if unknown

        print(f"[B2] Account '{acc.get('label', acc_id)}': used={used/(1024**3):.2f} GB, free={free/(1024**3):.2f} GB")

        if free > (B2_LIMIT - WARN_THRESHOLD):   # has meaningful free space
            all_full = False

        candidates.append((free, acc))

    if all_full:
        raise HTTPException(
            status_code=507,
            detail="All B2 accounts are full (>9.5 GB used). Please add a new Backblaze account from the admin panel."
        )

    # Sort by most free space descending
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_free, best = candidates[0]

    print(f"[B2] > Selected: '{best.get('label', best['id'])}' ({best_free/(1024**3):.2f} GB free)")
    return best


def _update_account_usage_cache(db, account_id: str, added_bytes: int):
    """
    Incrementally update the used_bytes_cache for an account after a successful upload.
    This keeps the cache fresh without a full bucket rescan.
    """
    try:
        ref = db.reference(f'b2_accounts/{account_id}/used_bytes_cache')
        current = ref.get()
        if current is not None:
            ref.set(int(current) + added_bytes)
        # If no cache exists yet, don't bother — next upload will trigger a live scan
    except Exception as e:
        print(f"[B2] Failed to update usage cache for {account_id}: {e}")


class B2InitRequest(BaseModel):
    filename: str
    filetype: str
    duration: Optional[str] = "never"
    video_duration: Optional[str] = "0"
    category: Optional[str] = ""

@app.post("/api/b2/init")
def b2_init_upload(req: B2InitRequest, db=Depends(get_db)):
    prefix = "VID" if req.filetype.startswith("video/") else "IMG"
    uploads_ref = db.reference('uploads').get() or {}
    max_num = 99
    pattern = re.compile(rf"^{prefix}(\d+)")
    for k in uploads_ref.keys():
        match = pattern.match(k)
        if match:
            max_num = max(max_num, int(match.group(1)))
    next_num = max_num + 1
    upload_id = f"{prefix}{next_num}"

    ext = ""
    if "." in req.filename:
        ext = req.filename[req.filename.rfind("."):]
    new_filename = f"{upload_id}{ext}"

    # Select the best account (most free space)
    account = _select_best_b2_account(db)
    client = get_b2_client_for(account)
    bucket = account['bucket']

    upload_url = client.generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket, 'Key': f"{upload_id}/{new_filename}", 'ContentType': req.filetype},
        ExpiresIn=3600,
        HttpMethod='PUT'
    )

    thumb_url = client.generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket, 'Key': f"{upload_id}/thumb.jpg", 'ContentType': "image/jpeg"},
        ExpiresIn=3600,
        HttpMethod='PUT'
    )

    expires_at = None
    if req.duration and req.duration not in ("never", ""):
        if req.duration == "30m":
            expires_at = datetime.utcnow() + timedelta(minutes=30)
        elif req.duration == "4h":
            expires_at = datetime.utcnow() + timedelta(hours=4)
        else:
            try:
                parsed_dt = parse_iso_datetime(req.duration)
                if parsed_dt and parsed_dt > datetime.utcnow():
                    expires_at = parsed_dt
            except Exception:
                pass

    session_data = {
        "id": upload_id,
        "filename": new_filename,
        "mimetype": req.filetype,
        "status": "uploading_direct",
        "expires_at": expires_at.isoformat() + "Z" if expires_at else None,
        "duration": int(req.video_duration) if req.video_duration else 0,
        "category": req.category,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "b2_direct": True,
        "b2_account_id": account['id']   # store which account was used
    }

    db.reference(f'uploads/{upload_id}').set(session_data)

    return {
        "uploadId": upload_id,
        "uploadUrl": upload_url,
        "thumbUrl": thumb_url,
        "filename": new_filename
    }

@app.post("/api/b2/finalize/{upload_id}")
def b2_finalize(upload_id: str, db=Depends(get_db)):
    session_ref = db.reference(f'uploads/{upload_id}')
    session = session_ref.get()
    if not session:
        raise HTTPException(status_code=404, detail="Upload not found")
    session_ref.update({'status': 'ready', 'b2_direct': True})

    # Incrementally update the storage cache for the account used
    acc_id = session.get('b2_account_id')
    file_size = session.get('size', 0) or 0
    if acc_id and file_size > 0:
        _update_account_usage_cache(db, acc_id, file_size)

    return {"status": "ready"}

@app.get("/b2-media/{upload_id}/{filename}")
def get_b2_media(upload_id: str, filename: str, db=Depends(get_db)):
    print(f"!!! HIT get_b2_media: {upload_id}/{filename}")
    """Serve B2 media from the correct account (looks up b2_account_id in DB)."""
    # Try to find which account this upload used
    account = None
    try:
        session = db.reference(f'uploads/{upload_id}').get()
        if session and session.get('b2_account_id'):
            acc_id = session['b2_account_id']
            all_accounts = _load_all_b2_accounts(db)
            account = next((a for a in all_accounts if a['id'] == acc_id), None)
    except Exception:
        pass

    if not account:
        # Fallback: try .env default
        env_endpoint = os.getenv("B2_ENDPOINT")
        env_key_id = os.getenv("B2_KEY_ID")
        env_app_key = os.getenv("B2_APP_KEY")
        env_bucket = os.getenv("B2_BUCKET")
        if env_endpoint and env_key_id and env_app_key and env_bucket:
            account = {"id": "default", "endpoint": env_endpoint, "key_id": env_key_id,
                       "app_key": env_app_key, "bucket": env_bucket}
        else:
            raise HTTPException(status_code=503, detail="No B2 account available")

    client = get_b2_client_for(account)
    url = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': account['bucket'], 'Key': f"{upload_id}/{filename}"},
        ExpiresIn=3600
    )
    return RedirectResponse(url)


# ─── B2 Account Management (Admin) ──────────────────────────────────────────

class B2AccountRequest(BaseModel):
    label: str
    endpoint: str
    key_id: str
    app_key: str
    bucket: str

def _require_admin(request: Request):
    if request.cookies.get("auth_token") != "198110":
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/api/b2/accounts")
def list_b2_accounts(request: Request, db=Depends(get_db)):
    _require_admin(request)
    accounts = _load_all_b2_accounts(db)
    # Mask app_key for security — show only first 6 chars
    safe = []
    for a in accounts:
        s = dict(a)
        if s.get('app_key') and len(s['app_key']) > 6:
            s['app_key_masked'] = s['app_key'][:6] + '••••••••'
        else:
            s['app_key_masked'] = '••••••••'
        s.pop('app_key', None)
        safe.append(s)
    return safe

@app.get("/api/b2/accounts/usage")
def get_b2_accounts_usage(request: Request, db=Depends(get_db)):
    """Full live rescan of all buckets — updates Firebase cache too. Use Refresh button sparingly."""
    _require_admin(request)
    B2_LIMIT = 10 * 1024 * 1024 * 1024
    accounts = _load_all_b2_accounts(db)
    result = []
    for acc in accounts:
        acc_id = acc['id']
        try:
            client = get_b2_client_for(acc)
            used = _get_account_used_bytes(client, acc['bucket'])
            free = B2_LIMIT - used if used >= 0 else None
            # Refresh the Firebase cache with the accurate scanned value
            if used >= 0:
                try:
                    db.reference(f'b2_accounts/{acc_id}/used_bytes_cache').set(used)
                except Exception:
                    pass
        except Exception as e:
            used = -1
            free = None
        result.append({
            "id": acc['id'],
            "label": acc.get('label', acc['id']),
            "bucket": acc.get('bucket', ''),
            "used_bytes": used,
            "free_bytes": free,
            "limit_bytes": B2_LIMIT
        })
    return result

@app.post("/api/b2/accounts")
def add_b2_account(request: Request, req: B2AccountRequest, db=Depends(get_db)):
    _require_admin(request)
    # Test the credentials before saving
    try:
        test_client = _make_b2_client(req.endpoint, req.key_id, req.app_key)
        test_client.list_objects_v2(Bucket=req.bucket, MaxKeys=1)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Credentials test failed: {str(e)}")

    new_id = str(uuid.uuid4())[:8]
    account_data = {
        "id": new_id,
        "label": req.label,
        "endpoint": req.endpoint,
        "key_id": req.key_id,
        "app_key": req.app_key,
        "bucket": req.bucket,
        "enabled": True,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    db.reference(f'b2_accounts/{new_id}').set(account_data)
    # Invalidate client cache
    _b2_clients.pop(new_id, None)
    return {"status": "added", "id": new_id}

@app.patch("/api/b2/accounts/{account_id}/toggle")
def toggle_b2_account(account_id: str, request: Request, db=Depends(get_db)):
    _require_admin(request)
    if account_id == "default":
        raise HTTPException(status_code=400, detail="Cannot toggle the default .env account")
    ref = db.reference(f'b2_accounts/{account_id}')
    acc = ref.get()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    new_state = not acc.get('enabled', True)
    ref.update({'enabled': new_state})
    return {"status": "toggled", "enabled": new_state}

@app.delete("/api/b2/accounts/{account_id}")
def delete_b2_account(account_id: str, request: Request, db=Depends(get_db)):
    _require_admin(request)
    if account_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default .env account")
    db.reference(f'b2_accounts/{account_id}').delete()
    _b2_clients.pop(account_id, None)
    return {"status": "deleted"}

