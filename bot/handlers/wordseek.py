"""WordSeek multiplayer word guessing game for Alexa."""
import random
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Economy
from bot.services.economy_service import add_coins

router = Router()

# Small built-in, common five-letter dictionary; add words here to expand the game.
WORDS = [
    "CRANE", "PLANT", "BRICK", "SHARE", "SLATE", "TRACE", "CLOUD", "GRAPE",
    "BRAIN", "LIGHT", "STONE", "HOUSE", "WATER", "MONEY", "NIGHT", "SMILE",
    "TRAIN", "WORLD", "HEART", "SOUND", "CHAIR", "SHEEP", "APPLE", "BEACH",
    "MOUSE", "RIVER", "DREAM", "GREEN", "BLACK", "WHITE", "SWEET", "QUICK",
    "ZEBRA", "TIGER", "PLANE", "FLAME", "SPOON", "CROWN", "ROUND", "SHINE",
    "BRAVE", "STORM", "EARTH", "OCEAN", "MUSIC", "ROBOT", "PARTY", "MAGIC",
    "BREAD", "FRUIT", "LEMON", "PEACH", "SUGAR", "HONEY", "CANDY", "PIZZA",
]
MAX_ATTEMPTS = 30
WIN_COINS = 100
FIRST_BONUS = 50
PARTICIPATION_XP = 2
WIN_XP = 10

# Active round state is per chat. Scores/rewards are stored in the existing economy DB.
games = {}


def join_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Join Game", callback_data=f"ws:join:{chat_id}"),
        InlineKeyboardButton(text="📊 Players", callback_data=f"ws:players:{chat_id}"),
    ]])


def render_guess(guess: str, target: str) -> str:
    # Duplicate-letter handling follows Wordle-style two-pass matching.
    marks = ["🟥"] * 5
    remaining = {}
    for ch in target:
        remaining[ch] = remaining.get(ch, 0) + 1
    for i, ch in enumerate(guess):
        if ch == target[i]:
            marks[i] = "🟩"
            remaining[ch] -= 1
    for i, ch in enumerate(guess):
        if marks[i] != "🟩" and remaining.get(ch, 0) > 0:
            marks[i] = "🟨"
            remaining[ch] -= 1
    return "".join(marks)


async def add_xp(session: AsyncSession, user_id: int, amount: int) -> None:
    row = await session.get(Economy, user_id)
    if row is None:
        row = Economy(user_id=user_id)
        session.add(row)
        await session.flush()
    row.total_xp = (row.total_xp or 0) + amount


@router.message(Command("wordseek"))
async def wordseek_help(message: Message, **kwargs):
    await message.reply(
        "🔤 <b>Alexa WordSeek</b>\n"
        "/new — start a group round\n"
        "/join — join the current round\n"
        "/wscores — see this group's WordSeek leaderboard\n\n"
        "Guess a five-letter English word. 🟩 correct letter and position, "
        "🟨 letter exists but is in another position, 🟥 letter is not needed. "
        "Only joined players' guesses count.",
        parse_mode="HTML",
    )


@router.message(Command("new"))
async def wordseek_new(message: Message, **kwargs):
    if not message.chat or message.chat.type == "private":
        await message.reply("WordSeek group rounds are played in a group.")
        return
    chat_id = message.chat.id
    games[chat_id] = {
        "target": random.choice(WORDS),
        "joined": {},
        "attempts": 0,
        "solved": set(),
        "winner": None,
        "round_over": False,
        "scores": {},
    }
    await message.reply(
        "🔤 <b>WordSeek started!</b>\n"
        "Guess the 5-letter word. Join first, then send your guesses as normal messages.\n"
        f"Attempts available: {MAX_ATTEMPTS}\n"
        "🟩 right place · 🟨 in the word, wrong place · 🟥 not in the word",
        reply_markup=join_keyboard(chat_id),
        parse_mode="HTML",
    )


@router.message(Command("join"))
async def wordseek_join_command(message: Message, **kwargs):
    if not message.chat or message.chat.id not in games or games[message.chat.id]["round_over"]:
        await message.reply("No active WordSeek round. Use /new to start one.")
        return
    if not message.from_user:
        return
    games[message.chat.id]["joined"][message.from_user.id] = message.from_user.full_name
    await message.reply(f"✅ {escape(message.from_user.full_name)} joined WordSeek. Send a 5-letter guess!")


