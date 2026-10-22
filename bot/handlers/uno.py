"""Group UNO game with private hand selection and one replaceable turn prompt.

Game state is intentionally in-memory; an app restart clears active games.
"""
import random
import os
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()
GAMES = {}
COLORS = ("Red", "Yellow", "Green", "Blue")
COLOR_EMOJI = {"Red": "🔴", "Yellow": "🟡", "Green": "🟢", "Blue": "🔵"}
VALUES = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "Skip", "Reverse", "+2")
WILD = ("Wild", "+4")


def _new_deck():
    deck = []
    for color in COLORS:
        deck.append({"color": color, "value": "0"})
        for value in VALUES[1:]:
            deck.extend([{"color": color, "value": value}, {"color": color, "value": value}])
    deck.extend([{"color": None, "value": "Wild"} for _ in range(4)])
    deck.extend([{"color": None, "value": "+4"} for _ in range(4)])
    random.shuffle(deck)
    return deck


def _draw_cards(game, user_id, count):
    """Draw cards, recycling the discard pile when the draw pile runs out."""
    for _ in range(count):
        if not game["deck"]:
            if len(game["discard"]) > 1:
                top = game["discard"][-1]
                game["deck"] = game["discard"][:-1]
                game["discard"] = [top]
                random.shuffle(game["deck"])
            else:
                # If no cards can be recycled, rebuild a deck rather than fail a penalty.
                game["deck"] = _new_deck()
        if game["deck"]:
            game["hands"][user_id].append(game["deck"].pop())


def _card_name(card):
    if card["color"] is None:
        return f"🌈 {card['value']}"
    return f"{COLOR_EMOJI[card['color']]} {card['value']}"


def _mention(player):
    return f'<a href="tg://user?id={player["id"]}">{escape(player["name"])}</a>'


def _valid(card, top):
    return (
        card["color"] is None
        or card["color"] == top["color"]
        or card["value"] == top["value"]
    )


