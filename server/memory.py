"""Student memory: a small SQLite store so the coach remembers who it's teaching.
Tables: students, games, mistakes, lessons_done, questions. Everything is keyed by a student name the
student types once; there are no accounts yet (that's a later patch)."""
import os, re, json, time, sqlite3, threading, chess

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tutor.db"))
_lock = threading.Lock()

CATEGORIES = {
    "hanging_piece": "leaving pieces undefended",
    "missed_mate": "missing a forced checkmate",
    "back_rank": "back-rank weakness",
    "missed_tactic": "missing a tactic the opponent left open",
    "opening": "opening play (developing pieces, king safety)",
    "endgame": "endgame technique",
    "trade": "trading badly",
    "other": "positional slips",
}

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL, last_seen REAL, sessions INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS games (id INTEGER PRIMARY KEY, student INTEGER, source TEXT, game_id TEXT, played TEXT, colour TEXT,
            result TEXT, accuracy INTEGER, counts TEXT, reviewed REAL, UNIQUE(student, source, game_id));
        CREATE TABLE IF NOT EXISTS mistakes (id INTEGER PRIMARY KEY, student INTEGER, game INTEGER, ply INTEGER, san TEXT, label TEXT,
            category TEXT, best TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS lessons_done (id INTEGER PRIMARY KEY, student INTEGER, lesson TEXT, ts REAL, UNIQUE(student, lesson));
        CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY, student INTEGER, ts REAL, mode TEXT, question TEXT, answer TEXT, source TEXT);
        """)

def student(name: str) -> dict:
    name = re.sub(r"\s+", " ", name.strip())[:40]
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM students WHERE lower(name)=lower(?)", (name,)).fetchone()
        if row:
            c.execute("UPDATE students SET last_seen=?, sessions=sessions+1 WHERE id=?", (time.time(), row["id"]))
            return dict(row)
        c.execute("INSERT INTO students(name, created, last_seen, sessions) VALUES(?,?,?,1)", (name, time.time(), time.time()))
        return dict(c.execute("SELECT * FROM students WHERE name=?", (name,)).fetchone())

def categorise(m: dict, ply: int, total_plies: int, fen_before: str) -> str:
    """Rough but useful buckets from the review data for one graded move."""
    if m["label"] == "missed_mate": return "missed_mate"
    if m.get("hanging"): return "hanging_piece"
    board = chess.Board(fen_before)
    best = m.get("best_san", "")
    # opponent's reply on the back rank with check -> back-rank weakness
    line = m.get("best_line") or []
    if re.match(r"[RQ].?[a-h][18][+#]", best or "") or any(re.match(r"[RQ].?[a-h][18][+#]", x) for x in line[:2]):
        return "back_rank"
    if best and ("x" in best or "+" in best) and m["label"] in ("mistake", "blunder"):
        return "missed_tactic"
    if m.get("captured") and m["label"] in ("mistake", "blunder"):
        return "trade"
    pieces = len(board.piece_map())
    if ply <= 16: return "opening"
    if pieces <= 10: return "endgame"
    return "other"

def record_review(student_id: int, source: str, game_id: str, summary: dict, moves: list, key: list) -> dict:
    """Store a reviewed game and its mistakes. Returns {new: bool, categories: {cat: n}}."""
    cats = {}
    with _lock, _conn() as c:
        try:
            c.execute("INSERT INTO games(student, source, game_id, played, colour, result, accuracy, counts, reviewed) VALUES(?,?,?,?,?,?,?,?,?)",
                      (student_id, source, game_id, "", summary["colour"], summary["result"], summary["accuracy"], json.dumps(summary["counts"]), time.time()))
        except sqlite3.IntegrityError:
            row = c.execute("SELECT id FROM games WHERE student=? AND source=? AND game_id=?", (student_id, source, game_id)).fetchone()
            for r in c.execute("SELECT category FROM mistakes WHERE game=?", (row["id"],)): cats[r["category"]] = cats.get(r["category"], 0) + 1
            return {"new": False, "categories": cats}
        gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for m in moves:
            if "label" not in m or m["label"] not in ("mistake", "blunder", "missed_mate"): continue
            cat = categorise(m, m["ply"], len(moves), m["fen_before"])
            cats[cat] = cats.get(cat, 0) + 1
            c.execute("INSERT INTO mistakes(student, game, ply, san, label, category, best, ts) VALUES(?,?,?,?,?,?,?,?)",
                      (student_id, gid, m["ply"], m["san"], m["label"], cat, m.get("best_san", ""), time.time()))
    return {"new": True, "categories": cats}

def lesson_done(student_id: int, lesson: str):
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO lessons_done(student, lesson, ts) VALUES(?,?,?)", (student_id, lesson, time.time()))

def log_question(student_id: int | None, mode: str, question: str, answer: str, source: str):
    with _lock, _conn() as c:
        c.execute("INSERT INTO questions(student, ts, mode, question, answer, source) VALUES(?,?,?,?,?,?)",
                  (student_id, time.time(), mode, question, answer[:500], source))

def profile(student_id: int) -> dict:
    with _lock, _conn() as c:
        s = dict(c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone())
        games = [dict(r) for r in c.execute("SELECT * FROM games WHERE student=? ORDER BY reviewed DESC", (student_id,))]
        cats = {r["category"]: r["n"] for r in c.execute("SELECT category, COUNT(*) n FROM mistakes WHERE student=? GROUP BY category ORDER BY n DESC", (student_id,))}
        lessons = [r["lesson"] for r in c.execute("SELECT lesson FROM lessons_done WHERE student=?", (student_id,))]
        recent = [dict(r) for r in c.execute("SELECT category FROM mistakes WHERE student=? ORDER BY ts DESC LIMIT 6", (student_id,))]
    top = max(cats, key=cats.get) if cats else None
    return {"name": s["name"], "sessions": s["sessions"], "games": len(games), "accuracy": [g["accuracy"] for g in games[:5]],
            "categories": cats, "top_category": top, "top_label": CATEGORIES.get(top, ""), "lessons_done": lessons,
            "recent_categories": [r["category"] for r in recent]}

def greeting(p: dict, lesson_titles: dict) -> str:
    """What the coach says when a returning student sits down."""
    if p["sessions"] <= 1 and not p["games"]:
        return f"Nice to meet you, {p['name']}. I'll remember what we work on. Shall we start with a lesson, a game, or a review of one you've played?"
    parts = [f"Welcome back, {p['name']}."]
    if p["games"]:
        acc = p["accuracy"]
        trend = ""
        if len(acc) >= 2: trend = " and your accuracy is climbing" if acc[0] > acc[1] else " and your accuracy dipped last time" if acc[0] < acc[1] else ""
        parts.append(f"We've reviewed {p['games']} {'game' if p['games']==1 else 'games'}{trend}.")
    if p["top_category"]:
        n = p["categories"][p["top_category"]]
        parts.append(f"Your most common issue has been {p['top_label']}, {n} {'time' if n==1 else 'times'}. Let's check today's game for that.")
    todo = [t for k, t in lesson_titles.items() if k not in p["lessons_done"]]
    if todo and not p["top_category"]:
        parts.append(f"We haven't done the {todo[0].lower()} lesson yet, if you'd like to.")
    return " ".join(parts)

def review_remark(p: dict, cats: dict) -> str:
    """What the coach adds after a review, using history."""
    if not cats: return "No serious errors this time. That's real progress."
    top = max(cats, key=cats.get)
    total = p["categories"].get(top, 0)
    label = CATEGORIES.get(top, top)
    if total > cats[top]:
        return f"I notice {label} again; that's {total} times across your games now. It's the one thing worth drilling."
    return f"The main theme today was {label}. I'll keep an eye on it next time."

def export_questions_csv() -> str:
    import csv, io
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["time", "student", "mode", "question", "answer", "source"])
    with _lock, _conn() as c:
        for r in c.execute("SELECT q.ts, s.name, q.mode, q.question, q.answer, q.source FROM questions q LEFT JOIN students s ON s.id=q.student ORDER BY q.ts"):
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(r[0])), r[1] or "", r[2], r[3], r[4], r[5]])
    return out.getvalue()
