"""Chess tutor server. One WebSocket per student session.
client -> server: {type: new_game|move|ask|lesson|lesson_next|hint|audio, ...}
server -> client: {type: say, text, audio?} | {type: stage, actions} | {type: state, fen, ...} | {type: lesson, ...}"""
import os, re, json, glob, asyncio, time, chess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from engine import Engine
from coach import explain_move, answer_question, template_explanation, llm_ok, EXAMPLES
import tts, stt, review, memory, importers

USE_LLM = os.environ.get("USE_LLM", "1") == "1"
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSONS = {}
for p in glob.glob(os.path.join(ROOT, "lessons", "*.json")):
    with open(p) as f:
        L = json.load(f)
    if isinstance(L, dict) and "steps" in L:      # examples.json is a library, not a lesson
        LESSONS[L["id"]] = L

app = FastAPI(title="Chess tutor")
engine = Engine()
engine_lock = asyncio.Lock()
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
os.makedirs(os.path.join(DATA_DIR, "reports"), exist_ok=True)
SESSIONS: dict[str, "Session"] = {}          # session_id -> Session, survives websocket reconnects
SESSION_TTL = 6 * 3600

async def engine_call(fn, *args):
    """Run an engine call off the event loop; restart Stockfish once if it has died."""
    global engine
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, fn, *args)
    except Exception as e:
        print(f"[engine] {e.__class__.__name__}: {e}; restarting Stockfish")
        try: engine.close()
        except Exception: pass
        engine = Engine()
        if hasattr(engine, fn.__name__): fn = getattr(engine, fn.__name__)
        elif args and isinstance(args[0], Engine): args = (engine,) + args[1:]
        return await loop.run_in_executor(None, fn, *args)

@app.on_event("startup")
async def _startup():
    memory.init()
    tts.warm_up()

def visible_lessons(student_name: str | None) -> dict:
    """Published lessons whose audience includes this student."""
    out = {}
    for k, L in LESSONS.items():
        if L.get("published") is False: continue
        aud = L.get("audience") or {"type": "all"}
        if aud.get("type") == "students" and (not student_name or student_name.lower() not in [n.lower() for n in aud.get("names", [])]):
            continue
        out[k] = L
    return out

