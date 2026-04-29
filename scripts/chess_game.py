"""Live profile chess game.

Reads ``chess/state.json``, applies a player move (UCI or SAN), plays a bot
reply, and writes:

* ``chess/state.json`` – updated FEN, move history, status
* ``chess/board.svg`` – animated SVG showing the current position with the
  bot's last move sliding into place (loops automatically)
* ``chess/history.md`` – human-readable move log appended to
* ``chess/comment.md`` – a markdown comment for the workflow to post on the
  triggering issue

Usage::

    python scripts/chess_game.py --move e2e4 --player @octocat
    python scripts/chess_game.py --new-game

The script is deterministic given its inputs except for the bot's choice of
move, which uses ``random``.  A seed can be supplied with ``--seed`` for
reproducible tests.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Optional

import chess
import chess.svg

REPO_ROOT = Path(__file__).resolve().parent.parent
CHESS_DIR = REPO_ROOT / "chess"
STATE_PATH = CHESS_DIR / "state.json"
BOARD_SVG_PATH = CHESS_DIR / "board.svg"
HISTORY_PATH = CHESS_DIR / "history.md"
COMMENT_PATH = CHESS_DIR / "comment.md"

# ---------------------------------------------------------------------------
# Geometry — must match python-chess's default svg.board layout (size auto).
# python-chess renders an inner 8x8 grid of 45px squares with a 15px margin
# for the coordinate ring.
# ---------------------------------------------------------------------------
SQUARE_SIZE = 45
MARGIN = 15


def square_xy(square: int) -> tuple[int, int]:
    """Return the top-left (x, y) pixel of ``square`` in the rendered SVG.

    Uses the white-orientation that ``chess.svg.board`` defaults to.
    """
    file_idx = chess.square_file(square)
    rank_idx = chess.square_rank(square)
    x = MARGIN + file_idx * SQUARE_SIZE
    y = MARGIN + (7 - rank_idx) * SQUARE_SIZE
    return x, y


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def _new_state() -> dict:
    return {
        "fen": chess.STARTING_FEN,
        "moves": [],
        "ply": 0,
        "status": "in_progress",
        "result": None,
        "last_player_move": None,
        "last_bot_move": None,
        "last_player": None,
        "updated_at": None,
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return _new_state()


def save_state(state: dict) -> None:
    CHESS_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    )
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Move parsing
# ---------------------------------------------------------------------------
_MOVE_CLEAN_RE = re.compile(r"[\s\-_>]+")


def parse_move(board: chess.Board, raw: str) -> chess.Move:
    """Parse ``raw`` as either UCI or SAN, returning a legal move.

    Accepts forms like ``e2e4``, ``e2-e4``, ``e2 e4``, ``Nf3``, ``O-O``,
    ``e8=Q``.  Raises ``ValueError`` if the move is illegal or unparseable.
    """
    if not raw or not raw.strip():
        raise ValueError("Empty move.")

    candidate = _MOVE_CLEAN_RE.sub("", raw.strip())

    # Try UCI first (cheapest, strictest).
    try:
        move = chess.Move.from_uci(candidate.lower())
        if move in board.legal_moves:
            return move
    except ValueError:
        pass

    # Fall back to SAN.  Try the original, the cleaned form, and a couple of
    # common cosmetic variants so users don't get tripped up by formatting.
    san_candidates = [raw.strip(), candidate, candidate.replace("0", "O")]
    seen: set[str] = set()
    for san in san_candidates:
        if san in seen:
            continue
        seen.add(san)
        try:
            return board.parse_san(san)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError,
                chess.AmbiguousMoveError):
            continue

    raise ValueError(
        f"Could not parse `{raw}` as a legal move. Use UCI like `e2e4` "
        "or SAN like `Nf3`."
    )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
# Material values used for a tiny one-ply heuristic: prefer winning the most
# material, prefer giving check, and always take a forced mate when available.
_PIECE_VALUE = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}


def _move_score(board: chess.Board, move: chess.Move) -> float:
    score = 0.0
    if board.is_capture(move):
        captured_piece_type: Optional[int]
        if board.is_en_passant(move):
            captured_piece_type = chess.PAWN
        else:
            captured = board.piece_at(move.to_square)
            captured_piece_type = captured.piece_type if captured else None
        if captured_piece_type is not None:
            score += _PIECE_VALUE[captured_piece_type] * 10
    if move.promotion:
        score += _PIECE_VALUE.get(move.promotion, 0) * 5
    if board.gives_check(move):
        score += 1.5
    # Slight randomness so the bot isn't perfectly predictable.
    score += random.random()
    return score


def choose_bot_move(board: chess.Board) -> chess.Move:
    legal = list(board.legal_moves)
    if not legal:
        raise RuntimeError("Bot has no legal moves.")

    # 1. Always play a forced mate when one exists.
    for move in legal:
        board.push(move)
        try:
            if board.is_checkmate():
                return move
        finally:
            board.pop()

    # 2. Otherwise pick the highest-scoring move.
    return max(legal, key=lambda m: _move_score(board, m))


# ---------------------------------------------------------------------------
# Animated SVG rendering
# ---------------------------------------------------------------------------
ANIMATION_CSS = """
<style>
.board-frame { animation: glow 6s ease-in-out infinite; }
@keyframes glow {
  0%, 100% { filter: drop-shadow(0 0 6px rgba(0, 240, 255, 0.35)); }
  50%      { filter: drop-shadow(0 0 14px rgba(255, 46, 170, 0.55)); }
}
</style>
""".strip()


def _animate_use_tag(
    svg: str,
    from_sq: int,
    to_sq: int,
    begin: str = "0.4s",
    dur: str = "1.1s",
) -> str:
    """Wrap the ``<use>`` element at ``to_sq`` so the piece slides from
    ``from_sq`` to ``to_sq`` and then loops the cycle."""
    fx, fy = square_xy(from_sq)
    tx, ty = square_xy(to_sq)

    target = f'transform="translate({tx}, {ty})"'
    needle = f'<use href='
    # Find every <use ... /> tag and rewrite the first one whose transform
    # matches the destination square.
    idx = 0
    while True:
        start = svg.find(needle, idx)
        if start == -1:
            return svg  # nothing to animate; bail out gracefully
        end = svg.find("/>", start)
        if end == -1:
            return svg
        tag = svg[start:end + 2]
        if target in tag:
            # Convert "<use ... />" into "<use ...><animateTransform .../></use>".
            # Slide values: hold at origin briefly, slide, hold at destination,
            # repeat.  ``calcMode="spline"`` plus a smooth bezier gives the
            # piece a satisfying ease-out feel.
            open_tag = tag[:-2].rstrip() + ">"
            anim = (
                '<animateTransform attributeName="transform" type="translate" '
                f'dur="6s" repeatCount="indefinite" calcMode="spline" '
                f'keyTimes="0;0.07;0.25;1" '
                f'keySplines="0.42 0 0.58 1; 0.25 0.1 0.25 1; 0 0 1 1" '
                f'values="{fx},{fy}; {fx},{fy}; {tx},{ty}; {tx},{ty}" />'
            )
            replacement = f"{open_tag}{anim}</use>"
            return svg[:start] + replacement + svg[end + 2:]
        idx = end + 2


def render_board_svg(
    board: chess.Board,
    player_move: Optional[chess.Move],
    bot_move: Optional[chess.Move],
) -> str:
    """Render an animated SVG showing the position after both moves.

    The user's move is highlighted with the standard "lastmove" colouring and
    the bot's reply is animated sliding from its origin square."""
    highlight = bot_move or player_move
    svg = chess.svg.board(
        board,
        lastmove=highlight,
        check=board.king(board.turn) if board.is_check() else None,
        size=420,
        coordinates=True,
    )

    if bot_move is not None:
        svg = _animate_use_tag(svg, bot_move.from_square, bot_move.to_square)

    # Inject a subtle glow + class so the board feels alive in the README.
    svg = svg.replace("<svg ", '<svg class="board-frame" ', 1)
    svg = svg.replace("</defs>", f"</defs>{ANIMATION_CSS}", 1)
    return svg


