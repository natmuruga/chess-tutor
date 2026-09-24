"""Text-to-speech. Tries Kokoro (Apache 2.0, fast, CPU-friendly) if installed;
otherwise returns None and the client falls back to the browser voice.
Swap in Chatterbox (MIT) here for a cloned teacher voice: same interface."""
import os, io, base64, asyncio

ENGINE = os.environ.get("TTS_ENGINE", "kokoro")   # kokoro | chatterbox | none
VOICE = os.environ.get("TTS_VOICE", "af_heart")
_pipe = None

def available() -> bool:
    if ENGINE == "none": return False
    try:
        if ENGINE == "kokoro": import kokoro  # noqa
        elif ENGINE == "chatterbox": import chatterbox  # noqa
        return True
    except Exception:
        return False

def _load():
    global _pipe
    if _pipe is not None or ENGINE == "none":
        return _pipe
    try:
        if ENGINE == "kokoro":
            from kokoro import KPipeline
            _pipe = ("kokoro", KPipeline(lang_code="a"))
        elif ENGINE == "chatterbox":
            from chatterbox.tts import ChatterboxTTS
            _pipe = ("chatterbox", ChatterboxTTS.from_pretrained(device=os.environ.get("TTS_DEVICE", "cuda")))
    except Exception as e:
        print(f"[tts] {ENGINE} unavailable ({e}); browser voice will be used")
        _pipe = False
    return _pipe

def _synth(text: str):
    import numpy as np, soundfile as sf
    kind, pipe = _pipe
    if kind == "kokoro":
        audio = np.concatenate([a for _, _, a in pipe(text, voice=VOICE)])
        sr = 24000
    else:
        ref = os.environ.get("TTS_REF_WAV")   # a few seconds of the teacher's voice
        wav = pipe.generate(text, audio_prompt_path=ref) if ref else pipe.generate(text)
        audio, sr = wav.squeeze().cpu().numpy(), pipe.sr
    buf = io.BytesIO(); sf.write(buf, audio, sr, format="WAV")
    return base64.b64encode(buf.getvalue()).decode()

async def speak(text: str) -> str | None:
    """Returns base64 WAV, or None if no local TTS."""
    if not _load():
        return None
    return await asyncio.get_event_loop().run_in_executor(None, _synth, text)
