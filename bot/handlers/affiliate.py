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


def _owner_request_kb(request_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"owner:affiliate:approve:{request_id}"),
         InlineKeyboardButton(text="❌ Reject", callback_data=f"owner:affiliate:reject:{request_id}")],
        [InlineKeyboardButton(text="💬 Reply to User", callback_data=f"owner:affiliate:reply:{request_id}")],
        [InlineKeyboardButton(text="🎁 Referral Board", callback_data="owner:referrals")],
    ])


async def _edit(callback: CallbackQuery, text: str, reply_markup=None):
    msg = callback.message
    if msg.photo:
        await msg.edit_caption(text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def _referral_count(session: AsyncSession, user_id: int) -> int:
    value = await session.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user_id))
    return int(value or 0)


async def _stars_balance(session: AsyncSession, user_id: int, referral_count: int) -> int:
    earned = 100 if referral_count >= 200 else 50 if referral_count >= 100 else 0
    used = await session.scalar(select(func.coalesce(func.sum(AffiliateRequest.requested_stars), 0)).where(
        AffiliateRequest.user_id == user_id,
        AffiliateRequest.request_type == "stars",
        AffiliateRequest.status == "approved",
    ))
    return max(0, earned - int(used or 0))


async def _request_text(session: AsyncSession, req: AffiliateRequest) -> str:
    user = await session.get(User, req.user_id)
    username = f"@{user.username}" if user and user.username else (escape(user.first_name) if user else "Unknown")
    amount = f"\nRequested: <b>{req.requested_stars} Stars</b>" if req.request_type == "stars" else "\nRequested: <b>Telegram Premium gift</b>"
    return (f"🎁 <b>Affiliate Request #{req.id}</b>\n\nUser: {username}\n"
            f"User ID: <code>{req.user_id}</code>{amount}\nStatus: <b>{req.status.upper()}</b>\n"
            f"Submitted: {req.created_at or 'just now'}")


