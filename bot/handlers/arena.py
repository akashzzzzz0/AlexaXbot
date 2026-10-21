"""Alexa World Arena: global quizzes, puzzles, daily missions and player battles."""
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    ArenaBattle, ArenaDailyClaim, ArenaGroup, ArenaProfile, ArenaScoreEvent,
    ArenaSession, Group, User,
)
from bot.services.arena_service import (
    award_xp, calculate_battle_rewards, choose_question, get_or_create_profile,
    rank_for_xp, utc_day_key,
)

router = Router()


def _menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Quick Play", callback_data="arena:play")
    kb.button(text="📜 Daily Mission", callback_data="arena:daily")
    kb.button(text="🏆 World Players", callback_data="arena:top")
    kb.button(text="🌍 World Groups", callback_data="arena:groups")
    kb.button(text="📊 My Rank", callback_data="arena:me")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def _answer_keyboard(session_id: str, options: list[str], battle: bool = False):
    kb = InlineKeyboardBuilder()
    prefix = "arena:b" if battle else "arena:a"
    for index, option in enumerate(options):
        kb.button(text=option, callback_data=f"{prefix}:{session_id}:{index}")
    kb.adjust(2)
    return kb.as_markup()


def _is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > value


async def _ensure_group(session: AsyncSession, chat_id: int, title: str) -> ArenaGroup:
    group = await session.get(ArenaGroup, chat_id)
    if group is None:
        group = ArenaGroup(group_id=chat_id, title=title or "Telegram group", enabled=True)
        session.add(group)
        await session.flush()
    else:
        group.title = title or group.title
    return group


async def _issue_challenge(target: Message, actor_id: int, session: AsyncSession,
                           challenge_type: str = "quiz") -> None:
    chat = target.chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    if group_id is not None:
        arena_group = await _ensure_group(session, group_id, chat.title or "Telegram group")
        if not arena_group.enabled:
            await target.answer("⏸️ World Arena is disabled in this group. The group creator can enable it with /arena enable.")
            return

    day_key = utc_day_key()
    if challenge_type == "daily":
        claim = await session.scalar(select(ArenaDailyClaim).where(
            ArenaDailyClaim.user_id == actor_id,
            ArenaDailyClaim.day_key == day_key,
        ))
        if claim is not None:
            state = "completed" if claim.completed else "already started"
            await target.answer(f"📜 Your daily Arena mission is {state}. Come back tomorrow.")
            return
        session.add(ArenaDailyClaim(user_id=actor_id, day_key=day_key, completed=False))
        await session.flush()

    question = choose_question("mixed")
    session_id = uuid4().hex
    challenge = ArenaSession(
        id=session_id,
        user_id=actor_id,
        group_id=group_id,
        challenge_type=challenge_type,
        prompt=question["prompt"],
        options=question["options"],
        correct_index=question["answer"],
        day_key=day_key if challenge_type == "daily" else None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        resolved=False,
    )
    session.add(challenge)
    label = "📜 <b>Daily Mission</b>" if challenge_type == "daily" else "🧠 <b>World Arena Challenge</b>"
    reward = 25 if challenge_type == "daily" else 10
    await target.answer(
        f"{label}\n\n{question['prompt']}\n\n"
        f"Choose your answer below. Reward: <b>{reward} XP</b> for a correct answer. "
        "This challenge expires in 5 minutes.",
        reply_markup=_answer_keyboard(session_id, question["options"]),
        parse_mode="HTML",
    )


