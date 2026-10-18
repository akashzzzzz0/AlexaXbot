from html import escape
from urllib.parse import quote

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import User, Referral, AffiliateRequest

router = Router()
MIN_WITHDRAWAL = 100
USER_PAGE_SIZE = 10
REFERRAL_PAGE_SIZE = 10


class AffiliateStates(StatesGroup):
    waiting_reply = State()


def _owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def _user_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 My Invite Link", callback_data="affiliate:link"),
         InlineKeyboardButton(text="📊 My Referrals", callback_data="affiliate:stats")],
        [InlineKeyboardButton(text="⭐ Withdraw Stars", callback_data="affiliate:stars"),
         InlineKeyboardButton(text="👑 Get Telegram Premium", callback_data="affiliate:premium")],
        [InlineKeyboardButton(text="📋 My Requests", callback_data="affiliate:requests"),
         InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
    ])


def _owner_request_kb(req: AffiliateRequest):
    rows = []
    if req.status == "pending":
        rows.append([
            InlineKeyboardButton(text="✅ Approve", callback_data=f"owner:affiliate:approve:{req.id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"owner:affiliate:reject:{req.id}"),
        ])
    if req.status == "approved":
        rows.append([InlineKeyboardButton(text="📦 Mark Paid / Delivered", callback_data=f"owner:affiliate:paid:{req.id}")])
    rows.append([InlineKeyboardButton(text="💬 Reply to User", callback_data=f"owner:affiliate:reply:{req.id}")])
    rows.append([InlineKeyboardButton(text="🎁 Referral Board", callback_data="owner:referrals:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(callback: CallbackQuery, text: str, reply_markup=None):
    msg = callback.message
    if msg.photo:
        await msg.edit_caption(text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def _tracked_count(session: AsyncSession, user_id: int) -> int:
    value = await session.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user_id))
    return int(value or 0)


async def _valid_count(session: AsyncSession, user_id: int) -> int:
    # "Valid" means a unique, non-bot account that has opened the bot in DM.
    # Telegram cannot prove that an account belongs to a real human.
    value = await session.scalar(
        select(func.count(Referral.id))
        .join(User, User.id == Referral.referred_id)
        .where(
            Referral.referrer_id == user_id,
            User.is_bot.is_(False),
            User.dm_started.is_(True),
        )
    )
    return int(value or 0)


def _earned_stars(valid_count: int) -> int:
    # 200 referrals means 100 Stars total, not an additional 100.
    return 100 if valid_count >= 200 else 50 if valid_count >= 100 else 0


async def _stars_balance(session: AsyncSession, user_id: int, valid_count: int) -> int:
    earned = _earned_stars(valid_count)
    reserved = await session.scalar(
        select(func.coalesce(func.sum(AffiliateRequest.requested_stars), 0)).where(
            AffiliateRequest.user_id == user_id,
            AffiliateRequest.request_type == "stars",
            AffiliateRequest.status.in_(("approved", "paid")),
        )
    )
    return max(0, earned - int(reserved or 0))


async def _request_text(session: AsyncSession, req: AffiliateRequest) -> str:
    user = await session.get(User, req.user_id)
    username = f"@{user.username}" if user and user.username else (escape(user.first_name) if user else "Unknown")
    amount = f"\nRequested: <b>{req.requested_stars} Stars</b>" if req.request_type == "stars" else "\nRequested: <b>Telegram Premium gift</b>"
    return (
        f"🎁 <b>Affiliate Request #{req.id}</b>\n\n"
        f"User: {username}\nUser ID: <code>{req.user_id}</code>{amount}\n"
        f"Status: <b>{req.status.upper()}</b>\n"
        f"Submitted: {req.created_at or 'just now'}"
    )


async def _notify_owner(callback: CallbackQuery, session: AsyncSession, req: AffiliateRequest):
    owner_id = settings.effective_owner_id
    if not owner_id:
        return
    try:
        await callback.bot.send_message(
            owner_id, await _request_text(session, req),
            parse_mode="HTML", reply_markup=_owner_request_kb(req),
        )
    except Exception:
        # The request remains stored; owner can find it in the owner referral board.
        pass


@router.callback_query(F.data == "affiliate:open")
async def affiliate_open(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    tracked = await _tracked_count(db_session, db_user.id)
    valid = await _valid_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, valid)
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{db_user.id}" if me.username else "Invite link unavailable"
    text = (
        "🎁 <b>Referral Program — Get Stars Plus Premium</b>\n\n"
        f"👥 Tracked invites: <b>{tracked}</b>\n"
        f"✅ Valid unique DM starts: <b>{valid}</b>\n"
        f"⭐ Eligible Stars reward balance: <b>{balance}</b>\n\n"
        "<b>Reward milestones</b>\n"
        "• 100 valid referrals = 50 Stars\n"
        "• 200 valid referrals = 100 Stars total\n"
        f"• Minimum Stars withdrawal request = {MIN_WITHDRAWAL} Stars\n\n"
        "👑 Telegram Premium requests go to the owner for review.\n"
        "Payout/gifting is not automatic; the owner must arrange supported delivery.\n\n"
        f"🔗 <b>Your personal invite link</b>\n<code>{escape(link)}</code>\n\n"
        "Valid means a unique non-bot account that started the bot in DM. This does not prove a real human."
    )
    await _edit(callback, text, _user_kb())
    await callback.answer()


@router.callback_query(F.data == "affiliate:link")
async def affiliate_link(callback: CallbackQuery, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    me = await callback.bot.get_me()
    if not me.username:
        await callback.answer("Invite link unavailable.", show_alert=True)
        return
    link = f"https://t.me/{me.username}?start=ref_{db_user.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Share invite link", url="https://t.me/share/url?url=" + quote(link))],
        [InlineKeyboardButton(text="◀️ Referral Dashboard", callback_data="affiliate:open")],
    ])
    await _edit(callback, f"🔗 <b>Your personal invite link</b>\n\n<code>{escape(link)}</code>\n\nEach user is attributed only once. Valid referrals are unique non-bot accounts that start the bot in DM.", kb)
    await callback.answer()


