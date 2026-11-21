import io
import os
import re
import shutil
import subprocess
import tempfile
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

MAX_BYTES = 50 * 1024 * 1024  # 50 MB limit to match frontend


def normalize_youtube_shorts(url: str) -> str:
    match = re.match(r"^https?://(?:www\.|m\.)?youtube\.com/shorts/([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url


def run_yt_dlp(url: str, output_template: str, extra_args=None):
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--force-ipv4",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "-o",
        output_template,
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=95)


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "Studio.2.O Audio Backend"})


@app.post("/extract-audio")
def extract_audio():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Please provide a valid http:// or https:// URL."}), 400

    url = normalize_youtube_shorts(url)
    tmpdir = tempfile.mkdtemp(prefix="studio_audio_")
    try:
        output_template = os.path.join(tmpdir, "audio.%(ext)s")

        # Try default yt-dlp clients first
        result = run_yt_dlp(url, output_template)

        # Fallback client if default fails
        if result.returncode != 0:
            result = run_yt_dlp(
                url,
                output_template,
                ["--extractor-args", "youtube:player_client=android,web_embedded"],
            )

        if result.returncode != 0:
            raw_err = (result.stderr or result.stdout or "").strip()
            print("yt-dlp error:", raw_err, flush=True)
            err_lower = raw_err.lower()
            if "confirm you're not a bot" in err_lower or "sign in" in err_lower or "login" in err_lower:
                return jsonify({"error": "Platform requested sign-in (bot check on cloud server)."}), 403
            if "private" in err_lower or "403" in err_lower:
                return jsonify({"error": "This video is private or restricted and cannot be imported."}), 403
            if "unavailable" in err_lower or "not found" in err_lower or "404" in err_lower:
                return jsonify({"error": "Video or audio source was not found or is unavailable."}), 404
            last_line = raw_err.splitlines()[-1] if raw_err else "Unknown extraction error"
            return jsonify({"error": f"Extraction failed: {last_line[:180]}"}), 422

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
    except Exception as exc:
        print("Server error:", repr(exc), flush=True)
        return jsonify({"error": f"Unexpected server error: {str(exc)[:150]}"}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