@router.message(Command("arena"))
async def arena_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session or not message.from_user:
        await message.answer("Arena database is temporarily unavailable.")
        return
    parts = (message.text or "").split(maxsplit=1)
    argument = parts[1].strip().lower() if len(parts) > 1 else ""
    if argument in {"enable", "disable"}:
        if message.chat.type not in ("group", "supergroup"):
            await message.answer("Use /arena enable or /arena disable inside a group.")
            return
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        except Exception:
            await message.answer("I couldn't verify the group creator. Please try again.")
            return
        if member.status != "creator":
            await message.answer("Only the group creator can enable or disable World Arena.")
            return
        arena_group = await _ensure_group(db_session, message.chat.id, message.chat.title or "Telegram group")
        arena_group.enabled = argument == "enable"
        await db_session.flush()
        await message.answer("✅ World Arena enabled in this group." if arena_group.enabled else
                             "⏸️ World Arena disabled in this group. Existing Alexa features are unchanged.")
        return

    if message.chat.type in ("group", "supergroup"):
        arena_group = await _ensure_group(db_session, message.chat.id, message.chat.title or "Telegram group")
        if not arena_group.enabled:
            await message.answer("⏸️ World Arena is disabled here. The group creator can use /arena enable.")
            return
    await message.answer(
        "🌍 <b>ALEXA WORLD ARENA</b>\nPlay in your group. Compete with the world.\n\n"
        "🧠 Quiz and brain puzzles\n⚔️ Reply with /battle to challenge another player\n"
        "📜 One daily mission per player\n🏆 Shared global XP, ranks and badges\n\n"
        "Group creator controls: /arena enable or /arena disable.",
        reply_markup=_menu_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("play"))
async def play_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session or not message.from_user:
        await message.answer("Arena database is temporarily unavailable.")
        return
    await _issue_challenge(message, message.from_user.id, db_session, "quiz")


@router.message(Command("arenadaily"))
async def daily_mission_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session or not message.from_user:
        await message.answer("Arena database is temporarily unavailable.")
        return
    await _issue_challenge(message, message.from_user.id, db_session, "daily")


@router.message(Command("myrank"))
async def my_rank_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session or not message.from_user:
        await message.answer("Arena database is temporarily unavailable.")
        return
    profile = await get_or_create_profile(db_session, message.from_user.id)
    ahead = await db_session.scalar(select(func.count(ArenaProfile.user_id)).where(ArenaProfile.xp > profile.xp))
    badges = ", ".join(profile.badges or []) or "None yet"
    await message.answer(
        f"📊 <b>Your World Arena Profile</b>\n"
        f"Rank: <b>{rank_for_xp(profile.xp)}</b>\nXP: <b>{profile.xp}</b>\n"
        f"Global position: <b>#{int(ahead or 0) + 1}</b>\n"
        f"Battles won: <b>{profile.wins}</b>\nBadges: {badges}",
        parse_mode="HTML",
    )


@router.message(Command("worldtop"))
async def world_top_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session:
        await message.answer("Arena database is temporarily unavailable.")
        return
    rows = (await db_session.execute(
        select(User.first_name, User.username, ArenaProfile.xp, ArenaProfile.wins)
        .join(ArenaProfile, ArenaProfile.user_id == User.id)
        .order_by(ArenaProfile.xp.desc(), ArenaProfile.wins.desc(), ArenaProfile.user_id.asc())
        .limit(10)
    )).all()
    if not rows:
        await message.answer("🌍 No World Arena scores yet. Use /play to become the first champion!")
        return
    lines = ["🌍 <b>WORLD ARENA — TOP PLAYERS</b>"]
    for index, (first_name, username, xp, wins) in enumerate(rows, 1):
        label = ("@" + username) if username else first_name
        lines.append(f"{index}. {html_escape(label)} — <b>{xp} XP</b> · {rank_for_xp(xp)} · {wins} wins")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("grouprank"))