@router.callback_query(F.data == "affiliate:stats")
async def affiliate_stats(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    tracked = await _tracked_count(db_session, db_user.id)
    valid = await _valid_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, valid)
    await _edit(
        callback,
        "📊 <b>Your Referral Statistics</b>\n\n"
        f"Tracked unique invites: <b>{tracked}</b>\n"
        f"Valid unique DM starts: <b>{valid}</b>\n"
        f"Eligible Stars reward balance: <b>{balance}</b>\n\n"
        "Telegram does not provide a reliable way for the bot to prove an account belongs to a real human.",
        _user_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "affiliate:stars")
async def affiliate_stars(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    valid = await _valid_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, valid)
    if balance < MIN_WITHDRAWAL:
        await callback.answer(
            f"Eligible balance: {balance} Stars. You need {MIN_WITHDRAWAL - balance} more Stars.",
            show_alert=True,
        )
        return
    pending = await db_session.scalar(select(AffiliateRequest.id).where(
        AffiliateRequest.user_id == db_user.id,
        AffiliateRequest.request_type == "stars",
        AffiliateRequest.status.in_(("pending", "approved")),
    ))
    if pending:
        await callback.answer("You already have an open Stars request.", show_alert=True)
        return
    req = AffiliateRequest(user_id=db_user.id, request_type="stars", requested_stars=balance, status="pending")
    db_session.add(req)
    await db_session.flush()
    await callback.message.answer(f"✅ Stars withdrawal request #{req.id} submitted. Status: PENDING.")
    await _notify_owner(callback, db_session, req)
    await callback.answer("Request submitted.")


