from aiogram.utils.keyboard import InlineKeyboardBuilder

def _build(items, rows=2):
    b=InlineKeyboardBuilder()
    for text,cb in items: b.button(text=text,callback_data=cb)
    b.adjust(*([rows] * ((len(items)+rows-1)//rows)))
    return b.as_markup()
def games_menu_kb(_):
    return _build([("🎲 Dice","game:dice"),("🧠 Quiz","game:quiz"),("💎 Treasure","game:treasure"),("🎡 Wheel","game:wheel"),("🃏 Cards","game:cards"),("🔢 Number war","game:numwar"),("✊ RPS","game:rps"),("💣 Mines","game:mines"),("🏆 Tournaments","menu:tournaments"),("🎰 Roulette","game:roulette"),("⚔️ Duels","menu:duels"),("◀️ Home","menu:main")],2)
def dice_menu_kb(_): return _build([("🎲 Play dice","dice:classic"),("🎁 Quick play","dice:daily"),("◀️ Games","menu:games"),("🏠 Home","menu:main")],2)
def game_action_kb(_, **kw): return _build([("▶️ Play","game:dice"),("◀️ Games","menu:games")],2)
def join_game_kb(_, game_id=0): return _build([("⚔️ Join duel",f"duel_accept:{game_id}"),("❌ Decline",f"duel_decline:{game_id}")],2)
def rps_choice_kb(_, match_id=0): return _build([("🪨 Rock",f"rps:play:rock:{match_id}"),("📄 Paper",f"rps:play:paper:{match_id}"),("✂️ Scissors",f"rps:play:scissors:{match_id}"),("◀️ Games","menu:games")],2)
def wheel_spin_kb(_): return _build([("🔴 Red","roulette:red:0"),("⚫ Black","roulette:black:0"),("🟢 Zero","roulette:zero:0"),("◀️ Games","menu:games")],2)
def mines_board_kb(_, **kw): return _build([("💣 Play Mines","game:mines"),("◀️ Games","menu:games")],2)

def game_result_kb(_): return _build([("🔁 Play again","game:dice"),("🎮 Games","menu:games")],2)
def tournament_kb(_): return _build([("🏆 Active","trn:active"),("👤 My tournaments","trn:mine"),("📜 History","trn:history"),("🏅 Rankings","trn:rankings"),("◀️ Home","menu:main")],2)
def duel_kb(_, duel_id=0): return _build([("✅ Accept",f"duel_accept:{duel_id}"),("❌ Decline",f"duel_decline:{duel_id}")],2)
