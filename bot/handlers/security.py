from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from bot.database.models import Group, SecuritySettings, GroupMember, User
from bot.keyboards.security import security_menu_kb, locks_menu_kb, captcha_menu_kb
import structlog
from bot.middlewares.admin_actions import AdminActionMiddleware

logger = structlog.get_logger()
router = Router()
router.message.outer_middleware(AdminActionMiddleware(commands={"security", "lock", "unlock", "locks", "locktypes", "setflood", "flood", "setfloodmode", "clearflood", "approve", "unapprove"}))
router.callback_query.outer_middleware(AdminActionMiddleware(prefixes=("sec:", "lock:", "captcha:")))


async def _get_or_create_security(db_session: AsyncSession, chat_id: int) -> SecuritySettings:
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == chat_id))
    if not security:
        security = SecuritySettings(group_id=chat_id)
        db_session.add(security)
        await db_session.flush()
    return security


async def _is_admin(message_or_callback, _) -> bool:
    obj = message_or_callback
    chat = obj.chat if hasattr(obj, 'chat') else obj.message.chat
    user = obj.from_user
    if chat.type == 'private':
        return False
    try:
        member = await obj.bot.get_chat_member(chat.id, user.id)
        if member.status not in ('administrator', 'creator'):
            return False
    except Exception:
        return False
    return True


#  /security 
@router.message(Command('locks', 'locktypes'))
async def cmd_locks(message: Message, _: callable, db_session: AsyncSession = None, **kwargs):
    if message.chat.type == 'private':
        await message.answer(_('error_admin_only'))
        return
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ('administrator', 'creator'):
            await message.answer(_('error_admin_only'))
            return
    except Exception:
        return
    security = await _get_or_create_security(db_session, message.chat.id) if db_session else None
    if security:
        await message.answer(_('locks_menu'), reply_markup=locks_menu_kb(_, security), parse_mode='HTML')


@router.message(Command('setflood'))
async def cmd_setflood(message: Message, _: callable, db_session: AsyncSession = None, **kwargs):
    if message.chat.type == 'private' or not db_session:
        await message.reply(_('error_group_only')); return
    parts = (message.text or '').split()
    try:
        limit = int(parts[1]); window = int(parts[2])
        if not (1 <= limit <= 50 and 1 <= window <= 3600): raise ValueError
    except (ValueError, IndexError):
        await message.reply('Usage: /setflood <messages 1-50> <window seconds 1-3600>'); return
    security = await _get_or_create_security(db_session, message.chat.id)
    security.flood_limit = limit; security.flood_window = window
    await db_session.flush()
    await message.reply(f'✅ Flood limit set to {limit} messages per {window} seconds.')


@router.message(Command('flood'))
async def cmd_flood(message: Message, db_session: AsyncSession = None, **kwargs):
    if message.chat.type == 'private' or not db_session:
        await message.reply('Groups only.'); return
    security = await _get_or_create_security(db_session, message.chat.id)
    await message.reply(f'Flood control: {"ON" if security.anti_flood else "OFF"}\nLimit: {security.flood_limit} messages / {security.flood_window}s\nAction: {security.flood_action}')


@router.message(Command('setfloodmode'))
async def cmd_setfloodmode(message: Message, db_session: AsyncSession = None, **kwargs):
    if message.chat.type == 'private' or not db_session:
        await message.reply('Groups only.'); return
    parts = (message.text or '').split()
    action = parts[1].lower() if len(parts) > 1 else ''
    if action not in {'delete', 'warn', 'mute', 'kick', 'ban'}:
        await message.reply('Usage: /setfloodmode delete|warn|mute|kick|ban'); return
    security = await _get_or_create_security(db_session, message.chat.id)
    security.flood_action = action
    await db_session.flush()
    await message.reply(f'✅ Flood action set to {action}.')


@router.message(Command('clearflood'))
async def cmd_clearflood(message: Message, **kwargs):
    if message.chat.type == 'private':
        await message.reply('Groups only.'); return
    from bot.services.security_service import clear_flood_state
    cleared = clear_flood_state(message.chat.id)
    await message.reply(f'✅ Cleared flood counters for {cleared} tracked user(s).')


