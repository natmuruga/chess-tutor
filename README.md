# Chess Tutor — local full-stack scaffold

A talking chess coach that runs on one machine: Stockfish decides what is true, Qwen3 explains it,
a TTS voice speaks it, and the browser shows a lip-synced avatar pointing at the board.
Every component is Apache 2.0 / MIT / GPL-as-a-separate-process (Stockfish), so it is safe to commercialise.

```
browser ──WebSocket──▶ FastAPI (server/app.py)
  │ avatar + board          ├─ engine.py   Stockfish via python-chess  (truth: best move, blunder grading, hanging pieces)
  │ browser voice fallback  ├─ coach.py    Qwen3 via Ollama            (words: ≤55-word explanation + stage actions, JSON)
  │ mic → WAV               ├─ tts.py      Kokoro or Chatterbox        (voice; optional, browser voice otherwise)
  └─ lessons UI             ├─ stt.py      faster-whisper              (ears; optional)
                            └─ lessons/*.json                          (content: steps = say + actions [+ puzzle])
```

## Quick start (no GPU needed)

```bash
docker compose up --build            # tutor on http://localhost:8000, Ollama on :11434
docker compose exec ollama ollama pull qwen3:8b   # once (use qwen3:4b on small machines)
```
Open http://localhost:8000. Pick a lesson and press *Start lesson*, or *Play a game*.

Without Docker:
```bash
sudo apt install stockfish espeak-ng     # macOS: brew install stockfish espeak-ng
pip install -r server/requirements.txt
ollama serve && ollama pull qwen3:8b     # https://ollama.com
cd server && uvicorn app:app --reload
```

## macOS note (read this first on a Mac)

Docker on macOS cannot use the Apple GPU, so Ollama inside Docker runs on CPU and Qwen3 8B can take minutes per answer.
Run Ollama natively instead:
```bash
brew install ollama
ollama serve            # or start the Ollama menu-bar app
ollama pull qwen3:4b
```
In `docker-compose.yml` set `OLLAMA_URL: http://host.docker.internal:11434` and `LLM_MODEL: qwen3:4b`, then
`docker compose up --build`. Answers should take 2–6 s on an M-series Mac.

## Checking what's live

The header shows a status line after connecting, e.g. `engine ✓ · LLM ✗ (model qwen3:8b not pulled …) · voice browser · ears browser`.
- **LLM ✗** → free-form questions fall back to a built-in glossary and engine hints. Fix: `ollama pull qwen3:8b` (Docker: `docker compose exec ollama ollama pull qwen3:8b`), then reload. The first answer after a pull can take 30–60 s while the model loads.
- **ears browser** → the Mic button uses Chrome/Edge's built-in speech recognition (Safari/Firefox: type instead). Allow microphone access when the browser asks. Install `faster-whisper` on the server for private, offline hearing.
- **voice browser** → your OS voice. `pip install kokoro misaki[en]` for the server voice.

You can talk or type at any time, including mid-lesson: pressing Mic or Ask interrupts the coach.

## Feature switches (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `USE_LLM` | `1` | `0` → templated explanations only; everything still works |
| `LLM_MODEL` | `qwen3:8b` | any Ollama model; `qwen3:4b` for 8 GB GPUs or CPU |
| `TTS_ENGINE` | `kokoro` | `none` → browser voice; `chatterbox` → cloned voice, set `TTS_REF_WAV=/path/teacher.wav` |
| `ENGINE_DEPTH` | `14` | Stockfish depth; 10 on slow CPUs |
| `WHISPER_MODEL` | `small` | needs `pip install faster-whisper` |
| `COACH_NAME` | `Ms. Ada` | |

Kokoro: `pip install kokoro misaki[en]` (and espeak-ng). Chatterbox: `pip install chatterbox-tts` (GPU).
Whisper: `pip install faster-whisper`. Each is detected at runtime; nothing breaks if absent.

## Message contract (the part that survives into production)

Client → server: `new_game{level}`, `move{from,to,promotion?}`, `ask{text}`, `audio{wav(base64)}`,
`lesson{id}`, `lesson_next`, `hint`.
Server → client: `state{fen,turn,last_move,game_over,mode}`, `say{text,audio?}`,
`stage{actions}`, `lesson{title,step,total,steps}`, `transcript{text}`.
Stage actions: `highlight{squares,color}`, `arrow{from,to}`, `clear`, `fen{fen}`, `move{san}`.

In production the same `say`/`stage` stream is produced in the cloud and rendered in the student's
browser; only the *producer* changes.

## Adding content

Drop a JSON file in `lessons/`. Each step is `{say, actions[, puzzle]}`; a puzzle has `fen`, `solution` (SAN list),
`hint`, `hint_squares`, and optionally `accept_engine_best: true`.

## Real face (MuseTalk)

`client/index.html` draws an illustrated SVG face driven by audio amplitude. To show a real teacher:
run MuseTalk's real-time mode (https://github.com/TMElyralab/MuseTalk, MIT) as a sidecar that takes the
`say.audio` WAV and the teacher's idle video, and stream its frames into a `<video>` element in place of the SVG.
That is the only GPU-heavy piece; everything else runs on CPU.

## Licences
python-chess GPL-3 (used as a library from GPL-compatible code, or keep your server AGPL/GPL; alternatively
call Stockfish over UCI with your own thin wrapper), Stockfish GPL-3 (separate process), Qwen3 Apache 2.0,
Kokoro Apache 2.0, Chatterbox MIT, faster-whisper MIT, FastAPI MIT.