@router.callback_query(F.data == "affiliate:open")
async def affiliate_open(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    count = await _referral_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, count)
    me = await callback.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{db_user.id}" if me.username else "Invite link unavailable"
    text = (
        "🎁 <b>Referral Program — Get Stars Plus Premium</b>\n\n"
        f"👥 Unique tracked referrals: <b>{count}</b>\n⭐ Available Stars reward: <b>{balance}</b>\n\n"
        "<b>Reward milestones</b>\n• 100 unique referrals = 50 Stars\n"
        "• 200 unique referrals = 100 Stars total\n"
        f"• Minimum withdrawal = {MIN_WITHDRAWAL} Stars\n\n"
        "👑 Telegram Premium requests are sent to the owner for review; delivery is not automatic.\n\n"
        f"🔗 <b>Your invite link</b>\n<code>{escape(link)}</code>\n\n"
        "A unique bot start can be tracked, but it does not prove the account belongs to a real human."
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
    await _edit(callback, f"🔗 <b>Your personal invite link</b>\n\n<code>{escape(link)}</code>\n\nUnique first-time bot starts are attributed to your account.", kb)
    await callback.answer()


@router.callback_query(F.data == "affiliate:stats")
async def affiliate_stats(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    count = await _referral_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, count)
    await _edit(callback, "📊 <b>Your Referral Statistics</b>\n\n"
                f"Unique tracked bot starts: <b>{count}</b>\nAvailable reward balance: <b>{balance} Stars</b>\n\n"
                "The bot cannot independently verify that each account is a real person.", _user_kb())
    await callback.answer()


@router.callback_query(F.data == "affiliate:stars")
async def affiliate_stars(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    count = await _referral_count(db_session, db_user.id)
    balance = await _stars_balance(db_session, db_user.id, count)
    if balance < MIN_WITHDRAWAL:
        await callback.answer(f"Balance: {balance} Stars; {MIN_WITHDRAWAL - balance} more needed.", show_alert=True)
        return
    pending = await db_session.scalar(select(AffiliateRequest.id).where(
        AffiliateRequest.user_id == db_user.id, AffiliateRequest.request_type == "stars",
        AffiliateRequest.status == "pending"))
    if pending:
        await callback.answer("You already have a pending Stars request.", show_alert=True)
        return
    req = AffiliateRequest(user_id=db_user.id, request_type="stars", requested_stars=balance, status="pending")
    db_session.add(req)
    await db_session.flush()
    await callback.message.answer(f"✅ Stars withdrawal request #{req.id} submitted. Status: PENDING.")
    owner_id = settings.effective_owner_id
    if owner_id:
        try:
            await callback.bot.send_message(owner_id, await _request_text(db_session, req), parse_mode="HTML", reply_markup=_owner_request_kb(req.id))
        except Exception:
            pass
    await callback.answer("Request submitted.")


@router.callback_query(F.data == "affiliate:premium")
async def affiliate_premium(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    pending = await db_session.scalar(select(AffiliateRequest.id).where(
        AffiliateRequest.user_id == db_user.id, AffiliateRequest.request_type == "premium",
        AffiliateRequest.status == "pending"))
    if pending:
        await callback.answer("You already have a pending Premium request.", show_alert=True)
        return
    req = AffiliateRequest(user_id=db_user.id, request_type="premium", status="pending")
    db_session.add(req)
    await db_session.flush()
    await callback.message.answer(f"👑 Premium reward request #{req.id} submitted for owner review. Status: PENDING.")
    owner_id = settings.effective_owner_id
    if owner_id:
        try:
            await callback.bot.send_message(owner_id, await _request_text(db_session, req), parse_mode="HTML", reply_markup=_owner_request_kb(req.id))
        except Exception:
            pass
    await callback.answer("Request submitted.")


@router.callback_query(F.data == "affiliate:requests")
async def affiliate_requests(callback: CallbackQuery, db_session: AsyncSession, db_user=None, **kwargs):
    if not db_user:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    rows = (await db_session.execute(select(AffiliateRequest).where(
        AffiliateRequest.user_id == db_user.id).order_by(AffiliateRequest.created_at.desc()).limit(10))).scalars().all()
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
async def owner_referrals(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    users = (await db_session.execute(select(User).order_by(User.id.desc()).limit(30))).scalars().all()
    pending_count = int(await db_session.scalar(select(func.count(AffiliateRequest.id)).where(AffiliateRequest.status == "pending")) or 0)
    lines = ["🎁 <b>Affiliate / Referral — Owner Only</b>", f"Pending requests: <b>{pending_count}</b>", "", "<b>Recent members</b>"]
    rows = []
    for user in users:
        count = await _referral_count(db_session, user.id)
        label = f"@{user.username}" if user.username else user.first_name
        lines.append(f"• {escape(label)} · ID <code>{user.id}</code> · tracked invites: <b>{count}</b>")
        rows.append([InlineKeyboardButton(text=f"👤 {label[:22]} · {count}", callback_data=f"owner:affiliate:member:{user.id}")])
    requests = (await db_session.execute(select(AffiliateRequest).where(
        AffiliateRequest.status == "pending").order_by(AffiliateRequest.created_at.asc()).limit(10))).scalars().all()
    for req in requests:
        user = await db_session.get(User, req.user_id)
        label = (user.username or user.first_name) if user else str(req.user_id)
        rows.append([InlineKeyboardButton(text=f"⏳ #{req.id} {label[:18]} · {req.request_type}", callback_data=f"owner:affiliate:view:{req.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Owner Panel", callback_data="owner:menu")])
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:affiliate:member:"))
async def owner_member_detail(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    user = await db_session.get(User, user_id)
    if not user:
        await callback.answer("Member not found.", show_alert=True)
        return
    refs = (await db_session.execute(select(Referral).where(Referral.referrer_id == user_id).order_by(Referral.created_at.desc()).limit(50))).scalars().all()
    count = await _referral_count(db_session, user_id)
    balance = await _stars_balance(db_session, user_id, count)
    label = f"@{user.username}" if user.username else user.first_name
    lines = [f"👤 <b>Referral record: {escape(label)}</b>", f"User ID: <code>{user.id}</code>",
             f"Tracked unique referrals: <b>{count}</b>", f"Calculated Stars balance: <b>{balance}</b>", "", "<b>Recent referred accounts</b>"]
    for ref in refs:
        referred = await db_session.get(User, ref.referred_id)
        name = f"@{referred.username}" if referred and referred.username else (referred.first_name if referred else str(ref.referred_id))
        lines.append(f"• {escape(name)} · <code>{ref.referred_id}</code> · {ref.created_at or 'time unavailable'}")
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Referral Board", callback_data="owner:referrals")],
        [InlineKeyboardButton(text="👑 Owner Panel", callback_data="owner:menu")]]))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:affiliate:view:"))
async def owner_view_request(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, int(callback.data.rsplit(":", 1)[1]))
    if not req:
        await callback.answer("Request not found.", show_alert=True)
        return
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req.id))
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
        await callback.bot.send_message(req.user_id, f"✅ Your {label} request #{req.id} was approved. Delivery must be arranged using the supported Telegram Stars/gift process.")
    except Exception:
        pass
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req.id))
    await callback.answer("Approved.")


@router.callback_query(F.data.startswith("owner:affiliate:reject:"))
async def owner_reject(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    req = await db_session.get(AffiliateRequest, int(callback.data.rsplit(":", 1)[1]))
    if not req or req.status != "pending":
        await callback.answer("Request missing or already processed.", show_alert=True)
        return
    req.status = "rejected"
    await db_session.flush()
    try:
        await callback.bot.send_message(req.user_id, f"❌ Your reward request #{req.id} was rejected.")
    except Exception:
        pass
    await _edit(callback, await _request_text(db_session, req), _owner_request_kb(req.id))
    await callback.answer("Rejected.")


@router.callback_query(F.data.startswith("owner:affiliate:reply:"))
async def owner_reply_start(callback: CallbackQuery, state: FSMContext, **kwargs):
    if not _owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    request_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(affiliate_request_id=request_id)
    await state.set_state(AffiliateStates.waiting_reply)
    await callback.message.answer(f"Type your reply for request #{request_id}. It will be sent by private DM. Send /cancel to stop.")
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
        await message.bot.send_message(req.user_id, "💬 <b>Message from Alexa Bot owner</b>\n\n" + escape(message.text), parse_mode="HTML")
        req.admin_reply = message.text
        await db_session.flush()
        await message.answer(f"✅ Reply sent to user for request #{req.id}.")
    except Exception as exc:
        await message.answer("Could not send the DM. The user may have blocked the bot or may not have started it.")
    await state.clear()
