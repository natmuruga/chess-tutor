"""Turn existing material into lessons: annotated PGN (Lichess studies), a reviewed game, or an LLM draft.
Lichess study PGN carries [%cal Ge1e8] arrows and [%csl Rf7,Rg7] coloured squares in comments; those become
our arrow/highlight actions, comments become 'say', and each annotated node becomes a step."""
import io, re, httpx, chess, chess.pgn

COLOURS = {"R": "red", "G": "green", "Y": "green", "B": "green"}

def _clean_comment(c: str) -> str:
    c = re.sub(r"\[%[a-z]+ [^\]]*\]", "", c)          # strip %cal/%csl/%clk/%eval
    return re.sub(r"\s+", " ", c).strip()

def _annotations(comment: str):
    hl, arrows = [], []
    for m in re.finditer(r"\[%csl ([^\]]+)\]", comment):
        for item in m.group(1).split(","):
            item = item.strip()
            if len(item) == 3: hl.append((COLOURS.get(item[0], "red"), item[1:]))
    for m in re.finditer(r"\[%cal ([^\]]+)\]", comment):
        for item in m.group(1).split(","):
            item = item.strip()
            if len(item) == 5: arrows.append((item[1:3], item[3:5]))
    return hl, arrows

def lesson_from_pgn(pgn_text: str, lesson_id: str, title: str | None = None, level: str = "beginner", keep_text: bool = True) -> list[dict]:
    """One lesson per game in the PGN (a Lichess study exports one game per chapter).
    keep_text=False imports only positions, moves and pointers; the coach dictates the words (use this for studies you don't own)."""
    out, idx = [], 0
    stream = io.StringIO(pgn_text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None: break
        idx += 1
        chapter = game.headers.get("Event", "") or game.headers.get("ChapterName", "")
        name = title or (chapter.split(":", 1)[-1].strip() if chapter else f"Lesson {idx}")
        steps, board = [], game.board()
        start_fen = board.fen()
        intro = _clean_comment(game.comment or "") if keep_text else ""
        hl, ar = _annotations(game.comment or "")
        actions = [{"type": "fen", "fen": start_fen}]
        if hl: actions.append({"type": "highlight", "squares": [s for _, s in hl], "color": hl[0][0]})
        for a, b in ar: actions.append({"type": "arrow", "from": a, "to": b})
        steps.append({"say": intro or (f"Let's look at {name.lower()}." if keep_text else "[Dictate your introduction to this position.]"), "actions": actions})
        node = game
        pending = []   # moves without comments, folded into the next commented step
        while node.variations:
            node = node.variations[0]
            san = board.san(node.move); board.push(node.move)
            comment = node.comment or ""
            text = _clean_comment(comment) if keep_text else ""
            has_note = bool(_clean_comment(comment))
            hl, ar = _annotations(comment)
            pending.append(san)
            if has_note or hl or ar or not node.variations:
                # several quiet moves in a row: jump to the position before the last one, then play it
                actions = [{"type": "fen", "fen": fen_before_last(board)}] if len(pending) > 1 else []
                actions.append({"type": "move", "san": pending[-1]})
                if hl: actions.append({"type": "highlight", "squares": [s for _, s in hl], "color": hl[0][0]})
                for a, b in ar: actions.append({"type": "arrow", "from": a, "to": b})
                steps.append({"say": text or (f"Then {san}." if keep_text else f"[Dictate why {san} matters here.]"), "actions": actions})
                pending = []
        sid = lesson_id if idx == 1 else f"{lesson_id}_{idx}"
        out.append({"id": sid, "title": name[:80], "level": level, "steps": steps, "source": "pgn" if keep_text else "pgn-positions"})
    return out

def fen_before_last(board_after_last: chess.Board) -> str:
    b = board_after_last.copy(); b.pop(); return b.fen()

async def fetch_lichess_study(url_or_id: str) -> str:
    m = re.search(r"study/([A-Za-z0-9]{8})(?:/([A-Za-z0-9]{8}))?", url_or_id)
    study, chapter = (m.group(1), m.group(2)) if m else (url_or_id.strip(), None)
    path = f"https://lichess.org/api/study/{study}/{chapter}.pgn" if chapter else f"https://lichess.org/api/study/{study}.pgn"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "chess-tutor/0.1"}) as c:
        r = await c.get(path, params={"comments": "true", "variations": "false", "clocks": "false", "orientation": "false"})
        r.raise_for_status()
        return r.text

