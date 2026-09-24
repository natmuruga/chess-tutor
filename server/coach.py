"""Turns engine facts into a short spoken explanation plus stage actions.
Qwen3 (via Ollama) writes the words; if it's unavailable, templates do."""
import os, json, re, httpx, chess
from engine import MoveReport

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
COACH_NAME = os.environ.get("COACH_NAME", "Ms. Ada")

SYSTEM = f"""You are {COACH_NAME}, a warm, concise chess coach speaking to a student.
You are given verified facts from a chess engine. Never contradict them and never invent moves.
Reply ONLY with JSON: {{"say": "<= 55 words, spoken aloud, plain text, no move lists longer than 3",
"actions": [ ... ]}}. Actions point at the board while you speak:
{{"type":"highlight","squares":["e4","d5"],"color":"red|green"}}, {{"type":"arrow","from":"e1","to":"e8"}}, {{"type":"clear"}}.
Use at most 3 actions. Prefer principles (why) over long variations."""

def _san_squares(board: chess.Board, san: str) -> tuple[str, str] | None:
    try:
        m = board.parse_san(san); return chess.square_name(m.from_square), chess.square_name(m.to_square)
    except Exception:
        return None

def template_explanation(board_before: chess.Board, r: MoveReport, student: bool = True) -> dict:
    """Deterministic fallback so the product works with no LLM at all."""
    who = "You" if student else "I"
    acts, say = [], ""
    best_sq = _san_squares(board_before, r.best_san) if r.best_san else None
    if r.mate_in == 0:
        say = f"{r.san}. Checkmate! " + ("Well played." if student else "Better luck next time.")
    elif r.label == "missed_mate":
        say = f"{r.san} is legal, but you missed a forced mate. Look at {r.best_san}."
        if best_sq: acts.append({"type": "arrow", "from": best_sq[0], "to": best_sq[1]})
    elif r.label == "blunder":
        why = f" It leaves your {r.hanging_after[0]} undefended." if r.hanging_after else ""
        say = f"{r.san} is a blunder.{why} Stronger was {r.best_san}."
        if r.hanging_after:
            acts.append({"type": "highlight", "squares": [r.hanging_after[0].split()[-1]], "color": "red"})
        if best_sq: acts.append({"type": "arrow", "from": best_sq[0], "to": best_sq[1]})
    elif r.label == "mistake":
        say = f"{r.san} loses some ground. The engine prefers {r.best_san}."
        if best_sq: acts.append({"type": "arrow", "from": best_sq[0], "to": best_sq[1]})
    elif r.label == "inaccuracy":
        say = f"{r.san} is playable, though {r.best_san} was a little more precise."
    elif r.label == "best":
        say = f"{r.san}. That's the engine's top choice." + (" Check!" if r.gives_check else "")
    else:
        extra = f" {who} captured a {r.captured}." if r.captured else (" Check!" if r.gives_check else "")
        say = f"{r.san}. A solid move.{extra}"
    return {"say": say, "actions": acts}

LAST_ERROR = {"msg": None}

async def llm_ok() -> tuple[bool, str]:
    """Is Ollama reachable and is the model pulled?"""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA}/api/tags"); r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", [])]
            if not any(n == MODEL or n.split(":")[0] == MODEL.split(":")[0] for n in names):
                return False, f"model {MODEL} not pulled (run: ollama pull {MODEL}); available: {', '.join(names) or 'none'}"
            return True, f"{MODEL} ready"
    except Exception as e:
        return False, f"Ollama not reachable at {OLLAMA} ({e.__class__.__name__})"

