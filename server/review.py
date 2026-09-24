"""Game review: fetch a student's games (chess.com / Lichess / pasted PGN), grade the student's moves
with Stockfish, and pick the key moments a coach would talk about."""
import io, os, re, asyncio, datetime, httpx, chess, chess.pgn
from engine import Engine, MoveReport

REVIEW_DEPTH = int(os.environ.get("REVIEW_DEPTH", "12"))
UA = {"User-Agent": "chess-tutor/0.1 (local coaching app)"}

async def fetch_games(source: str, username: str, limit: int = 10) -> list[dict]:
    """Recent games as [{id, url, pgn, white, black, result, date, time_class}]."""
    username = username.strip()
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as c:
        if source == "lichess":
            r = await c.get(f"https://lichess.org/api/games/user/{username}", params={"max": limit, "pgnInJson": "true"},
                            headers={**UA, "Accept": "application/x-ndjson"})
            r.raise_for_status()
            import json
            out = []
            for line in r.text.splitlines():
                if not line.strip(): continue
                g = json.loads(line)
                out.append({"id": g["id"], "url": f"https://lichess.org/{g['id']}", "pgn": g.get("pgn", ""),
                            "white": g["players"]["white"].get("user", {}).get("name", "?"),
                            "black": g["players"]["black"].get("user", {}).get("name", "?"),
                            "result": _lichess_result(g), "date": datetime.datetime.utcfromtimestamp(g["createdAt"]/1000).strftime("%Y-%m-%d"),
                            "time_class": g.get("speed", "")})
            return out
        # chess.com: monthly archives, newest first
        r = await c.get(f"https://api.chess.com/pub/player/{username}/games/archives"); r.raise_for_status()
        archives = r.json().get("archives", [])[::-1]
        out = []
        for url in archives[:3]:
            r = await c.get(url); r.raise_for_status()
            for g in reversed(r.json().get("games", [])):
                if "pgn" not in g: continue
                out.append({"id": g["url"].rsplit("/", 1)[-1], "url": g["url"], "pgn": g["pgn"],
                            "white": g["white"]["username"], "black": g["black"]["username"],
                            "result": _chesscom_result(g, username), "date": datetime.datetime.utcfromtimestamp(g["end_time"]).strftime("%Y-%m-%d"),
                            "time_class": g.get("time_class", "")})
                if len(out) >= limit: return out
        return out

def _chesscom_result(g, username):
    me = "white" if g["white"]["username"].lower() == username.lower() else "black"
    res = g[me]["result"]
    return "win" if res == "win" else "draw" if res in ("agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient") else "loss"

def _lichess_result(g):
    w = g.get("winner")
    return "draw" if not w else w   # caller maps to the student's colour

def parse_pgn(pgn: str) -> chess.pgn.Game | None:
    return chess.pgn.read_game(io.StringIO(pgn))

def student_colour(game: chess.pgn.Game, username: str | None) -> chess.Color:
    if username and game.headers.get("Black", "").lower() == username.lower():
        return chess.BLACK
    return chess.WHITE

def review_game(engine: Engine, game: chess.pgn.Game, colour: chess.Color, progress=None) -> dict:
    """Grade every move of `colour`. Returns {moves:[...], summary:{...}, key:[ply...]}."""
    old_depth = engine.__class__.__dict__  # unused; depth is module-level in engine, override per call below
    import engine as engine_mod
    saved = engine_mod.DEPTH; engine_mod.DEPTH = REVIEW_DEPTH
    board = game.board(); moves = []; ply = 0
    mainline = list(game.mainline_moves()); total = len(mainline)
    try:
        for mv in mainline:
            ply += 1
            san = board.san(mv)
            entry = {"ply": ply, "san": san, "from": chess.square_name(mv.from_square), "to": chess.square_name(mv.to_square),
                     "colour": "w" if board.turn else "b", "fen_before": board.fen()}
            if board.turn == colour:
                r: MoveReport = engine.review_move(board, mv)
                entry.update({"label": r.label, "loss": r.loss, "best_san": r.best_san, "best_line": r.best_line[:3],
                              "cp_before": r.cp_before, "cp_after": r.cp_after, "mate_in": r.mate_in,
                              "hanging": r.hanging_after, "captured": r.captured, "check": r.gives_check})
                if r.best_san:
                    try:
                        bm = board.parse_san(r.best_san); entry["best_from"] = chess.square_name(bm.from_square); entry["best_to"] = chess.square_name(bm.to_square)
                    except Exception: pass
            board.push(mv)
            entry["fen_after"] = board.fen()
            moves.append(entry)
            if progress: progress(ply, total)
    finally:
        engine_mod.DEPTH = saved
    graded = [m for m in moves if "label" in m]
    counts = {k: sum(1 for m in graded if m["label"] == k) for k in ("best", "good", "inaccuracy", "mistake", "blunder", "missed_mate")}
    avg_loss = sum(min(m["loss"], 300) for m in graded) / max(1, len(graded))
    accuracy = max(0, round(100 - avg_loss / 3))     # rough 0-100 feel, not chess.com's formula
    key = sorted([m["ply"] for m in graded if m["label"] in ("mistake", "blunder", "missed_mate")])
    best_moment = max(graded, key=lambda m: -m["loss"] if m["label"] == "best" and m.get("captured") else -999, default=None)
    return {"moves": moves, "key": key, "summary": {"graded": len(graded), "counts": counts, "accuracy": accuracy,
            "result": game.headers.get("Result", "*"), "white": game.headers.get("White", "?"), "black": game.headers.get("Black", "?"),
            "opening": game.headers.get("ECOUrl", game.headers.get("Opening", "")).rsplit("/", 1)[-1].replace("-", " "),
            "colour": "white" if colour else "black"}}

def summary_speech(s: dict, key: list[int]) -> str:
    c = s["counts"]; you = s["colour"]
    res = s["result"]
    outcome = "You won" if (res == "1-0") == (you == "white") and res in ("1-0", "0-1") else "You lost" if res in ("1-0", "0-1") else "It was a draw"
    parts = [f"{outcome} with {you}." + (f" The opening was the {s['opening']}." if s["opening"] else "")]
    parts.append(f"I graded {s['graded']} of your moves: {c['best']} best, {c['good']} good, {c['inaccuracy']} inaccuracies, "
                 f"{c['mistake']} mistakes and {c['blunder'] + c['missed_mate']} blunders.")
    if key:
        parts.append(f"Let's look at {'the key moment' if len(key) == 1 else f'the {len(key)} key moments'}. Press Next to step through, or click a move.")
    else:
        parts.append("No serious errors. Let's step through and I'll point out what worked.")
    return " ".join(parts)
