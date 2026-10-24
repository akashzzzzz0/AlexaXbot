"""Owner-only natural-language control for ALEXA.

Only explicitly addressed messages from the configured owner are considered.
The model may classify requests, but execution is restricted to a small allowlist.
"""
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
from bot.database.models import Group, GroupMember, SecuritySettings, User

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
        "Allowed action values: report_today, list_groups, group_status, lock_content, unlock_content, help, unsupported. "
        "Schema: {\"action\": string, \"type\": string}. "
        "For lock/unlock, type must be one of text, links, photos, videos, stickers, all. "
        "For global daily user counts choose report_today. For listing active registered groups choose list_groups. "
        "For current chat bot permissions/status choose group_status. If not clearly one of these actions choose unsupported. "
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
    allowed = {"report_today", "list_groups", "group_status", "lock_content", "unlock_content", "help", "unsupported"}
    if action not in allowed:
        return {"action": "unsupported", "type": ""}
    if action in {"lock_content", "unlock_content"} and kind not in {"text", "links", "photos", "videos", "stickers", "all"}:
        return {"action": "unsupported", "type": ""}
    return {"action": action, "type": kind}


async def _today_report(session: AsyncSession) -> str:
    now_local = datetime.now(_PK)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
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
    lines = [f"• {g.title} — <code>{g.id}</code>" for g in rows]
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
        return f"🤖 <b>Alexa status — {message.chat.title or 'this group'}</b>\nStatus: <b>{status}</b>\n" + "\n".join(lines)
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
    return f"✅ <b>{lock_type.title()}</b> {verb} in <b>{message.chat.title or 'this group'}</b>."


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
    if action == "help":
        return (
            "👑 <b>Owner AI abhi ye requests support karti hai:</b>\n"
            "• Aaj ke registered users aur active groups ki report\n"
            "• Active registered groups ki list\n"
            "• Current group mein Alexa ki permissions/status\n"
            "• Current group mein text/links/photos/videos/stickers/all lock ya unlock\n\n"
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


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def owner_ai_group_prompt(message: Message, db_session: AsyncSession, **kwargs):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    if not await _explicitly_addressed(message):
        return
    prompt = _strip_prefix(message.text or "")
    if not prompt and message.reply_to_message:
        prompt = (message.reply_to_message.text or "").strip()
    if not prompt:
        await message.reply("Bolo, kya karna hai?")
        return
    result = await _run_request(message, db_session, prompt)
    await message.reply(result, parse_mode="HTML", disable_web_page_preview=True)
