import glob
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
COOKIES_PATH = "/etc/secrets/cookies.txt"


def normalize_youtube_shorts(url: str) -> str:
    match = re.match(r"^https?://(?:www\.|m\.)?youtube\.com/shorts/([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url


def run_yt_dlp(url: str, output_template: str, cookies_file=None, extra_args=None):
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--force-ipv4",
        "--concurrent-fragments",
        "4",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-f",
        "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "-o",
        output_template,
    ]
    if cookies_file:
        cmd.extend(["--cookies", cookies_file])
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=95)


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "Studio.2.O Audio Backend",
        "cookies_loaded": os.path.exists(COOKIES_PATH),
    })


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

        cookies_file = None
        if os.path.exists(COOKIES_PATH):
            cookies_file = os.path.join(tmpdir, "cookies.txt")
            shutil.copy2(COOKIES_PATH, cookies_file)

        # Try default yt-dlp clients first
        result = run_yt_dlp(url, output_template, cookies_file=cookies_file)

        # Fallback client if default fails
        if result.returncode != 0:
            result = run_yt_dlp(
                url,
                output_template,
                cookies_file=cookies_file,
                extra_args=["--extractor-args", "youtube:player_client=android,web_embedded"],
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

        downloaded = [p for p in glob.glob(os.path.join(tmpdir, "audio.*")) if not p.endswith(".part")]
        if not downloaded:
            return jsonify({"error": "No playable audio stream was found."}), 422

        audio_path = downloaded[0]
        ext = os.path.splitext(audio_path)[1].lower()

        if ext == ".m4a":
            final_path = audio_path
            mimetype = "audio/mp4"
            download_name = "extracted.m4a"
        elif ext == ".mp3":
            final_path = audio_path
            mimetype = "audio/mpeg"
            download_name = "extracted.mp3"
        else:
            # Convert non-M4A/non-MP3 formats (e.g. mp4/webm) to MP3
            final_path = os.path.join(tmpdir, "converted.mp3")
            ff = subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-vn", "-b:a", "192k", final_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if ff.returncode != 0 or not os.path.exists(final_path):
                return jsonify({"error": "Audio conversion failed."}), 422
            mimetype = "audio/mpeg"
            download_name = "extracted.mp3"

        size = os.path.getsize(final_path)
        if size == 0:
            return jsonify({"error": "Extracted audio file is empty."}), 422
        if size > MAX_BYTES:
            return jsonify({"error": "Extracted audio exceeds the 50 MB maximum file size limit."}), 422

        with open(final_path, "rb") as f:
            audio_bytes = f.read()

        return send_file(
            io.BytesIO(audio_bytes),
            mimetype=mimetype,
            as_attachment=False,
            download_name=download_name,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Audio extraction timed out. Try a shorter video."}), 504
    except Exception as exc:
        print("Server error:", repr(exc), flush=True)
        return jsonify({"error": f"Unexpected server error: {str(exc)[:150]}"}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)



