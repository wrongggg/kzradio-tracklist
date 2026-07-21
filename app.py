import asyncio
import os
import ssl
from flask import Flask, render_template, request, jsonify
from pydub import AudioSegment
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

async def process_file(file_path, interval_min, snippet_sec, max_retries):
    audio = AudioSegment.from_file(file_path)
    shazam = Shazam()
    
    interval_ms = int(interval_min * 60 * 1000)
    snippet_len_ms = snippet_sec * 1000
    total_duration_ms = len(audio)
    
    recent_tracks = {}
    DEDUP_WINDOW_MS = 8 * 60 * 1000
    
    raw_logs = []
    clean_tracks = []
    seen_clean = set()
    
    for current_ms in range(0, total_duration_ms, interval_ms):
        seconds = int((current_ms / 1000) % 60)
        minutes = int((current_ms / (1000 * 60)) % 60)
        hours = int((current_ms / (1000 * 60 * 60)) % 24)
        timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        track_info = None
        matched_offset_sec = 0
        offsets_to_try = [0] + [15 * (r + 1) for r in range(max_retries)]
        
        for offset_sec in offsets_to_try:
            sample_ms = current_ms + (offset_sec * 1000)
            if sample_ms + snippet_len_ms > total_duration_ms:
                break
                
            snippet = audio[sample_ms : sample_ms + snippet_len_ms]
            snippet_path = os.path.join(TEMP_FOLDER, f"snippet_{sample_ms}.mp3")
            snippet.export(snippet_path, format="mp3")
            
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
            
            if last_seen is None or (current_ms - last_seen) > DEDUP_WINDOW_MS:
                raw_logs.append(f"✓ [{timestamp}] {track_info}{retry_tag}")
                if track_info not in seen_clean:
                    seen_clean.add(track_info)
                    clean_tracks.append(track_info)
            else:
                raw_logs.append(f"  [{timestamp}] (Still playing: {track_info})")
            recent_tracks[track_info] = current_ms
        else:
            raw_logs.append(f"✗ [{timestamp}] No match")
            
    return clean_tracks, raw_logs

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/process", methods=["POST"])
def process():
    if "audio_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files["audio_file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
        
    interval_min = float(request.form.get("interval", 1.5))
    snippet_sec = int(request.form.get("snippet", 25))
    max_retries = int(request.form.get("retries", 2))
    
    save_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(save_path)
    
    try:
        # Safe asyncio execution across different server environments
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        clean_tracks, raw_logs = loop.run_until_complete(
            process_file(save_path, interval_min, snippet_sec, max_retries)
        )
        loop.close()
    except Exception as e:
        print(f"Processing Exception: {e}")
        return jsonify({"error": f"Audio Error: {str(e)}"}), 500
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
