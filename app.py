import io
import os
import shutil
import subprocess
import tempfile
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

MAX_BYTES = 50 * 1024 * 1024  # 50 MB limit to match frontend


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "Studio.2.O Audio Backend"})


@app.post("/extract-audio")
def extract_audio():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Please provide a valid http:// or https:// URL."}), 400

    tmpdir = tempfile.mkdtemp(prefix="studio_audio_")
    try:
        output_template = os.path.join(tmpdir, "audio.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "192K",
            "-o",
            output_template,
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=95)

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").lower()
            if "private" in err or "sign in" in err or "login" in err or "403" in err:
                return jsonify({"error": "This video is private or restricted and cannot be imported."}), 403
            if "unavailable" in err or "not found" in err or "404" in err:
                return jsonify({"error": "Video or audio source was not found or is unavailable."}), 404
            return jsonify({"error": "Could not extract audio from this link."}), 422

        mp3_path = os.path.join(tmpdir, "audio.mp3")
        if not os.path.exists(mp3_path):
            return jsonify({"error": "No playable audio stream was found."}), 422

        size = os.path.getsize(mp3_path)
        if size == 0:
            return jsonify({"error": "Extracted audio file is empty."}), 422
        if size > MAX_BYTES:
            return jsonify({"error": "Extracted audio exceeds the 50 MB maximum file size limit."}), 422

        with open(mp3_path, "rb") as f:
            audio_bytes = f.read()

        return send_file(
            io.BytesIO(audio_bytes),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="extracted.mp3",
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Audio extraction timed out. Try a shorter video."}), 504
    except Exception:
        return jsonify({"error": "Unexpected server error while extracting audio."}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)