def lesson_from_review(review: dict, lesson_id: str, title: str, level: str = "beginner") -> dict:
    """Key moments of a reviewed game become steps; the engine's verdict is the draft text."""
    s = review["summary"]; moves = review["moves"]
    steps = [{"say": f"This is a game you played as {s['colour']}. Let's look at the moments that decided it.",
              "actions": [{"type": "fen", "fen": moves[0]["fen_before"]}]}]
    for ply in review["key"]:
        m = moves[ply - 1]
        acts = [{"type": "fen", "fen": m["fen_before"]}]
        if m.get("best_from"): acts.append({"type": "arrow", "from": m["best_from"], "to": m["best_to"]})
        if m.get("hanging"): acts.append({"type": "highlight", "squares": [h.split()[-1] for h in m["hanging"][:2]], "color": "red"})
        what = {"blunder": "a blunder", "mistake": "a mistake", "missed_mate": "a missed checkmate"}.get(m["label"], m["label"])
        say = f"Move {(ply + 1) // 2}. You played {m['san']}, which was {what}. Better was {m['best_san']}."
        if m.get("hanging"): say += f" It left your {m['hanging'][0]} undefended."
        puzzle = {"fen": m["fen_before"], "solution": [m["best_san"]], "hint": f"Look at the {m['best_san'][0].lower() if m['best_san'][0] in 'KQRBN' else 'pawn'} move.",
                  "hint_squares": [m["best_from"]] if m.get("best_from") else []}
        steps.append({"say": say, "actions": acts})
        steps.append({"say": "Now you find the better move.", "actions": [{"type": "fen", "fen": m["fen_before"]}], "puzzle": puzzle})
    return {"id": lesson_id, "title": title, "level": level, "steps": steps, "source": "review"}

DRAFT_PROMPT = """Write a short chess lesson as JSON for a {level} student about: {topic}.
{position_line}
Return ONLY JSON: {{"title": "...", "steps": [{{"say": "<= 45 words", "actions": [...]}}, ...]}} with 3 to 6 steps.
Actions (use sparingly, only legal ones): {{"type":"fen","fen":"..."}}, {{"type":"move","san":"Nf3"}}, {{"type":"highlight","squares":["e4"],"color":"red|green"}}, {{"type":"arrow","from":"e2","to":"e4"}}.
The first step must set a position with a fen action. Keep moves legal from that position."""

async def draft_with_llm(topic: str, fen: str | None, level: str, llm_json) -> dict | None:
    position_line = f"Start from this position (FEN): {fen}" if fen else "Choose a simple, clear position and give it as FEN."
    out = await llm_json(DRAFT_PROMPT.format(level=level, topic=topic, position_line=position_line))
    if not out or not isinstance(out.get("steps"), list): return None
    steps = []
    for s in out["steps"]:
        if not isinstance(s, dict) or not str(s.get("say", "")).strip(): continue
        acts = [x for x in (s.get("actions") or []) if isinstance(x, dict) and x.get("type") in ("fen", "move", "highlight", "arrow", "clear")]
        steps.append({"say": str(s["say"]).strip(), "actions": acts})
    if not steps: return None
    if not any(x["type"] == "fen" for x in steps[0]["actions"]):
        steps[0]["actions"].insert(0, {"type": "fen", "fen": fen or chess.STARTING_FEN})
    out["steps"] = steps
    return out