def _keyboard(game, chat_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Join UNO", callback_data=f"uno:join:{chat_id}")
    kb.button(text="🔄 Restart", callback_data=f"uno:restart:{chat_id}")
    kb.adjust(1, 1)
    return kb.as_markup()


def _turn_keyboard(chat_id, user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎴 Make Your Move", callback_data=f"uno:hand:{chat_id}:{user_id}")
    return kb.as_markup()


def _hand_keyboard(chat_id, user_id, game):
    kb = InlineKeyboardBuilder()
    hand = game["hands"][user_id]
    for idx, card in enumerate(hand):
        kb.button(text=_card_name(card), callback_data=f"uno:play:{chat_id}:{user_id}:{idx}")
    kb.button(text="➕ Draw a card", callback_data=f"uno:draw:{chat_id}:{user_id}")
    kb.adjust(4, 4, 4, 4, 1)
    return kb.as_markup()


async def _turn_prompt(bot, chat_id, game):
    old = game.get("turn_message_id")
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except Exception:
            pass
    uid = game["players"][game["turn"]]
    player = game["members"][uid]
    try:
        sent = await bot.send_message(
            chat_id,
            f"🎴 <b>UNO — Next player!</b>\n\n{_mention(player)}, it's your turn!\nMake your choice!",
            reply_markup=_turn_keyboard(chat_id, uid),
        )
        game["turn_message_id"] = sent.message_id
    except Exception:
        game["turn_message_id"] = None


async def _board(message_or_bot, chat_id, game):
    bot = message_or_bot.bot if isinstance(message_or_bot, Message) else message_or_bot
    top = game["discard"][-1]
    direction = "↻ Clockwise" if game["direction"] == 1 else "↺ Counter-clockwise"
    rows = [
        "🃏 <b>UNO GAME</b>",
        f"Top Card: <b>{_card_name(top)}</b>",
        f"Current Color: {COLOR_EMOJI.get(top['color'], '🌈')} {top['color'] or 'Choose on Wild'}",
        f"Direction: {direction}",
        "",
        "<b>Players:</b>",
    ]
    for uid in game["players"]:
        p = game["members"][uid]
        marker = " ▶️" if uid == game["players"][game["turn"]] else ""
        rows.append(f"• {_mention(p)} — {len(game['hands'][uid])} cards{marker}")
    try:
        await bot.send_message(chat_id, "\n".join(rows))
    except Exception:
        pass


@router.message(Command("uno"))
async def uno_start(message: Message, **kwargs):
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await message.answer("🎴 UNO group mein start karo, phir players Join UNO dabayen.")
        return
    chat_id = message.chat.id
    if message.text and len(message.text.split()) > 1 and message.text.split(maxsplit=1)[1].strip().lower() == "restart":
        previous = GAMES.get(chat_id)
        if previous and previous.get("turn_message_id"):
            try:
                await message.bot.delete_message(chat_id, previous["turn_message_id"])
            except Exception:
                pass
        GAMES.pop(chat_id, None)
    existing = GAMES.get(chat_id)
    if existing and existing.get("active") and len(existing["players"]) >= 2:
        await message.answer("UNO game already chal rahi hai. Nayi game ke liye /uno restart bhejo.")
        return
    game = {
        "active": True, "players": [], "members": {}, "hands": {}, "deck": _new_deck(),
        "discard": [], "turn": 0, "direction": 1, "turn_message_id": None,
    }
    GAMES[chat_id] = game
    await message.answer(
        "🎴 <b>UNO is starting!</b>\n\nPlayers, join the game below. At least 2 players are needed.",
        reply_markup=_keyboard(game, chat_id),
    )


@router.message(Command("uno_restart"))
async def uno_restart(message: Message, **kwargs):
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        await message.answer("Ye command group mein use karo.")
        return
    GAMES.pop(message.chat.id, None)
    await uno_start(message, **kwargs)


@router.callback_query(F.data.startswith("uno:restart:"))
async def restart_callback(callback: CallbackQuery):
    chat_id = int(callback.data.rsplit(":", 1)[1])
    if callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Group mein restart karo.", show_alert=True)
        return
    previous = GAMES.get(chat_id)
    if previous and previous.get("turn_message_id"):
        try:
            await callback.bot.delete_message(chat_id, previous["turn_message_id"])
        except Exception:
            pass
    GAMES.pop(chat_id, None)
    await callback.answer("UNO restarted!")
    game = {
        "active": True, "players": [], "members": {}, "hands": {}, "deck": _new_deck(),
        "discard": [], "turn": 0, "direction": 1, "turn_message_id": None,
    }
    GAMES[chat_id] = game
    await callback.message.answer(
        "🎴 <b>UNO restarted!</b> Players, join below.",
        reply_markup=_keyboard(game, chat_id),
    )


@router.callback_query(F.data.startswith("uno:join:"))
async def join_uno(callback: CallbackQuery):
    chat_id = int(callback.data.rsplit(":", 1)[1])
    game = GAMES.get(chat_id)
    if not game or not game["active"]:
        await callback.answer("Game khatam ho gayi. /uno bhejo.", show_alert=True)
        return
    user = callback.from_user
    if user.id in game["members"]:
        await callback.answer("Tum already joined ho.")
        return
    game["members"][user.id] = {"id": user.id, "name": user.full_name}
    game["players"].append(user.id)
    await callback.answer("UNO mein join ho gaye!")
    if len(game["players"]) < 2:
        await callback.message.edit_text(
            f"🎴 <b>UNO is starting!</b>\n\nPlayers joined: {len(game['players'])}\nAt least 2 players chahiye. Join karo!",
            reply_markup=_keyboard(game, chat_id),
        )
        return
    if game["discard"]:
        await callback.message.edit_text("🎴 Players joined! Game is running.")
        return
    deck = game["deck"]
    for uid in game["players"]:
        game["hands"][uid] = [deck.pop() for _ in range(7)]
    first = deck.pop()
    while first["color"] is None:
        deck.insert(0, first)
        first = deck.pop()
    game["discard"].append(first)
    game["turn"] = random.randrange(len(game["players"]))
    await callback.message.edit_text("🎴 <b>UNO started!</b> Turn prompts will be sent separately.")
    await _board(callback.bot, chat_id, game)
    await _turn_prompt(callback.bot, chat_id, game)


@router.callback_query(F.data.startswith("uno:hand:"))
async def show_hand(callback: CallbackQuery):
    _, _, chat, target = callback.data.split(":")
    chat_id, target_id = int(chat), int(target)
    game = GAMES.get(chat_id)
    if not game or not game["active"]:
        await callback.answer("Game active nahi hai.", show_alert=True)
        return
    if callback.from_user.id != target_id:
        await callback.answer("Ye turn kisi aur ka hai!", show_alert=True)
        return
    if game["players"][game["turn"]] != target_id:
        await callback.answer("Ab tumhari turn nahi hai.", show_alert=True)
        return
    try:
        base_url = (os.environ.get("UNO_WEBAPP_URL") or os.environ.get("UNO_CARD_BASE_URL") or "https://laplace.fadehost.com").rstrip("/")
        if not base_url.endswith("/uno"):
            base_url += "/uno"
        app_url = f"{base_url}/?chat_id={chat_id}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎴 Open UNO Cards", web_app=WebAppInfo(url=app_url))
        ]])
        await callback.bot.send_message(
            target_id,
            "🎴 <b>Your UNO turn!</b>\nApne private graphical cards kholne ke liye button dabao. Sirf tum apni hand dekh sakte ho.",
            reply_markup=markup,
        )
        await callback.answer("Private UNO app DM mein bhej di.")
    except Exception:
        await callback.answer("Pehle Alexa ko private mein /start bhejo, phir button dobara dabao.", show_alert=True)


