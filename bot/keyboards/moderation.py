from aiogram.utils.keyboard import InlineKeyboardBuilder

def moderation_menu_kb(_, is_group=False, bio_link_enabled=False):
    b=InlineKeyboardBuilder()
    items=[("🚫 Ban","mod:ban"),("🔓 Unban","mod:unban"),("🔇 Mute","mod:mute"),("🔊 Unmute","mod:unmute"),("👢 Kick","mod:kick"),("⚠️ Warn","mod:warn"),("↩️ Remove warn","mod:unwarn"),("⏱️ Temp ban","mod:tban"),("⏱️ Temp mute","mod:tmute"),("📝 Notes","mod:notes"),("📋 History","mod:history"),("🎭 Roles","mod:roles"),("🚦 Strikes","mod:strike"),("◀️ Home","menu:main")]
    for text,cb in items:
        b.button(text=text,callback_data=cb)
    status = "🟢" if bio_link_enabled else "🔴"
    b.button(text="🛡️ Bio Link Protection", callback_data="mod:bio_link_protection")
    b.adjust(2,2,2,2,2,2,2,1); return b.as_markup()
def bio_link_protection_kb(enabled=False):
    b = InlineKeyboardBuilder()
    status = "🟢 ON" if enabled else "🔴 OFF"
    b.button(text=f"Bio Link Protection: {status}", callback_data="mod:bio_link_status")
    if enabled:
        b.button(text="🔴 Turn OFF", callback_data="mod:bio_link_off")
    else:
        b.button(text="🟢 Turn ON", callback_data="mod:bio_link_on")
    b.button(text="⚙️ Settings", callback_data="mod:bio_link_settings")
    b.button(text="◀️ Back to Moderation", callback_data="mod:menu")
    b.adjust(1)
    return b.as_markup()

def bio_link_action_kb(action="mute", mute_duration=3600):
    b = InlineKeyboardBuilder()
    for label, value in [("⚠️ Warn", "warn"), ("🔇 Mute", "mute"), ("🚫 Ban", "ban")]:
        prefix = "✅ " if action == value else ""
        b.button(text=f"{prefix}{label}", callback_data=f"mod:bio_action:{value}")
    for label, seconds in [("15 min", 900), ("30 min", 1800), ("1 hour", 3600), ("6 hours", 21600), ("12 hours", 43200), ("24 hours", 86400)]:
        prefix = "✅ " if mute_duration == seconds else ""
        b.button(text=f"{prefix}⏱️ {label}", callback_data=f"mod:bio_duration:{seconds}")
    b.button(text="💾 Save Settings", callback_data="mod:bio_settings_save")
    b.button(text="◀️ Back", callback_data="mod:bio_link_protection")
    b.adjust(1, 1, 1, 2, 2, 2, 1, 1)
    return b.as_markup()

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
