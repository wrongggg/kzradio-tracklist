import asyncio
import os
import ssl
import subprocess
import uuid
import threading
import traceback
from flask import Flask, render_template, request, jsonify
from shazamio import Shazam
from mutagen import File as MutagenFile
import static_ffmpeg

# Auto-add static ffmpeg binaries to PATH
static_ffmpeg.add_paths()

try:
    import certifi
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

app = Flask(__name__)

UPLOAD_FOLDER = "/tmp/radio_uploads"
TEMP_FOLDER = "/tmp/radio_temp_snippets"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# In-memory storage for background jobs
JOBS = {}

def get_audio_duration(file_path):
    """ Get exact duration in seconds via mutagen """
    audio = MutagenFile(file_path)
    if audio is not None and audio.info is not None:
        return float(audio.info.length)
    raise ValueError("Could not read audio duration from file.")

def extract_snippet(file_path, start_sec, duration_sec, output_path):
    """ Fast direct disk slice via FFmpeg static binary """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", file_path,
        "-t", str(duration_sec),
        "-acodec", "copy",
        output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

async def async_process_file(job_id, file_path, interval_min, snippet_sec, max_retries):
    try:
        total_duration_sec = get_audio_duration(file_path)
        shazam = Shazam()
        
        interval_sec = int(interval_min * 60)
        recent_tracks = {}
        DEDUP_WINDOW_SEC = 8 * 60
        
        raw_logs = []
        clean_tracks = []
        seen_clean = set()
        
        total_steps = max(1, int(total_duration_sec // interval_sec))
        step_count = 0
        
        for current_sec in range(0, int(total_duration_sec), interval_sec):
            step_count += 1
            JOBS[job_id]["progress"] = int((step_count / total_steps) * 100)
            
            seconds = current_sec % 60
            minutes = (current_sec // 60) % 60
            hours = current_sec // 3600
            timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            track_info = None
            matched_offset_sec = 0
            offsets_to_try = [0] + [15 * (r + 1) for r in range(max_retries)]
            
            for offset_sec in offsets_to_try:
                sample_sec = current_sec + offset_sec
                if sample_sec + snippet_sec > total_duration_sec:
                    break
                    
                snippet_path = os.path.join(TEMP_FOLDER, f"snippet_{job_id}_{sample_sec}.mp3")
                extract_snippet(file_path, sample_sec, snippet_sec, snippet_path)
                
                try:
                    out = await shazam.recognize(snippet_path)
                    track = out.get("track")
                    if track:
                        title = track.get("title", "Unknown Title")
                        artist = track.get("subtitle", "Unknown Artist")
                        track_info = f"{artist} - {title}"
                        matched_offset_sec = offset_sec
                        break
                except Exception:
                    pass
                finally:
                    if os.path.exists(snippet_path):
                        os.remove(snippet_path)
                        
            if track_info:
                last_seen = recent_tracks.get(track_info)
                retry_tag = f" (offset +{matched_offset_sec}s)" if matched_offset_sec > 0 else ""
                
                if last_seen is None or (current_sec - last_seen) > DEDUP_WINDOW_SEC:
                    raw_logs.append(f"✓ [{timestamp}] {track_info}{retry_tag}")
                    if track_info not in seen_clean:
                        seen_clean.add(track_info)
                        clean_tracks.append(track_info)
                else:
                    raw_logs.append(f"  [{timestamp}] (Still playing: {track_info})")
                recent_tracks[track_info] = current_sec
            else:
                raw_logs.append(f"✗ [{timestamp}] No match")
                
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["clean_tracklist"] = "\n".join(clean_tracks)
        JOBS[job_id]["logs"] = "\n".join(raw_logs)
        
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        print(f"Job {job_id} Error: {e}")
        traceback.print_exc()
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def run_job_in_thread(job_id, file_path, interval_min, snippet_sec, max_retries):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_process_file(job_id, file_path, interval_min, snippet_sec, max_retries))
    loop.close()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/process", methods=["POST"])
def process():
    if "audio_file" not in request.files:
        return jsonify({"error": "לא נבחר קובץ"}), 400
        
    file = request.files["audio_file"]
    if file.filename == "":
        return jsonify({"error": "קובץ רֵיק"}), 400
        
    interval_min = float(request.form.get("interval", 1.5))
    snippet_sec = int(request.form.get("snippet", 25))
    max_retries = int(request.form.get("retries", 2))
    
    job_id = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{file.filename}")
    file.save(save_path)
    
    JOBS[job_id] = {
        "status": "processing",
        "progress": 0,
        "clean_tracklist": "",
        "logs": "",
        "error": None
    }
    
    thread = threading.Thread(
        target=run_job_in_thread,
        args=(job_id, save_path, interval_min, snippet_sec, max_retries),
        daemon=True
    )
    thread.start()
    
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "עבודה לא נמצאה"}), 404
    return jsonify(job)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