async def llm_json(prompt: str, timeout: float = 90.0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            res = await c.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL, "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0.4, "num_predict": 300},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            })
            res.raise_for_status()
            txt = res.json()["message"]["content"]
            txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            LAST_ERROR["msg"] = None
            return json.loads(txt)
    except Exception as e:
        LAST_ERROR["msg"] = f"{e.__class__.__name__}: {e}"
        print(f"[coach] LLM call failed: {LAST_ERROR['msg']}")
        return None

def _facts(board_before: chess.Board, r: MoveReport, student: bool) -> str:
    mover = "the student" if student else "you (the coach, playing the other side)"
    return (f"Move played by {mover}: {r.san}. Engine verdict: {r.label}. "
            f"Eval before: {r.cp_before/100:+.2f}, after: {r.cp_after/100:+.2f} (mover's view, pawns). "
            f"Best move was {r.best_san}, best line: {' '.join(r.best_line)}. "
            f"Gives check: {r.gives_check}. Capture: {r.captured or 'no'}. "
            f"Mover's pieces now hanging: {', '.join(r.hanging_after) or 'none'}. "
            f"Mate: {'delivered' if r.mate_in == 0 else (f'in {r.mate_in}' if r.mate_in else 'no')}. "
            f"FEN after move: {r.uci and board_before.fen()}")

async def explain_move(board_before: chess.Board, r: MoveReport, student: bool = True, use_llm: bool = True) -> dict:
    fallback = template_explanation(board_before, r, student)
    if not use_llm or r.mate_in == 0:
        return fallback
    hint = "Explain briefly to the student in the coach's voice. If it was good, say why in one sentence."
    out = await llm_json(_facts(board_before, r, student) + "\n" + hint)
    if out and isinstance(out.get("say"), str):
        out["actions"] = [a for a in out.get("actions", []) if isinstance(a, dict) and a.get("type")][:3] or fallback["actions"]
        return out
    return fallback

GLOSSARY = {
    "knight": ("The knight moves in an L shape: two squares one way, then one square sideways. It's the only piece that can jump over others.", "n"),
    "bishop": ("The bishop moves diagonally, any distance, and always stays on its starting colour.", "b"),
    "rook": ("The rook moves in straight lines, along ranks and files, any distance. Rooks love open files.", "r"),
    "queen": ("The queen combines rook and bishop: straight lines and diagonals, any distance. Your strongest piece.", "q"),
    "king": ("The king moves one square in any direction. Keep it safe; if it's attacked and can't escape, that's checkmate.", "k"),
    "pawn": ("Pawns move forward one square, two on their first move, and capture diagonally. Reach the last rank and they promote.", "p"),
    "fork": ("A fork is one piece attacking two or more enemy pieces at once, so the opponent can only save one.", None),
    "pin": ("A pin is when a piece can't move because it would expose a more valuable piece behind it, often the king.", None),
    "check": ("Check means the king is under attack and must deal with it right away: move, block, or capture.", None),
    "checkmate": ("Checkmate is check with no escape. The game ends.", None),
    "castle": ("Castling moves the king two squares toward a rook and the rook jumps over it. It tucks the king away and connects the rooks.", None),
}

def offline_answer(board: chess.Board, question: str, best_san: str | None) -> dict | None:
    q = question.lower()
    for key, (text, ptype) in GLOSSARY.items():
        if key in q:
            acts = []
            if ptype:
                sqs = [chess.square_name(s) for s, p in board.piece_map().items() if p.symbol().lower() == ptype][:4]
                if sqs: acts.append({"type": "highlight", "squares": sqs, "color": "green"})
            return {"say": text, "actions": acts}
    if best_san and any(w in q for w in ("play", "move", "should i", "best", "next")):
        m = board.parse_san(best_san)
        return {"say": f"Think about {best_san}. Look at what it attacks and what it defends.",
                "actions": [{"type": "arrow", "from": chess.square_name(m.from_square), "to": chess.square_name(m.to_square)}]}
    return None

async def answer_question(board: chess.Board, question: str, engine_summary: str, use_llm: bool = True, best_san: str | None = None) -> dict:
    quick = offline_answer(board, question, best_san)
    if not use_llm:
        return quick or {"say": "My language model is switched off, so I can only answer basic questions about pieces, tactics, and what to play next.", "actions": []}
    prompt = (f"The student asks: \"{question}\".\nPosition FEN: {board.fen()}. Side to move: "
              f"{'white' if board.turn else 'black'}. Engine summary: {engine_summary}.\n"
              "Answer in the coach's voice. Point at relevant squares with actions.")
    out = await llm_json(prompt)
    if out and isinstance(out.get("say"), str):
        out["actions"] = [a for a in out.get("actions", []) if isinstance(a, dict) and a.get("type")][:3]
        return out
    if quick:
        return quick
    ok, why = await llm_ok()
    return {"say": f"I can't reach my language model right now: {why}. I can still explain moves and answer basic questions.", "actions": []}