async def _advance(callback, chat_id, game, steps=1):
    game["turn"] = (game["turn"] + game["direction"] * steps) % len(game["players"])
    await _board(callback.bot, chat_id, game)
    await _turn_prompt(callback.bot, chat_id, game)


@router.callback_query(F.data.startswith("uno:draw:"))
async def draw_card(callback: CallbackQuery):
    _, _, chat, target = callback.data.split(":")
    chat_id, uid = int(chat), int(target)
    game = GAMES.get(chat_id)
    if not game or not game["active"] or callback.from_user.id != uid:
        await callback.answer("Ye tumhara turn nahi hai.", show_alert=True)
        return
    if game["players"][game["turn"]] != uid:
        await callback.answer("Ab tumhari turn nahi hai.", show_alert=True)
        return
    drawn = game.setdefault("drawn_card_index", {})
    if uid in drawn:
        await callback.answer("Is turn mein tum already card draw kar chuke ho.", show_alert=True)
        return
    _draw_cards(game, uid, 1)
    drawn[uid] = len(game["hands"][uid]) - 1
    await callback.answer("1 card draw kiya. Naya card play kar sakte ho, warna End turn dabao.")
    kb = InlineKeyboardBuilder()
    drawn_card = game["hands"][uid][-1]
    kb.button(text=f"🎴 Play {_card_name(drawn_card)}", callback_data=f"uno:play:{chat_id}:{uid}:{len(game['hands'][uid]) - 1}")
    kb.button(text="⏭ End turn", callback_data=f"uno:end:{chat_id}:{uid}")
    kb.adjust(1)
    try:
        await callback.bot.send_message(uid, "➕ Tumne ek card draw kiya.", reply_markup=kb.as_markup())
    except Exception:
        pass


@router.callback_query(F.data.startswith("uno:end:"))
async def end_drawn_turn(callback: CallbackQuery):
    _, _, chat, target = callback.data.split(":")
    chat_id, uid = int(chat), int(target)
    game = GAMES.get(chat_id)
    drawn = game.setdefault("drawn_card_index", {}) if game else {}
    if not game or not game.get("active") or callback.from_user.id != uid:
        await callback.answer("Ye tumhara turn nahi hai.", show_alert=True)
        return
    if game["players"][game["turn"]] != uid or uid not in drawn:
        await callback.answer("Pehle card draw karo ya apni turn check karo.", show_alert=True)
        return
    drawn.pop(uid, None)
    await callback.answer("Turn end ho gayi.")
    await _advance(callback, chat_id, game)


