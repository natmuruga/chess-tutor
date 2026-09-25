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

## Stability features (v0.1.8)

- **Server voice:** the Docker image installs Kokoro and pre-downloads its model, so every device hears the same coach. Set `TTS_VOICE` (e.g. `af_bella`, `am_michael`, `bf_emma`) to change it; `TTS_ENGINE=none` to save memory.
- **Reconnect:** the browser keeps a session id; if the page reloads or the network drops, the coach resumes the same lesson, game or review. Sessions expire after 6 hours idle.
- **Report a problem:** saves the last 200 lines of transcript plus the board state to `data/reports/` for the coach.
- **Engine watchdog:** if Stockfish crashes, it's restarted on the next call.
- **Fetch errors** from chess.com / Lichess are explained in plain words (unknown user, rate limit, blocked, offline).

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

## Student memory (v0.1.9)

The student types their name once ("That's me"); the browser remembers it. The coach keeps a SQLite database in
`data/tutor.db` (mounted from `./data` in Docker) with games reviewed, every mistake bucketed by type (hanging pieces,
missed mates, back-rank weakness, missed tactics, opening, endgame, bad trades), lessons completed, and every question asked.
Returning students get a greeting built from that history, reviews end with a remark about recurring themes, and the LLM
sees the profile when answering. `GET /api/questions.csv` exports every question and answer for the coach to read.

## Reviewing a student's games

Open *Review my game* under the board. Enter a chess.com or Lichess username and *Fetch games* (their public API, no login),
click a game, and the engine grades every move the student played (`REVIEW_DEPTH`, default 12). The coach summarises the game,
then *Next key moment* jumps to each mistake or blunder and explains it with the better move drawn on the board. Clicking any
move in the list shows that position; making a move on the board from there asks "what if I'd played this?".
You can also paste a PGN, which works offline.

## Coach editor (v0.1.10)

Open **http://localhost:8000/coach** (password: `COACH_TOKEN` from `docker-compose.yml`). Lessons and examples are edited
as forms: what to say, a position (FEN), a move to play, squares to highlight, an arrow, an optional puzzle. The board on
the right previews each step and can read it aloud. Saving validates the position and moves with the engine and the tutor
uses the new content immediately, no restart. Tip: build positions at lichess.org/editor and copy the FEN.

## Adding content by hand

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