@router.callback_query(F.data.startswith("ws:"))
async def wordseek_buttons(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Invalid game button.", show_alert=True)
        return
    action, chat_raw = parts[1], parts[2]
    try:
        chat_id = int(chat_raw)
    except ValueError:
        await callback.answer("Invalid game.", show_alert=True)
        return
    game = games.get(chat_id)
    if not game or game["round_over"]:
        await callback.answer("This round has ended. Start another with /new.", show_alert=True)
        return
    user = callback.from_user
    if action == "join":
        game["joined"][user.id] = user.full_name
        await callback.answer("You joined WordSeek!")
        if callback.message:
            await callback.message.reply(f"🎮 {escape(user.full_name)} joined! Total players: {len(game['joined'])}")
    elif action == "players":
        names = list(game["joined"].values())
        listing = "\n".join(f"• {escape(name)}" for name in names) if names else "No players yet. Tap Join Game."
        await callback.answer(f"Players: {len(names)}")
        if callback.message:
            await callback.message.reply(f"👥 <b>WordSeek players ({len(names)})</b>\n{listing}", parse_mode="HTML")
    else:
        await callback.answer("Unknown action.")
        return
    if callback.message and callback.message.chat.id != chat_id:
        await callback.answer("This button belongs to another group.", show_alert=True)


@router.message(F.text.regexp(r"^[A-Za-z]{5}$"))
async def wordseek_guess(message: Message, db_session: AsyncSession, db_user, **kwargs):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    game = games.get(message.chat.id)
    if not game or game["round_over"]:
        return
    guess = (message.text or "").strip().upper()
    if len(guess) != 5 or not guess.isalpha() or not guess.isascii():
        return
    uid = message.from_user.id
    if uid not in game["joined"]:
        return
    if uid in game["solved"]:
        await message.reply("✅ You already solved this round. Wait for the next /new.")
        return
    if game["attempts"] >= MAX_ATTEMPTS:
        return

    game["attempts"] += 1
    target = game["target"]
    marks = render_guess(guess, target)
    if guess == target:
        game["solved"].add(uid)
        first = game["winner"] is None
        if first:
            game["winner"] = uid
        reward = WIN_COINS + (FIRST_BONUS if first else 0)
        await add_coins(db_session, db_user.id, reward, "wordseek_win", "WordSeek correct word")
        await add_xp(db_session, db_user.id, WIN_XP)
        game["scores"][uid] = game["scores"].get(uid, 0) + reward
        await message.reply(
            f"{marks} <b>{guess}</b>\n🎉 <b>{escape(message.from_user.full_name)}</b> guessed it!\n"
            f"🪙 +{reward} coins · ⭐ +{WIN_XP} XP" +
            ("\n🥇 First correct guess bonus included!" if first else ""),
            parse_mode="HTML",
        )
        # Keep the shared round open so other joined members can solve it too.
        all_joined_solved = bool(game["joined"]) and set(game["joined"]).issubset(game["solved"])
        if all_joined_solved or game["attempts"] >= MAX_ATTEMPTS:
            game["round_over"] = True
            await message.answer(
                f"🏁 WordSeek over! The word was <b>{target}</b>. Use /new for another round.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "🔤 Round is still open! Other joined players can keep guessing. "
                "Solved players are finished for this round.",
                parse_mode="HTML",
            )
        return

    await add_xp(db_session, db_user.id, PARTICIPATION_XP)
    await message.reply(
        f"{marks} <b>{guess}</b>\n"
        f"👤 {escape(message.from_user.full_name)} · Attempt {game['attempts']}/{MAX_ATTEMPTS}\n"
        f"⭐ +{PARTICIPATION_XP} XP"
    )
    if game["attempts"] >= MAX_ATTEMPTS:
        game["round_over"] = True
        await message.answer(f"🏁 Game over! The word was <b>{target}</b>. Use /new to play again.", parse_mode="HTML")


@router.message(Command("wscores"))
async def wordseek_scores(message: Message, db_session: AsyncSession, **kwargs):
    if not message.chat:
        return
    # Scoreboard is for the current round and is intentionally not a persistent all-time leaderboard.
    game = games.get(message.chat.id)
    if not game or not game["scores"]:
        await message.reply("📊 No WordSeek winners recorded in the current round yet.")
        return
    ranked = sorted(game["scores"].items(), key=lambda item: item[1], reverse=True)
    lines = []
    for rank, (uid, coins) in enumerate(ranked[:10], 1):
        name = game["joined"].get(uid, f"Player {uid}")
        lines.append(f"{rank}. {escape(name)} — {coins} coins")
    await message.reply("🏆 <b>WordSeek round scores</b>\n" + "\n".join(lines), parse_mode="HTML")