class Session:
    def __init__(self, ws: WebSocket, session_id: str):
        self.ws = ws
        self.id = session_id
        self.last_seen = time.time()
        self.transcript = []    # [(ts, who, text)] for "report a problem"
        self.student = None     # memory.student(...) row, once the student gives a name
        self.profile = None
        self.board = chess.Board()
        self.level = 5
        self.lesson = None
        self.step = -1
        self.puzzle = None      # active puzzle dict, if any
        self.mode = "free"      # free | lesson | puzzle
        self.history = []       # last few Q&A turns, so follow-up questions make sense
        self.review = None      # {moves, key, summary} of the game being reviewed
        self.review_ply = 0

    async def send(self, **msg):
        if msg.get("type") == "say":
            self.transcript.append((time.time(), "coach", msg.get("text", "")))
            self.transcript = self.transcript[-200:]
        try:
            await self.ws.send_json(msg)
        except Exception:
            pass                      # socket gone; the session object stays for reconnect

    async def say(self, text: str, actions=None):
        if actions:
            await self.send(type="stage", actions=actions)
        audio = await tts.speak(text)
        await self.send(type="say", text=text, audio=audio)

    async def state(self, last_move=None, fresh=False):
        """fresh=True means a new position was set up: the client clears old arrows and highlights."""
        b = self.board
        await self.send(type="state", fen=b.fen(), turn="w" if b.turn else "b", fresh=fresh,
                        last_move=last_move, game_over=b.is_game_over(), result=b.result() if b.is_game_over() else None,
                        mode=self.mode)

    async def run_step(self):
        L = self.lesson; s = L["steps"][self.step]
        pending_move = None
        for a in s["actions"]:
            if a["type"] == "fen": self.board = chess.Board(a["fen"]); await self.state(fresh=True)
            elif a["type"] == "move": pending_move = a["san"]
        self.puzzle = s.get("puzzle")
        self.mode = "puzzle" if self.puzzle else "lesson"
        await self.send(type="lesson", id=L["id"], title=L["title"], step=self.step, total=len(L["steps"]),
                        steps=[x["say"] for x in L["steps"]])
        pointing = [a for a in s["actions"] if a["type"] in ("highlight", "arrow", "clear")]
        if pending_move:
            await self.push_san_animated(pending_move)     # the piece slides as the sentence begins
        await self.say(s["say"], pointing)

    async def push_san_animated(self, san: str):
        mv = self.board.parse_san(san); self.board.push(mv)
        await self.state(last_move={"san": san, "from": chess.square_name(mv.from_square), "to": chess.square_name(mv.to_square)})

    async def apply_coach_actions(self, out: dict):
        """Board-changing actions (example, lesson) are executed here; pointing actions go to the client."""
        actions = out.get("actions") or []
        example = next((a for a in actions if a.get("type") == "example" and a.get("id") in EXAMPLES), None)
        lesson = next((a for a in actions if a.get("type") == "lesson" and a.get("id") in visible_lessons(self.student["name"] if self.student else None)), None)
        pointing = [a for a in actions if a.get("type") in ("highlight", "arrow", "clear")]
        if example:
            ex = EXAMPLES[example["id"]]
            self.board = chess.Board(ex["fen"]); self.mode = "free"; self.puzzle = None
            await self.say(out["say"], [])
            await self.state(fresh=True)
            stage = []
            for a in ex["actions"]:
                if a["type"] == "move": await self.push_san_animated(a["san"])
                else: stage.append(a)
            await self.say(ex["say"], stage)
            self.history[-1]["content"] += " " + ex["say"]
            return
        if lesson:
            await self.say(out["say"], [])
            self.lesson = LESSONS[lesson["id"]]; self.step = 0; await self.run_step(); return
        await self.say(out["say"], pointing)

    async def goto_ply(self, ply: int, explain: bool = True):
        if not self.review: return
        moves = self.review["moves"]; ply = max(0, min(ply, len(moves)))
        self.review_ply = ply
        self.board = chess.Board(moves[ply - 1]["fen_after"]) if ply else chess.Board(moves[0]["fen_before"])
        m = moves[ply - 1] if ply else None
        await self.state(last_move={"from": m["from"], "to": m["to"], "san": m["san"]} if m else None, fresh=not m)
        if not (explain and m and "label" in m): return None
        stage = []
        if m["label"] in ("mistake", "blunder", "missed_mate", "inaccuracy") and m.get("best_from"):
            stage.append({"type": "arrow", "from": m["best_from"], "to": m["best_to"]})
        if m.get("hanging"):
            stage.append({"type": "highlight", "squares": [h.split()[-1] for h in m["hanging"][:2]], "color": "red"})
        fake = chess.Board(m["fen_before"])
        from engine import MoveReport
        r = MoveReport(san=m["san"], uci="", cp_before=m["cp_before"], cp_after=m["cp_after"], loss=m["loss"], best_san=m["best_san"],
                       best_line=m["best_line"], label=m["label"], mate_in=m["mate_in"], gives_check=m["check"],
                       is_capture=bool(m["captured"]), captured=m["captured"], hanging_after=m["hanging"])
        exp = await explain_move(fake, r, student=True, use_llm=USE_LLM and m["label"] in ("mistake", "blunder", "missed_mate"))
        n = self.review["key"].index(ply) + 1 if ply in self.review["key"] else None
        lead = f"Key moment {n} of {len(self.review['key'])}, move {(ply + 1) // 2}{'' if m['colour'] == 'w' else ' for black'}: " if n else f"Move {(ply + 1) // 2}: "
        text = lead + exp["say"]
        self.history += [{"role": "user", "content": f"(looking at my move {m['san']})"}, {"role": "assistant", "content": text}]
        await self.say(text, stage + [a for a in exp.get("actions", []) if a not in stage][:2])
        return text

    async def walk_key_moments(self, start_index: int = 0):
        """Narrate every key moment in turn, pausing roughly for the speech to finish. Any new message cancels it."""
        keys = self.review["key"][start_index:]
        for i, ply in enumerate(keys):
            text = await self.goto_ply(ply, explain=True)
            await asyncio.sleep(2.5 + len(text or "") * 0.065)
        await self.say("Those were the key moments. Ask me about any of them, or click a move to look closer.")

    def review_intent(self, q: str):
        """Map natural requests during a review to review actions. Returns a coroutine or None."""
        if not self.review: return None
        t = q.lower(); keys = self.review["key"]
        m = re.search(r"(?:key moment|moment)\s*(?:number\s*)?(\d+)|(first|second|third|1st|2nd|3rd|last) (?:key )?moment", t)
        if m:
            idx = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": len(keys) - 1}.get(m.group(2)) if m.group(2) else int(m.group(1)) - 1
            if keys and 0 <= idx < len(keys): return self.goto_ply(keys[idx], explain=True)
        if re.search(r"next (?:key )?moment|next mistake|next one", t):
            return self.handle({"type": "review_next_key"})
        if re.search(r"key moments|all the moments|walk|go through|mistakes|blunders|where did i go wrong|what went wrong", t):
            return self.walk_key_moments() if keys else self.say("There were no mistakes or blunders in this game. Let's step through it move by move instead.")
        mm = re.search(r"move (\d+)", t)
        if mm:
            ply = int(mm.group(1)) * 2 - (1 if self.review["summary"]["colour"] == "white" else 0)
            return self.goto_ply(ply, explain=True)
        return None

    async def handle_move(self, msg):
        try:
            move = chess.Move.from_uci(msg["from"] + msg["to"] + msg.get("promotion", ""))
            if move not in self.board.legal_moves:
                move = chess.Move.from_uci(msg["from"] + msg["to"] + "q")
        except Exception:
            move = None
        if not move or move not in self.board.legal_moves:
            await self.say("That's not a legal move here. Try another."); return
        before = self.board.copy()
        san = before.san(move)
        async with engine_lock:
            report = await engine_call(engine.review_move, before, move)
        self.board.push(move)
        await self.state(last_move={"from": msg["from"], "to": msg["to"], "san": san})

        if self.mode == "review":
            exp = await explain_move(before, report, student=True, use_llm=USE_LLM)
            await self.say("Trying a different move? " + exp["say"], exp.get("actions"))
            self.board = before; await self.state(); return
        if self.mode == "puzzle" and self.puzzle:
            ok = san in self.puzzle["solution"] or (self.puzzle.get("accept_engine_best") and report.label == "best")
            if ok:
                await self.say(f"{san}. " + ("Checkmate! " if self.board.is_checkmate() else "Exactly right. ") + "That's the idea.",
                               [{"type": "highlight", "squares": [msg["from"], msg["to"]], "color": "green"}])
                self.puzzle = None; self.mode = "lesson"
                if self.student and self.lesson:
                    memory.lesson_done(self.student["id"], self.lesson["id"]); self.profile = memory.profile(self.student["id"]); await self.send(type="profile", **self.profile)
            else:
                exp = template_explanation(before, report)
                self.board = before
                await self.say(exp["say"] + " Have another go.", exp["actions"]); await self.state()
            return

        exp = await explain_move(before, report, student=True, use_llm=USE_LLM)
        self.history += [{"role": "user", "content": f"(I played {san})"}, {"role": "assistant", "content": exp["say"]}]
        await self.say(exp["say"], exp.get("actions"))
        if self.board.is_game_over():
            await self.say(f"Game over: {self.board.result()}. Want to review it or start again?"); return
        # coach replies
        before2 = self.board.copy()
        async with engine_lock:
            reply = await engine_call(engine.play, before2, self.level)
            report2 = await engine_call(engine.review_move, before2, reply)
        rsan = before2.san(reply); self.board.push(reply)
        await self.state(last_move={"from": chess.square_name(reply.from_square), "to": chess.square_name(reply.to_square), "san": rsan})
        if self.board.is_checkmate():
            await self.say(f"{rsan}. Checkmate. Let's look at where it went wrong: ask me to review.")
        else:
            await self.say(f"I play {rsan}." + (" Check." if report2.gives_check else "") + " Your move.",
                           [{"type": "highlight", "squares": [chess.square_name(reply.from_square), chess.square_name(reply.to_square)], "color": "green"}])

    async def send_lessons(self):
        name = self.student["name"] if self.student else None
        await self.send(type="lessons", items=[{"id": k, "title": v["title"], "level": v["level"]} for k, v in visible_lessons(name).items()])

    async def resume(self):
        """After a reconnect, put the client back where it was."""
        await self.send_lessons()
        if self.lesson:
            await self.send(type="lesson", id=self.lesson["id"], title=self.lesson["title"], step=self.step, total=len(self.lesson["steps"]),
                            steps=[x["say"] for x in self.lesson["steps"]])
        if self.review:
            await self.send(type="review", **self.review)
        if self.profile:
            await self.send(type="profile", **self.profile)
        await self.state(fresh=True)
        await self.send(type="say", text="Reconnected. We're back where we left off.", audio=None)

    async def handle(self, msg):
        t = msg.get("type")
        self.last_seen = time.time()
        if t in ("ask", "audio") and msg.get("text"):
            self.transcript.append((time.time(), "student", msg["text"]))
        walk = getattr(self, "walk_task", None)
        if walk and not walk.done() and t != "review_progress":
            walk.cancel()                       # the student spoke or clicked: stop narrating
        if t == "new_game":
            self.board = chess.Board(); self.level = int(msg.get("level", 5)); self.mode = "free"; self.puzzle = None; self.lesson = None
            await self.state(fresh=True); await self.say(f"New game. You're white and I'm playing at level {self.level} of 20. Your move.")
        elif t == "move":
            await self.handle_move(msg)
        elif t == "lesson":
            allowed = visible_lessons(self.student["name"] if self.student else None)
            self.lesson = allowed.get(msg.get("id")) or (next(iter(allowed.values())) if allowed else None)
            if not self.lesson:
                await self.say("There are no lessons available for you yet. Ask your coach to publish one."); return
            self.step = 0; await self.run_step()
        elif t == "lesson_next":
            if self.lesson and self.step + 1 < len(self.lesson["steps"]):
                self.step += 1; await self.run_step()
            else:
                await self.say("That's the end of this lesson. Try the puzzle, or start a game.")
        elif t == "hint":
            if self.puzzle:
                await self.say(self.puzzle["hint"], [{"type": "highlight", "squares": self.puzzle.get("hint_squares", []), "color": "green"}])
            else:
                async with engine_lock:
                    best, _ = await engine_call(engine.best_move, self.board)
                if best:
                    await self.say("Think about this piece.", [{"type": "highlight", "squares": [chess.square_name(best.from_square)], "color": "green"}])
        elif t in ("ask", "audio"):
            text = msg.get("text")
            coro = self.review_intent(text or "") if t == "ask" else None
            if coro:
                self.history += [{"role": "user", "content": text}]
                if coro.__name__ == "walk_key_moments":
                    self.walk_task = asyncio.create_task(coro)
                else:
                    await coro
                return
            if t == "audio":
                text = await stt.transcribe(msg["wav"])
                if text is None:
                    await self.send(type="say", text="Server speech recognition isn't installed (pip install faster-whisper). Using your browser's instead.", audio=None); return
                if not text.strip():
                    await self.say("I couldn't hear anything. Try again, a little closer to the mic."); return
                await self.send(type="transcript", text=text)
            await self.send(type="thinking", text="Thinking…")
            async with engine_lock:
                best, info = await engine_call(engine.best_move, self.board)
            summary = f"best move for side to move is {self.board.san(best) if best else 'none'}; eval {info['score'].pov(self.board.turn)}"
            if self.profile and self.profile["games"]:
                summary += (f". Student profile: {self.profile['games']} games reviewed, most common issue: {self.profile['top_label'] or 'none'}, "
                            f"lessons done: {', '.join(self.profile['lessons_done']) or 'none'}")
            if self.review:
                s = self.review["summary"]
                summary += (f". The student is reviewing a game they played as {s['colour']} (result {s['result']}); "
                            f"key moments (mistakes/blunders) at plies {self.review['key']}; currently at ply {self.review_ply}")
            t0 = time.time()
            try:
                out = await asyncio.wait_for(
                    answer_question(self.board, text, summary, use_llm=USE_LLM, best_san=self.board.san(best) if best else None,
                                    history=self.history[-6:], lesson_ids=", ".join(LESSONS)),
                    timeout=LLM_TIMEOUT)
            except asyncio.TimeoutError:
                out = {"say": f"My language model took more than {int(LLM_TIMEOUT)} seconds to answer. "
                              "On a Mac, run Ollama natively rather than in Docker, or switch to qwen3:4b.", "actions": []}
            print(f"[ask] {text!r} answered in {time.time()-t0:.1f}s")
            memory.log_question(self.student["id"] if self.student else None, self.mode, text, out["say"], "llm" if USE_LLM else "offline")
            self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": out["say"]}]
            await self.apply_coach_actions(out)
        elif t == "games":
            try:
                games = await review.fetch_games(msg.get("source", "chess.com"), msg.get("username", ""), int(msg.get("limit", 10)))
                await self.send(type="games", items=[{k: v for k, v in g.items() if k != "pgn"} for g in games], source=msg.get("source"), username=msg.get("username"))
                self._games = {g["id"]: g for g in games}
            except Exception as e:
                await self.say(review.explain_fetch_error(e, msg.get("source", "chess.com"), msg.get("username", "")))
        elif t == "review":
            pgn = msg.get("pgn") or (getattr(self, "_games", {}).get(msg.get("game_id"), {}) or {}).get("pgn")
            if not pgn:
                await self.say("Pick a game from the list or paste a PGN first."); return
            game = review.parse_pgn(pgn)
            if not game or not list(game.mainline_moves()):
                await self.say("I couldn't read that PGN."); return
            colour = review.student_colour(game, msg.get("username"))
            await self.send(type="thinking", text="Reviewing your game with the engine…")
            loop = asyncio.get_event_loop()
            def progress(p, n): asyncio.run_coroutine_threadsafe(self.send(type="review_progress", ply=p, total=n), loop)
            async with engine_lock:
                self.review = await engine_call(review.review_game, engine, game, colour, progress)
            self.mode = "review"; self.puzzle = None; self.lesson = None; self.review_ply = 0
            self.board = game.board()
            await self.send(type="review", **self.review)
            await self.state(fresh=True)
            text = review.summary_speech(self.review["summary"], self.review["key"])
            if self.student:
                gid = msg.get("game_id") or f"pgn-{abs(hash(pgn)) % 10**8}"
                rec = memory.record_review(self.student["id"], msg.get("source", "pgn"), gid, self.review["summary"], self.review["moves"], self.review["key"])
                self.profile = memory.profile(self.student["id"])
                await self.send(type="profile", **self.profile)
                text += " " + memory.review_remark(self.profile, rec["categories"])
            self.history += [{"role": "user", "content": "(review my game)"}, {"role": "assistant", "content": text}]
            await self.say(text)
        elif t == "review_goto":
            await self.goto_ply(int(msg.get("ply", 0)), explain=msg.get("explain", True))
        elif t == "review_walk":
            if self.review: self.walk_task = asyncio.create_task(self.walk_key_moments())
        elif t == "review_next_key":
            if self.review:
                nxt = next((p for p in self.review["key"] if p > self.review_ply), None)
                if nxt: await self.goto_ply(nxt, explain=True)
                else: await self.say("That was the last key moment. Ask me anything about the game, or start a new one.")
        elif t == "student":
            name = (msg.get("name") or "").strip()
            if not name:
                await self.say("Tell me your name so I can remember our work together."); return
            self.student = memory.student(name)
            self.profile = memory.profile(self.student["id"])
            await self.send(type="profile", **self.profile)
            await self.send_lessons()
            await self.say(memory.greeting(self.profile, {k: v["title"] for k, v in LESSONS.items()}))
        elif t == "report":
            path = os.path.join(DATA_DIR, "reports", f"{time.strftime('%Y%m%d-%H%M%S')}-{self.id[:8]}.json")
            with open(path, "w") as f:
                json.dump({"note": msg.get("note", ""), "session": self.id, "mode": self.mode, "fen": self.board.fen(),
                           "lesson": self.lesson["id"] if self.lesson else None, "review_ply": self.review_ply,
                           "transcript": [{"t": t_, "who": w, "text": x} for t_, w, x in self.transcript]}, f, indent=1)
            print(f"[report] saved {path}")
            await self.say("Thanks, I've saved that report for the coach to look at.")
        elif t == "lessons":
            await self.send_lessons()

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    # drop stale sessions
    for sid in [k for k, v in SESSIONS.items() if time.time() - v.last_seen > SESSION_TTL]:
        SESSIONS.pop(sid, None)
    first = await ws.receive_json()               # client always sends {type:"hello", session_id}
    sid = str(first.get("session_id") or f"s{int(time.time()*1000)}")
    s = SESSIONS.get(sid)
    ok, why = (await llm_ok()) if USE_LLM else (False, "USE_LLM=0")
    if s and first.get("type") == "hello":
        s.ws = ws
        await s.send(type="capabilities", llm=ok, llm_note=why, stt=stt.available(), tts=tts.available(), tts_note=tts.status(),
                     engine="Stockfish", session_id=sid, resumed=True)
        await s.resume()
    else:
        s = Session(ws, sid); SESSIONS[sid] = s
        await s.send(type="capabilities", llm=ok, llm_note=why, stt=stt.available(), tts=tts.available(), tts_note=tts.status(),
                     engine="Stockfish", session_id=sid, resumed=False)
        await s.send_lessons()
        await s.state(fresh=True)
        if first.get("type") != "hello":
            await s.handle(first)
    try:
        while True:
            await s.handle(await ws.receive_json())
    except WebSocketDisconnect:
        pass

