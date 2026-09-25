"""Text-to-speech. Kokoro (Apache 2.0, runs on CPU) by default; Chatterbox (MIT, GPU) for a cloned voice.
If neither loads, speak() returns None and the client uses the browser voice. Loading happens once, in the
background at startup, so the first sentence isn't delayed by model loading."""
import os, io, re, base64, asyncio, threading, time

ENGINE = os.environ.get("TTS_ENGINE", "kokoro")   # kokoro | chatterbox | none
VOICE = os.environ.get("TTS_VOICE", "af_heart")     # kokoro voices: af_heart, af_bella, am_michael, bf_emma, bm_george ...
_pipe = None          # None = not tried, False = unavailable, else (kind, pipeline)
_lock = threading.Lock()
_status = "not loaded"

def available() -> bool:
    if ENGINE == "none": return False
    try:
        if ENGINE == "kokoro": import kokoro  # noqa
        elif ENGINE == "chatterbox": import chatterbox  # noqa
        return True
    except Exception:
        return False

def status() -> str:
    return _status

def _load():
    global _pipe, _status
    with _lock:
        if _pipe is not None or ENGINE == "none":
            return _pipe
        t0 = time.time()
        try:
            if ENGINE == "kokoro":
                from kokoro import KPipeline
                _pipe = ("kokoro", KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M"))
            elif ENGINE == "chatterbox":
                from chatterbox.tts import ChatterboxTTS
                _pipe = ("chatterbox", ChatterboxTTS.from_pretrained(device=os.environ.get("TTS_DEVICE", "cuda")))
            else:
                _pipe = False
            _status = f"{ENGINE} ready ({time.time()-t0:.0f}s to load)" if _pipe else "off"
        except Exception as e:
            print(f"[tts] {ENGINE} unavailable ({e.__class__.__name__}: {e}); browser voice will be used")
            _pipe = False; _status = f"{ENGINE} failed: {e.__class__.__name__}"
        return _pipe

def warm_up():
    """Call once at startup; loads the model on a thread so the server starts immediately."""
    if ENGINE != "none" and available():
        threading.Thread(target=_load, daemon=True).start()

def _clean(text: str) -> str:
    # spoken chess: "Re8#" -> "rook e8 mate", "O-O" -> "castles", "Nxf7+" -> "knight takes f7 check"
    names = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}
    def san(m):
        s = m.group(0)
        if s in ("O-O", "0-0"): return "castles kingside"
        if s in ("O-O-O", "0-0-0"): return "castles queenside"
        out = ""
        if s[0] in names: out += names[s[0]] + " "; s = s[1:]
        s = s.replace("x", " takes ").replace("+", " check").replace("#", " mate").replace("=", " promotes to ")
        return re.sub(r"\s+", " ", out + s).strip()
    return re.sub(r"(?<![A-Za-z0-9])(?:O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?|[a-h][1-8][+#])(?![A-Za-z0-9])", san, text)

def _synth(text: str):
    import numpy as np, soundfile as sf
    kind, pipe = _pipe
    text = _clean(text)
    if kind == "kokoro":
        chunks = [a for _, _, a in pipe(text, voice=VOICE, speed=1.0)]
        audio = np.concatenate(chunks) if chunks else np.zeros(2400, dtype="float32")
        sr = 24000
    else:
        ref = os.environ.get("TTS_REF_WAV")   # a few seconds of the coach's voice
        wav = pipe.generate(text, audio_prompt_path=ref) if ref else pipe.generate(text)
        audio, sr = wav.squeeze().cpu().numpy(), pipe.sr
    buf = io.BytesIO(); sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode()

async def speak(text: str) -> str | None:
    """Returns base64 WAV, or None if no local TTS (client falls back to the browser voice)."""
    if not _load():
        return None
    try:
        return await asyncio.get_event_loop().run_in_executor(None, _synth, text)
    except Exception as e:
        print(f"[tts] synthesis failed: {e.__class__.__name__}: {e}")
        return None
