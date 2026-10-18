from aiogram.utils.keyboard import InlineKeyboardBuilder

LANG_FLAGS = {"en":"🇬🇧"}

def _btn(builder, text, callback):
    builder.button(text=text, callback_data=callback)

def language_selection_kb(_=None):
    b = InlineKeyboardBuilder()
    b.button(text="🇬🇧 English (only supported language)", callback_data="lang:en")
    b.button(text="◀️ Back", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()

def main_menu_kb(_, is_owner=False):
    b = InlineKeyboardBuilder()
    for text, cb in [("🔒 Security","menu:security"),("⚖️ Moderation","menu:moderation"),("💰 Economy","menu:economy"),("🎮 Games","menu:games"),("⚔️ Duels","menu:duels"),("🏅 Achievements","menu:achievements"),("🎁 Referral Program — Get Stars Plus Premium","affiliate:open"),("📊 Statistics","menu:statistics"),("⚙️ Settings","menu:settings"),("❓ Help","menu:help")]: _btn(b,text,cb)
    if is_owner:
        _btn(b, "👑 Owner Panel", "owner:menu")
    b.adjust(2,2,1,2,1,1)
    return b.as_markup()

def group_main_menu_kb(_):
    b = InlineKeyboardBuilder()
    for text, cb in [("🔒 Security","menu:security"),("⚖️ Moderation","menu:moderation"),("⚙️ Settings","menu:settings"),("📊 Statistics","menu:statistics"),("❓ Help","menu:help"),("🔄 Refresh","group:main")]: _btn(b,text,cb)
    b.adjust(2,2,2)
    return b.as_markup()

def back_button_kb(_, back_cb="menu:main"):
    b = InlineKeyboardBuilder(); b.button(text="◀️ Back", callback_data=back_cb); return b.as_markup()

def nav_kb(_, back_cb="menu:main", refresh_cb=None):
    b = InlineKeyboardBuilder(); b.button(text="◀️ Back", callback_data=back_cb)
    if refresh_cb: b.button(text="🔄 Refresh", callback_data=refresh_cb)
    b.button(text="🏠 Home", callback_data="menu:main"); b.adjust(2,1); return b.as_markup()

def confirm_cancel_kb(_, confirm_cb="", cancel_cb="menu:main"):
    b = InlineKeyboardBuilder()
    if confirm_cb: b.button(text="✅ Confirm", callback_data=confirm_cb)
    b.button(text="❌ Cancel", callback_data=cancel_cb); b.adjust(2); return b.as_markup()