@app.get("/health")
async def health():
    ok, why = (await llm_ok()) if USE_LLM else (False, "USE_LLM=0")
    return {"ok": True, "engine": True, "llm": ok, "llm_note": why, "stt": stt.available(), "tts": tts.available(), "tts_note": tts.status(),
            "sessions": len(SESSIONS), "lessons": list(LESSONS)}

# ---------- coach content API (lessons & examples as editable data) ----------
from fastapi import Request, HTTPException
from pydantic import BaseModel
COACH_TOKEN = os.environ.get("COACH_TOKEN", "")

def _auth(request: Request):
    if COACH_TOKEN and request.headers.get("x-coach-token") != COACH_TOKEN and request.query_params.get("token") != COACH_TOKEN:
        raise HTTPException(401, "coach token required")

def _validate_actions(actions, board: chess.Board | None):
    for act in actions:
        t = act.get("type")
        if t == "fen":
            try: board = chess.Board(act["fen"])
            except Exception as e: raise HTTPException(400, f"bad FEN: {act.get('fen')}")
        elif t == "move":
            if board is None: raise HTTPException(400, "move before any position")
            try: board.push_san(act["san"])
            except Exception: raise HTTPException(400, f"illegal move {act.get('san')} in {board.fen()}")
        elif t == "highlight":
            if not all(re.fullmatch(r"[a-h][1-8]", s) for s in act.get("squares", [])): raise HTTPException(400, "bad square in highlight")
        elif t == "arrow":
            if not (re.fullmatch(r"[a-h][1-8]", act.get("from", "")) and re.fullmatch(r"[a-h][1-8]", act.get("to", ""))): raise HTTPException(400, "bad arrow")
        elif t not in ("clear",):
            raise HTTPException(400, f"unknown action {t}")
    return board

