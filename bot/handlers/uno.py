"""Chat-based multiplayer UNO for ALEXA. Active matches live in memory."""
import asyncio
import io
import random
import secrets
from functools import wraps
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, InputMediaPhoto,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()
_GAMES = {}
_GROUP_GAME = {}


def _serialize_game_action(handler):
    """Serialize callbacks for a match so rapid/double taps cannot apply two moves."""
    @wraps(handler)
    async def wrapped(callback, *args, **kwargs):
        parts = (getattr(callback, "data", "") or "").split(":")
        game_id = parts[2] if len(parts) > 2 and parts[0] == "uno" else None
        game = _GAMES.get(game_id)
        if game is None:
            return await handler(callback, *args, **kwargs)
        lock = game.setdefault("_action_lock", asyncio.Lock())
        async with lock:
            return await handler(callback, *args, **kwargs)
    return wrapped


COLORS = {
    "R": ("Red", "#e53935", "🔴"),
    "G": ("Green", "#168447", "🟢"),
    "B": ("Blue", "#1976d2", "🔵"),
    "Y": ("Yellow", "#f4c20d", "🟡"),
}
DARK = "#202938"


def _card_name(card):
    color, value = card
    if color == "W":
        return "Wild Draw Four" if value == "+4" else "Wild"
    return f"{COLORS[color][0]} {value}"


def _card_emoji(card):
    color, value = card
    if color == "W":
        return "🌈" if value == "Wild" else "🌈 +4"
    return f"{COLORS[color][2]} {value}"


def _make_deck():
    deck = []
    for color in COLORS:
        deck.append((color, "0"))
        for number in range(1, 10):
            deck.extend([(color, str(number)), (color, str(number))])
        for action in ("Skip", "Reverse", "+2"):
            deck.extend([(color, action), (color, action)])
    deck.extend([("W", "Wild")] * 4)
    deck.extend([("W", "+4")] * 4)
    random.shuffle(deck)
    return deck


def _draw(game, count=1):
    for _ in range(count):
        if not game["deck"]:
            top = game["discard"][-1:] 
            recyclable = game["discard"][:-1]
            random.shuffle(recyclable)
            game["deck"] = recyclable
            game["discard"] = top
        if game["deck"]:
            game["hands"][game["players"][game["current"]]["id"]].append(game["deck"].pop())


def _draw_for(game, user_id, count=1):
    for _ in range(count):
        if not game["deck"]:
            top = game["discard"][-1:] 
            recyclable = game["discard"][:-1]
            random.shuffle(recyclable)
            game["deck"] = recyclable
            game["discard"] = top
        if game["deck"]:
            game["hands"][user_id].append(game["deck"].pop())


def _legal(game, card, user_id):
    top = game["discard"][-1]
    color, value = card
    if color == "W":
        if value == "+4" and any(c[0] == game["color"] for c in game["hands"][user_id]):
            return False
        return True
    return color == game["color"] or value == top[1]


def _lobby_keyboard(game_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="🙋 Join UNO", callback_data=f"uno:join:{game_id}")
    builder.button(text="▶️ Start Game", callback_data=f"uno:start:{game_id}")
    builder.adjust(1, 1)
    return builder.as_markup()


def _color_keyboard(game_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{emoji} {name}", callback_data=f"uno:color:{game_id}:{color}")
        for color, (name, _, emoji) in COLORS.items()
    ]])


def _hand_keyboard(game, user_id):
    builder = InlineKeyboardBuilder()
    is_turn = game["started"] and not game.get("pending_wild") and game["players"][game["current"]]["id"] == user_id
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    if is_turn and drawn_index is not None:
        card = game["hands"][user_id][drawn_index]
        builder.button(text=f"▶️ Play drawn: {_card_emoji(card)}"[:60], callback_data=f"uno:play:{game['id']}:{drawn_index}")
        builder.button(text="⏭️ Pass turn", callback_data=f"uno:pass:{game['id']}")
        builder.adjust(1, 1)
    elif is_turn:
        for index, card in enumerate(game["hands"][user_id]):
            builder.button(text=_card_emoji(card)[:30], callback_data=f"uno:play:{game['id']}:{index}")
        builder.adjust(2)
        builder.button(text="➕ Draw a card", callback_data=f"uno:draw:{game['id']}")
    if len(game["hands"].get(user_id, [])) == 1 and user_id in game.get("uno_pending", set()):
        builder.button(text="📣 UNO!", callback_data=f"uno:call:{game['id']}")
    if not builder.export():
        return None
    return builder.as_markup()



