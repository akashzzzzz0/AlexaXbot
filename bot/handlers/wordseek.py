"""WordSeek multiplayer word guessing game for Alexa."""
import random
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
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
        "/wscores — see this group's WordSeek leaderboard\n\n"
        "Guess a five-letter English word. 🟩 correct letter and position, "
        "🟨 letter exists but is in another position, 🟥 letter is not needed. "
        "Everyone in the group can guess directly — no joining needed.",
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
        "attempts": 0,
        "round_over": False,
        "scores": {},
        "names": {},
    }
    await message.reply(
        "🔤 <b>WordSeek started!</b>\n"
        "Everyone can play immediately — just send any 5-letter English word as a message.\n"
        f"Attempts available: {MAX_ATTEMPTS}\n"
        "🟩 right place · 🟨 in the word, wrong place · 🟥 not in the word",
        parse_mode="HTML",
    )


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
    if game["attempts"] >= MAX_ATTEMPTS:
        return

    game["attempts"] += 1
    game["names"][uid] = message.from_user.full_name
    target = game["target"]
    marks = render_guess(guess, target)
    if guess == target:
        reward = WIN_COINS + FIRST_BONUS
        await add_coins(db_session, db_user.id, reward, "wordseek_win", "WordSeek correct word")
        await add_xp(db_session, db_user.id, WIN_XP)
        game["scores"][uid] = game["scores"].get(uid, 0) + reward
        await message.reply(
            f"{marks} <b>{guess}</b>\n🎉 <b>{escape(message.from_user.full_name)}</b> guessed it!\n"
            f"🪙 +{reward} coins · ⭐ +{WIN_XP} XP" +
            "\n🥇 First correct guess bonus included!",
            parse_mode="HTML",
        )
        game["round_over"] = True
        await message.answer(
            f"🏁 WordSeek over! The word was <b>{target}</b>. Use /new to play again.",
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
        name = game["names"].get(uid, f"Player {uid}")
        lines.append(f"{rank}. {escape(name)} — {coins} coins")
    await message.reply("🏆 <b>WordSeek round scores</b>\n" + "\n".join(lines), parse_mode="HTML")