def _reload_content():
    global LESSONS
    LESSONS.clear()
    for p in glob.glob(os.path.join(ROOT, "lessons", "*.json")):
        with open(p) as f: L = json.load(f)
        if isinstance(L, dict) and "steps" in L: LESSONS[L["id"]] = L
    EXAMPLES.clear()
    with open(os.path.join(ROOT, "lessons", "examples.json")) as f: EXAMPLES.update(json.load(f))

@app.get("/api/lessons")
def api_lessons(request: Request):
    _auth(request); return {"lessons": list(LESSONS.values()), "examples": EXAMPLES}

@app.put("/api/lessons/{lesson_id}")
async def api_save_lesson(lesson_id: str, request: Request):
    _auth(request)
    L = await request.json()
    if not re.fullmatch(r"[a-z0-9_]{2,40}", lesson_id): raise HTTPException(400, "id: lowercase letters, digits, underscores")
    if not L.get("title") or not L.get("steps"): raise HTTPException(400, "title and at least one step required")
    board = None
    for i, s in enumerate(L["steps"]):
        if not s.get("say", "").strip(): raise HTTPException(400, f"step {i+1} has nothing to say")
        board = _validate_actions(s.get("actions", []), board)
        if s.get("puzzle"):
            pz = s["puzzle"]
            try: pb = chess.Board(pz["fen"])
            except Exception: raise HTTPException(400, f"step {i+1}: bad puzzle FEN")
            for sol in pz.get("solution", []):
                try: pb.copy().push_san(sol)
                except Exception: raise HTTPException(400, f"step {i+1}: puzzle solution {sol} is not legal")
    L["id"] = lesson_id; L.setdefault("level", "beginner"); L.setdefault("published", True)
    aud = L.get("audience") or {"type": "all"}
    if aud.get("type") not in ("all", "students"): raise HTTPException(400, "audience.type must be all or students")
    if aud.get("type") == "students": aud["names"] = [n.strip() for n in aud.get("names", []) if n.strip()]
    L["audience"] = aud
    with open(os.path.join(ROOT, "lessons", f"{lesson_id}.json"), "w") as f: json.dump(L, f, indent=1)
    _reload_content(); return {"ok": True, "id": lesson_id}

