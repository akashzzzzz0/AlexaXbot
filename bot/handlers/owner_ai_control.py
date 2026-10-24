"""Owner-only natural-language control for ALEXA.

Only explicitly addressed messages from the configured owner are considered.
The model may classify requests, but execution is restricted to a small allowlist.
"""
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_gemini_api_key, settings
from bot.database.models import Group, GroupMember, GroupSettings, SecuritySettings, User, Warning
from bot.services.moderation_service import ban_user, unban_user, kick_user, mute_user, unmute_user, warn_user

router = Router(name="owner_ai_control")
logger = logging.getLogger(__name__)
_OWNER_PREFIX = re.compile(r"^\s*(?:alexa\b|@alexasuper_bot\b)[\s,!:.-]*(.*)$", re.I)
_BOT_USERNAME = None
_PK = ZoneInfo("Asia/Karachi")


class OwnerAIState(StatesGroup):
    waiting_prompt = State()


def _is_owner(user_id: int | None) -> bool:
    return bool(user_id and settings.effective_owner_id and user_id == settings.effective_owner_id)


async def _explicitly_addressed(message: Message) -> bool:
    global _BOT_USERNAME
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    if _OWNER_PREFIX.match(text):
        return True
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        return True
    if _BOT_USERNAME is None:
        try:
            me = await message.bot.get_me()
            _BOT_USERNAME = (me.username or "").casefold()
        except Exception:
            _BOT_USERNAME = ""
    return bool(_BOT_USERNAME and re.search(rf"(?<!\w)@{re.escape(_BOT_USERNAME)}\b", text, re.I))


def _strip_prefix(text: str) -> str:
    match = _OWNER_PREFIX.match(text or "")
    if match:
        return match.group(1).strip()
    if _BOT_USERNAME:
        return re.sub(rf"(?<!\w)@{re.escape(_BOT_USERNAME)}\b", "", text or "", flags=re.I).strip()
    return (text or "").strip()


async def _classify_request(text: str) -> dict:
    """Use Gemini only to classify into a fixed, validated action schema."""
    key = get_gemini_api_key()
    if not key:
        raise RuntimeError("Gemini is not configured")
    model = (settings.GEMINI_MODEL or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    instructions = (
        "Classify the user's Roman Urdu/English request into JSON only. "
        "Allowed action values: report_today, list_groups, group_status, lock_content, unlock_content, ban, mute, kick, promote, demote, demote_self, make_bot_admin, unban, unmute, warn, reset_warnings, help, unsupported. "
        "Schema: {\"action\": string, \"type\": string, \"duration_seconds\": integer, \"reason\": string}. "
        "For lock/unlock, type must be one of text, links, photos, videos, stickers, all. "
        "For global daily user counts choose report_today. For listing active registered groups choose list_groups. "
        "For current chat bot permissions/status choose group_status. If owner says remove me/myself from admin, choose demote_self. If owner asks to make Alexa/the bot an admin, choose make_bot_admin (never pretend the bot can promote itself). For ban/mute/kick/promote/demote/unban/unmute/warn/reset warnings choose the matching action only when clearly requested. Mute duration is seconds, default 3600. Keep reason short. Target is resolved separately from an explicit mention, reply, or user ID. If not clearly supported choose unsupported. "
        "Never output code, commands, explanations, or extra JSON fields."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": text[:1500]}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 100, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        response = await client.post(url, params={"key": key}, json=payload)
        response.raise_for_status()
        data = response.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    result = json.loads(raw)
    action = result.get("action")
    kind = str(result.get("type", "")).lower()
    allowed = {"report_today", "list_groups", "group_status", "lock_content", "unlock_content", "ban", "mute", "kick", "promote", "demote", "demote_self", "make_bot_admin", "unban", "unmute", "warn", "reset_warnings", "help", "unsupported"}
    if action not in allowed:
        return {"action": "unsupported", "type": ""}
    if action in {"lock_content", "unlock_content"} and kind not in {"text", "links", "photos", "videos", "stickers", "all"}:
        return {"action": "unsupported", "type": ""}
    return {"action": action, "type": kind, "duration_seconds": max(60, min(int(result.get("duration_seconds") or 3600), 2419200)), "reason": str(result.get("reason") or "Owner AI command")[:180]}


async def _today_report(session: AsyncSession) -> str:
    now_local = datetime.now(_PK)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    new_users = await session.scalar(
        select(func.count(User.id)).where(User.created_at >= start_utc, User.created_at < end_utc)
    )
    total_users = await session.scalar(select(func.count(User.id)))
    active_groups = await session.scalar(
        select(func.count(Group.id)).where(Group.is_active.is_(True))
    )
    return (
        "📊 <b>ALEXA — Aaj ki global report</b>\n"
        f"👤 Aaj registered naye users: <b>{new_users or 0}</b>\n"
        f"👥 Total registered users: <b>{total_users or 0}</b>\n"
        f"🌐 Active registered groups: <b>{active_groups or 0}</b>\n"
        f"🕒 Date (Pakistan): <b>{now_local.strftime('%d %b %Y')}</b>\n\n"
        "<i>Counts database ke recorded users par based hain; Telegram ke tamam users ka count nahi.</i>"
    )


async def _groups_report(session: AsyncSession) -> str:
    rows = (await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc()).limit(30)
    )).all()
    if not rows:
        return "Abhi database mein koi active registered group nahi mila."
    lines = [f"• {html.escape(g.title or 'Untitled')} — <code>{g.id}</code>" for g in rows]
    more = "\n<i>Showing up to 30 groups.</i>" if len(rows) == 30 else ""
    return "🌐 <b>Active registered groups</b>\n\n" + "\n".join(lines) + more


