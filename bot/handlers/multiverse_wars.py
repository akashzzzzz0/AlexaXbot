"""Telegram commands for ALEXA Multiverse Wars.

War commands are isolated from existing moderation, economy, and owner controls.
"""
from datetime import datetime, timezone
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import WarGuild, WarChallenge
from bot.services.multiverse_wars import (
    accept_challenge, create_challenge, daily_mission, leaderboard,
    register_guild, season_key, get_score, settle_expired_challenges,
    submit_answer, world_leaderboard, claim_mystery_reward,
)

router = Router()


async def _is_group_admin(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        status = str(getattr(member.status, "value", member.status))
        return status in ("administrator", "creator")
    except Exception:
        return False


def _group_only(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


@router.message(Command("war"))
async def war_home(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.answer("⚔️ Multiverse Wars works inside Telegram groups. Add Alexa to a group to begin.")
        return
    guild = await db_session.get(WarGuild, message.chat.id) if db_session else None
    if not guild or not guild.is_active:
        await message.reply(
            "⚔️ <b>ALEXA MULTIVERSE WARS</b>\n\n"
            "This group is not registered yet. A group admin can use /warregister.\n\n"
            "After registration:\n"
            "• /warmission — daily team mission\n"
            "• /waranswer ANSWER — submit your answer (3 attempts/day)\n"
            "• /warboard — weekly group leaderboard\n"
            "• /warchallenge GROUP_ID — challenge another registered group\n"
            "• /waraccept ID — accept a challenge (challenged group's admin)\n"
            "• /warresult ID — view challenge results\n"
            "• /warleave — leave the competition",
            parse_mode="HTML",
        )
        return
    await settle_expired_challenges(db_session)
    score = await get_score(db_session, message.chat.id)
    await db_session.commit()
    question, _ = daily_mission()
    await message.reply(
        f"⚔️ <b>ALEXA MULTIVERSE WARS</b>\n"
        f"🏰 Guild: <b>{escape(guild.title)}</b>\n"
        f"📅 Season: <code>{season_key()}</code>\n"
        f"⭐ Season points: <b>{score.points}</b> · Wins: <b>{score.wins}</b>\n\n"
        f"🎯 Today's mission: {escape(question)}\n"
        "Use /warmission to see the mission and /waranswer ANSWER to score points.",
        parse_mode="HTML",
    )


@router.message(Command("warregister"))
async def war_register(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.reply("Use /warregister inside the group you want to enroll.")
        return
    if not await _is_group_admin(message):
        await message.reply("🔒 Only this group's admins/owner can register it for Wars.")
        return
    if db_session is None:
        await message.reply("Database is temporarily unavailable; no changes were made.")
        return
    guild = await register_guild(
        db_session, group_id=message.chat.id, title=message.chat.title or "Telegram group",
        username=message.chat.username, admin_id=message.from_user.id,
    )
    await db_session.commit()
    await message.reply(
        f"🏰 <b>{escape(guild.title)}</b> joined ALEXA Multiverse Wars!\n"
        "Daily missions are now available. Use /warmission to start.",
        parse_mode="HTML",
    )


@router.message(Command("warleave"))
async def war_leave(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.reply("Use /warleave inside your registered group.")
        return
    if not await _is_group_admin(message):
        await message.reply("🔒 Only this group's admins/owner can remove it from Wars.")
        return
    guild = await db_session.get(WarGuild, message.chat.id) if db_session else None
    if not guild or not guild.is_active:
        await message.reply("This group is not currently registered for Wars.")
        return
    guild.is_active = False
    await db_session.commit()
    await message.reply("🏳️ This group has left new Multiverse Wars scoring. Existing scores and history are preserved.")


@router.message(Command("warmission"))
async def war_mission(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.answer("Open /warmission inside a registered Telegram group.")
        return
    guild = await db_session.get(WarGuild, message.chat.id) if db_session else None
    if not guild or not guild.is_active:
        await message.reply("This group must be registered first. Ask an admin to use /warregister.")
        return
    question, _ = daily_mission()
    await message.reply(
        f"🎯 <b>Daily War Mission</b>\n{escape(question)}\n\n"
        f"Submit with <code>/waranswer your_answer</code>.\n"
        "Each member can earn 5 points once per UTC day, with up to 3 attempts.",
        parse_mode="HTML",
    )


@router.message(Command("waranswer"))
async def war_answer(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message) or not message.from_user:
        await message.reply("Submit your mission answer inside a registered Telegram group.")
        return
    answer = (message.text or "").partition(" ")[2].strip()
    if not answer:
        question, _ = daily_mission()
        await message.reply(
            f"🎯 {escape(question)}\nUse <code>/waranswer your_answer</code>.",
            parse_mode="HTML",
        )
        return
    if db_session is None:
        await message.reply("Database is temporarily unavailable; your answer was not scored.")
        return
    try:
        result = await submit_answer(
            db_session, group_id=message.chat.id, user_id=message.from_user.id, answer=answer,
        )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        await message.reply("⚠️ Could not safely record that answer. Please try again.")
        return

    status = result["status"]
    if status == "not_registered":
        await message.reply("This group must be registered first. Ask an admin to use /warregister.")
    elif status == "already_completed":
        await message.reply(f"✅ You already completed today's mission and earned {result['points']} points.")
    elif status == "attempts_exhausted":
        await message.reply("⏳ Today's 3 attempts are used. Come back after the next UTC day reset.")
    elif status == "incorrect":
        await message.reply(f"❌ Not quite. Attempts used: {result['attempts']}/3; remaining: {result['remaining']}. Try /warmission again.")
    else:
        extra = (
            f"\n⚔️ Challenge contribution: +{result['challenge_points']}"
            if result["challenge_points"] else ""
        )
        await message.reply(
            f"✅ <b>Correct!</b> +{result['points']} group season points.\n"
            f"🏰 Group total: {result['season_points']} points.{extra}",
            parse_mode="HTML",
        )


@router.message(Command("warboard"))
async def war_board(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.answer("Use /warboard inside a Telegram group.")
        return
    if db_session is None:
        await message.reply("Leaderboard is temporarily unavailable.")
        return
    await settle_expired_challenges(db_session)
    rows = await leaderboard(db_session)
    await db_session.commit()
    if not rows:
        await message.reply("🏆 No scored groups this season yet. Register with /warregister and complete /warmission.")
        return
    lines = [f"⚔️ <b>Multiverse Wars · {season_key()}</b>", ""]
    medals = ("🥇", "🥈", "🥉")
    for index, (guild, score) in enumerate(rows, 1):
        mark = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(
            f"{mark} <b>{escape(guild.title)}</b> — {score.points} pts "
            f"(W:{score.wins} D:{score.draws} L:{score.losses})"
        )
    lines.append("\n🎯 Earn 5 points by completing today's mission.")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("warchallenge"))
async def war_challenge(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.reply("Use /warchallenge GROUP_ID inside your registered group.")
        return
    if not await _is_group_admin(message):
        await message.reply("🔒 Only this group's admins/owner can send a war challenge.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("Usage: /warchallenge GROUP_ID\nThe target group must first use /warregister.")
        return
    try:
        opponent_id = int(parts[1].strip())
    except ValueError:
        await message.reply("Use the target group's numeric Telegram chat ID, e.g. /warchallenge -1001234567890.")
        return
    if db_session is None:
        await message.reply("Database is temporarily unavailable; no challenge was created.")
        return
    try:
        challenge = await create_challenge(
            db_session, challenger_group_id=message.chat.id,
            opponent_group_id=opponent_id, created_by=message.from_user.id,
        )
        opponent = await db_session.get(WarGuild, opponent_id)
        await db_session.flush()
        await message.bot.send_message(
            opponent_id,
            f"⚔️ <b>ALEXA MULTIVERSE WAR CHALLENGE</b>\n"
            f"<b>{escape(message.chat.title or 'A Telegram group')}</b> challenged your guild!\n"
            f"Challenge ID: <code>{challenge.id}</code>\n"
            f"An admin can accept with <code>/waraccept {challenge.id}</code>.\n"
            "After acceptance, both groups have 24 hours to earn mission contributions.",
            parse_mode="HTML",
        )
        await db_session.commit()
    except ValueError as exc:
        await db_session.rollback()
        await message.reply(escape(str(exc)))
        return
    except Exception:
        await db_session.rollback()
        await message.reply("⚠️ Challenge could not be delivered. Check that Alexa can send messages in the target group; no challenge was saved.")
        return
    await message.reply(
        f"⚔️ Challenge #{challenge.id} sent to <b>{escape(opponent.title)}</b>. "
        "It becomes active when that group's admin accepts.",
        parse_mode="HTML",
    )


@router.message(Command("waraccept"))
async def war_accept(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.reply("Accept challenges inside the challenged group.")
        return
    if not await _is_group_admin(message):
        await message.reply("🔒 Only the challenged group's admins/owner can accept.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("Usage: /waraccept CHALLENGE_ID")
        return
    if db_session is None:
        await message.reply("Database is temporarily unavailable.")
        return
    try:
        challenge = await accept_challenge(
            db_session, challenge_id=int(parts[1]), group_id=message.chat.id,
            admin_id=message.from_user.id,
        )
        await db_session.commit()
    except ValueError as exc:
        await db_session.rollback()
        await message.reply(escape(str(exc)))
        return
    await message.reply(
        f"⚔️ <b>Challenge #{challenge.id} accepted!</b>\n"
        "Both groups have 24 hours to complete daily missions. Each member contributes at most once to this challenge.",
        parse_mode="HTML",
    )
    for chat_id in (challenge.challenger_group_id, challenge.opponent_group_id):
        try:
            await message.bot.send_message(
                chat_id,
                f"⚔️ Challenge #{challenge.id} is LIVE for 24 hours! Use /warmission and /waranswer ANSWER.",
            )
        except Exception:
            pass


@router.message(Command("warresult"))
async def war_result(message: Message, db_session: AsyncSession = None, **kwargs):
    if not _group_only(message):
        await message.reply("Use /warresult CHALLENGE_ID inside either participating group.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("Usage: /warresult CHALLENGE_ID")
        return
    if db_session is None:
        await message.reply("Challenge results are temporarily unavailable.")
        return
    challenge = await db_session.get(WarChallenge, int(parts[1]))
    if not challenge or message.chat.id not in (challenge.challenger_group_id, challenge.opponent_group_id):
        await message.reply("Challenge not found for this group.")
        return
    await settle_expired_challenges(db_session)
    await db_session.refresh(challenge)
    await db_session.commit()
    left = await db_session.get(WarGuild, challenge.challenger_group_id)
    right = await db_session.get(WarGuild, challenge.opponent_group_id)
    state = challenge.status.upper()
    if challenge.status == "completed":
        if challenge.winner_group_id is None:
            result_line = "🤝 The challenge ended in a draw."
        else:
            winner = left if challenge.winner_group_id == left.group_id else right
            result_line = f"🏆 Winner: <b>{escape(winner.title)}</b> (+{20} season bonus points)"
    elif challenge.status == "active":
        result_line = f"⏳ Ends: {challenge.ends_at.isoformat() if challenge.ends_at else 'unknown'}"
    else:
        result_line = "This challenge is still waiting for the challenged group's admin to accept."
    await message.reply(
        f"⚔️ <b>Challenge #{challenge.id} · {state}</b>\n"
        f"🏰 {escape(left.title if left else str(challenge.challenger_group_id))}: {challenge.challenger_points} challenge points\n"
        f"🏰 {escape(right.title if right else str(challenge.opponent_group_id))}: {challenge.opponent_points} challenge points\n\n"
        f"{result_line}",
        parse_mode="HTML",
    )



@router.message(Command("worldrank"))
async def world_rank(message: Message, db_session: AsyncSession = None, **kwargs):
    """Show all-time points accumulated by registered groups across seasons."""
    if db_session is None:
        await message.reply("🌍 World Ranking is temporarily unavailable.")
        return
    rows = await world_leaderboard(db_session, limit=10)
    if not rows:
        await message.reply("🌍 No groups are ranked yet. Group admins can start with /warregister.")
        return
    lines = ["🌍 <b>ALEXA WORLD RANKING</b>", "🏆 All-time points across every Wars season", ""]
    medals = ("🥇", "🥈", "🥉")
    for index, (guild, points) in enumerate(rows, 1):
        mark = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(f"{mark} <b>{escape(guild.title)}</b> — {points} pts")
    lines.append("\n⚔️ Earn more by completing /warmission and winning group challenges.")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("mystery"))
async def mystery_reward(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    """Claim a random virtual-coin reward once per UTC day."""
    if not message.from_user or not db_user:
        await message.reply("Open Alexa in a private chat and press /start first, then try /mystery again.")
        return
    if db_session is None:
        await message.reply("🎁 Mystery Reward is temporarily unavailable.")
        return
    try:
        result = await claim_mystery_reward(db_session, user_id=db_user.id)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        await message.reply("⚠️ The mystery reward could not be saved safely. Please try again.")
        return
    if result["status"] == "already_claimed":
        await message.reply(
            f"🎁 You already opened today's Mystery Reward and received {result['amount']} coins. Come back after the UTC daily reset."
        )
        return
    await message.reply(
        "🎁 <b>MYSTERY REWARD UNLOCKED!</b>\n\n"
        f"✨ Surprise prize: <b>+{result['amount']} coins</b>\n"
        f"💰 Your wallet balance: <b>{result['balance']} coins</b>\n\n"
        "Come back tomorrow for another mystery box!",
        parse_mode="HTML",
    )
