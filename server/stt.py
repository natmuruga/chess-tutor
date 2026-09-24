"""Speech-to-text with faster-whisper (MIT) if installed; the client also
supports browser speech recognition, so this is optional."""
import os, io, base64, asyncio
_model = None
def available() -> bool:
    try:
        import faster_whisper  # noqa
        return True
    except Exception:
        return False
def _load():
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
            _model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"),
                                  device=os.environ.get("WHISPER_DEVICE", "auto"), compute_type="int8")
        except Exception as e:
            print(f"[stt] faster-whisper unavailable ({e})"); _model = False
    return _model

def _transcribe(b64: str) -> str:
    import numpy as np, soundfile as sf
    data, sr = sf.read(io.BytesIO(base64.b64decode(b64)), dtype="float32")
    if data.ndim > 1: data = data.mean(axis=1)
    if sr != 16000:
        import math
        idx = np.linspace(0, len(data) - 1, int(len(data) * 16000 / sr)); data = np.interp(idx, np.arange(len(data)), data)
    segs, _ = _model.transcribe(data, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segs)

async def transcribe(b64_wav: str) -> str | None:
    if not _load(): return None
    return await asyncio.get_event_loop().run_in_executor(None, _transcribe, b64_wav)