async def _group_status(message: Message) -> str:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Group status ke liye Alexa ko kisi group mein address karo."
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        status = str(getattr(member.status, "value", member.status))
        if status == "administrator":
            checks = [
                ("Delete messages", "can_delete_messages"),
                ("Restrict members", "can_restrict_members"),
                ("Invite users", "can_invite_users"),
                ("Pin messages", "can_pin_messages"),
                ("Manage topics", "can_manage_topics"),
            ]
            lines = [f"• {label}: {'✅' if getattr(member, attr, False) else '❌'}" for label, attr in checks]
        elif status == "creator":
            lines = ["• Group creator permissions"]
        else:
            lines = ["• Alexa is not an administrator in this group."]
        return f"🤖 <b>Alexa status — {html.escape(message.chat.title or 'this group')}</b>\nStatus: <b>{html.escape(status)}</b>\n" + "\n".join(lines)
    except Exception:
        return "Alexa group status check nahi kar saki. Telegram permissions check karo."


async def _set_content_lock(message: Message, session: AsyncSession, action: str, lock_type: str) -> str:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Content lock/unlock ke liye Alexa ko us group mein instruction do."
    try:
        bot_member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        status = str(getattr(bot_member.status, "value", bot_member.status))
        if status not in {"administrator", "creator"}:
            return "❌ Alexa is group mein admin nahi hai, isliye lock change nahi kar sakti."
        if status == "administrator" and not getattr(bot_member, "can_delete_messages", False):
            return "❌ Alexa ko Delete Messages permission chahiye."
    except Exception:
        return "❌ Alexa ki group permissions verify nahi ho sakin."
    fields = {
        "text": ["lock_text"],
        "links": ["lock_links"],
        "photos": ["lock_photos"],
        "videos": ["lock_videos"],
        "stickers": ["lock_stickers"],
    }
    all_fields = ["lock_text", "lock_links", "lock_photos", "lock_videos", "lock_gifs", "lock_audio",
                  "lock_voice", "lock_documents", "lock_polls", "lock_games", "lock_bots", "lock_forwards",
                  "lock_contacts", "lock_locations", "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags"]
    security = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == message.chat.id))
    if security is None:
        security = SecuritySettings(group_id=message.chat.id)
        session.add(security)
        await session.flush()
    enabled = action == "lock_content"
    targets = all_fields if lock_type == "all" else fields.get(lock_type, [])
    if not targets:
        return "Ye lock type supported nahi. Text, links, photos, videos, stickers ya all try karo."
    for field in targets:
        setattr(security, field, enabled)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        return "❌ Lock setting save nahi ho saki. Dobara try karo."
    verb = "locked" if enabled else "unlocked"
    return f"✅ <b>{html.escape(lock_type.title())}</b> {verb} in <b>{html.escape(message.chat.title or 'this group')}</b>."


