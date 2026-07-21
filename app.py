import asyncio
import os
import ssl
import subprocess
import json
import traceback
from flask import Flask, render_template, request, jsonify
from shazamio import Shazam

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

def get_audio_duration(file_path):
    """ Get exact duration in seconds without loading audio to RAM """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def extract_snippet(file_path, start_sec, duration_sec, output_path):
    """ Fast direct disk slice via FFmpeg (uses almost 0 RAM) """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", file_path,
        "-t", str(duration_sec),
        "-acodec", "copy",
        output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

async def process_file(file_path, interval_min, snippet_sec, max_retries):
    total_duration_sec = get_audio_duration(file_path)
    shazam = Shazam()
    
    interval_sec = int(interval_min * 60)
    recent_tracks = {}
    DEDUP_WINDOW_SEC = 8 * 60
    
    raw_logs = []
    clean_tracks = []
    seen_clean = set()
    
    for current_sec in range(0, int(total_duration_sec), interval_sec):
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
                
            snippet_path = os.path.join(TEMP_FOLDER, f"snippet_{sample_sec}.mp3")
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
            
    return clean_tracks, raw_logs

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
    
    save_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(save_path)
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        clean_tracks, raw_logs = loop.run_until_complete(
            process_file(save_path, interval_min, snippet_sec, max_retries)
        )
        loop.close()
    except Exception as e:
        err_msg = str(e)
        print(f"Processing Error: {err_msg}")
        traceback.print_exc()
        return jsonify({"error": f"שגיאה בעיבוד הקובץ: {err_msg}"}), 500
    finally:
        if os.path.exists(save_path):
            os.remove(save_path)
            
    return jsonify({
        "clean_tracklist": "\n".join(clean_tracks),
        "logs": "\n".join(raw_logs)
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