def write_board_svg(svg: str) -> None:
    CHESS_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_SVG_PATH.write_text(svg, encoding="utf-8")


# ---------------------------------------------------------------------------
# History + comment formatting
# ---------------------------------------------------------------------------
def _move_pair_md(state: dict) -> str:
    rows = []
    moves = state["moves"]
    for i in range(0, len(moves), 2):
        white = moves[i]
        black = moves[i + 1] if i + 1 < len(moves) else None
        rows.append(
            f"| {i // 2 + 1} | `{white['san']}` ({white.get('player') or 'bot'}) | "
            f"{('`' + black['san'] + '` (' + (black.get('player') or 'bot') + ')') if black else '—'} |"
        )
    if not rows:
        return "_No moves yet — be the first!_"
    header = "| # | White | Black |\n|--:|:------|:------|"
    return header + "\n" + "\n".join(rows[-12:])


def write_history(state: dict) -> None:
    CHESS_DIR.mkdir(parents=True, exist_ok=True)
    body = (
        "# ♟️ Profile Chess — Move History\n\n"
        f"_Updated: {state.get('updated_at') or 'n/a'}_  \n"
        f"_Status: **{state.get('status')}**"
        + (f" — {state.get('result')}" if state.get("result") else "")
        + f" · ply {state.get('ply', 0)}_\n\n"
        + _move_pair_md(state)
        + "\n"
    )
    HISTORY_PATH.write_text(body, encoding="utf-8")