async def _resolve_target(message: Message, session: AsyncSession, prompt: str, action: str = ""):
    """Resolve explicit target, or the owner for an explicit self-demotion request."""
    if action == "demote_self" and message.from_user:
        tg = message.from_user
        target = await session.get(User, tg.id)
        if target is None:
            target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User", last_name=tg.last_name, is_bot=tg.is_bot)
            session.add(target)
            await session.flush()
        return target
    if message.reply_to_message and message.reply_to_message.from_user:
        tg = message.reply_to_message.from_user
        target = await session.get(User, tg.id)
        if target is None:
            target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User",
                          last_name=tg.last_name, is_bot=tg.is_bot)
            session.add(target)
            await session.flush()
        return target
    for entity in (message.entities or []):
        if getattr(entity.type, "value", entity.type) == "text_mention" and entity.user:
            tg = entity.user
            target = await session.get(User, tg.id)
            if target is None:
                target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User",
                              last_name=tg.last_name, is_bot=tg.is_bot)
                session.add(target)
                await session.flush()
            return target
    username_match = re.search(r"(?<![\w])@([A-Za-z0-9_]{5,32})\b", prompt)
    if username_match:
        target = await session.scalar(select(User).where(func.lower(User.username) == username_match.group(1).casefold()))
        if target:
            return target
    id_match = re.search(r"(?<!\d)(\d{5,20})(?!\d)", prompt)
    if id_match:
        return await session.get(User, int(id_match.group(1)))
    return None