@router.callback_query(F.data.startswith("uno:play:"))
async def play_card(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id, uid, index = int(parts[2]), int(parts[3]), int(parts[4])
    game = GAMES.get(chat_id)
    if not game or not game["active"] or callback.from_user.id != uid:
        await callback.answer("Ye tumhara turn nahi hai.", show_alert=True)
        return
    if game["players"][game["turn"]] != uid:
        await callback.answer("Ab tumhari turn nahi hai.", show_alert=True)
        return
    hand = game["hands"][uid]
    if index >= len(hand):
        await callback.answer("Cards update ho chuke hain. Make Your Move dobara dabao.", show_alert=True)
        return
    drawn = game.setdefault("drawn_card_index", {})
    if uid in drawn and index != drawn[uid]:
        await callback.answer("Draw ke baad sirf naya card play kar sakte ho.", show_alert=True)
        return
    card = hand[index]
    if not _valid(card, game["discard"][-1]):
        await callback.answer("Ye card play nahi ho sakta!", show_alert=True)
        return
    if card["value"] == "+4":
        active_color = game["discard"][-1]["color"]
        if active_color and any(other.get("color") == active_color for i, other in enumerate(hand) if i != index):
            await callback.answer("Current color ka card haath mein hai, +4 nahi khel sakte.", show_alert=True)
            return
    hand.pop(index)
    game.setdefault("drawn_card_index", {}).pop(uid, None)
    if card["color"] is None:
        kb = InlineKeyboardBuilder()
        for color in COLORS:
            kb.button(text=f"{COLOR_EMOJI[color]} {color}", callback_data=f"uno:color:{chat_id}:{uid}:{card['value']}:{color}")
        kb.adjust(2, 2)
        # Keep the card in a pending state until a color is chosen.
        game["pending_wild"] = {"uid": uid, "card": card}
        await callback.message.answer("🌈 Wild card ke liye color choose karo:", reply_markup=kb.as_markup())
        await callback.answer()
        return
    game["discard"].append(card)
    await callback.answer(f"{_card_name(card)} played!")
    try:
        await callback.bot.send_message(uid, f"✅ Played {_card_name(card)}.", reply_markup=_hand_keyboard(chat_id, uid, game))
    except Exception:
        pass
    if not hand:
        game["active"] = False
        old = game.get("turn_message_id")
        if old:
            try:
                await callback.bot.delete_message(chat_id, old)
            except Exception:
                pass
        await callback.bot.send_message(chat_id, f"🏆 {_mention(game['members'][uid])} UNO jeet gaya!")
        return
    if card["value"] == "Reverse":
        game["direction"] *= -1
        # In a 2-player game, Reverse skips the opponent.
        steps = 2 if len(game["players"]) == 2 else 1
    elif card["value"] == "Skip":
        steps = 2
    elif card["value"] == "+2":
        next_index = (game["turn"] + game["direction"]) % len(game["players"])
        next_uid = game["players"][next_index]
        _draw_cards(game, next_uid, 2)
        steps = 2
    else:
        steps = 1
    await _advance(callback, chat_id, game, steps=steps)


@router.callback_query(F.data.startswith("uno:color:"))
async def choose_wild_color(callback: CallbackQuery):
    _, _, chat, target, value, color = callback.data.split(":")
    chat_id, uid = int(chat), int(target)
    game = GAMES.get(chat_id)
    pending = game.get("pending_wild") if game else None
    if not game or not pending or pending["uid"] != uid or callback.from_user.id != uid:
        await callback.answer("Wild move expire ho chuki hai.", show_alert=True)
        return
    pending["card"]["color"] = color
    game["discard"].append(pending["card"])
    game["pending_wild"] = None
    await callback.answer(f"Color {color} selected!")
    hand = game["hands"][uid]
    if not hand:
        game["active"] = False
        await callback.bot.send_message(chat_id, f"🏆 {_mention(game['members'][uid])} UNO jeet gaya!")
        return
    steps = 1
    if value == "+4":
        next_index = (game["turn"] + game["direction"]) % len(game["players"])
        next_uid = game["players"][next_index]
        _draw_cards(game, next_uid, 4)
        steps = 2
    await _advance(callback, chat_id, game, steps=steps)
