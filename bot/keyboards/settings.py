from aiogram.utils.keyboard import InlineKeyboardBuilder

def settings_menu_kb(_):
    b=InlineKeyboardBuilder()
    for text,cb in [("👋 Welcome","settings:welcome"),("👋 Goodbye","settings:goodbye"),("📋 Log channel","settings:logs"),("⚙️ General settings","settings:general"),("🐢 Slow mode","settings:slowmode"),("🧹 Auto-delete","settings:autodel"),("◀️ Home","menu:main")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2,1); return b.as_markup()
def welcome_settings_kb(_, settings=None):
    enabled=bool(getattr(settings,'enabled',False) if settings else False); b=InlineKeyboardBuilder()
    b.button(text=("🟢 Enabled" if enabled else "🔴 Disabled"),callback_data="welcome:toggle")
    for text,cb in [("✏️ Set message","welcome:set"),("🔎 Variables","welcome:vars"),("👁️ Preview","welcome:preview"),("◀️ Back","settings:menu"),("🏠 Home","menu:main")]: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2); return b.as_markup()
def log_channel_kb(_):
    b=InlineKeyboardBuilder(); b.button(text="Set channel",callback_data="logs:set"); b.button(text="Remove channel",callback_data="logs:remove"); b.button(text="Log events",callback_data="logs:events"); b.button(text="◀️ Back",callback_data="settings:menu"); b.adjust(2,2); return b.as_markup()