async def group_rank_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session:
        await message.answer("Arena database is temporarily unavailable.")
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Use /grouprank inside a group to see its Arena leaderboard.")
        return
    rows = (await db_session.execute(
        select(User.first_name, User.username, func.sum(ArenaScoreEvent.xp_delta).label("xp"))
        .join(ArenaScoreEvent, ArenaScoreEvent.user_id == User.id)
        .where(ArenaScoreEvent.group_id == message.chat.id)
        .group_by(User.id, User.first_name, User.username)
        .order_by(func.sum(ArenaScoreEvent.xp_delta).desc(), User.id.asc())
        .limit(10)
    )).all()
    if not rows:
        await message.answer("🏆 No Arena scores in this group yet. Use /play to start!")
        return
    lines = [f"🏆 <b>{html_escape(message.chat.title or 'Group')} — ARENA RANK</b>"]
    for index, (first_name, username, xp) in enumerate(rows, 1):
        label = ("@" + username) if username else first_name
        lines.append(f"{index}. {html_escape(label)} — <b>{int(xp or 0)} XP</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("worldgroups"))
async def world_groups_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session:
        await message.answer("Arena database is temporarily unavailable.")
        return
    rows = (await db_session.execute(
        select(Group.title, func.sum(ArenaScoreEvent.xp_delta).label("xp"))
        .join(ArenaScoreEvent, ArenaScoreEvent.group_id == Group.id)
        .group_by(Group.id, Group.title)
        .order_by(func.sum(ArenaScoreEvent.xp_delta).desc(), Group.id.asc())
        .limit(10)
    )).all()
    if not rows:
        await message.answer("🌍 No group scores yet. Add Alexa to a group and play!")
        return
    lines = ["🌍 <b>WORLD ARENA — TOP GROUPS</b>"]
    for index, (title, xp) in enumerate(rows, 1):
        lines.append(f"{index}. {html_escape(title)} — <b>{int(xp or 0)} XP</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("battle"))
async def battle_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if not db_session or not message.from_user:
        await message.answer("Arena database is temporarily unavailable.")
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Start player battles inside a group.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Reply to another member's message with /battle to challenge them.")
        return
    opponent = message.reply_to_message.from_user
    if opponent.is_bot or opponent.id == message.from_user.id:
        await message.answer("Choose another human player for this battle.")
        return
    arena_group = await _ensure_group(db_session, message.chat.id, message.chat.title or "Telegram group")
    if not arena_group.enabled:
        await message.answer("⏸️ World Arena is disabled in this group.")
        return
    target_user = await db_session.get(User, opponent.id)
    if target_user is None:
        target_user = User(
            id=opponent.id, telegram_id=opponent.id, username=opponent.username,
            first_name=opponent.first_name or "Player", last_name=opponent.last_name,
            is_bot=False,
        )
        db_session.add(target_user)
        await db_session.flush()
    question = choose_question("mixed")
    battle_id = uuid4().hex
    battle = ArenaBattle(
        id=battle_id, challenger_id=message.from_user.id, opponent_id=opponent.id,
        group_id=message.chat.id, prompt=question["prompt"], options=question["options"],
        correct_index=question["answer"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        resolved=False,
    )
    db_session.add(battle)
    await db_session.flush()
    await message.answer(
        f"⚔️ <b>WORLD ARENA BATTLE</b>\n{message.from_user.full_name} challenged "
        f"{opponent.full_name}!\n\n{question['prompt']}\n\nBoth players answer the same question. "
        "A correct answer wins; if both are correct, the faster answer wins.",
        reply_markup=_answer_keyboard(battle_id, question["options"], battle=True),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("arena:"))
async def arena_callback(callback: CallbackQuery, db_session: AsyncSession = None, **kwargs):
    if not db_session or not callback.from_user:
        await callback.answer("Arena database is temporarily unavailable.", show_alert=True)
        return
    data = callback.data or ""
    if data == "arena:play":
        await callback.answer()
        await _issue_challenge(callback.message, callback.from_user.id, db_session, "quiz")
        return
    if data == "arena:daily":
        await callback.answer()
        await _issue_challenge(callback.message, callback.from_user.id, db_session, "daily")
        return
    if data == "arena:top":
        await callback.answer()
        await world_top_command_from_callback(callback, db_session)
        return
    if data == "arena:groups":
        await callback.answer()
        await world_groups_from_callback(callback, db_session)
        return
    if data == "arena:me":
        profile = await get_or_create_profile(db_session, callback.from_user.id)
        ahead = await db_session.scalar(select(func.count(ArenaProfile.user_id)).where(ArenaProfile.xp > profile.xp))
        badges = ", ".join(profile.badges or []) or "None yet"
        await callback.message.answer(
            f"📊 <b>Your World Arena Profile</b>\nRank: <b>{rank_for_xp(profile.xp)}</b>\n"
            f"XP: <b>{profile.xp}</b> · Global position: <b>#{int(ahead or 0) + 1}</b>\n"
            f"Battles won: <b>{profile.wins}</b>\nBadges: {badges}", parse_mode="HTML")
        await callback.answer()
        return
    if data.startswith("arena:a:"):
        parts = data.split(":")
        if len(parts) != 4:
            await callback.answer("Invalid challenge button.", show_alert=True)
            return
        _, _, session_id, choice_text = parts
        try:
            choice = int(choice_text)
        except ValueError:
            await callback.answer("Invalid answer.", show_alert=True)
            return
        challenge = await db_session.get(ArenaSession, session_id)
        if not challenge or challenge.user_id != callback.from_user.id:
            await callback.answer("This challenge belongs to another player.", show_alert=True)
            return
        if challenge.resolved:
            await callback.answer("This challenge has already been answered.", show_alert=True)
            return
        if _is_expired(challenge.expires_at):
            challenge.resolved = True
            await callback.answer("This challenge expired. Start another with /play.", show_alert=True)
            return
        if choice < 0 or choice >= len(challenge.options):
            await callback.answer("Invalid answer.", show_alert=True)
            return
        challenge.resolved = True
        correct = choice == challenge.correct_index
        reward = 25 if challenge.challenge_type == "daily" else 10
        if correct and challenge.challenge_type == "daily":
            claim = await db_session.scalar(select(ArenaDailyClaim).where(
                ArenaDailyClaim.user_id == callback.from_user.id,
                ArenaDailyClaim.day_key == (challenge.day_key or utc_day_key()),
            ))
            if claim is not None:
                claim.completed = True
            await award_xp(db_session, callback.from_user.id, challenge.group_id,
                            "daily", reward, f"arena:daily:{callback.from_user.id}:{challenge.day_key}")
        elif correct:
            await award_xp(db_session, callback.from_user.id, challenge.group_id,
                            challenge.challenge_type, reward, f"arena:answer:{challenge.id}")
        answer = challenge.options[challenge.correct_index]
        if correct:
            text = f"✅ Correct! You earned <b>{reward} XP</b>.\nAnswer: {answer}"
        else:
            text = f"❌ Not quite. Correct answer: <b>{answer}</b>. Try again with /play."
        profile = await get_or_create_profile(db_session, callback.from_user.id)
        text += f"\nYour total: <b>{profile.xp} XP</b> · Rank: <b>{rank_for_xp(profile.xp)}</b>"
        await callback.answer("Correct!" if correct else "Answer submitted.")
        await callback.message.answer(text, parse_mode="HTML")
        return
    if data.startswith("arena:b:"):
        parts = data.split(":")
        if len(parts) != 4:
            await callback.answer("Invalid battle button.", show_alert=True)
            return
        _, _, battle_id, choice_text = parts
        try:
            choice = int(choice_text)
        except ValueError:
            await callback.answer("Invalid answer.", show_alert=True)
            return
        battle = await db_session.get(ArenaBattle, battle_id)
        if not battle or callback.from_user.id not in (battle.challenger_id, battle.opponent_id):
            await callback.answer("Only the two battle players can answer.", show_alert=True)
            return
        if battle.resolved:
            await callback.answer("This battle has already ended.", show_alert=True)
            return
        if _is_expired(battle.expires_at):
            battle.resolved = True
            await callback.answer("Battle expired. Start a new one with /battle.", show_alert=True)
            return
        if choice < 0 or choice >= len(battle.options):
            await callback.answer("Invalid answer.", show_alert=True)
            return
        now = datetime.now(timezone.utc)
        if callback.from_user.id == battle.challenger_id:
            if battle.challenger_answer is not None:
                await callback.answer("You have already answered.", show_alert=True)
                return
            battle.challenger_answer = choice
            battle.challenger_answered_at = now
        else:
            if battle.opponent_answer is not None:
                await callback.answer("You have already answered.", show_alert=True)
                return
            battle.opponent_answer = choice
            battle.opponent_answered_at = now
        if battle.challenger_answer is None or battle.opponent_answer is None:
            await callback.answer("Answer saved — waiting for the other player.")
            return
        battle.resolved = True
        c_time = battle.challenger_answered_at.timestamp() if battle.challenger_answered_at else 0
        o_time = battle.opponent_answered_at.timestamp() if battle.opponent_answered_at else 0
        c_correct = battle.challenger_answer == battle.correct_index
        o_correct = battle.opponent_answer == battle.correct_index
        c_xp, o_xp, c_wins, o_wins = calculate_battle_rewards(c_correct, o_correct, c_time, o_time)
        await award_xp(db_session, battle.challenger_id, battle.group_id, "battle",
                        c_xp, f"arena:battle:{battle.id}:{battle.challenger_id}", c_wins)
        await award_xp(db_session, battle.opponent_id, battle.group_id, "battle",
                        o_xp, f"arena:battle:{battle.id}:{battle.opponent_id}", o_wins)
        if c_wins:
            winner_id = battle.challenger_id
        elif o_wins:
            winner_id = battle.opponent_id
        else:
            winner_id = None
        battle.winner_id = winner_id
        if winner_id is None:
            winner = "Draw — both players receive XP."
        else:
            winner_user = await db_session.get(User, winner_id)
            winner_name = winner_user.first_name if winner_user else "Champion"
            winner = f"🏆 Winner: {html_escape(winner_name)}!"
        await callback.answer("Battle resolved!")
        await callback.message.answer(
            f"⚔️ <b>BATTLE RESULT</b>\n{winner}\n"
            f"Correct answer: <b>{battle.options[battle.correct_index]}</b>\n"
            f"Rewards: challenger +{c_xp} XP · opponent +{o_xp} XP.",
            parse_mode="HTML")
        return
    await callback.answer("Unknown Arena action.", show_alert=True)


async def world_top_command_from_callback(callback: CallbackQuery, session: AsyncSession):
    rows = (await session.execute(
        select(User.first_name, User.username, ArenaProfile.xp, ArenaProfile.wins)
        .join(ArenaProfile, ArenaProfile.user_id == User.id)
        .order_by(ArenaProfile.xp.desc(), ArenaProfile.wins.desc(), ArenaProfile.user_id.asc()).limit(10)
    )).all()
    if not rows:
        await callback.message.answer("🌍 No World Arena scores yet. Use /play to become the first champion!")
        return
    lines = ["🌍 <b>WORLD ARENA — TOP PLAYERS</b>"]
    for index, (first_name, username, xp, wins) in enumerate(rows, 1):
        label = ("@" + username) if username else first_name
        lines.append(f"{index}. {label} — <b>{xp} XP</b> · {rank_for_xp(xp)} · {wins} wins")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


async def world_groups_from_callback(callback: CallbackQuery, session: AsyncSession):
    rows = (await session.execute(
        select(Group.title, func.sum(ArenaScoreEvent.xp_delta).label("xp"))
        .join(ArenaScoreEvent, ArenaScoreEvent.group_id == Group.id)
        .group_by(Group.id, Group.title)
        .order_by(func.sum(ArenaScoreEvent.xp_delta).desc(), Group.id.asc()).limit(10)
    )).all()
    if not rows:
        await callback.message.answer("🌍 No group scores yet. Add Alexa to a group and play!")
        return
    lines = ["🌍 <b>WORLD ARENA — TOP GROUPS</b>"]
    for index, (title, xp) in enumerate(rows, 1):
        lines.append(f"{index}. {title} — <b>{int(xp or 0)} XP</b>")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
