from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from html import escape
import re
from bot.database.models import GroupRule, GroupFilter, SavedMessage, GroupBlocklist, GroupDisabledCommand, GroupMember, User, SecuritySettings, GroupSettings
from bot.middlewares.admin_actions import AdminActionMiddleware

router = Router()
router.message.outer_middleware(AdminActionMiddleware(commands={"setrules", "filter", "stopfilter", "approve", "unapprove", "save", "clear", "addblocklist", "rmblocklist", "locktypes", "disablecmd", "enablecmd", "autodelete", "grouppromo"}))

class GroupToolsStates(StatesGroup):
    waiting_rules = State()


@router.message(Command("id"))
async def show_ids(message: Message, **kwargs):
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    await message.reply(f"👤 User ID: <code>{target.id}</code>\n💬 Chat ID: <code>{message.chat.id}</code>", parse_mode="HTML")

@router.message(Command("info"))
async def user_info(message: Message, **kwargs):
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    username = f"@{target.username}" if target.username else "Not set"
    await message.reply(f"👤 <b>User Info</b>\nName: {escape(target.full_name or target.first_name or 'User')}\nUsername: {escape(username)}\nID: <code>{target.id}</code>\nBot account: {'yes' if target.is_bot else 'no'}", parse_mode="HTML")


@router.message(Command("admins"))
async def list_admins(message: Message, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /admins inside a group."); return
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
        lines = []
        for item in admins:
            user = item.user
            role = "Creator" if item.status == "creator" else "Admin"
            label = f"@{user.username}" if user.username else (user.full_name or user.first_name or str(user.id))
            lines.append(f"• {escape(label)} — {role} (<code>{user.id}</code>)")
        await message.reply("👥 <b>Group Admins</b>\n\n" + "\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.reply("Could not fetch the admin list.")

async def _send_admin_report(message: Message, reason: str):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /report or @admin in a group.")
        return
    replied = message.reply_to_message
    if not replied or not replied.from_user:
        await message.reply("Reply to the message you want to report, then use /report [reason] or @admin [reason].")
        return
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
    except Exception:
        await message.reply("Could not notify group admins. Check the bot's group permissions.")
        return
    mentions = []
    for item in admins:
        user = item.user
        if user.is_bot:
            continue
        label = user.full_name or user.first_name or str(user.id)
        mentions.append(f'<a href="tg://user?id={user.id}">{escape(label)}</a>')
    admin_line = " ".join(mentions) if mentions else "No mentionable admins found."
    target = replied.from_user
    reporter = message.from_user
    report_text = (
        "🚩 <b>Member Report</b>\n"
        + f'Reported: <a href="tg://user?id={target.id}">{escape(target.full_name or target.first_name or str(target.id))}</a> (<code>{target.id}</code>)\n'
        + f'Reported by: <a href="tg://user?id={reporter.id}">{escape(reporter.full_name or reporter.first_name or str(reporter.id))}</a> (<code>{reporter.id}</code>)\n'
        + "Reason: " + escape(reason or "No reason provided") + "\n"
        + "📣 <b>Group admins:</b> " + admin_line
    )
    await message.reply(report_text, parse_mode="HTML")


@router.message(Command("report"))
async def report_member(message: Message, **kwargs):
    parts = (message.text or "").split(None, 1)
    reason = parts[1] if len(parts) > 1 else "No reason provided"
    await _send_admin_report(message, reason)


@router.message(F.text.regexp(r"(?i)^@admin(?:\s|$)"))
async def report_to_admins_mention(message: Message, **kwargs):
    parts = (message.text or "").split(None, 1)
    reason = parts[1] if len(parts) > 1 else "No reason provided"
    await _send_admin_report(message, reason)



@router.message(Command("autodelete"))
async def set_auto_delete(message: Message, db_session: AsyncSession, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /autodelete inside a group.")
        return
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        current = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == message.chat.id))
        seconds = current.auto_delete_seconds if current else 0
        status = f"{seconds} seconds" if seconds else "OFF"
        await message.reply(f"Current command auto-delete: {status}.\nUsage: /autodelete 5 (0-60 seconds) or /autodelete off")
        return
    value = parts[1].strip().lower()
    if value in {"off", "disable", "disabled", "0"}:
        seconds = 0
    else:
        try:
            seconds = int(value)
        except ValueError:
            await message.reply("Use /autodelete 5, a number from 1 to 60, or /autodelete off.")
            return
        if not 1 <= seconds <= 60:
            await message.reply("Choose a delay from 1 to 60 seconds, or use /autodelete off.")
            return
    row = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == message.chat.id))
    if not row:
        row = GroupSettings(group_id=message.chat.id, auto_delete_seconds=seconds)
        db_session.add(row)
    else:
        row.auto_delete_seconds = seconds
    await db_session.flush()
    status = f"{seconds} seconds" if seconds else "OFF"
    await message.reply(f"✅ Command auto-delete is now {status}. ALEXA needs Delete Messages permission.")


