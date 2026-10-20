"""Small standalone Ludo-race and Bingo games for Alexa.

These lightweight games keep active rounds in memory; restarting the bot ends
unfinished rounds. They do not modify users' coin balances.
"""
import random

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()
_ludo_games = {}
_bingo_games = {}


def _ludo_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 Roll dice", callback_data=f"mini:ludo:{user_id}")
    ]])


def _bingo_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔢 Draw next number", callback_data=f"mini:bingo:{user_id}")
    ]])


def _bingo_text(game, heading="🎯 BINGO"):
    card = game["card"]
    called = game["called"]
    marked = set(called)
    rows = []
    for r, row in enumerate(card):
        cells = []
        for c, number in enumerate(row):
            if r == 2 and c == 2:
                cells.append(" ⭐ ")
            elif number in marked:
                cells.append(f"🟩{number:02d}")
            else:
                cells.append(f" {number:02d} ")
        rows.append("  ".join(cells))
    recent = ", ".join(map(str, called[-8:])) or "None yet"
    return (
        f"<b>{heading}</b>\n\n"
        + "\n".join(rows)
        + f"\n\nCalled numbers: <code>{recent}</code>\n"
        + f"Numbers drawn: {len(called)}"
    )


def _bingo_won(game):
    card, called = game["card"], set(game["called"])
    lines = []
    lines.extend(card)
    lines.extend([[card[r][c] for r in range(5)] for c in range(5)])
    lines.append([card[i][i] for i in range(5)])
    lines.append([card[i][4 - i] for i in range(5)])
    for line in lines:
        values = [n for n in line if n != 0]
        if all(n in called for n in values):
            return True
    return False


@router.message(Command("ludo"))
async def cmd_ludo(message: Message, **kwargs):
    user_id = message.from_user.id
    key = (message.chat.id, user_id)
    _ludo_games[key] = {"position": 0, "bot_position": 0, "turns": 0}
    await message.answer(
        "🎲 <b>LUDO RACE</b>\n\nReach square 30 before Alexa. "
        "Press Roll Dice each turn. First to reach or pass 30 wins!\n\n"
        "You: 0/30\nAlexa: 0/30",
        parse_mode="HTML",
        reply_markup=_ludo_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("mini:ludo:"))
async def ludo_roll(callback: CallbackQuery):
    try:
        owner_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid game button.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer("Start your own /ludo game to play.", show_alert=True)
        return
    key = (callback.message.chat.id, owner_id)
    game = _ludo_games.get(key)
    if not game:
        await callback.answer("This game has ended. Start again with /ludo.", show_alert=True)
        return

    player_roll = random.randint(1, 6)
    game["position"] = min(30, game["position"] + player_roll)
    if game["position"] >= 30:
        _ludo_games.pop(key, None)
        await callback.message.edit_text(
            f"🎲 <b>LUDO RACE — YOU WIN!</b>\n\nYou rolled {player_roll} and reached square 30 first! 🏆",
            parse_mode="HTML",
        )
        await callback.answer("You won!")
        return

    bot_roll = random.randint(1, 6)
    game["bot_position"] = min(30, game["bot_position"] + bot_roll)
    game["turns"] += 1
    if game["bot_position"] >= 30:
        _ludo_games.pop(key, None)
        await callback.message.edit_text(
            f"🎲 <b>LUDO RACE — ALEXA WINS!</b>\n\n"
            f"You rolled {player_roll}; Alexa rolled {bot_roll}.\n"
            f"Final: You {game['position']}/30 · Alexa 30/30",
            parse_mode="HTML",
        )
        await callback.answer("Alexa reached square 30.")
        return

    await callback.message.edit_text(
        f"🎲 <b>LUDO RACE</b>\n\n"
        f"You rolled: {player_roll} · Alexa rolled: {bot_roll}\n"
        f"You: <b>{game['position']}/30</b>\n"
        f"Alexa: <b>{game['bot_position']}/30</b>\n\n"
        "Roll again!",
        parse_mode="HTML",
        reply_markup=_ludo_keyboard(owner_id),
    )
    await callback.answer()


@router.message(Command("bingo"))
async def cmd_bingo(message: Message, **kwargs):
    user_id = message.from_user.id
    key = (message.chat.id, user_id)
    numbers = random.sample(range(1, 76), 25)
    card = [numbers[i:i + 5] for i in range(0, 25, 5)]
    card[2][2] = 0  # free center square
    _bingo_games[key] = {"card": card, "called": []}
    await message.answer(
        _bingo_text(_bingo_games[key]) + "\n\nPress the button to draw numbers.",
        parse_mode="HTML",
        reply_markup=_bingo_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("mini:bingo:"))
async def bingo_draw(callback: CallbackQuery):
    try:
        owner_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid game button.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer("Start your own /bingo game to play.", show_alert=True)
        return
    key = (callback.message.chat.id, owner_id)
    game = _bingo_games.get(key)
    if not game:
        await callback.answer("This game has ended. Start again with /bingo.", show_alert=True)
        return
    remaining = [n for n in range(1, 76) if n not in game["called"]]
    if not remaining:
        _bingo_games.pop(key, None)
        await callback.answer("All numbers have been drawn.", show_alert=True)
        return
    number = random.choice(remaining)
    game["called"].append(number)
    if _bingo_won(game):
        _bingo_games.pop(key, None)
        await callback.message.edit_text(
            _bingo_text(game, "🏆 BINGO — YOU WIN!") + f"\n\nWinning number: {number}",
            parse_mode="HTML",
        )
        await callback.answer(f"{number} — BINGO!")
        return
    await callback.message.edit_text(
        _bingo_text(game) + f"\n\nLast number: <b>{number}</b>",
        parse_mode="HTML",
        reply_markup=_bingo_keyboard(owner_id),
    )
    await callback.answer(f"Number {number}")
