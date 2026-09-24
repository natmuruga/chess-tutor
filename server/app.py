"""Chess tutor server. One WebSocket per student session.
client -> server: {type: new_game|move|ask|lesson|lesson_next|hint|audio, ...}
server -> client: {type: say, text, audio?} | {type: stage, actions} | {type: state, fen, ...} | {type: lesson, ...}"""
import os, json, glob, asyncio, time, chess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from engine import Engine
from coach import explain_move, answer_question, template_explanation, llm_ok, EXAMPLES
import tts, stt

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

class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.board = chess.Board()
        self.level = 5
        self.lesson = None
        self.step = -1
        self.puzzle = None      # active puzzle dict, if any
        self.mode = "free"      # free | lesson | puzzle
        self.history = []       # last few Q&A turns, so follow-up questions make sense

    async def send(self, **msg):
        await self.ws.send_json(msg)

    async def say(self, text: str, actions=None):
        if actions:
            await self.send(type="stage", actions=actions)
        audio = await tts.speak(text)
        await self.send(type="say", text=text, audio=audio)

    async def state(self, last_move=None):
        b = self.board
        await self.send(type="state", fen=b.fen(), turn="w" if b.turn else "b",
                        last_move=last_move, game_over=b.is_game_over(), result=b.result() if b.is_game_over() else None,
                        mode=self.mode)

    async def run_step(self):
        L = self.lesson; s = L["steps"][self.step]
        for a in s["actions"]:
            if a["type"] == "fen": self.board = chess.Board(a["fen"])
            elif a["type"] == "move": self.board.push_san(a["san"])
        self.puzzle = s.get("puzzle")
        self.mode = "puzzle" if self.puzzle else "lesson"
        await self.send(type="lesson", id=L["id"], title=L["title"], step=self.step, total=len(L["steps"]),
                        steps=[x["say"] for x in L["steps"]])
        await self.say(s["say"], s["actions"])
        await self.state()

    async def apply_coach_actions(self, out: dict):
        """Board-changing actions (example, lesson) are executed here; pointing actions go to the client."""
        actions = out.get("actions") or []
        example = next((a for a in actions if a.get("type") == "example" and a.get("id") in EXAMPLES), None)
        lesson = next((a for a in actions if a.get("type") == "lesson" and a.get("id") in LESSONS), None)
        pointing = [a for a in actions if a.get("type") in ("highlight", "arrow", "clear")]
        if example:
            ex = EXAMPLES[example["id"]]
            self.board = chess.Board(ex["fen"]); self.mode = "free"; self.puzzle = None
            await self.say(out["say"], [])
            await self.state()
            stage = []
            for a in ex["actions"]:
                if a["type"] == "move":
                    self.board.push_san(a["san"]); await self.state(last_move={"san": a["san"], "from": None, "to": None})
                else:
                    stage.append(a)
            await self.say(ex["say"], stage)
            self.history[-1]["content"] += " " + ex["say"]
            return
        if lesson:
            await self.say(out["say"], [])
            self.lesson = LESSONS[lesson["id"]]; self.step = 0; await self.run_step(); return
        await self.say(out["say"], pointing)

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
            report = await asyncio.get_event_loop().run_in_executor(None, engine.review_move, before, move)
        self.board.push(move)
        await self.state(last_move={"from": msg["from"], "to": msg["to"], "san": san})

        if self.mode == "puzzle" and self.puzzle:
            ok = san in self.puzzle["solution"] or (self.puzzle.get("accept_engine_best") and report.label == "best")
            if ok:
                await self.say(f"{san}. " + ("Checkmate! " if self.board.is_checkmate() else "Exactly right. ") + "That's the idea.",
                               [{"type": "highlight", "squares": [msg["from"], msg["to"]], "color": "green"}])
                self.puzzle = None; self.mode = "lesson"
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
            reply = await asyncio.get_event_loop().run_in_executor(None, engine.play, before2, self.level)
            report2 = await asyncio.get_event_loop().run_in_executor(None, engine.review_move, before2, reply)
        rsan = before2.san(reply); self.board.push(reply)
        await self.state(last_move={"from": chess.square_name(reply.from_square), "to": chess.square_name(reply.to_square), "san": rsan})
        if self.board.is_checkmate():
            await self.say(f"{rsan}. Checkmate. Let's look at where it went wrong: ask me to review.")
        else:
            await self.say(f"I play {rsan}." + (" Check." if report2.gives_check else "") + " Your move.",
                           [{"type": "highlight", "squares": [chess.square_name(reply.from_square), chess.square_name(reply.to_square)], "color": "green"}])

    async def handle(self, msg):
        t = msg.get("type")
        if t == "new_game":
            self.board = chess.Board(); self.level = int(msg.get("level", 5)); self.mode = "free"; self.puzzle = None; self.lesson = None
            await self.state(); await self.say(f"New game. You're white and I'm playing at level {self.level} of 20. Your move.")
        elif t == "move":
            await self.handle_move(msg)
        elif t == "lesson":
            self.lesson = LESSONS.get(msg.get("id")) or next(iter(LESSONS.values())); self.step = 0; await self.run_step()
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
                    best, _ = await asyncio.get_event_loop().run_in_executor(None, engine.best_move, self.board)
                if best:
                    await self.say("Think about this piece.", [{"type": "highlight", "squares": [chess.square_name(best.from_square)], "color": "green"}])
        elif t in ("ask", "audio"):
            text = msg.get("text")
            if t == "audio":
                text = await stt.transcribe(msg["wav"])
                if text is None:
                    await self.send(type="say", text="Server speech recognition isn't installed (pip install faster-whisper). Using your browser's instead.", audio=None); return
                if not text.strip():
                    await self.say("I couldn't hear anything. Try again, a little closer to the mic."); return
                await self.send(type="transcript", text=text)
            await self.send(type="thinking", text="Thinking…")
            async with engine_lock:
                best, info = await asyncio.get_event_loop().run_in_executor(None, engine.best_move, self.board)
            summary = f"best move for side to move is {self.board.san(best) if best else 'none'}; eval {info['score'].pov(self.board.turn)}"
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
            self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": out["say"]}]
            await self.apply_coach_actions(out)
        elif t == "lessons":
            await self.send(type="lessons", items=[{"id": k, "title": v["title"], "level": v["level"]} for k, v in LESSONS.items()])

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    s = Session(ws)
    await s.send(type="lessons", items=[{"id": k, "title": v["title"], "level": v["level"]} for k, v in LESSONS.items()])
    ok, why = (await llm_ok()) if USE_LLM else (False, "USE_LLM=0")
    await s.send(type="capabilities", llm=ok, llm_note=why, stt=stt.available(), tts=tts.available(),
                 engine=os.path.basename(engine.engine.id.get("name", "stockfish")))
    await s.state()
    try:
        while True:
            await s.handle(await ws.receive_json())
    except WebSocketDisconnect:
        pass

@app.get("/health")
async def health():
    ok, why = (await llm_ok()) if USE_LLM else (False, "USE_LLM=0")
    return {"ok": True, "engine": True, "llm": ok, "llm_note": why, "stt": stt.available(), "tts": tts.available(), "lessons": list(LESSONS)}

app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "client")), name="static")
@app.get("/")
def index(): return FileResponse(os.path.join(ROOT, "client", "index.html"))