@router.message(Command("grouppromo"))
async def group_promotion(message: Message, **kwargs):
    """Post an admin-authored promotional announcement in this group."""
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /grouppromo inside a group.")
        return
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("Usage: /grouppromo <announcement text>")
        return
    promo_text = parts[1].strip()
    if len(promo_text) > 3500:
        await message.reply("Promotion text is too long. Keep it under 3500 characters.")
        return
    await message.answer("📣 <b>Group Promotion</b>\n\n" + escape(promo_text), parse_mode="HTML", disable_web_page_preview=False)


@router.message(Command("disablecmd"))
async def disable_command(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /disablecmd <command without slash>"); return
    command = parts[1].strip().lstrip("/").split("@", 1)[0].lower()[:64]
    if command in {"enablecmd", "disablecmd", "disabledcmds", "owner", "settings", "security", "help", "start", "cancel"}:
        await message.reply("That command is protected and cannot be disabled."); return
    if not re.fullmatch(r"[a-z0-9_]{1,64}", command):
        await message.reply("Use a valid command name."); return
    existing = await db_session.scalar(select(GroupDisabledCommand).where(GroupDisabledCommand.group_id == message.chat.id, GroupDisabledCommand.command == command))
    if not existing:
        db_session.add(GroupDisabledCommand(group_id=message.chat.id, command=command, disabled_by=message.from_user.id))
    await message.reply(f"✅ /{escape(command)} disabled in this group.", parse_mode="HTML")

@router.message(Command("enablecmd"))
async def enable_command(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /enablecmd <command without slash>"); return
    command = parts[1].strip().lstrip("/").split("@", 1)[0].lower()[:64]
    await db_session.execute(delete(GroupDisabledCommand).where(GroupDisabledCommand.group_id == message.chat.id, GroupDisabledCommand.command == command))
    await message.reply(f"✅ /{escape(command)} enabled in this group.", parse_mode="HTML")

@router.message(Command("disabledcmds"))
async def list_disabled_commands(message: Message, db_session: AsyncSession, **kwargs):
    rows = (await db_session.execute(select(GroupDisabledCommand).where(GroupDisabledCommand.group_id == message.chat.id).order_by(GroupDisabledCommand.command.asc()))).scalars().all()
    await message.reply("🚫 <b>Disabled Commands</b>\n\n" + ("\n".join(f"• /{escape(row.command)}" for row in rows) if rows else "None."), parse_mode="HTML")


@router.message(Command("rules"))
async def show_rules(message: Message, db_session: AsyncSession, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("/rules is available in groups."); return
    row = await db_session.get(GroupRule, message.chat.id)
    text = row.rules if row else "Be respectful. No spam, scams, or harassment. Follow the group admins' instructions."
    await message.reply("📜 <b>Group Rules</b>\n\n" + escape(text), parse_mode="HTML")

@router.message(Command("setrules"))
async def set_rules_start(message: Message, state: FSMContext, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Groups only."); return
    await state.set_state(GroupToolsStates.waiting_rules)
    await message.reply("Send the new group rules in one message. Use /cancel to cancel.")

@router.message(Command("cancel"))
async def cancel_group_state(message: Message, state: FSMContext, **kwargs):
    await state.clear(); await message.reply("Cancelled.")

@router.message(GroupToolsStates.waiting_rules)
async def set_rules_finish(message: Message, db_session: AsyncSession, state: FSMContext, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await state.clear(); return
    row = await db_session.get(GroupRule, message.chat.id)
    if not row:
        row = GroupRule(group_id=message.chat.id, rules=message.text or "")
        db_session.add(row)
    else:
        row.rules = message.text or ""
    await db_session.flush(); await state.clear()
    await message.reply("✅ Group rules saved.")

@router.message(Command("locktypes"))
async def lock_types(message: Message, **kwargs):
    await message.reply("🔒 Lock types: text, links, photos, videos, gifs, audio, voice, docs, polls, games, bots, forwards, contacts, locations, stickers, inline, mentions, hashtags. Use /lock <type> or /unlock <type>.")

@router.message(Command("filter"))
async def add_filter(message: Message, db_session: AsyncSession, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Groups only."); return
    parts = (message.text or "").split(None, 2)
    if len(parts) < 2:
        await message.reply("Usage: /filter <keyword> <reply text>, or reply to text/sticker with /filter <keyword> [@username or user ID]."); return
    keyword = parts[1].strip().lower()[:128]
    replied = message.reply_to_message
    target_user_id = None
    target_username = None
    response_type = "text"
    if replied and replied.sticker:
        response = replied.sticker.file_id
        response_type = "sticker"
        if len(parts) > 2:
            token = parts[2].strip()
            if token.startswith("@"):
                target_username = token[1:].lower()[:64]
                target = await db_session.scalar(select(User).where(User.username.ilike(token[1:])))
                target_user_id = target.id if target else None
            else:
                try:
                    target_user_id = int(token)
                except ValueError:
                    await message.reply("Use a numeric user ID or a username already known to ALEXA."); return
            if target_user_id is None and not target_username:
                await message.reply("Use a numeric user ID or @username."); return
    else:
        response = (replied.text or replied.caption) if replied else (parts[2] if len(parts) > 2 else None)
        if not response:
            await message.reply("Provide reply text or reply to a text/sticker message."); return
        if len(parts) > 2 and replied:
            token = parts[2].strip()
            # A target argument is supported when setting sticker filters; text filters keep their legacy response syntax.
    row = await db_session.scalar(select(GroupFilter).where(GroupFilter.group_id == message.chat.id, GroupFilter.keyword == keyword))
    if row:
        row.response = response
        row.response_type = response_type
        row.target_user_id = target_user_id
        row.target_username = target_username
    else:
        db_session.add(GroupFilter(group_id=message.chat.id, keyword=keyword, response=response, response_type=response_type, target_user_id=target_user_id, target_username=target_username))
    await db_session.flush()
    target_text = (f" for @{escape(target_username)}" if target_username else (f" for user <code>{target_user_id}</code>" if target_user_id else " for everyone"))
    kind = "sticker" if response_type == "sticker" else "reply"
    await message.reply(f"✅ {kind.title()} filter saved for <code>{escape(keyword)}</code>{target_text}.", parse_mode="HTML")


@router.message(Command("stopfilter"))
async def remove_filter(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /stopfilter <keyword>"); return
    keyword = parts[1].strip().lower()
    await db_session.execute(delete(GroupFilter).where(GroupFilter.group_id == message.chat.id, GroupFilter.keyword == keyword))
    await message.reply("✅ Filter removed (if it existed).")

async def _resolve_member_target(message: Message, db_session: AsyncSession, token: str | None = None):
    """Resolve a target from a replied-to user or an explicit ID/known username."""
    replied = message.reply_to_message
    replied_user = getattr(replied, "from_user", None) if replied else None
    if replied_user is not None:
        return replied_user.id, replied_user.username, replied_user.first_name or "User"

    for entity in (message.entities or []):
        if entity.type == "text_mention" and entity.user:
            mentioned = entity.user
            return mentioned.id, mentioned.username, mentioned.first_name or "User"

    if not token:
        return None, None, None
    token = token.strip().split("@", 1)[0] if token.startswith("/") else token.strip()
    if token.startswith("@"):
        username = token[1:].lstrip("@").lower()
        target = await db_session.scalar(
            select(User).where(func.lower(User.username) == username)
        ) if username else None
        return (target.id, target.username, target.first_name or "User") if target else (None, None, None)
    try:
        uid = int(token)
    except ValueError:
        username = token.lstrip("@").lower()
        target = await db_session.scalar(
            select(User).where(func.lower(User.username) == username)
        ) if username else None
        return (target.id, target.username, target.first_name or "User") if target else (None, None, None)
    if uid <= 0:
        return None, None, None
    target = await db_session.get(User, uid)
    return uid, target.username if target else None, (target.first_name if target else f"User {uid}")


@router.message(Command("approve"))
async def approve_stickers(message: Message, db_session: AsyncSession, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /approve in a group."); return
    try:
        admin = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if admin.status not in ("administrator", "creator"):
            await message.reply("Only group admins or the group owner can approve stickers."); return
    except Exception:
        await message.reply("Could not verify admin permissions."); return
    parts = (message.text or "").split()
    target_id, username, name = await _resolve_member_target(message, db_session, parts[1] if len(parts) > 1 else None)
    if not target_id:
        await message.reply("Reply to a member with /approve, or use /approve @username /approve 123456789."); return
    target = await db_session.get(User, target_id)
    if not target:
        target = User(id=target_id, telegram_id=target_id, username=username, first_name=name or f"User {target_id}")
        db_session.add(target)
        await db_session.flush()
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target_id))
    if not member:
        member = GroupMember(group_id=message.chat.id, user_id=target_id)
        db_session.add(member)
    member.approved_sticker = True
    await message.reply(f'✅ Stickers approved for <a href="tg://user?id={target_id}">{escape(name or str(target_id))}</a>. Other locks remain active.', parse_mode="HTML")


@router.message(Command("unapprove"))
async def unapprove_stickers(message: Message, db_session: AsyncSession, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /unapprove in a group."); return
    try:
        admin = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if admin.status not in ("administrator", "creator"):
            await message.reply("Only group admins or the group owner can remove sticker approval."); return
    except Exception:
        await message.reply("Could not verify admin permissions."); return
    parts = (message.text or "").split()
    target_id, _, _ = await _resolve_member_target(message, db_session, parts[1] if len(parts) > 1 else None)
    if not target_id:
        await message.reply("Reply to a member with /unapprove, or use /unapprove @username /unapprove 123456789."); return
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target_id))
    if member:
        member.approved_sticker = False
    await message.reply("✅ Sticker approval removed.")


@router.message(Command("save"))
async def save_message(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not message.reply_to_message:
        await message.reply("Reply to a text message with /save <name>."); return
    content = message.reply_to_message.text or message.reply_to_message.caption
    if not content:
        await message.reply("Only text and captions can be saved currently."); return
    name = parts[1].strip().lower()[:64]
    row = await db_session.scalar(select(SavedMessage).where(SavedMessage.group_id == message.chat.id, SavedMessage.name == name))
    if row:
        row.content = content; row.created_by = message.from_user.id
    else:
        db_session.add(SavedMessage(group_id=message.chat.id, name=name, content=content, created_by=message.from_user.id))
    await db_session.flush(); await message.reply(f"✅ Saved <code>{escape(name)}</code>.", parse_mode="HTML")

@router.message(Command("get"))
async def get_saved_message(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /get <name>"); return
    name = parts[1].strip().lower()
    row = await db_session.scalar(select(SavedMessage).where(SavedMessage.group_id == message.chat.id, SavedMessage.name == name))
    if not row:
        await message.reply("Saved message not found."); return
    await message.reply(row.content, parse_mode=None)

@router.message(Command("clear"))
async def clear_saved_message(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /clear <saved name>"); return
    name = parts[1].strip().lower()
    await db_session.execute(delete(SavedMessage).where(SavedMessage.group_id == message.chat.id, SavedMessage.name == name))
    await message.reply("Saved message cleared (if it existed).")

@router.message(Command("blocklist"))
async def show_blocklist(message: Message, db_session: AsyncSession, **kwargs):
    rows = (await db_session.execute(select(GroupBlocklist).where(GroupBlocklist.group_id == message.chat.id).order_by(GroupBlocklist.value.asc()))).scalars().all()
    await message.reply("🚫 <b>Blocklist</b>\n\n" + ("\n".join(f"• <code>{escape(row.value)}</code>" for row in rows) if rows else "Empty."), parse_mode="HTML")

@router.message(Command("addblocklist"))
async def add_blocklist(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /addblocklist <word or phrase>"); return
    value = parts[1].strip().lower()[:128]
    existing = await db_session.scalar(select(GroupBlocklist).where(GroupBlocklist.group_id == message.chat.id, GroupBlocklist.value == value))
    if not existing:
        db_session.add(GroupBlocklist(group_id=message.chat.id, value=value, added_by=message.from_user.id))
    await message.reply("✅ Blocklist entry saved.")

@router.message(Command("rmblocklist"))
async def remove_blocklist(message: Message, db_session: AsyncSession, **kwargs):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /rmblocklist <word or phrase>"); return
    value = parts[1].strip().lower()
    await db_session.execute(delete(GroupBlocklist).where(GroupBlocklist.group_id == message.chat.id, GroupBlocklist.value == value))
    await message.reply("Blocklist entry removed (if it existed).")