@router.message(Command('security'))
async def cmd_security(message: Message, _: callable, db_session: AsyncSession = None, **kwargs):
    if message.chat.type == 'private':
        await message.answer(_('error_admin_only'))
        return
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ('administrator', 'creator'):
            await message.answer(_('error_admin_only'))
            return
    except Exception:
        return
    if db_session:
        security = await _get_or_create_security(db_session, message.chat.id)
        await message.answer(_('security_menu_title'), reply_markup=security_menu_kb(_, security), parse_mode='HTML')
    else:
        await message.answer(_('error_generic'))


@router.callback_query(F.data == "menu:security")
async def security_menu_cb(callback: CallbackQuery, _: callable, db_session: AsyncSession = None, **kwargs):
    if callback.message.chat.type == 'private':
        await callback.answer(_('error_admin_only'), show_alert=True)
        return
    try:
        member = await callback.bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
        if member.status not in ('administrator', 'creator'):
            await callback.answer(_('error_admin_only'), show_alert=True)
            return
    except Exception:
        await callback.answer()
        return
    if db_session:
        security = await _get_or_create_security(db_session, callback.message.chat.id)
        await callback.message.edit_text(_('security_menu_title'), reply_markup=security_menu_kb(_, security), parse_mode='HTML')
    await callback.answer()


@router.callback_query(F.data == "sec:menu")
async def security_section_back(callback: CallbackQuery, _: callable, db_session: AsyncSession = None, **kwargs):
    if db_session:
        security = await _get_or_create_security(db_session, callback.message.chat.id)
        await callback.message.edit_text(_('security_menu_title'), reply_markup=security_menu_kb(_, security), parse_mode='HTML')
    await callback.answer()


@router.callback_query(F.data.startswith('sec:'))
async def security_section(callback: CallbackQuery, _: callable, db_session: AsyncSession = None, **kwargs):
    action = callback.data.split('sec:', 1)[1]

    if not db_session:
        await callback.answer(_('error_generic'), show_alert=True)
        return

    security = await _get_or_create_security(db_session, callback.message.chat.id)

    # Toggle boolean security settings
    toggle_map = {
        'anti_spam': 'anti_spam',
        'anti_flood': 'anti_flood',
        'anti_raid': 'anti_raid',
        'anti_bot': 'anti_bot',
        'anti_fake': 'anti_fake',
        'anti_ad': 'anti_advertisement',
        'anti_link': 'anti_link',
        'anti_mention': 'anti_mention_spam',
        'anti_forward': 'anti_forward_spam',
        'anti_emoji': 'anti_emoji_spam',
        'anti_hashtag': 'anti_hashtag_spam',
        'anti_phone': 'anti_phone',
        'anti_scam': 'anti_scam',
        'anti_crypto': 'anti_crypto_scam',
        'anti_nsfw': 'anti_nsfw',
        'anti_invite': 'anti_invite',
        'anti_promo': 'anti_channel_promo',
        'anti_mass_join': 'anti_mass_join',
        'anti_mass_leave': 'anti_mass_leave',
        'anti_username': 'anti_username_spam',
        'captcha': 'captcha_enabled',
        'emergency': 'emergency_mode',
    }

    if action in toggle_map:
        field = toggle_map[action]
        current = getattr(security, field, False)
        setattr(security, field, not current)
        status = _('enabled') if not current else _('disabled')
        await callback.message.edit_reply_markup(reply_markup=security_menu_kb(_, security))
    elif action == 'locks':
        await callback.message.edit_text(_('locks_menu'), reply_markup=locks_menu_kb(_, security), parse_mode='HTML')
    elif action in ('antiflood', 'antispam', 'advanced'):
        await callback.message.edit_text('Use the Anti-spam and Anti-flood switches in this panel to configure these protections.', parse_mode='HTML')
    else:
        await callback.answer(_('feature_coming'), show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith('lock:'))
