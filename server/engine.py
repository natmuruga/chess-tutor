"""Stockfish wrapper: the source of truth for everything chess-related.
The LLM never evaluates positions; it only explains what this module reports."""
import os, shutil, chess, chess.engine
from dataclasses import dataclass, field

STOCKFISH = os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish") or "/usr/games/stockfish"
DEPTH = int(os.environ.get("ENGINE_DEPTH", "14"))

@dataclass
class MoveReport:
    san: str
    uci: str
    cp_before: int            # eval (centipawns) from the mover's perspective, before the move
    cp_after: int             # eval after the move, mover's perspective
    loss: int                 # what the move cost the mover, centipawns (>=0)
    best_san: str
    best_line: list[str]
    label: str                # best | good | inaccuracy | mistake | blunder
    mate_in: int | None = None
    gives_check: bool = False
    is_capture: bool = False
    captured: str | None = None
    hanging_after: list[str] = field(default_factory=list)   # mover's pieces left en prise

class Engine:
    def __init__(self):
        self.engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH)

    def close(self):
        self.engine.quit()

    def set_skill(self, level: int):
        """0-20. Stockfish 'Skill Level' makes it play weaker for students."""
        self.engine.configure({"Skill Level": max(0, min(20, level))})

    def _score(self, info, pov: chess.Color) -> tuple[int, int | None]:
        s = info["score"].pov(pov)
        if s.is_mate():
            m = s.mate()
            return (100000 if m > 0 else -100000), m
        return s.score(), None

    def analyse(self, board: chess.Board):
        return self.engine.analyse(board, chess.engine.Limit(depth=DEPTH))

    def best_move(self, board: chess.Board):
        info = self.analyse(board)
        pv = info.get("pv", [])
        return (pv[0] if pv else None), info

    def play(self, board: chess.Board, level: int = 8) -> chess.Move:
        self.set_skill(level)
        res = self.engine.play(board, chess.engine.Limit(depth=max(4, min(DEPTH, 8 + level // 2))))
        self.set_skill(20)
        return res.move

    def hanging_pieces(self, board: chess.Board, color: chess.Color) -> list[str]:
        out = []
        for sq, piece in board.piece_map().items():
            if piece.color != color or piece.piece_type == chess.KING:
                continue
            if board.attackers(not color, sq) and not board.attackers(color, sq):
                out.append(f"{chess.piece_name(piece.piece_type)} on {chess.square_name(sq)}")
        return out

    def review_move(self, board: chess.Board, move: chess.Move) -> MoveReport:
        """Evaluate the position before and after `move` (must be legal in `board`)."""
        mover = board.turn
        best, info_before = self.best_move(board)
        cp_before, _ = self._score(info_before, mover)
        best_line, b2 = [], board.copy()
        for m in info_before.get("pv", [])[:5]:
            best_line.append(b2.san(m)); b2.push(m)
        san = board.san(move)
        gives_check = board.gives_check(move)
        is_capture = board.is_capture(move)
        captured = None
        if is_capture:
            cap_sq = move.to_square if not board.is_en_passant(move) else (move.to_square + (-8 if mover else 8))
            p = board.piece_at(cap_sq)
            captured = chess.piece_name(p.piece_type) if p else "pawn"
        after = board.copy(); after.push(move)
        if after.is_checkmate():
            cp_after, mate_in = 100000, 0
        else:
            cp_after, mate_in = self._score(self.analyse(after), mover)
        loss = max(0, cp_before - cp_after)
        if best and move == best: label = "best"
        elif cp_before >= 100000 and mate_in is None: label = "missed_mate"
        elif loss <= 40: label = "good"
        elif loss <= 90: label = "inaccuracy"
        elif loss <= 200: label = "mistake"
        else: label = "blunder"
        return MoveReport(san=san, uci=move.uci(), cp_before=cp_before, cp_after=cp_after, loss=loss,
                          best_san=board.san(best) if best else "", best_line=best_line, label=label,
                          mate_in=mate_in, gives_check=gives_check, is_capture=is_capture, captured=captured,
                          hanging_after=self.hanging_pieces(after, mover))