# ---------- Lichess puzzles (CC0) ----------
PUZZLE_THEMES = {
    "backRankMate": "Back-rank mate", "fork": "Fork", "pin": "Pin", "skewer": "Skewer", "discoveredAttack": "Discovered attack",
    "hangingPiece": "Hanging piece", "mateIn1": "Mate in 1", "mateIn2": "Mate in 2", "smotheredMate": "Smothered mate",
    "doubleCheck": "Double check", "deflection": "Deflection", "attraction": "Attraction", "sacrifice": "Sacrifice",
    "trappedPiece": "Trapped piece", "promotion": "Promotion", "advancedPawn": "Advanced pawn", "defensiveMove": "Defensive move",
    "endgame": "Endgame", "rookEndgame": "Rook endgame", "pawnEndgame": "Pawn endgame", "queenEndgame": "Queen endgame",
    "opening": "Opening", "middlegame": "Middlegame", "short": "Short tactics", "oneMove": "One-move puzzles",
}
DIFFICULTY = {"easiest": "easiest", "easier": "easier", "normal": "normal", "harder": "harder", "hardest": "hardest"}

async def fetch_lichess_puzzles(theme: str, count: int, difficulty: str = "normal") -> list[dict]:
    """Random puzzles for a theme via the public API (one call per puzzle; rate-limited, so keep count modest)."""
    import asyncio
    out, seen = [], set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "chess-tutor/0.1"}) as c:
        for _ in range(count * 2):
            if len(out) >= count: break
            r = await c.get("https://lichess.org/api/puzzle/next", params={"angle": theme, "difficulty": difficulty})
            if r.status_code == 429:
                await asyncio.sleep(2); continue
            r.raise_for_status()
            p = r.json()
            pid = p.get("puzzle", {}).get("id")
            if not pid or pid in seen: continue
            seen.add(pid); out.append(p)
            await asyncio.sleep(0.4)     # be polite to the API
    return out

def puzzle_position(p: dict) -> tuple[chess.Board, chess.Move] | None:
    """Play the puzzle's truncated PGN to reach the start position; the first solution move is the student's."""
    moves = (p.get("game", {}).get("pgn") or "").split()
    sol = p.get("puzzle", {}).get("solution") or []
    if not sol: return None
    first = chess.Move.from_uci(sol[0])
    board = chess.Board()
    played = []
    for san in moves:
        try: board.push_san(san); played.append(san)
        except Exception: return None
    for _ in range(2):                  # the API's PGN sometimes ends one ply early or late
        if first in board.legal_moves: return board, first
        if board.move_stack: board.pop()
        else: break
    return None

def lesson_from_puzzles(puzzles: list[dict], lesson_id: str, theme: str, level: str = "beginner") -> dict:
    name = PUZZLE_THEMES.get(theme, theme)
    steps = [{"say": f"{len(puzzles)} puzzles on {name.lower()}, from the Lichess puzzle collection. Find the best move each time; I'll give a hint if you ask.",
              "actions": [{"type": "fen", "fen": chess.STARTING_FEN}]}]
    n = 0
    for p in puzzles:
        pos = puzzle_position(p)
        if not pos: continue
        board, first = pos
        n += 1
        san = board.san(first)
        piece = chess.piece_name(board.piece_at(first.from_square).piece_type)
        more = len(p["puzzle"]["solution"]) > 1
        say = f"Puzzle {n}. {'White' if board.turn else 'Black'} to move. Rating about {p['puzzle'].get('rating', '?')}." + (" Find the first move of the combination." if more else "")
        steps.append({"say": say, "actions": [{"type": "fen", "fen": board.fen()}],
                      "puzzle": {"fen": board.fen(), "solution": [san], "hint": f"Look at your {piece}. What does it attack from a different square?",
                                 "hint_squares": [chess.square_name(first.from_square)], "lichess_id": p["puzzle"]["id"]}})
    steps.append({"say": "That's the set. Want to try a harder batch, or review one of your own games?", "actions": []})
    return {"id": lesson_id, "title": f"{name} puzzles", "level": level, "steps": steps, "source": "lichess-puzzles (CC0)"}