async def toggle_lock(callback: CallbackQuery, _: callable, db_session: AsyncSession = None, **kwargs):
    if not db_session:
        await callback.answer(_('error_generic'), show_alert=True)
        return
    if callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Open content locks inside the group.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can change content locks.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your group-admin permissions.", show_alert=True)
        return
    lock_type = callback.data.split('lock:', 1)[1]
    security = await _get_or_create_security(db_session, callback.message.chat.id)

    field_map = {
        'text': 'lock_text', 'links': 'lock_links', 'photos': 'lock_photos',
        'videos': 'lock_videos', 'gifs': 'lock_gifs', 'audio': 'lock_audio',
        'voice': 'lock_voice', 'docs': 'lock_documents', 'polls': 'lock_polls',
        'games': 'lock_games', 'bots': 'lock_bots', 'forwards': 'lock_forwards',
        'contacts': 'lock_contacts', 'locations': 'lock_locations',
        'stickers': 'lock_stickers', 'inline': 'lock_inline',
        'mentions': 'lock_mentions', 'hashtags': 'lock_hashtags',
    }
    if lock_type in ("all_on", "all_off"):
        enabled = lock_type == "all_on"
        for lock_field in field_map.values():
            setattr(security, lock_field, enabled)
        try:
            await db_session.commit()
        except Exception as exc:
            await db_session.rollback()
            logger.warning("Failed to save content-lock panel settings", error=str(exc), group_id=callback.message.chat.id)
            await callback.answer("Could not save lock settings. Try again.", show_alert=True)
            return
        await callback.message.edit_reply_markup(reply_markup=locks_menu_kb(_, security))
        await callback.answer("All content locks enabled." if enabled else "All content locks disabled.")
        return
    field = field_map.get(lock_type)
    if field:
        current = getattr(security, field, False)
        setattr(security, field, not current)
        try:
            await db_session.commit()
        except Exception as exc:
            await db_session.rollback()
            logger.warning("Failed to save content-lock toggle", error=str(exc), group_id=callback.message.chat.id, lock_type=lock_type)
            await callback.answer("Could not save lock setting. Try again.", show_alert=True)
            return
        status = _('enabled') if not current else _('disabled')
        await callback.message.edit_reply_markup(reply_markup=locks_menu_kb(_, security))
        await callback.answer(f"{lock_type.title()}: {status}")
    else:
        await callback.answer(_('no_data'), show_alert=True)


@router.callback_query(F.data.startswith('captcha:'))
async def toggle_captcha(callback: CallbackQuery, _: callable, db_session: AsyncSession = None, **kwargs):
    if not db_session:
        await callback.answer(_('error_generic'), show_alert=True)
        return
    parts = callback.data.split(':', 2)
    action = parts[1] if len(parts) > 1 else ''
    security = await _get_or_create_security(db_session, callback.message.chat.id)

    if action == 'toggle':
        security.captcha_enabled = not security.captcha_enabled
        status = _('enabled') if security.captcha_enabled else _('disabled')
        await callback.answer(f"Captcha: {status}")
    elif action == 'type' and len(parts) > 2:
        captcha_type = parts[2].lower()
        if captcha_type not in {"math", "button"}:
            await callback.answer("Choose Math or Button captcha.", show_alert=True)
            return
        security.captcha_type = captcha_type
        await callback.answer(f"Captcha type: {captcha_type}")
    else:
        await callback.answer(_('feature_coming'), show_alert=True)

    await callback.message.edit_reply_markup(reply_markup=captcha_menu_kb(_, security))