class ImportBody(BaseModel):
    kind: str                      # pgn | lichess | review | draft
    id: str
    title: str | None = None
    level: str = "beginner"
    pgn: str | None = None
    url: str | None = None
    session_id: str | None = None  # for kind=review: the tutor session holding the review
    topic: str | None = None
    fen: str | None = None

@app.post("/api/lessons/import")
async def api_import(body: ImportBody, request: Request):
    """Create draft lessons (unpublished) from a PGN, a Lichess study, a reviewed game, or an LLM draft."""
    _auth(request)
    if not re.fullmatch(r"[a-z0-9_]{2,40}", body.id): raise HTTPException(400, "id: lowercase letters, digits, underscores")
    lessons = []
    try:
        if body.kind == "pgn":
            if not body.pgn: raise HTTPException(400, "pgn required")
            lessons = importers.lesson_from_pgn(body.pgn, body.id, body.title, body.level)
        elif body.kind == "lichess":
            if not body.url: raise HTTPException(400, "url required")
            pgn = await importers.fetch_lichess_study(body.url)
            lessons = importers.lesson_from_pgn(pgn, body.id, body.title, body.level)
        elif body.kind == "review":
            s = SESSIONS.get(body.session_id or "")
            if not s or not s.review: raise HTTPException(400, "no reviewed game in that session")
            lessons = [importers.lesson_from_review(s.review, body.id, body.title or "Lessons from your game", body.level)]
        elif body.kind == "draft":
            from coach import llm_json
            if not body.topic: raise HTTPException(400, "topic required")
            d = await importers.draft_with_llm(body.topic, body.fen, body.level, llm_json)
            if not d: raise HTTPException(502, "the language model didn't return a usable draft; is Ollama running?")
            lessons = [{"id": body.id, "title": d.get("title") or body.title or body.topic, "level": body.level, "steps": d["steps"], "source": "draft"}]
        else:
            raise HTTPException(400, "kind must be pgn, lichess, review or draft")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(400, f"import failed: {e.__class__.__name__}: {e}")
    if not lessons: raise HTTPException(400, "nothing importable found")
    saved, problems = [], []
    for L in lessons:
        board = None
        try:
            for st in L["steps"]: board = _validate_actions(st.get("actions", []), board)
        except HTTPException as e:
            problems.append(f"{L['id']}: {e.detail}")
            for st in L["steps"]: st["actions"] = [x for x in st.get("actions", []) if x["type"] in ("fen", "highlight", "arrow", "clear")]
        L["published"] = False; L["audience"] = {"type": "all"}
        with open(os.path.join(ROOT, "lessons", f"{L['id']}.json"), "w") as f: json.dump(L, f, indent=1)
        saved.append(L["id"])
    _reload_content()
    return {"ok": True, "ids": saved, "problems": problems, "note": "Saved as drafts (unpublished). Review, edit and publish them."}

