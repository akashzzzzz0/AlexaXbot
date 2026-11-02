import random
import json
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, func, case
from bot.database.models import GameMatch, GameStats, DuelStats, Duel
from bot.services.economy_service import add_coins, deduct_coins
from bot.services.user_service import add_xp, get_or_create_game_stats, get_or_create_duel_stats
import structlog

logger = structlog.get_logger()

XP_PER_WIN = 25
XP_PER_GAME = 5

async def _increment_game_stats(session: AsyncSession, user_id: int, **deltas: int) -> None:
    """Apply game counters in SQL so concurrent games cannot lose increments."""
    await get_or_create_game_stats(session, user_id)
    allowed = {
        "total_games", "total_wins", "total_losses", "total_coins_bet",
        "total_coins_won", "dice_wins", "wheel_wins", "rps_wins",
        "quiz_wins", "mines_wins", "card_wins",
    }
    values = {}
    for field, amount in deltas.items():
        if field not in allowed:
            raise ValueError(f"Unsupported game stat: {field}")
        if amount:
            column = getattr(GameStats, field)
            values[field] = func.coalesce(column, 0) + amount
    if values:
        await session.execute(
            update(GameStats)
            .where(GameStats.user_id == user_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


async def _record_duel_result(
    session: AsyncSession, winner_id: int, loser_id: int, wager: int
) -> None:
    """Atomically update both players' duel counters and streaks."""
    winner = await get_or_create_duel_stats(session, winner_id)
    loser = await get_or_create_duel_stats(session, loser_id)
    next_streak = func.coalesce(DuelStats.win_streak, 0) + 1
    await session.execute(
        update(DuelStats)
        .where(DuelStats.user_id == winner_id)
        .values(
            wins=func.coalesce(DuelStats.wins, 0) + 1,
            win_streak=next_streak,
            best_streak=case(
                (next_streak > func.coalesce(DuelStats.best_streak, 0), next_streak),
                else_=func.coalesce(DuelStats.best_streak, 0),
            ),
            coins_won=func.coalesce(DuelStats.coins_won, 0) + wager,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(DuelStats)
        .where(DuelStats.user_id == loser_id)
        .values(
            losses=func.coalesce(DuelStats.losses, 0) + 1,
            win_streak=0,
            coins_lost=func.coalesce(DuelStats.coins_lost, 0) + wager,
        )
        .execution_options(synchronize_session=False)
    )


WHEEL_SEGMENTS = [
    (2, "2x", 0.25),
    (0, "LOSE", 0.30),
    (1.5, "1.5x", 0.20),
    (3, "3x", 0.15),
    (0.5, "0.5x", 0.05),
    (5, "5x", 0.03),
    (10, "10x", 0.01),
    (0.75, "0.75x", 0.01),
]


async def play_classic_dice(
    session: AsyncSession,
    user_id: int,
    wager: int = 0,
    opponent_id: Optional[int] = None,
) -> dict:
    player_roll = random.randint(1, 6)
    opponent_roll = random.randint(1, 6) if opponent_id else random.randint(1, 6)

    if player_roll > opponent_roll:
        result = "win"
    elif player_roll < opponent_roll:
        result = "lose"
    else:
        result = "draw"

    winnings = 0
    if wager > 0:
        success, _ = await deduct_coins(session, user_id, wager, "game_bet", "Dice bet")
        if not success:
            return {"error": "insufficient_funds"}

        if result == "win":
            win_amount = int(wager * 1.9)
            await add_coins(session, user_id, win_amount, "game_win", "Dice win")
            winnings = win_amount - wager
        elif result == "draw":
            await add_coins(session, user_id, wager, "game_draw", "Dice draw refund")

    deltas = {"total_games": 1, "total_coins_bet": wager}
    if result == "win":
        deltas.update(total_wins=1, dice_wins=1, total_coins_won=max(0, winnings))
        await add_xp(session, user_id, XP_PER_WIN)
    elif result == "lose":
        deltas["total_losses"] = 1
    await _increment_game_stats(session, user_id, **deltas)
    await add_xp(session, user_id, XP_PER_GAME)

    return {
        "result": result,
        "player_roll": player_roll,
        "opponent_roll": opponent_roll,
        "winnings": winnings,
    }


async def spin_wheel(
    session: AsyncSession,
    user_id: int,
    wager: int = 0,
) -> dict:
    if wager > 0:
        success, _ = await deduct_coins(session, user_id, wager, "game_bet", "Wheel spin bet")
        if not success:
            return {"error": "insufficient_funds"}

    rand = random.random()
    cumulative = 0.0
    chosen_multiplier = 0
    chosen_label = "LOSE"
    for multiplier, label, probability in WHEEL_SEGMENTS:
        cumulative += probability
        if rand <= cumulative:
            chosen_multiplier = multiplier
            chosen_label = label
            break

    winnings = 0
    if wager > 0 and chosen_multiplier > 0:
        win_amount = int(wager * chosen_multiplier)
        await add_coins(session, user_id, win_amount, "game_win", "Wheel win")
        winnings = win_amount - wager

    await _increment_game_stats(
        session, user_id, total_games=1,
        wheel_wins=1 if chosen_multiplier > 1 else 0,
    )
    await add_xp(session, user_id, XP_PER_GAME)

    return {"multiplier": chosen_multiplier, "label": chosen_label, "winnings": winnings}


async def play_rps(
    session: AsyncSession,
    user_id: int,
    player_choice: str,
    wager: int = 0,
) -> dict:
    choices = ["rock", "paper", "scissors"]
    bot_choice = random.choice(choices)

    wins = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if player_choice == bot_choice:
        result = "draw"
    elif wins.get(player_choice) == bot_choice:
        result = "win"
    else:
        result = "lose"

    winnings = 0
    if wager > 0:
        success, _ = await deduct_coins(session, user_id, wager, "game_bet", "RPS bet")
        if not success:
            return {"error": "insufficient_funds"}
        if result == "win":
            win_amount = int(wager * 1.9)
            await add_coins(session, user_id, win_amount, "game_win", "RPS win")
            winnings = win_amount - wager
        elif result == "draw":
            await add_coins(session, user_id, wager, "game_draw", "RPS draw refund")

    deltas = {"total_games": 1, "rps_wins": 1 if result == "win" else 0}
    if result == "win":
        deltas["total_wins"] = 1
        await add_xp(session, user_id, XP_PER_WIN)
    elif result == "lose":
        deltas["total_losses"] = 1
    await _increment_game_stats(session, user_id, **deltas)
    await add_xp(session, user_id, XP_PER_GAME)

    return {
        "result": result,
        "player_choice": player_choice,
        "bot_choice": bot_choice,
        "winnings": winnings,
    }


async def create_duel(
    session: AsyncSession,
    group_id: int,
    challenger_id: int,
    opponent_id: Optional[int],
    wager: int,
    game_type: str,
    message_id: Optional[int] = None,
) -> Optional[Duel]:
    if wager > 0:
        success, _ = await deduct_coins(session, challenger_id, wager, "duel_bet", "Duel wager")
        if not success:
            return None

    duel = Duel(
        group_id=group_id,
        challenger_id=challenger_id,
        opponent_id=opponent_id,
        wager=wager,
        game_type=game_type,
        status="pending",
        message_id=message_id,
    )
    session.add(duel)
    await session.flush()
    return duel


async def accept_duel(
    session: AsyncSession,
    duel_id: int,
    opponent_id: int,
) -> Tuple[Optional[Duel], dict]:
    duel = await session.get(Duel, duel_id)
    if not duel or duel.status != "pending":
        return None, {}

    # Claim the pending duel atomically before touching either wallet. Only one
    # concurrent Accept callback can change pending -> accepting.
    previous_opponent_id = duel.opponent_id
    claim = await session.execute(
        update(Duel)
        .where(Duel.id == duel_id, Duel.status == "pending")
        .values(status="accepting", opponent_id=opponent_id)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        return None, {}

    await session.refresh(duel)
    if duel.wager > 0:
        success, _ = await deduct_coins(session, opponent_id, duel.wager, "duel_bet", "Duel wager")
        if not success:
            await session.execute(
                update(Duel)
                .where(Duel.id == duel_id, Duel.status == "accepting", Duel.opponent_id == opponent_id)
                .values(status="pending", opponent_id=previous_opponent_id)
                .execution_options(synchronize_session=False)
            )
            await session.refresh(duel)
            return duel, {"error": "insufficient_funds"}

    duel.status = "active"
    result = await _play_duel(session, duel)
    return duel, result


async def _play_duel(session: AsyncSession, duel: Duel) -> dict:
    game = duel.game_type

    if game in ("dice", "classic_dice"):
        c_score = random.randint(1, 6)
        o_score = random.randint(1, 6)
    elif game == "rps":
        choices = [1, 2, 3]
        c_score = random.choice(choices)
        o_score = random.choice(choices)
    else:
        c_score = random.randint(1, 100)
        o_score = random.randint(1, 100)

    duel.challenger_score = c_score
    duel.opponent_score = o_score

    if c_score > o_score:
        winner_id = duel.challenger_id
        loser_id = duel.opponent_id
    elif o_score > c_score:
        winner_id = duel.opponent_id
        loser_id = duel.challenger_id
    else:
        duel.status = "completed"
        duel.winner_id = None
        duel.completed_at = datetime.now(timezone.utc)
        if duel.wager > 0:
            await add_coins(session, duel.challenger_id, duel.wager, "duel_draw", "Duel draw refund")
            await add_coins(session, duel.opponent_id, duel.wager, "duel_draw", "Duel draw refund")
        return {"result": "draw", "challenger_score": c_score, "opponent_score": o_score}

    duel.winner_id = winner_id
    duel.status = "completed"
    duel.completed_at = datetime.now(timezone.utc)

    if duel.wager > 0:
        prize = duel.wager * 2
        await add_coins(session, winner_id, prize, "duel_win", "Duel prize")

    await _record_duel_result(session, winner_id, loser_id, duel.wager)

    await add_xp(session, winner_id, XP_PER_WIN * 2)
    await add_xp(session, loser_id, XP_PER_GAME)

    return {
        "result": "win",
        "winner_id": winner_id,
        "loser_id": loser_id,
        "challenger_score": c_score,
        "opponent_score": o_score,
        "prize": duel.wager * 2 if duel.wager > 0 else 0,
    }


async def play_mines(
    session: AsyncSession,
    user_id: int,
    row: int,
    col: int,
    match_id: int,
    wager: int = 0,
) -> dict:
    match = await session.get(GameMatch, match_id)
    if not match or match.status != "active":
        return {"error": "match_not_found"}

    data = match.data or {}
    board = data.get("board", [])
    revealed = data.get("revealed", [])
    mines = data.get("mines", [])
    multiplier = data.get("multiplier", 1.0)

    cell_idx = row * 5 + col
    if cell_idx in revealed:
        return {"error": "already_revealed"}

    revealed.append(cell_idx)
    is_mine = cell_idx in mines

    if is_mine:
        match.status = "completed"
        data["revealed"] = revealed
        data["result"] = "mine"
        match.data = data
        return {"result": "mine", "row": row, "col": col}

    safe_count = len(revealed)
    multiplier = round(1 + (safe_count * 0.2), 2)
    data["revealed"] = revealed
    data["multiplier"] = multiplier
    match.data = data

    return {
        "result": "safe",
        "row": row,
        "col": col,
        "multiplier": multiplier,
        "safe_count": safe_count,
    }


def create_mines_board(rows: int = 5, cols: int = 5, mine_count: int = 5) -> dict:
    total = rows * cols
    mines = random.sample(range(total), mine_count)
    return {"board": [[None] * cols for _ in range(rows)], "mines": mines, "revealed": [], "multiplier": 1.0}