def _render_hand_image(cards):
    from PIL import Image, ImageDraw, ImageFont
    cols = 7
    rows = max(1, (len(cards) + cols - 1) // cols)
    card_w, card_h, gap = 112, 164, 12
    width = 24 + cols * card_w + (cols - 1) * gap
    height = 62 + rows * card_h + (rows - 1) * gap + 24
    image = Image.new("RGB", (width, height), "#17212b")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        card_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        title_font = card_font = small_font = ImageFont.load_default()
    draw.text((18, 14), "ALEXA UNO  •  YOUR HAND", fill="white", font=title_font)
    for i, card in enumerate(cards):
        color, value = card
        row, col = divmod(i, cols)
        x = 12 + col * (card_w + gap)
        y = 54 + row * (card_h + gap)
        fill = COLORS[color][1] if color in COLORS else "#30343b"
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=14, fill=fill, outline="#f4f4f4", width=3)
        draw.ellipse((x + 13, y + 30, x + card_w - 13, y + card_h - 30), fill="#ffffff", outline="#eeeeee", width=2)
        label = value.upper()
        if value == "Reverse":
            label = "↔"
        elif value == "Skip":
            label = "SKIP"
        elif value == "Wild":
            label = "WILD"
        elif value == "+4":
            label = "+4"
        box = draw.textbbox((0, 0), label, font=card_font)
        tw, th = box[2] - box[0], box[3] - box[1]
        ink = "#222222"
        draw.text((x + (card_w - tw) / 2, y + (card_h - th) / 2 - 2), label, fill=ink, font=card_font)
        draw.text((x + 8, y + 7), label[:5], fill="white", font=small_font)
        draw.text((x + card_w - 32, y + card_h - 24), label[:5], fill="white", font=small_font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return BufferedInputFile(output.getvalue(), filename="alexa_uno_hand.png")


def _board_text(game, status=None):
    top = game["discard"][-1]
    lines = ["🃏 <b>ALEXA UNO</b>"]
    if status:
        lines.extend(["", status])
    lines.extend([
        "",
        f"🗂️ Top card: <b>{escape(_card_name(top))}</b>",
        f"🎨 Current colour: <b>{COLORS.get(game['color'], ('Wild', '', '🌈'))[0]}</b>",
        "",
        "👥 <b>Players</b>",
    ])
    for index, player in enumerate(game["players"]):
        hand_count = len(game["hands"].get(player["id"], []))
        marker = " ⬅️ <b>TURN</b>" if game["started"] and index == game["current"] else ""
        lines.append(f"• {escape(player['name'])}: {hand_count} cards{marker}")
    if not game["started"] and not game.get("finished"):
        lines.extend(["", f"Players joined: <b>{len(game['players'])}/8</b>", "Creator: press Start when at least 2 players have joined."])
    elif game.get("pending_wild"):
        lines.extend(["", "🌈 Waiting for the active player to choose a colour."])
    else:
        player = game["players"][game["current"]]
        lines.extend(["", f"🎯 Turn: <b>{escape(player['name'])}</b>", "Each player receives their own cards in the bot's private chat."])
    return "\n".join(lines)


async def _update_board(bot, game, status=None):
    try:
        await bot.edit_message_text(
            _board_text(game, status), chat_id=game["group_id"],
            message_id=game["group_message_id"], parse_mode="HTML",
            reply_markup=_lobby_keyboard(game["id"]) if not game["started"] and not game.get("finished") else None,
        )
    except Exception:
        pass


async def _send_or_update_hand(bot, game, user_id, status=None):
    cards = game["hands"].get(user_id, [])
    player = next((p for p in game["players"] if p["id"] == user_id), None)
    if not player:
        return
    if not game["started"]:
        return
    if game.get("pending_wild") and game["pending_wild"]["user_id"] == user_id:
        keyboard = _color_keyboard(game["id"])
        status = "Choose the colour for your Wild card."
    else:
        keyboard = _hand_keyboard(game, user_id)
    is_turn = game["players"][game["current"]]["id"] == user_id and not game.get("pending_wild")
    if status is None:
        status = "🟢 Your turn — tap a card button." if is_turn else f"⏳ Waiting for {game['players'][game['current']]['name']}."
    caption = f"🃏 <b>ALEXA UNO</b>\n{escape(status)}\nCards in your hand: <b>{len(cards)}</b>"
    photo = _render_hand_image(cards)
    message_id = game["dm_messages"].get(user_id)
    if message_id:
        try:
            previous_hand = game.get("rendered_hands", {}).get(user_id)
            if previous_hand == cards:
                # Most moves do not change every player's cards. Update the status
                # and buttons without uploading an identical image again.
                await bot.edit_message_caption(
                    chat_id=user_id, message_id=message_id, caption=caption,
                    parse_mode="HTML", reply_markup=keyboard,
                )
            else:
                await bot.edit_message_media(
                    chat_id=user_id, message_id=message_id,
                    media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                    reply_markup=keyboard,
                )
            game.setdefault("rendered_hands", {})[user_id] = list(cards)
            return
        except Exception as exc:
            # Telegram reports "message is not modified" when neither the caption
            # nor keyboard changed. That is not a reason to send duplicate DMs.
            if "message is not modified" in str(exc).lower():
                game.setdefault("rendered_hands", {})[user_id] = list(cards)
                return
    sent = await bot.send_photo(user_id, photo=photo, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    game["dm_messages"][user_id] = sent.message_id
    game.setdefault("rendered_hands", {})[user_id] = list(cards)


async def _refresh_hands(bot, game, status=None):
    for player in game["players"]:
        try:
            await _send_or_update_hand(bot, game, player["id"], status if player["id"] == game["players"][game["current"]]["id"] else None)
        except Exception:
            # A player can block the bot or have not pressed /start; notify in group.
            pass


def _advance(game, steps=1):
    game["current"] = (game["current"] + game["direction"] * steps) % len(game["players"])


async def _finish(bot, game, winner_id):
    winner = next(p for p in game["players"] if p["id"] == winner_id)
    game["started"] = False
    game["finished"] = True
    await _update_board(bot, game, f"🏆 <b>{escape(winner['name'])} wins!</b> UNO game finished.")
    for player in game["players"]:
        try:
            message_id = game["dm_messages"].get(player["id"])
            if message_id:
                await bot.edit_message_caption(
                    chat_id=player["id"], message_id=message_id,
                    caption=f"🏆 <b>{escape(winner['name'])} won the UNO game!</b>",
                    parse_mode="HTML", reply_markup=None,
                )
            else:
                await bot.send_message(player["id"], f"🏆 {escape(winner['name'])} won the UNO game in your group!")
        except Exception:
            pass
    _GROUP_GAME.pop(game["group_id"], None)
    _GAMES.pop(game["id"], None)


async def _penalize_uno(game, acting_user_id):
    pending = game.get("uno_pending", set())
    for user_id in list(pending):
        if user_id != acting_user_id:
            _draw_for(game, user_id, 2)
            pending.discard(user_id)


async def _begin_game(callback, game):
    if len(game["players"]) < 2:
        await callback.answer("At least 2 players must join.", show_alert=True)
        return
    game["deck"] = _make_deck()
    game["hands"] = {p["id"]: [game["deck"].pop() for _ in range(7)] for p in game["players"]}
    # Start on a number card. Select it directly instead of rotating action cards
    # through the deck, which can make startup logic harder to reason about.
    first_number = next(
        (index for index, card in enumerate(game["deck"])
         if card[0] in COLORS and card[1].isdigit()),
        None,
    )
    if first_number is None:
        game["discard"] = [("R", "0")]
    else:
        game["discard"] = [game["deck"].pop(first_number)]
    game["color"] = game["discard"][-1][0]
    game["current"] = 0
    game["direction"] = 1
    game["started"] = True
    game["uno_pending"] = set()
    game["pending_wild"] = None
    game["drawn_pending"] = {}
    game["dm_messages"] = {}
    game["rendered_hands"] = {}
    try:
        for player in game["players"]:
            photo = _render_hand_image(game["hands"][player["id"]])
            sent = await callback.bot.send_photo(
                player["id"], photo=photo,
                caption="🃏 <b>ALEXA UNO</b>\nThe game has started! Wait for your turn.",
                parse_mode="HTML",
            )
            game["dm_messages"][player["id"]] = sent.message_id
    except Exception:
        # Roll back partial startup: remove already-sent private hand messages so
        # nobody is left with cards from a game that never actually began.
        for user_id, message_id in list(game["dm_messages"].items()):
            try:
                await callback.bot.delete_message(chat_id=user_id, message_id=message_id)
            except Exception:
                pass
        game["started"] = False
        game["hands"] = {}
        game["deck"] = []
        game["discard"] = []
        game["dm_messages"] = {}
        game["rendered_hands"] = {}
        game["pending_wild"] = None
        game["drawn_pending"] = {}
        await _update_board(callback.bot, game, "⚠️ I couldn't DM every player. Each player must open ALEXA and press /start, then the creator can press Start Game again.")
        await callback.answer("A player must open the bot and press /start first.", show_alert=True)
        return
    await _update_board(callback.bot, game, "🚀 Game started! Cards have been sent privately.")
    await _refresh_hands(callback.bot, game)
    await callback.answer("UNO started!")


async def _create_lobby(message: Message, creator_user=None):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Start UNO in a Telegram group with /uno. Your cards will be sent privately by ALEXA.")
        return
    if message.chat.id in _GROUP_GAME:
        existing = _GAMES.get(_GROUP_GAME[message.chat.id])
        if existing:
            await message.reply("An UNO game is already active in this group.")
            return
    game_id = secrets.token_hex(3)
    creator = creator_user or message.from_user
    game = {
        "id": game_id, "group_id": message.chat.id, "creator_id": creator.id,
        "players": [{"id": creator.id, "name": creator.full_name or creator.first_name or "Player"}],
        "hands": {}, "deck": [], "discard": [], "color": "R", "current": 0,
        "direction": 1, "started": False, "dm_messages": {}, "uno_pending": set(),
        "pending_wild": None, "drawn_pending": {},
    }
    sent = await message.answer(
        _board_text(game, "🎮 Join the game, then the creator can start."),
        parse_mode="HTML", reply_markup=_lobby_keyboard(game_id),
    )
    game["group_message_id"] = sent.message_id
    _GAMES[game_id] = game
    _GROUP_GAME[message.chat.id] = game_id


@router.message(Command("uno"))
async def cmd_uno(message: Message, **kwargs):
    await _create_lobby(message)


@router.callback_query(F.data == "game:uno")
async def menu_uno(callback: CallbackQuery, **kwargs):
    if callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Open the Games menu in your group, then choose UNO.", show_alert=True)
        return
    await _create_lobby(callback.message, creator_user=callback.from_user)
    await callback.answer()


@router.callback_query(F.data.startswith("uno:join:"))
@_serialize_game_action
async def join_uno(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    if not game or game["started"]:
        await callback.answer("This UNO lobby is no longer open.", show_alert=True)
        return
    user = callback.from_user
    if any(p["id"] == user.id for p in game["players"]):
        await callback.answer("You're already in this game.")
        return
    if len(game["players"]) >= 8:
        await callback.answer("UNO is full (8 players maximum).", show_alert=True)
        return
    game["players"].append({"id": user.id, "name": user.full_name or user.first_name or "Player"})
    await _update_board(callback.bot, game, f"🙋 {escape(user.full_name or user.first_name or 'Player')} joined UNO.")
    await callback.answer("Joined! Open ALEXA and press /start if you haven't before.")


@router.callback_query(F.data.startswith("uno:start:"))
@_serialize_game_action
async def start_uno(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    if not game or game["started"]:
        await callback.answer("This UNO lobby is no longer open.", show_alert=True)
        return
    if callback.from_user.id != game["creator_id"]:
        await callback.answer("Only the player who created this lobby can start it.", show_alert=True)
        return
    await _begin_game(callback, game)


@router.callback_query(F.data.startswith("uno:play:"))
@_serialize_game_action
async def play_uno_card(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid card action.", show_alert=True)
        return
    game = _GAMES.get(parts[2])
    user_id = callback.from_user.id
    if not game or not game["started"]:
        await callback.answer("This game has ended.", show_alert=True)
        return
    if game.get("pending_wild"):
        await callback.answer("Choose a colour first.", show_alert=True)
        return
    if game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    try:
        index = int(parts[3])
        card = game["hands"][user_id][index]
    except (ValueError, IndexError, KeyError):
        await callback.answer("That card is no longer in your hand. Refresh your DM.", show_alert=True)
        return
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    if drawn_index is not None and index != drawn_index:
        await callback.answer("After drawing, you can only play the card you just drew.", show_alert=True)
        return
    if not _legal(game, card, user_id):
        await callback.answer("You can't play that card on the current colour/card.", show_alert=True)
        return
    await _penalize_uno(game, user_id)
    game.get("drawn_pending", {}).pop(user_id, None)
    game["hands"][user_id].pop(index)
    game["discard"].append(card)
    if not game["hands"][user_id]:
        await callback.answer("UNO!")
        await _finish(callback.bot, game, user_id)
        return
    if len(game["hands"][user_id]) == 1:
        game["uno_pending"].add(user_id)
    if card[0] == "W":
        game["pending_wild"] = {"user_id": user_id, "value": card[1]}
        await _update_board(callback.bot, game, f"🌈 {escape(next(p['name'] for p in game['players'] if p['id'] == user_id))} played {escape(_card_name(card))} and must choose a colour.")
        try:
            await callback.message.edit_reply_markup(reply_markup=_color_keyboard(game["id"]))
        except Exception:
            pass
        await callback.answer("Choose a colour.")
        await _refresh_hands(callback.bot, game)
        return
    game["color"] = card[0]
    value = card[1]
    if value == "Reverse":
        game["direction"] *= -1
        _advance(game, 2 if len(game["players"]) == 2 else 1)
    elif value == "Skip":
        _advance(game, 2)
    elif value == "+2":
        next_index = (game["current"] + game["direction"]) % len(game["players"])
        _draw_for(game, game["players"][next_index]["id"], 2)
        _advance(game, 2)
    else:
        _advance(game)
    await _update_board(callback.bot, game, f"🃏 Played {_card_emoji(card)}.")
    await callback.answer("Card played!")
    await _refresh_hands(callback.bot, game)


@router.callback_query(F.data.startswith("uno:color:"))
@_serialize_game_action
async def choose_uno_color(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid colour.", show_alert=True)
        return
    game = _GAMES.get(parts[2])
    user_id = callback.from_user.id
    color = parts[3]
    if not game or not game["started"] or not game.get("pending_wild"):
        await callback.answer("No colour choice is pending.", show_alert=True)
        return
    pending = game["pending_wild"]
    if pending["user_id"] != user_id or color not in COLORS:
        await callback.answer("Only the player who played Wild can choose its colour.", show_alert=True)
        return
    game["color"] = color
    value = pending["value"]
    game["pending_wild"] = None
    if value == "+4":
        next_index = (game["current"] + game["direction"]) % len(game["players"])
        _draw_for(game, game["players"][next_index]["id"], 4)
        _advance(game, 2)
    else:
        _advance(game)
    await _update_board(callback.bot, game, f"🌈 Colour changed to {COLORS[color][0]}.")
    await callback.answer(f"{COLORS[color][0]} selected!")
    await _refresh_hands(callback.bot, game)


@router.callback_query(F.data.startswith("uno:draw:"))
@_serialize_game_action
async def draw_uno_card(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    user_id = callback.from_user.id
    if not game or not game["started"]:
        await callback.answer("This game has ended.", show_alert=True)
        return
    if game.get("pending_wild") or game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    await _penalize_uno(game, user_id)
    _draw_for(game, user_id, 1)
    drawn_index = len(game["hands"][user_id]) - 1
    if _legal(game, game["hands"][user_id][drawn_index], user_id):
        game.setdefault("drawn_pending", {})[user_id] = drawn_index
        await _update_board(callback.bot, game, f"➕ {escape(next(p['name'] for p in game['players'] if p['id'] == user_id))} drew a playable card and must play it or pass.")
        await callback.answer("Playable card drawn — play it or pass.")
    else:
        _advance(game)
        await _update_board(callback.bot, game, f"➕ {escape(next(p['name'] for p in game['players'] if p['id'] == user_id))} drew a card and passed.")
        await callback.answer("Card drawn; turn passed.")
    await _refresh_hands(callback.bot, game)


@router.callback_query(F.data.startswith("uno:pass:"))
@_serialize_game_action
async def pass_after_draw(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    user_id = callback.from_user.id
    if not game or not game["started"] or game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    if game.get("drawn_pending", {}).pop(user_id, None) is None:
        await callback.answer("There is no drawn card to pass.", show_alert=True)
        return
    _advance(game)
    name = next((p["name"] for p in game["players"] if p["id"] == user_id), "Player")
    await _update_board(callback.bot, game, f"⏭️ {escape(name)} passed after drawing.")
    await callback.answer("Turn passed.")
    await _refresh_hands(callback.bot, game)


@router.callback_query(F.data.startswith("uno:call:"))
@_serialize_game_action
async def call_uno(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    user_id = callback.from_user.id
    if not game or not game["started"] or len(game["hands"].get(user_id, [])) != 1:
        await callback.answer("You can call UNO when you have one card.", show_alert=True)
        return
    if user_id not in game.get("uno_pending", set()):
        await callback.answer("UNO already called!")
        return
    game["uno_pending"].discard(user_id)
    name = next((p["name"] for p in game["players"] if p["id"] == user_id), "Player")
    await _update_board(callback.bot, game, f"📣 {escape(name)} called UNO!")
    await callback.answer("UNO! 🎉")
    await _refresh_hands(callback.bot, game)