@app.delete("/api/lessons/{lesson_id}")
def api_delete_lesson(lesson_id: str, request: Request):
    _auth(request)
    p = os.path.join(ROOT, "lessons", f"{lesson_id}.json")
    if lesson_id not in LESSONS or not os.path.exists(p): raise HTTPException(404, "no such lesson")
    os.remove(p); _reload_content(); return {"ok": True}

@app.put("/api/examples/{example_id}")
async def api_save_example(example_id: str, request: Request):
    _auth(request)
    ex = await request.json()
    if not re.fullmatch(r"[a-z0-9_]{2,40}", example_id): raise HTTPException(400, "id: lowercase letters, digits, underscores")
    if not ex.get("title") or not ex.get("say") or not ex.get("fen"): raise HTTPException(400, "title, position and text required")
    try: board = chess.Board(ex["fen"])
    except Exception: raise HTTPException(400, "bad FEN")
    _validate_actions(ex.get("actions", []), board)
    ex["keywords"] = [k.strip().lower() for k in ex.get("keywords", []) if k.strip()] or [ex["title"].lower()]
    path = os.path.join(ROOT, "lessons", "examples.json")
    with open(path) as f: allx = json.load(f)
    allx[example_id] = ex
    with open(path, "w") as f: json.dump(allx, f, indent=1)
    _reload_content(); return {"ok": True, "id": example_id}

@app.delete("/api/examples/{example_id}")
def api_delete_example(example_id: str, request: Request):
    _auth(request)
    path = os.path.join(ROOT, "lessons", "examples.json")
    with open(path) as f: allx = json.load(f)
    if example_id not in allx: raise HTTPException(404, "no such example")
    del allx[example_id]
    with open(path, "w") as f: json.dump(allx, f, indent=1)
    _reload_content(); return {"ok": True}

@app.get("/coach")
def coach_page(): return FileResponse(os.path.join(ROOT, "client", "coach.html"))

@app.get("/api/questions.csv")
def questions_csv(request: Request):
    _auth(request)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(memory.export_questions_csv(), media_type="text/csv")

app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "client")), name="static")
@app.get("/")
def index(): return FileResponse(os.path.join(ROOT, "client", "index.html"))
