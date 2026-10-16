from aiogram.utils.keyboard import InlineKeyboardBuilder

def economy_menu_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("👛 Wallet","eco:wallet"),("🎁 Daily reward","eco:daily"),("📅 Weekly reward","eco:weekly"),("🗓️ Monthly reward","eco:monthly"),("🏦 Bank","eco:bank"),("💸 Transfer","eco:transfer"),("🔗 Referral","eco:referral"),("🏅 Achievements","eco:achievements"),("📜 Transactions","eco:transactions"),("🛍️ Shop","eco:shop"),("🎯 Missions","eco:missions"),("◀️ Home","menu:main")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2,2,2,2); return b.as_markup()
def wallet_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("🎁 Daily","eco:daily"),("📅 Weekly","eco:weekly"),("🗓️ Monthly","eco:monthly"),("🏦 Bank","eco:bank"),("💸 Transfer","eco:transfer"),("📜 Transactions","eco:transactions"),("◀️ Economy","menu:economy")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2,1); return b.as_markup()
def rewards_menu_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("🏅 Achievements","rewards:achievements"),("🎯 Missions","rewards:missions"),("🛍️ Shop","rewards:shop"),("◀️ Economy","menu:economy")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2); return b.as_markup()
def shop_categories_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("🏅 Badges","shop:badges"),("🎭 Titles","shop:titles"),("🖼️ Frames","shop:frames"),("📦 Loot boxes","shop:lootboxes"),("✨ Cosmetics","shop:cosmetics"),("◀️ Rewards","menu:rewards")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2); return b.as_markup()
def missions_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("📅 Daily","missions:daily"),("🗓️ Weekly","missions:weekly"),("📆 Monthly","missions:monthly"),("◀️ Rewards","menu:rewards")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2); return b.as_markup()
def transfer_confirm_kb(_, target_id=0, amount=0):
    b=InlineKeyboardBuilder(); b.button(text="✅ Confirm",callback_data=f"transfer:confirm:{target_id}:{amount}"); b.button(text="❌ Cancel",callback_data="transfer:cancel"); b.adjust(2); return b.as_markup()
