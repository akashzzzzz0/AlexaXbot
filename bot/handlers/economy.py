from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from bot.services.economy_service import (
    get_balance, claim_daily_reward, claim_weekly_reward, claim_monthly_reward,
    deposit_to_bank, withdraw_from_bank, transfer_coins, get_transactions
)
from bot.services.user_service import get_or_create_wallet, add_xp
from bot.keyboards.economy import economy_menu_kb, wallet_kb, rewards_menu_kb, shop_categories_kb, missions_kb
from bot.keyboards.main_menu import back_button_kb, nav_kb
from bot.utils.helpers import format_number, get_time_until
import structlog

logger = structlog.get_logger()
router = Router()


class EconomyStates(StatesGroup):
    waiting_deposit = State()
    waiting_withdraw = State()
    waiting_transfer_user = State()
    waiting_transfer_amount = State()


#  /wallet  /balance 
@router.message(Command('wallet', 'balance', 'economy', 'eco'))
async def cmd_wallet(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    balance, bank = await get_balance(db_session, db_user.id)
    wallet = await get_or_create_wallet(db_session, db_user.id)
    text = (
        _('wallet_title') + '\n\n'
        + _('balance').format(amount=format_number(balance)) + '\n'
        + _('bank').format(amount=format_number(bank)) + '\n'
        + _('total_earned').format(amount=format_number(wallet.total_earned or 0))
    )
    await message.reply(text, parse_mode='HTML')


#  /daily 
@router.message(Command('daily'))
async def cmd_daily(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    success, amount, next_claim = await claim_daily_reward(db_session, db_user.id)
    if success:
        await message.reply(_('daily_reward').format(amount=amount))
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        await message.reply(_('reward_cooldown').format(type='daily', time=time_str))


#  /weekly 
@router.message(Command('weekly'))
async def cmd_weekly(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    success, amount, next_claim = await claim_weekly_reward(db_session, db_user.id)
    if success:
        await message.reply(_('weekly_reward').format(amount=amount))
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        await message.reply(_('reward_cooldown').format(type='weekly', time=time_str))


#  /monthly 
@router.message(Command('monthly'))
async def cmd_monthly(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    success, amount, next_claim = await claim_monthly_reward(db_session, db_user.id)
    if success:
        await message.reply(_('monthly_reward').format(amount=amount))
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        await message.reply(_('reward_cooldown').format(type='monthly', time=time_str))


#  /deposit 
@router.message(Command('deposit'))
async def cmd_deposit(message: Message, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_deposit)
    await message.reply(_('enter_amount'))


@router.message(EconomyStates.waiting_deposit)
async def process_deposit(message: Message, _: callable, db_session: AsyncSession, db_user, state: FSMContext, **kwargs):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply(_('invalid_amount'))
        return
    success, bal, bank = await deposit_to_bank(db_session, db_user.id, amount)
    if success:
        await message.reply(_('deposit_success').format(amount=format_number(amount)))
    else:
        wallet = await get_or_create_wallet(db_session, db_user.id)
        await message.reply(_('insufficient_funds').format(balance=format_number(wallet.balance or 0)))
    await state.clear()


#  /withdraw 
@router.message(Command('withdraw'))
async def cmd_withdraw(message: Message, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_withdraw)
    await message.reply(_('enter_amount'))


@router.message(EconomyStates.waiting_withdraw)
async def process_withdraw(message: Message, _: callable, db_session: AsyncSession, db_user, state: FSMContext, **kwargs):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply(_('invalid_amount'))
        return
    success, bal, bank = await withdraw_from_bank(db_session, db_user.id, amount)
    if success:
        await message.reply(_('withdraw_success').format(amount=format_number(amount)))
    else:
        wallet = await get_or_create_wallet(db_session, db_user.id)
        await message.reply(_('insufficient_funds').format(balance=format_number(wallet.bank_balance or 0)))
    await state.clear()


#  /transfer 
@router.message(Command('transfer'))
async def cmd_transfer(message: Message, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_transfer_user)
    await message.reply(_('transfer_enter_user'), parse_mode='HTML')


@router.message(EconomyStates.waiting_transfer_user)
async def process_transfer_user(message: Message, _: callable, db_session: AsyncSession, db_user, state: FSMContext, **kwargs):
    from sqlalchemy import select as _select
    from bot.database.models import User
    text = message.text.strip().lstrip('@')
    try:
        uid = int(text)
        target = await db_session.get(User, uid)
    except ValueError:
        target = await db_session.scalar(_select(User).where(User.username == text))
    if not target:
        await message.reply(_('transfer_user_not_found'))
        return
    if target.id == db_user.id:
        await message.reply("You can't transfer coins to yourself. Please choose another user.")
        return
    await state.update_data(transfer_target_id=target.id, transfer_target_name=target.first_name)
    await state.set_state(EconomyStates.waiting_transfer_amount)
    await message.reply(_('transfer_enter_amount').format(name=target.first_name), parse_mode='HTML')


@router.message(EconomyStates.waiting_transfer_amount)
async def process_transfer_amount(message: Message, _: callable, db_session: AsyncSession, db_user, state: FSMContext, **kwargs):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply(_('invalid_amount'))
        return
    data = await state.get_data()
    target_id = data.get('transfer_target_id')
    target_name = data.get('transfer_target_name', 'User')
    success, sender_bal, _ = await transfer_coins(db_session, db_user.id, target_id, amount)
    if success:
        await message.reply(
            _('transfer_success').format(amount=format_number(amount), name=target_name, balance=format_number(sender_bal)),
            parse_mode='HTML',
        )
    else:
        wallet = await get_or_create_wallet(db_session, db_user.id)
        await message.reply(_('insufficient_funds').format(balance=format_number(wallet.balance or 0)))
    await state.clear()


#  /referral 
@router.message(Command('referral', 'ref'))
async def cmd_referral(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    from bot.config import settings
    bot_info = await message.bot.get_me()
    ref_link = f'https://t.me/{bot_info.username}?start=ref_{db_user.id}'
    text = (
        _('referral_title') + '\n\n'
        + _('referral_desc').format(reward=settings.REFERRAL_REWARD) + '\n\n'
        + _('referral_link') + ':\n'
        + f'<code>{ref_link}</code>'
    )
    await message.reply(text, parse_mode='HTML')


#  /achievements 
@router.message(Command('achievements'))
async def cmd_achievements(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    from bot.services.achievement_service import get_user_achievements
    user_achs = await get_user_achievements(db_session, db_user.id)
    text = _('achievements_menu') + '\n\n'
    text += _('achievements_count').format(earned=len(user_achs), total=100) + '\n\n'
    if user_achs:
        for ua, ach in user_achs[:10]:
            text += f'{ach.icon} <b>{ach.code}</b> — {ach.points} pts\n'
    else:
        text += _('no_data')
    await message.reply(text, parse_mode='HTML')


#  /transactions 
@router.message(Command('transactions', 'tx'))
async def cmd_transactions(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    txs = await get_transactions(db_session, db_user.id, limit=10)
    if not txs:
        await message.reply(_('no_data'))
        return
    text = _('transactions_title') + '\n\n'
    for tx in txs:
        sign = '+' if tx.amount > 0 else ''
        text += f'{sign}{format_number(tx.amount)} — {tx.type} — {tx.created_at.strftime("%m/%d %H:%M")}\n'
    await message.reply(text, parse_mode='HTML')


#
# ECONOMY CALLBACK HANDLERS — Make inline buttons fully functional
#
@router.callback_query(F.data == "eco:wallet")
async def eco_wallet(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    balance, bank = await get_balance(db_session, db_user.id)
    wallet = await get_or_create_wallet(db_session, db_user.id)
    text = (
        _('wallet_title') + '\n\n'
        + _('balance').format(amount=format_number(balance)) + '\n'
        + _('bank').format(amount=format_number(bank)) + '\n'
        + _('total_earned').format(amount=format_number(wallet.total_earned or 0))
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=wallet_kb(_))
    await callback.answer()


@router.callback_query(F.data == "eco:bank")
async def eco_bank(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    balance, bank = await get_balance(db_session, db_user.id)
    text = (
        _('bank_title') + '\n\n'
        + _('balance').format(amount=format_number(balance)) + '\n'
        + _('bank').format(amount=format_number(bank)) + '\n\n'
        + _('bank_hint')
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=wallet_kb(_))
    await callback.answer()


@router.callback_query(F.data == "eco:daily")
async def eco_daily(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    success, amount, next_claim = await claim_daily_reward(db_session, db_user.id)
    if success:
        text = _('daily_reward').format(amount=amount)
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        text = _('reward_cooldown').format(type='daily', time=time_str)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:daily"))
    await callback.answer()


@router.callback_query(F.data == "eco:weekly")
async def eco_weekly(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    success, amount, next_claim = await claim_weekly_reward(db_session, db_user.id)
    if success:
        text = _('weekly_reward').format(amount=amount)
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        text = _('reward_cooldown').format(type='weekly', time=time_str)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:weekly"))
    await callback.answer()


@router.callback_query(F.data == "eco:monthly")
async def eco_monthly(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    success, amount, next_claim = await claim_monthly_reward(db_session, db_user.id)
    if success:
        text = _('monthly_reward').format(amount=amount)
    else:
        time_str = get_time_until(next_claim) if next_claim else 'soon'
        text = _('reward_cooldown').format(type='monthly', time=time_str)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:monthly"))
    await callback.answer()


@router.callback_query(F.data == "eco:referral")
async def eco_referral(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    from bot.config import settings
    bot_info = await callback.bot.get_me()
    ref_link = f'https://t.me/{bot_info.username}?start=ref_{db_user.id}'
    text = (
        _('referral_title') + '\n\n'
        + _('referral_desc').format(reward=settings.REFERRAL_REWARD) + '\n\n'
        + _('referral_link') + ':\n'
        + f'<code>{ref_link}</code>'
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:referral"))
    await callback.answer()


@router.callback_query(F.data.startswith("transfer:confirm:"))
async def transfer_confirm_callback(callback: CallbackQuery, db_session: AsyncSession, db_user, **kwargs):
    try:
        _, _, target_raw, amount_raw = callback.data.split(":", 3)
        target_id = int(target_raw); amount = int(amount_raw)
    except (ValueError, TypeError):
        await callback.answer("Invalid transfer request.", show_alert=True); return
    if not db_user or amount <= 0 or target_id == db_user.id:
        await callback.answer("Invalid transfer.", show_alert=True); return
    success, sender_balance, _ = await transfer_coins(db_session, db_user.id, target_id, amount)
    await callback.answer("Transfer completed." if success else f"Not enough coins. Balance: {sender_balance}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "transfer:cancel")
async def transfer_cancel_callback(callback: CallbackQuery, state: FSMContext, **kwargs):
    await state.clear()
    await callback.answer("Transfer cancelled.", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "eco:transactions")
async def eco_transactions(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    txs = await get_transactions(db_session, db_user.id, limit=10)
    if not txs:
        text = _('transactions_title') + '\n\n' + _('no_data')
    else:
        text = _('transactions_title') + '\n\n'
        for tx in txs:
            sign = '+' if tx.amount > 0 else ''
            text += f'{sign}{format_number(tx.amount)} — {tx.type} — {tx.created_at.strftime("%m/%d %H:%M")}\n'
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:transactions"))
    await callback.answer()


@router.callback_query(F.data == "eco:deposit")
async def eco_deposit(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_deposit)
    await callback.message.answer(_('enter_amount'))
    await callback.answer()


@router.callback_query(F.data == "eco:withdraw")
async def eco_withdraw(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_withdraw)
    await callback.message.answer(_('enter_amount'))
    await callback.answer()


@router.callback_query(F.data == "eco:transfer")
async def eco_transfer(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    await state.set_state(EconomyStates.waiting_transfer_user)
    await callback.message.answer(_('transfer_enter_user'), parse_mode='HTML')
    await callback.answer()


@router.callback_query(F.data == "eco:achievements")
async def eco_achievements(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    from bot.services.achievement_service import get_user_achievements
    user_achs = await get_user_achievements(db_session, db_user.id)
    text = _('achievements_menu') + '\n\n'
    text += _('achievements_count').format(earned=len(user_achs), total=100) + '\n\n'
    if user_achs:
        for ua, ach in user_achs[:10]:
            text += f'{ach.icon} <b>{ach.code}</b> — {ach.points} pts\n'
    else:
        text += _('no_data')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "eco:achievements"))
    await callback.answer()


@router.callback_query(F.data == "eco:rewards")
async def eco_rewards(callback: CallbackQuery, _: callable, **kwargs):
    text = _('rewards_center_title')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=rewards_menu_kb(_))
    await callback.answer()


@router.callback_query(F.data == "eco:shop")
async def eco_shop(callback: CallbackQuery, _: callable, **kwargs):
    text = _('btn_shop')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=shop_categories_kb(_))
    await callback.answer()


@router.callback_query(F.data == "eco:missions")
async def eco_missions(callback: CallbackQuery, _: callable, **kwargs):
    text = _('btn_missions')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=missions_kb(_))
    await callback.answer()


@router.callback_query(F.data == "menu:economy")
async def menu_economy(callback: CallbackQuery, _: callable, **kwargs):
    text = _('economy_menu')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=economy_menu_kb(_))
    await callback.answer()


@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(callback: CallbackQuery, _: callable, **kwargs):
    from bot.keyboards.economy import rewards_menu_kb
    text = _('rewards_center_title')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=rewards_menu_kb(_))
    await callback.answer()


@router.callback_query(F.data == "rewards:achievements")
async def rewards_achievements(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user, **kwargs):
    if not db_user:
        await callback.answer(_("error_generic"), show_alert=True)
        return
    from bot.services.achievement_service import get_user_achievements
    user_achs = await get_user_achievements(db_session, db_user.id)
    text = _('achievements_menu') + '\n\n'
    text += _('achievements_count').format(earned=len(user_achs), total=100) + '\n\n'
    if user_achs:
        for ua, ach in user_achs[:10]:
            text += f'{ach.icon} <b>{ach.code}</b> — {ach.points} pts\n'
    else:
        text += _('no_data')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb(_, "menu:economy", "rewards:achievements"))
    await callback.answer()


@router.callback_query(F.data == "rewards:missions")
async def rewards_missions(callback: CallbackQuery, _: callable, **kwargs):
    from bot.keyboards.economy import missions_kb
    text = _('btn_missions')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=missions_kb(_))
    await callback.answer()


@router.callback_query(F.data == "rewards:shop")
async def rewards_shop(callback: CallbackQuery, _: callable, **kwargs):
    from bot.keyboards.economy import shop_categories_kb
    text = _('btn_shop')
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=shop_categories_kb(_))
    await callback.answer()


@router.callback_query(F.data.in_({"shop:badges", "shop:titles", "shop:frames", "shop:lootboxes", "shop:cosmetics"}))
async def shop_category(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user=None, **kwargs):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.database.models import ShopItem
    category = callback.data.split(":", 1)[1]
    catalog = [
        ("star_badge", "⭐ Star Badge", "badges", 500, "A profile badge"),
        ("veteran_title", "🎖 Veteran Title", "titles", 1000, "A veteran title"),
        ("neon_frame", "🖼 Neon Frame", "frames", 2000, "A neon profile frame"),
        ("loot_box", "📦 Loot Box", "lootboxes", 300, "A random cosmetic item"),
        ("xp_boost", "✨ XP Boost", "cosmetics", 1500, "An XP boost token"),
    ]
    for code, name, cat, price, desc_text in catalog:
        item = await db_session.scalar(select(ShopItem).where(ShopItem.code == code))
        if not item:
            db_session.add(ShopItem(code=code, name_key=name, description_key=desc_text, category=cat, price=price, icon=name.split()[0], is_active=True))
    await db_session.flush()
    result = await db_session.execute(select(ShopItem).where(ShopItem.category == category, ShopItem.is_active.is_(True)).order_by(ShopItem.price.asc()))
    items = result.scalars().all()
    builder = InlineKeyboardBuilder()
    lines = [f"🛍️ <b>{category.title()} Shop</b>"]
    for item in items:
        label = f"{item.icon or '🎁'} {item.name_key} — {item.price} coins"
        builder.button(text=f"Buy · {item.price} 🪙", callback_data=f"shop:buy:{item.id}")
        lines.append(f"• <b>{item.name_key}</b> — {item.price} coins\n  {item.description_key or ''}")
    builder.button(text="◀️ Shop", callback_data="eco:shop")
    builder.adjust(*([1] * (len(items) + 1)))
    await callback.message.edit_text("\n\n".join(lines), parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("shop:buy:"))
async def shop_buy(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user=None, **kwargs):
    from bot.database.models import ShopItem, UserShopItem
    from bot.services.economy_service import deduct_coins
    if not db_user:
        await callback.answer("User record is unavailable. Send /start first.", show_alert=True)
        return
    try:
        item_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid item.", show_alert=True)
        return
    item = await db_session.get(ShopItem, item_id)
    if not item or not item.is_active:
        await callback.answer("This item is unavailable.", show_alert=True)
        return
    owned = await db_session.scalar(select(UserShopItem).where(UserShopItem.user_id == db_user.id, UserShopItem.item_id == item.id))
    if owned:
        await callback.answer("You already own this item.", show_alert=True)
        return
    ok, balance = await deduct_coins(db_session, db_user.id, item.price, "shop_purchase", f"Purchased {item.name_key}")
    if not ok:
        await callback.answer(f"Not enough coins. Balance: {balance}", show_alert=True)
        return
    db_session.add(UserShopItem(user_id=db_user.id, item_id=item.id, quantity=1, is_equipped=False))
    await callback.answer(f"Purchased {item.name_key} for {item.price} coins!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.in_({"missions:daily", "missions:weekly", "missions:monthly"}))
async def missions_category(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user=None, **kwargs):
    from bot.database.models import Mission, UserMission
    from bot.services.mission_service import seed_missions, period_key
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    period = callback.data.split(":", 1)[1]
    await seed_missions(db_session)
    result = await db_session.execute(select(Mission).where(Mission.type == period, Mission.is_active.is_(True)).order_by(Mission.id.asc()).limit(20))
    missions = result.scalars().all()
    lines = [f"🎯 <b>{period.title()} Missions</b>"]
    builder = InlineKeyboardBuilder()
    if not db_user:
        lines.append("Send /start first so ALEXA can track your progress.")
    else:
        key = period_key(period)
        for mission in missions:
            row = await db_session.scalar(select(UserMission).where(UserMission.user_id == db_user.id, UserMission.mission_id == mission.id, UserMission.period == key))
            if row is None:
                row = UserMission(user_id=db_user.id, mission_id=mission.id, period=key, progress=0, completed=False)
                db_session.add(row)
                await db_session.flush()
            progress = min(row.progress or 0, mission.requirement_value or 1)
            status = "✅ Claimed" if row.completed_at else ("🎁 Ready to claim" if row.completed else "⏳ In progress")
            lines.append(f"\n{mission.icon or '🎯'} <b>{mission.name_key}</b> — {progress}/{mission.requirement_value}\nReward: {mission.reward_coins} coins + {mission.reward_xp} XP\n{mission.description_key} · {status}")
            if row.completed and not row.completed_at:
                builder.button(text=f"Claim: {mission.name_key[:25]}", callback_data=f"mission:claim:{row.id}")
    builder.button(text="◀️ Missions", callback_data="eco:missions")
    builder.adjust(1)
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("mission:claim:"))
async def claim_mission(callback: CallbackQuery, _: callable, db_session: AsyncSession, db_user=None, **kwargs):
    from datetime import datetime, timezone
    from bot.database.models import UserMission, Mission
    from bot.services.economy_service import add_coins
    from bot.services.user_service import add_xp
    if not db_user:
        await callback.answer("Send /start first.", show_alert=True); return
    try:
        row_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid mission.", show_alert=True); return
    row = await db_session.get(UserMission, row_id)
    if not row or row.user_id != db_user.id:
        await callback.answer("Mission not found for your account.", show_alert=True); return
    mission = await db_session.get(Mission, row.mission_id)
    if not mission or not row.completed:
        await callback.answer("Mission is not complete yet.", show_alert=True); return
    if row.completed_at:
        await callback.answer("Reward already claimed.", show_alert=True); return
    if mission.reward_coins:
        await add_coins(db_session, db_user.id, mission.reward_coins, "mission_reward", mission.name_key)
    if mission.reward_xp:
        await add_xp(db_session, db_user.id, mission.reward_xp)
    row.completed_at = datetime.now(timezone.utc)
    await db_session.flush()
    await callback.answer(f"Reward claimed: {mission.reward_coins} coins + {mission.reward_xp} XP", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)

