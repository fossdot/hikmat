#!/usr/bin/env python3
"""
Roshni local voice server — ONE process, both STT + TTS, bound to 127.0.0.1.
Everything stays on this Mac; audio is transcribed/synthesized and immediately discarded.

Pip-only, no compiling:
    python3 -m venv ~/roshni-voice && source ~/roshni-voice/bin/activate
    pip install flask faster-whisper piper-tts
    python3 roshni_voice.py            # first run downloads the models (~150MB STT + ~60MB voice)

Then in Frappe → Hikmat Settings → Roshni voice:
    Whisper STT endpoint = http://127.0.0.1:8090
    Piper TTS endpoint   = http://127.0.0.1:8090/tts
(The Frappe proxies ai_transcribe/ai_tts call THIS server; the browser only talks to Frappe.)

STT = faster-whisper "base" (CPU int8, stays warm).  TTS = piper CLI (stable across versions).
"""
import io
import os
import subprocess
import sys
import tempfile

from flask import Flask, Response, request

PORT = int(os.environ.get("ROSHNI_VOICE_PORT", "8090"))
WHISPER_MODEL = os.environ.get("ROSHNI_WHISPER_MODEL", "large-v3")  # best Hindi; ~heavy. Override down (medium/small) if RAM swaps.
PIPER_VOICE = os.environ.get("ROSHNI_PIPER_VOICE", "hi_IN-priyamvada-medium")
VOICE_DIR = os.path.expanduser(os.environ.get("ROSHNI_VOICE_DIR", "~/roshni-voice/voices"))
os.makedirs(VOICE_DIR, exist_ok=True)
PIPER_BIN = os.path.join(os.path.dirname(sys.executable), "piper")   # the piper CLI in this venv

# --- STT: load once, keep warm ---
print(f"Loading Whisper ({WHISPER_MODEL}, cpu/int8)… first run downloads the model", flush=True)
from faster_whisper import WhisperModel
STT = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("Whisper ready.", flush=True)

app = Flask(__name__)


@app.post("/inference")
def inference():
    """whisper.cpp-compatible: multipart 'file' (or 'audio') + form 'language' → {"text": ...}"""
    f = request.files.get("file") or request.files.get("audio")
    if not f:
        return {"text": ""}
    lang = (request.form.get("language") or "hi")[:5]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        path = tf.name
        f.save(path)
    try:
        segments, _ = STT.transcribe(
            path, language=lang, beam_size=5, vad_filter=True,
            initial_prompt="यह एक बच्ची का हिंदी में सवाल है।",   # bias toward natural Hindi
        )
        text = " ".join(s.text for s in segments).strip()
    except Exception as e:
        print("STT error:", e, flush=True)
        text = ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    print("STT →", repr(text), flush=True)
    return {"text": text}


@app.post("/tts")
def tts():
    """Raw UTF-8 text body → WAV bytes (audio/wav). Calls the piper CLI (version-stable)."""
    text = (request.get_data(as_text=True) or "").strip()
    if not text:
        return ("", 400)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out = tf.name
    try:
        # piper auto-downloads the named voice into VOICE_DIR on first use, then caches it.
        proc = subprocess.run(
            [PIPER_BIN, "-m", PIPER_VOICE, "--data-dir", VOICE_DIR,
             "--download-dir", VOICE_DIR, "-f", out],
            input=text.encode("utf-8"), capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            print("TTS error:", proc.stderr.decode("utf-8", "ignore")[:400], flush=True)
            return ("", 503)
        with open(out, "rb") as fh:
            wav = fh.read()
    except Exception as e:
        print("TTS exception:", e, flush=True)
        return ("", 503)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return Response(wav, mimetype="audio/wav")


@app.get("/health")
def health():
    return {"ok": True, "stt": WHISPER_MODEL, "voice": PIPER_VOICE, "piper_bin_exists": os.path.exists(PIPER_BIN)}


if __name__ == "__main__":
    print(f"piper CLI: {PIPER_BIN} (exists={os.path.exists(PIPER_BIN)})", flush=True)
    print(f"Roshni voice server on http://127.0.0.1:{PORT}  (STT /inference · TTS /tts · /health)", flush=True)
    app.run(host="127.0.0.1", port=PORT)
