# Roshni voice — local Whisper (STT) + Piper (TTS) on the MacBook

Two small local daemons, both bound to **127.0.0.1** (never the LAN). Frappe proxies the
browser to them (`ai_transcribe`, `ai_tts`) so there's no CORS and the daemons aren't exposed.
Verified-efficient picks for a 16GB fanned Mac running gemma4:12b-mlx + Frappe:

- **STT:** whisper.cpp `base` on Metal (ANE is broken on M4+macOS26), ~1GB, single-flights with gemma on the GPU.
- **TTS:** Piper `hi_IN-priyamvada-medium` (female), CPU-only (~0.2GB), so it overlaps the LLM for free.

> Versions move; if a command name differs on your install, run its `--help` and tell me the
> exact request/response shape so I match the proxy. The integration **contract** the Frappe
> proxies assume is at the bottom.

## 1. Whisper STT  → 127.0.0.1:8080

```bash
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build && cmake --build build -j --config Release        # Metal is on by default on macOS
sh ./models/download-ggml-model.sh base                          # ~142MB; small + fast
# optional RAM trim: ./build/bin/quantize models/ggml-base.bin models/ggml-base-q5_0.bin q5_0
./build/bin/whisper-server -m models/ggml-base.bin \
    --host 127.0.0.1 --port 8080 --language hi --convert
```

Test it:
```bash
curl -F file=@/path/to/clip.wav -F response_format=json -F language=hi \
     http://127.0.0.1:8080/inference
# → {"text":"..."}
```

## 2. Piper TTS  → 127.0.0.1:5000

```bash
python3 -m venv ~/piper-env && source ~/piper-env/bin/activate
pip install 'piper-tts[http]'
python3 -m piper.download_voices hi_IN-priyamvada-medium          # female; pratham-medium = male fallback
python3 -m piper.http_server -m hi_IN-priyamvada-medium --host 127.0.0.1 --port 5000
```

Test it (the proxy POSTs raw UTF-8 text and expects WAV bytes back):
```bash
curl -X POST --data 'नमस्ते, मैं रोशनी हूँ।' http://127.0.0.1:5000 --output roshni.wav && afplay roshni.wav
```

## 3. Keep them running

For a classroom box, run both under a process manager so a crash auto-restarts (a `launchd`
plist each, or add two lines to the bench `Procfile` if you start everything with `bench start`).
Bind both to `127.0.0.1` so other devices on the school wifi can't reach them.

## 4. Turn it on in Frappe

Desk → **Hikmat Settings** → **Roshni voice** section → ✅ **Enable voice**. Endpoints default to
the ports above; `Piper Hindi voice` is informational (the voice is fixed when you launch the
Piper server). Run `add_ai_fields` again first if the voice fields aren't showing:
```bash
bench --site hikmat.local execute hikmat.setup_data.add_ai_fields
```

Test the proxies (logged-in student token required — grab one from a login response):
```bash
curl -F audio=@clip.wav "http://localhost:8002/api/method/hikmat.api.ai_transcribe?student=<id>&token=<tok>&lang=hi"
curl -X POST "http://localhost:8002/api/method/hikmat.api.ai_tts" -d 'student=<id>&token=<tok>&text=शाबाश!' --output r.wav
```

## Integration contract (what the Frappe proxies assume)

- **STT** (`ai_transcribe`): POSTs multipart `file=<wav>` + `language` to `<stt_endpoint>/inference`,
  reads `{"text": "..."}`. Whisper's server gives this natively.
- **TTS** (`ai_tts`): POSTs raw UTF-8 text body to `<tts_endpoint>` and expects `audio/wav` bytes.
  If your Piper server wants JSON (`{"text": ...}`) or a `/api/tts` path instead, tell me and I'll
  adjust the one `requests.post(...)` line.

## Efficiency / ops notes (from the verified design)

- **Single-flight** STT→LLM→TTS is enforced client-side (the upcoming mic UI): Whisper and gemma
  share the GPU, so they must take turns. Piper (CPU) overlaps the LLM safely.
- gemma stays warm (`keep_alive=-1`); don't evict it between turns.
- True headroom on 16GB is only ~0.5–1GB. **Measure before relying on it:** during a live turn run
  `footprint`, `memory_pressure`, and watch `vm_stat` swapins. If it swaps, drop Whisper to `tiny`.
- Audio is forwarded, **never stored** — only the transcript is logged (via `ai_ask`).