@router.message(Command("approve", "unapprove"))
async def cmd_sticker_approval(message: Message, db_session: AsyncSession = None, **kwargs):
    """Approve or revoke a member's exemption from the sticker lock."""
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use this command in a group.")
        return
    try:
        admin = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if admin.status not in ("administrator", "creator"):
            await message.reply("Only group admins can approve stickers.")
            return
    except Exception:
        await message.reply("I couldn't verify your admin permissions.")
        return
    if db_session is None:
        await message.reply("Database is unavailable. Please try again.")
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is None:
        parts = (message.text or "").split()
        if len(parts) > 1 and parts[1].isdigit():
            target_id = int(parts[1])
            try:
                target_chat_member = await message.bot.get_chat_member(message.chat.id, target_id)
                target = target_chat_member.user
            except Exception:
                target = None
    if target is None or target.is_bot:
        await message.reply("Reply to a member's message with /approve or /unapprove, or provide a numeric user ID.")
        return

    # GroupMember.user_id references users.id, so ensure the target exists
    # before creating or updating the sticker approval record.
    db_target = await db_session.get(User, target.id)
    if db_target is None:
        db_target = User(
            id=target.id,
            telegram_id=target.id,
            first_name=target.first_name or "User",
            last_name=target.last_name,
            username=target.username,
            is_bot=False,
        )
        db_session.add(db_target)
        try:
            await db_session.flush()
        except Exception as exc:
            await db_session.rollback()
            logger.exception("Could not create sticker-approval user", group_id=message.chat.id, user_id=target.id, error=str(exc))
            await message.reply("Couldn't save this approval because the member record could not be created.")
            return

    member = await db_session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == message.chat.id,
            GroupMember.user_id == target.id,
        )
    )
    if member is None:
        member = GroupMember(group_id=message.chat.id, user_id=target.id)
        db_session.add(member)
    approved = (message.text or "").split()[0].split("@", 1)[0].lstrip("/").lower() == "approve"
    member.approved_sticker = approved
    try:
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.exception("Failed to save sticker approval", group_id=message.chat.id, user_id=target.id, error=str(exc))
        await message.reply("Couldn't save sticker approval. Please check the bot logs.")
        return
    status = "approved to send stickers" if approved else "no longer approved to send stickers"
    await message.reply(f"✅ {target.full_name} is {status} while Sticker Lock is enabled.")


# /lock and /unlock: support all content types and persist settings.
@router.message(Command('lock', 'unlock'))
async def cmd_lock(message: Message, _: callable, db_session: AsyncSession = None, **kwargs):
    if message.chat.type not in ('group', 'supergroup'):
        await message.answer(_('error_admin_only'))
        return
    if db_session is None:
        await message.answer(_('error_generic'))
        return

    parts = (message.text or '').split()
    command = parts[0].split('@', 1)[0].lstrip('/').lower() if parts else ''
    lock_type = parts[1].lower() if len(parts) > 1 else ''
    is_locking = command == 'lock'

    field_map = {
        'text': 'lock_text',
        'link': 'lock_links', 'links': 'lock_links', 'url': 'lock_links', 'urls': 'lock_links',
        'photo': 'lock_photos', 'photos': 'lock_photos', 'image': 'lock_photos', 'images': 'lock_photos',
        'video': 'lock_videos', 'videos': 'lock_videos', 'videonote': 'lock_videos',
        'gif': 'lock_gifs', 'gifs': 'lock_gifs', 'animation': 'lock_gifs',
        'audio': 'lock_audio', 'voice': 'lock_voice',
        'document': 'lock_documents', 'documents': 'lock_documents', 'doc': 'lock_documents', 'docs': 'lock_documents',
        'poll': 'lock_polls', 'polls': 'lock_polls',
        'game': 'lock_games', 'games': 'lock_games',
        'bot': 'lock_bots', 'bots': 'lock_bots',
        'forward': 'lock_forwards', 'forwards': 'lock_forwards',
        'contact': 'lock_contacts', 'contacts': 'lock_contacts',
        'location': 'lock_locations', 'locations': 'lock_locations',
        'sticker': 'lock_stickers', 'stickers': 'lock_stickers',
        'inline': 'lock_inline', 'mention': 'lock_mentions', 'mentions': 'lock_mentions',
        'hashtag': 'lock_hashtags', 'hashtags': 'lock_hashtags',
    }
    all_fields = sorted(set(field_map.values()))
    if not lock_type:
        await message.reply('Usage: /lock <all|text|links|photos|videos|gifs|audio|voice|docs|polls|games|bots|forwards|contacts|locations|stickers|inline|mentions|hashtags>\nUse /unlock <type> to turn a lock off.')
        return

    security = await _get_or_create_security(db_session, message.chat.id)
    if lock_type == 'all':
        for field in all_fields:
            setattr(security, field, is_locking)
        label = 'all content types'
    elif lock_type in field_map:
        setattr(security, field_map[lock_type], is_locking)
        label = lock_type
    else:
        await message.reply('Unknown lock type. Use /lock all or /locks to see supported types.')
        return

    try:
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("Failed to save content-lock settings", error=str(exc), group_id=message.chat.id)
        await message.reply("❌ Could not save lock settings. Please try again.")
        return

    verb = 'locked' if is_locking else 'unlocked'
    await message.reply(f'✅ {label.title()} {verb}.')