@router.callback_query(F.data == "affiliate:premium")
async def affiliate_premium(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    pending = await db_session.scalar(select(AffiliateRequest.id).where(
        AffiliateRequest.user_id == db_user.id,
        AffiliateRequest.request_type == "premium",
        AffiliateRequest.status.in_(("pending", "approved")),
    ))
    if pending:
        await callback.answer("You already have an open Premium request.", show_alert=True)
        return
    req = AffiliateRequest(user_id=db_user.id, request_type="premium", status="pending")
    db_session.add(req)
    await db_session.flush()
    await callback.message.answer(f"👑 Telegram Premium reward request #{req.id} submitted. Status: PENDING.")
    await _notify_owner(callback, db_session, req)
    await callback.answer("Request submitted.")


@router.callback_query(F.data == "affiliate:requests")
async def affiliate_requests(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    rows = (await db_session.execute(select(AffiliateRequest).where(
        AffiliateRequest.user_id == db_user.id
    ).order_by(AffiliateRequest.created_at.desc(), AffiliateRequest.id.desc()).limit(10))).scalars().all()
    lines = ["📋 <b>Your Affiliate Requests</b>"]
    if not rows:
        lines.append("\nNo requests yet.")
    for req in rows:
        label = f"{req.requested_stars} Stars" if req.request_type == "stars" else "Telegram Premium"
        lines.append(f"\n#{req.id} · {label} · <b>{req.status.upper()}</b>")
        if req.admin_reply:
            lines.append(f"\nAdmin reply: {escape(req.admin_reply)}")
    await _edit(callback, "\n".join(lines), _user_kb())
    await callback.answer()


@router.callback_query(F.data == "owner:referrals")
@router.callback_query(F.data.startswith("owner:referrals:page:"))
async def owner_referrals(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1]) if callback.data != "owner:referrals" else 0
    except ValueError:
        page = 0
    page = max(0, page)
    total_users = int(await db_session.scalar(select(func.count(User.id)).where(User.is_bot.is_(False))) or 0)
    total_pages = max(1, (total_users + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE)
    page = min(page, total_pages - 1)
    users = (await db_session.execute(
        select(User).where(User.is_bot.is_(False)).order_by(User.id.desc())
        .offset(page * USER_PAGE_SIZE).limit(USER_PAGE_SIZE)
    )).scalars().all()
    pending_count = int(await db_session.scalar(select(func.count(AffiliateRequest.id)).where(AffiliateRequest.status == "pending")) or 0)
    lines = [
        "🎁 <b>Affiliate / Referral — Owner Only</b>",
        f"Pending requests: <b>{pending_count}</b>",
        f"Member page: <b>{page + 1}/{total_pages}</b>",
        "",
        "<b>Members: tracked / valid referrals</b>",
    ]
    rows = []
    for user in users:
        tracked = await _tracked_count(db_session, user.id)
        valid = await _valid_count(db_session, user.id)
        label = f"@{user.username}" if user.username else user.first_name
        lines.append(f"• {escape(label)} · ID <code>{user.id}</code> · <b>{tracked}/{valid}</b>")
        rows.append([InlineKeyboardButton(
            text=f"👤 {label[:20]} · {tracked}/{valid}",
            callback_data=f"owner:affiliate:member:{user.id}:0",
        )])
    rows.append([InlineKeyboardButton(
        text=f"📥 Review Requests ({pending_count})",
        callback_data="owner:affiliate:requests:page:0",
    )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"owner:referrals:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"owner:referrals:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Owner Panel", callback_data="owner:menu")])
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:affiliate:member:"))
async def owner_member_detail(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    parts = callback.data.split(":")
    try:
        user_id, page = int(parts[-2]), max(0, int(parts[-1]))
    except (ValueError, IndexError):
        await callback.answer("Invalid member.", show_alert=True)
        return
    user = await db_session.get(User, user_id)
    if not user:
        await callback.answer("Member not found.", show_alert=True)
        return
    tracked = await _tracked_count(db_session, user_id)
    valid = await _valid_count(db_session, user_id)
    balance = await _stars_balance(db_session, user_id, valid)
    label = f"@{user.username}" if user.username else user.first_name
    refs = (await db_session.execute(
        select(Referral).where(Referral.referrer_id == user_id)
        .order_by(Referral.created_at.desc(), Referral.id.desc())
        .offset(page * REFERRAL_PAGE_SIZE).limit(REFERRAL_PAGE_SIZE)
    )).scalars().all()
    lines = [
        f"👤 <b>Referral record: {escape(label)}</b>",
        f"User ID: <code>{user.id}</code>",
        f"Tracked invites: <b>{tracked}</b>",
        f"Valid unique DM starts: <b>{valid}</b>",
        f"Eligible Stars reward balance: <b>{balance}</b>",
        f"Referred-user page: <b>{page + 1}/{max(1, (tracked + REFERRAL_PAGE_SIZE - 1) // REFERRAL_PAGE_SIZE)}</b>",
        "", "<b>Referred accounts</b>",
    ]
    for ref in refs:
        referred = await db_session.get(User, ref.referred_id)
        name = f"@{referred.username}" if referred and referred.username else (referred.first_name if referred else str(ref.referred_id))
        is_valid = bool(referred and not referred.is_bot and referred.dm_started)
        lines.append(f"• {escape(name)} · ID <code>{ref.referred_id}</code> · {'✅ valid' if is_valid else '⏳ not valid yet'}")
    total_pages = max(1, (tracked + REFERRAL_PAGE_SIZE - 1) // REFERRAL_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"owner:affiliate:member:{user_id}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"owner:affiliate:member:{user_id}:{page + 1}"))
    rows = [nav] if nav else []
    rows += [
        [InlineKeyboardButton(text="◀️ Referral Board", callback_data="owner:referrals:page:0")],
        [InlineKeyboardButton(text="👑 Owner Panel", callback_data="owner:menu")],
    ]
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data == "owner:affiliate:requests")
@router.callback_query(F.data.startswith("owner:affiliate:requests:page:"))
async def owner_request_queue(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1]) if callback.data != "owner:affiliate:requests" else 0
    except ValueError:
        page = 0
    page = max(0, page)
    query = select(AffiliateRequest).where(AffiliateRequest.status.in_(("pending", "approved"))).order_by(
        AffiliateRequest.created_at.asc(), AffiliateRequest.id.asc()
    )
    total = int(await db_session.scalar(select(func.count(AffiliateRequest.id)).where(
        AffiliateRequest.status.in_(("pending", "approved"))
    )) or 0)
    total_pages = max(1, (total + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE)
    page = min(page, total_pages - 1)
    requests = (await db_session.execute(query.offset(page * USER_PAGE_SIZE).limit(USER_PAGE_SIZE))).scalars().all()
    lines = [
        "📥 <b>Affiliate Requests — Owner Only</b>",
        f"Open requests: <b>{total}</b>",
        f"Page: <b>{page + 1}/{total_pages}</b>",
        "",
    ]
    rows = []
    for req in requests:
        user = await db_session.get(User, req.user_id)
        label = (user.username or user.first_name) if user else str(req.user_id)
        reward = f"{req.requested_stars} Stars" if req.request_type == "stars" else "Premium"
        lines.append(f"• #{req.id} · {escape(label)} · {reward} · <b>{req.status.upper()}</b>")
        rows.append([InlineKeyboardButton(
            text=f"#{req.id} {label[:16]} · {req.status}",
            callback_data=f"owner:affiliate:view:{req.id}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"owner:affiliate:requests:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"owner:affiliate:requests:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="◀️ Referral Board", callback_data="owner:referrals:page:0")],
        [InlineKeyboardButton(text="👑 Owner Panel", callback_data="owner:menu")],
    ]
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:affiliate:view:"))
async def owner_view_request(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, request_id)
    if not req:
        await callback.answer("Request not found.", show_alert=True)
        return
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:affiliate:approve:"))
async def owner_approve(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, int(callback.data.rsplit(":", 1)[1]))
    if not req or req.status != "pending":
        await callback.answer("Request missing or already processed.", show_alert=True)
        return
    req.status = "approved"
    await db_session.flush()
    try:
        label = f"{req.requested_stars} Stars withdrawal" if req.request_type == "stars" else "Telegram Premium reward"
        await callback.bot.send_message(
            req.user_id,
            f"✅ Your {label} request #{req.id} was approved. Delivery is still pending until the owner completes the supported payout/gift process."
        )
    except Exception:
        pass
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req))
    await callback.answer("Approved. Mark as paid/delivered after completing delivery.")


@router.callback_query(F.data.startswith("owner:affiliate:reject:"))
async def owner_reject(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, int(callback.data.rsplit(":", 1)[1]))
    if not req or req.status not in ("pending", "approved"):
        await callback.answer("Request missing or already processed.", show_alert=True)
        return
    req.status = "rejected"
    await db_session.flush()
    try:
        await callback.bot.send_message(req.user_id, f"❌ Your reward request #{req.id} was rejected.")
    except Exception:
        pass
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req))
    await callback.answer("Rejected.")


@router.callback_query(F.data.startswith("owner:affiliate:paid:"))
async def owner_mark_paid(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, int(callback.data.rsplit(":", 1)[1]))
    if not req or req.status != "approved":
        await callback.answer("Only approved requests can be marked paid/delivered.", show_alert=True)
        return
    req.status = "paid"
    await db_session.flush()
    try:
        label = f"{req.requested_stars} Stars withdrawal" if req.request_type == "stars" else "Telegram Premium gift"
        await callback.bot.send_message(req.user_id, f"🎉 Your {label} request #{req.id} has been marked as paid/delivered by the owner.")
    except Exception:
        pass
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req))
    await callback.answer("Marked paid/delivered.")


@router.callback_query(F.data.startswith("owner:affiliate:reply:"))
async def owner_reply_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, request_id)
    if not req:
        await callback.answer("Request not found.", show_alert=True)
        return
    await state.update_data(affiliate_request_id=request_id)
    await state.set_state(AffiliateStates.waiting_reply)
    await callback.message.answer(f"Type your reply for request #{request_id}. It will be sent privately to the requester. Send /cancel to stop.")
    await callback.answer()


@router.message(AffiliateStates.waiting_reply, F.text)
async def owner_reply_send(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not _owner(message.from_user.id):
        await state.clear()
        return
    if message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Reply cancelled.")
        return
    data = await state.get_data()
    req = await db_session.get(AffiliateRequest, int(data.get("affiliate_request_id", 0)))
    if not req:
        await state.clear()
        await message.answer("Request not found.")
        return
    try:
        await message.bot.send_message(
            req.user_id,
            "💬 <b>Message from Alexa Bot owner</b>\n\n" + escape(message.text),
            parse_mode="HTML",
        )
        req.admin_reply = message.text
        await db_session.flush()
        await message.answer(f"✅ Reply sent privately to the user for request #{req.id}.")
    except Exception:
        await message.answer("Could not send the DM. The user may have blocked the bot.")
    await state.clear()