async def _moderation_action(message: Message, session: AsyncSession, action: str, intent: dict, prompt: str) -> str:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Ye moderation action group mein hi chal sakta hai."
    if action == "make_bot_admin":
        return "Main khud ko admin promote nahi kar sakti. Group ka owner/admin mujhe Telegram ke Admins menu se manually admin banaye; uske baad main apni available permissions ke mutabiq kaam karungi."
    target = await _resolve_target(message, session, prompt, action)
    if not target:
        return "Member ko Telegram mention se select karo, uske message ka reply do, ya uska numeric user ID likho."
    if target.id == message.bot.id:
        return "Main apne aap par ye action nahi laga sakti."
    if target.id == settings.effective_owner_id and action != "demote_self":
        return "Configured bot owner par moderation action execute nahi kiya gaya, siwaye aapki explicit self-demotion request ke."
    try:
        bot_member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        bot_status = str(getattr(bot_member.status, "value", bot_member.status))
        target_member = await message.bot.get_chat_member(message.chat.id, target.id)
        target_status = str(getattr(target_member.status, "value", target_member.status))
    except Exception:
        return "Member/group status verify nahi ho saka. Target isi group mein hona chahiye."
    if bot_status != "administrator":
        return "Alexa ko is group mein admin banana hoga."
    needed = {
        "ban": "can_restrict_members", "kick": "can_restrict_members", "mute": "can_restrict_members",
        "unmute": "can_restrict_members", "unban": "can_restrict_members",
        "promote": "can_promote_members", "demote": "can_promote_members",
        "warn": "can_delete_messages", "reset_warnings": "can_delete_messages",
    }.get(action)
    if needed and not getattr(bot_member, needed, False):
        permission_name = {"can_restrict_members": "Ban/Restrict Members", "can_promote_members": "Add New Admins",
                           "can_delete_messages": "Delete Messages"}.get(needed, needed)
        return "Alexa ke paas " + permission_name + " permission nahi hai."
    if target_status == "creator" and action in {"ban", "mute", "kick", "promote", "demote", "demote_self"}:
        return "Admin/owner par ye action execute nahi kiya; Telegram admin hierarchy ke rules apply hote hain."
    actor_id = settings.effective_owner_id or 0
    reason = intent.get("reason") or "Owner AI command"
    ok = False
    if action == "ban":
        ok = await ban_user(message.bot, session, message.chat.id, target.id, actor_id, reason)
    elif action == "mute":
        ok = await mute_user(message.bot, session, message.chat.id, target.id, actor_id, int(intent.get("duration_seconds") or 3600), reason)
    elif action == "kick":
        ok = await kick_user(message.bot, session, message.chat.id, target.id, actor_id, reason)
    elif action == "unban":
        ok = await unban_user(message.bot, session, message.chat.id, target.id, actor_id)
    elif action == "unmute":
        ok = await unmute_user(message.bot, session, message.chat.id, target.id, actor_id)
    elif action == "warn":
        count, reached = await warn_user(session, message.chat.id, target.id, actor_id, reason)
        if reached:
            group_settings = await session.scalar(select(GroupSettings).where(GroupSettings.group_id == message.chat.id))
            warn_action = group_settings.warn_action if group_settings else "mute"
            if warn_action == "mute":
                await mute_user(message.bot, session, message.chat.id, target.id, actor_id, 3600, "Warning limit reached")
            elif warn_action == "kick":
                await kick_user(message.bot, session, message.chat.id, target.id, actor_id, "Warning limit reached")
            elif warn_action == "ban":
                await ban_user(message.bot, session, message.chat.id, target.id, actor_id, "Warning limit reached")
            await session.commit()
            return "⚠️ Warning recorded for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>. Count: <b>" + str(count) + "</b>; limit action: <b>" + html.escape(warn_action) + "</b>."
        await session.commit()
        return "⚠️ Warning recorded for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>. Count: <b>" + str(count) + "</b>."
    elif action == "reset_warnings":
        from sqlalchemy import delete
        await session.execute(delete(Warning).where(Warning.group_id == message.chat.id, Warning.user_id == target.id))
        member = await session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target.id))
        if member:
            member.warn_count = 0
        await session.commit()
        return "✅ Warnings reset for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>."
    elif action in {"promote", "demote", "demote_self"}:
        try:
            if action == "promote":
                await message.bot.promote_chat_member(
                    chat_id=message.chat.id, user_id=target.id, can_manage_chat=True, can_delete_messages=True,
                    can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False,
                    can_change_info=False, can_invite_users=True, can_pin_messages=True, can_post_messages=False,
                    can_edit_messages=False, can_manage_topics=True,
                )
            else:
                await message.bot.promote_chat_member(
                    chat_id=message.chat.id, user_id=target.id, can_manage_chat=False, can_delete_messages=False,
                    can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False,
                    can_change_info=False, can_invite_users=False, can_pin_messages=False, can_post_messages=False,
                    can_edit_messages=False, can_manage_topics=False,
                )
            verified = await message.bot.get_chat_member(message.chat.id, target.id)
            expected = "administrator" if action == "promote" else "member"
            ok = str(getattr(verified.status, "value", verified.status)) == expected
        except Exception:
            ok = False
    if not ok:
        await session.rollback()
        return "❌ " + action.replace("_", " ").title() + " fail hua. Alexa ki permissions aur target ka group status check karo."
    await session.commit()
    verb = {
        "ban": "banned", "mute": "muted for " + str((intent.get("duration_seconds") or 3600) // 60) + " minutes",
        "kick": "kicked", "unban": "unbanned", "unmute": "unmuted",
        "promote": "promoted to admin", "demote": "demoted", "demote_self": "removed from admin",
    }.get(action, action.replace("_", " "))
    return "✅ <b>" + html.escape(target.first_name or "Member") + "</b> " + html.escape(verb) + " successfully in <b>" + html.escape(message.chat.title or "this group") + "</b>."


async def _run_request(message: Message, session: AsyncSession, prompt: str) -> str:
    try:
        intent = await _classify_request(prompt)
    except Exception as exc:
        logger.warning("Owner AI request classification failed: %s", type(exc).__name__)
        return "AI command process nahi ho saki. Gemini connection/configuration check karo; koi action execute nahi hua."
    action = intent["action"]
    if action == "report_today":
        return await _today_report(session)
    if action == "list_groups":
        return await _groups_report(session)
    if action == "group_status":
        return await _group_status(message)
    if action in {"lock_content", "unlock_content"}:
        return await _set_content_lock(message, session, action, intent["type"])
    if action in {"ban", "mute", "kick", "promote", "demote", "demote_self", "make_bot_admin", "unban", "unmute", "warn", "reset_warnings"}:
        return await _moderation_action(message, session, action, intent, prompt)
    if action == "help":
        return (
            "👑 <b>Owner AI abhi ye requests support karti hai:</b>\n"
            "• Aaj ke registered users aur active groups ki report\n"
            "• Active registered groups ki list\n"
            "• Current group mein Alexa ki permissions/status\n"
            "• Current group mein text/links/photos/videos/stickers/all lock ya unlock\n"
            "• Mention/reply wale member ko ban, mute, kick, promote, demote, unban, unmute, warn ya warnings reset\n"
            "• Khud ko admin se demote karne ki request; Alexa ko admin banane ke liye group owner ko manually promote karna hoga\n\n"
            "Baaki commands ko safely connect karne ke liye additional actions implement karne honge."
        )
    return (
        "Is request ka safe action abhi connect nahi hai, isliye maine kuch execute nahi kiya. "
        "Filhaal bolo: aaj ki report, active groups ki list, group status, ya links/text/photos/videos/stickers lock/unlock."
    )


@router.callback_query(F.data == "owner:ai")
async def owner_ai_panel(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    await state.set_state(OwnerAIState.waiting_prompt)
    await callback.message.answer(
        "🧠 <b>Alexa Global AI Control</b>\n\n"
        "Apni request normal Roman Urdu ya English mein bhejo. Global reports yahan chalengi. "
        "Group-specific actions ke liye wohi instruction us group mein Alexa ko address karke do.\n\n"
        "Try: <code>Aaj kitne naye users aaye?</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(F.chat.type == ChatType.PRIVATE, F.text, OwnerAIState.waiting_prompt)
async def owner_ai_panel_prompt(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not _is_owner(message.from_user.id):
        await state.clear()
        return
    prompt = (message.text or "").strip()
    if prompt.lower() in {"/cancel", "cancel", "close"}:
        await state.clear()
        await message.answer("AI Control session closed.")
        return
    status = await message.answer("🧠 Request check kar rahi hoon…")
    result = await _run_request(message, db_session, prompt)
    await state.clear()
    try:
        await status.edit_text(result, parse_mode="HTML")
    except Exception:
        await message.answer(result, parse_mode="HTML")


async def is_owner_ai_message(message: Message) -> bool:
    return bool(
        message.from_user
        and _is_owner(message.from_user.id)
        and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and await _explicitly_addressed(message)
    )


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    is_owner_ai_message,
)
async def owner_ai_group_prompt(message: Message, db_session: AsyncSession, **kwargs):
    prompt = _strip_prefix(message.text or "")
    if not prompt and message.reply_to_message:
        prompt = (message.reply_to_message.text or "").strip()
    if not prompt:
        await message.reply("Bolo, kya karna hai?")
        return
    result = await _run_request(message, db_session, prompt)
    await message.reply(result, parse_mode="HTML", disable_web_page_preview=True)
