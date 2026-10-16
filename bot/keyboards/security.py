from aiogram.utils.keyboard import InlineKeyboardBuilder

def _status(v): return "🟢" if bool(v) else "🔴"
def security_menu_kb(_, security=None):
    b=InlineKeyboardBuilder()
    items=[("Anti-spam","anti_spam"),("Anti-flood","anti_flood"),("Anti-raid","anti_raid"),("Anti-bot","anti_bot"),("Anti-fake","anti_fake"),("Anti-ad","anti_advertisement"),("Anti-link","anti_link"),("Anti-mention","anti_mention_spam"),("Anti-forward","anti_forward_spam"),("Anti-emoji","anti_emoji_spam"),("Anti-hashtag","anti_hashtag_spam"),("Anti-phone","anti_phone"),("Anti-scam","anti_scam"),("Anti-crypto","anti_crypto_scam"),("Anti-NSFW","anti_nsfw"),("Anti-invite","anti_invite"),("Anti-promo","anti_channel_promo"),("Anti-mass-join","anti_mass_join"),("Anti-mass-leave","anti_mass_leave"),("Anti-username spam","anti_username_spam")]
    for label,field in items: b.button(text=f"{_status(getattr(security,field,False) if security else False)} {label}",callback_data="sec:"+{"anti_advertisement":"anti_ad","anti_mention_spam":"anti_mention","anti_forward_spam":"anti_forward","anti_crypto_scam":"anti_crypto","anti_channel_promo":"anti_promo","anti_mass_leave":"anti_mass_leave","anti_username_spam":"anti_username"}.get(field,field))
    b.button(text="🔒 Content Locks",callback_data="sec:locks")
    b.button(text=f"{_status(getattr(security,'captcha_enabled',False) if security else False)} Captcha",callback_data="captcha:toggle")
    b.button(text=f"{_status(getattr(security,'emergency_mode',False) if security else False)} Emergency",callback_data="sec:emergency")
    b.button(text="◀️ Home",callback_data="menu:main")
    b.adjust(2,2,2,2,2,2,2,2,2,1,1,1)
    return b.as_markup()

def locks_menu_kb(_, security=None):
    b=InlineKeyboardBuilder(); items=[("Text","text","lock_text"),("Links","links","lock_links"),("Photos","photos","lock_photos"),("Videos","videos","lock_videos"),("GIFs","gifs","lock_gifs"),("Audio","audio","lock_audio"),("Voice","voice","lock_voice"),("Documents","docs","lock_documents"),("Polls","polls","lock_polls"),("Games","games","lock_games"),("Bots","bots","lock_bots"),("Forwards","forwards","lock_forwards"),("Contacts","contacts","lock_contacts"),("Locations","locations","lock_locations"),("Stickers","stickers","lock_stickers"),("Inline","inline","lock_inline"),("Mentions","mentions","lock_mentions"),("Hashtags","hashtags","lock_hashtags")]
    for label,cb,field in items: b.button(text=f"{_status(getattr(security,field,False) if security else False)} {label}",callback_data=f"lock:{cb}")
    b.button(text="🔒 Lock ALL",callback_data="lock:all_on")
    b.button(text="🔓 Unlock ALL",callback_data="lock:all_off")
    b.button(text="◀️ Back",callback_data="sec:menu"); b.adjust(3,3,3,3,3,2,1); return b.as_markup()

def captcha_menu_kb(_, security=None):
    b=InlineKeyboardBuilder(); enabled=bool(getattr(security,'captcha_enabled',False) if security else False)
    b.button(text=f"{_status(enabled)} Toggle captcha",callback_data="captcha:toggle")
    for typ in ("math","button"): b.button(text=typ.title(),callback_data=f"captcha:type:{typ}")
    b.button(text="◀️ Back",callback_data="sec:menu"); b.adjust(2,1,1); return b.as_markup()