def write_comment(message: str) -> None:
    CHESS_DIR.mkdir(parents=True, exist_ok=True)
    COMMENT_PATH.write_text(message.rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Game flow
# ---------------------------------------------------------------------------
def _game_over_state(board: chess.Board) -> tuple[str, Optional[str]]:
    if board.is_checkmate():
        winner = "White" if board.turn == chess.BLACK else "Black"
        return "finished", f"Checkmate — {winner} wins"
    if board.is_stalemate():
        return "finished", "Stalemate — draw"
    if board.is_insufficient_material():
        return "finished", "Draw — insufficient material"
    if board.is_seventyfive_moves():
        return "finished", "Draw — 75-move rule"
    if board.is_fivefold_repetition():
        return "finished", "Draw — fivefold repetition"
    if board.can_claim_draw():
        return "in_progress", None  # draw available but not auto-claimed
    return "in_progress", None


def reset_game() -> dict:
    state = _new_state()
    save_state(state)
    board = chess.Board()
    write_board_svg(render_board_svg(board, None, None))
    write_history(state)
    write_comment(
        "🆕 **A new game has begun!** White to move. "
        "[Make the first move →](../../issues/new?template=chess-move.yml)"
    )
    return state


def play_move(raw_move: str, player: Optional[str]) -> dict:
    state = load_state()
    if state.get("status") == "finished":
        write_comment(
            "🏁 The current game is **already over**"
            + (f" ({state.get('result')})" if state.get("result") else "")
            + ". A maintainer can start a new one with "
            "`python scripts/chess_game.py --new-game`."
        )
        return state

    board = chess.Board(state["fen"])

    try:
        player_move = parse_move(board, raw_move)
    except ValueError as exc:
        side = "White" if board.turn == chess.WHITE else "Black"
        write_comment(
            f"❌ **Move rejected:** {exc}\n\n"
            "Tips:\n"
            "* Use UCI like `e2e4`, `g1f3`, or `e7e8q` (promotion).\n"
            "* SAN like `Nf3`, `O-O`, `exd5` also works.\n"
            f"* It is **{side}**'s turn."
        )
        state["status"] = state.get("status", "in_progress")
        return state

    player_san = board.san(player_move)
    player_uci = player_move.uci()
    board.push(player_move)
    state["moves"].append({
        "san": player_san,
        "uci": player_uci,
        "player": player or "anonymous",
        "kind": "human",
    })
    state["ply"] = board.ply()
    state["last_player_move"] = player_uci
    state["last_player"] = player

    bot_move: Optional[chess.Move] = None
    bot_san: Optional[str] = None
    status, result = _game_over_state(board)
    if status == "in_progress":
        bot_move = choose_bot_move(board)
        bot_san = board.san(bot_move)
        board.push(bot_move)
        state["moves"].append({
            "san": bot_san,
            "uci": bot_move.uci(),
            "player": "bot",
            "kind": "bot",
        })
        state["ply"] = board.ply()
        state["last_bot_move"] = bot_move.uci()
        status, result = _game_over_state(board)

    state["fen"] = board.fen()
    state["status"] = status
    state["result"] = result
    save_state(state)

    write_board_svg(render_board_svg(board, player_move, bot_move))
    write_history(state)

    # Build the issue comment.
    lines = [
        f"✅ **{player or 'You'}** played **`{player_san}`** "
        f"(`{player_uci}`).",
    ]
    if bot_move is not None and bot_san is not None:
        lines.append(
            f"🤖 The bot replies with **`{bot_san}`** (`{bot_move.uci()}`)."
        )
    if board.is_check() and status == "in_progress":
        lines.append("⚠️ **Check!**")
    if status == "finished":
        lines.append(f"🏁 **Game over — {result}.**")
        lines.append(
            "A maintainer can begin a new game by running "
            "`python scripts/chess_game.py --new-game` or re-running the "
            "workflow with the `new-game` input."
        )
    else:
        lines.append(
            "It's **White**'s turn again — "
            "[play the next move →](../../issues/new?template=chess-move.yml)"
        )
    lines.append("")
    lines.append(
        "View the live, animated board on the "
        "[profile README](../../#-live-profile-chess)."
    )
    write_comment("\n".join(lines))
    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Profile chess driver")
    parser.add_argument("--move", help="Player move (UCI or SAN)")
    parser.add_argument("--player", help="GitHub @handle of the player")
    parser.add_argument(
        "--new-game", action="store_true",
        help="Reset the board to the starting position",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Seed the bot's RNG for reproducible play (testing)",
    )
    args = parser.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)
    else:
        # Mix in the current ply so bot replies vary across games even if the
        # workflow runner uses the same default seed.
        random.seed(os.urandom(16))

    if args.new_game:
        reset_game()
        return 0

    if not args.move:
        parser.error("--move is required unless --new-game is given")

    play_move(args.move, args.player)
    return 0


if __name__ == "__main__":
    sys.exit(main())
