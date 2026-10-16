from aiogram.utils.keyboard import InlineKeyboardBuilder

def moderation_menu_kb(_, is_group=False):
    b=InlineKeyboardBuilder()
    items=[("🚫 Ban","mod:ban"),("🔓 Unban","mod:unban"),("🔇 Mute","mod:mute"),("🔊 Unmute","mod:unmute"),("👢 Kick","mod:kick"),("⚠️ Warn","mod:warn"),("↩️ Remove warn","mod:unwarn"),("⏱️ Temp ban","mod:tban"),("⏱️ Temp mute","mod:tmute"),("📝 Notes","mod:notes"),("📋 History","mod:history"),("🎭 Roles","mod:roles"),("🚦 Strikes","mod:strike"),("◀️ Home","menu:main")]
    for text,cb in items: b.button(text=text,callback_data=cb)
    b.adjust(2,2,2,2,2,2,2); return b.as_markup()
def mute_duration_kb(_, user_id=0):
    b=InlineKeyboardBuilder()
    for label,d in [("15m","15m"),("1h","1h"),("6h","6h"),("1d","1d"),("7d","7d"),("Forever","forever")]: b.button(text=label,callback_data=f"mute_duration:{user_id}:{d}")
    b.button(text="Cancel",callback_data="mod:menu"); b.adjust(3,3,1); return b.as_markup()
def ban_duration_kb(_, user_id=0):
    b=InlineKeyboardBuilder()
    for label,d in [("1h","1h"),("1d","1d"),("7d","7d"),("30d","30d"),("Forever","forever")]: b.button(text=label,callback_data=f"ban_duration:{user_id}:{d}")
    b.button(text="Cancel",callback_data="mod:menu"); b.adjust(3,3); return b.as_markup()
def warn_actions_kb(_, user_id=0):
    b=InlineKeyboardBuilder(); b.button(text="🔇 Mute",callback_data=f"warn_action:mute:{user_id}"); b.button(text="👢 Kick",callback_data=f"warn_action:kick:{user_id}"); b.button(text="🚫 Ban",callback_data=f"warn_action:ban:{user_id}"); b.button(text="Cancel",callback_data="mod:menu"); b.adjust(2,2); return b.as_markup()
def notes_kb(_, **kw):
    b=InlineKeyboardBuilder(); b.button(text="📋 View history",callback_data="mod:history"); b.button(text="◀️ Back",callback_data="mod:menu"); b.adjust(2); return b.as_markup()
def warn_kb(_, user_id=0): return warn_actions_kb(_, user_id)
def ban_confirm_kb(_, user_id=0):
    b=InlineKeyboardBuilder(); b.button(text="✅ Confirm ban",callback_data=f"ban_confirm:{user_id}"); b.button(text="❌ Cancel",callback_data="mod:menu"); b.adjust(2); return b.as_markup()
