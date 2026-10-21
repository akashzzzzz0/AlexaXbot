"""Chat-based multiplayer UNO for ALEXA. Active matches live in memory."""
import asyncio
import random
import secrets
from functools import wraps
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
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


def _render_card_image(card):
    """Create a compact UNO-style PNG for the group when a card is played."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    color, value = card
    palette = {
        "R": (220, 48, 48),
        "G": (30, 145, 78),
        "B": (35, 112, 210),
        "Y": (238, 190, 30),
        "W": (104, 72, 180),
    }
    bg = palette.get(color, palette["W"])
    image = Image.new("RGB", (420, 600), (28, 35, 48))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 18, 398, 582), radius=34, fill=bg, outline="white", width=8)
    draw.ellipse((55, 145, 365, 455), fill=(255, 255, 255), outline=(245, 245, 245), width=4)
    try:
        font_large = ImageFont.truetype("DejaVuSans.ttf", 92)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 30)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    label = {"Skip": "SKIP", "Reverse": "REVERSE", "+2": "+2", "+4": "+4", "Wild": "WILD"}.get(value, value)
    draw.text((210, 290), label, fill=bg, font=font_large, anchor="mm", stroke_width=1)
    draw.text((48, 35), label, fill="white", font=font_small)
    draw.text((280, 520), label, fill="white", font=font_small)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return BufferedInputFile(buffer.getvalue(), filename="alexa_uno_card.png")


def _render_hand_image(hand):
    """Render UNO-style card faces; card numbers correspond to selection buttons."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    card_w, card_h, gap = 132, 190, 12
    columns = min(4, max(1, len(hand)))
    rows = (len(hand) + columns - 1) // columns
    image = Image.new("RGB", (columns * card_w + (columns + 1) * gap, rows * card_h + (rows + 1) * gap), "#176B48")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
        small = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        font = small = ImageFont.load_default()
    fills = {"R": "#e53935", "G": "#168447", "B": "#1976d2", "Y": "#f4c20d", "W": "#242424"}
    for i, (color, value) in enumerate(hand):
        x = gap + (i % columns) * (card_w + gap)
        y = gap + (i // columns) * (card_h + gap)
        fill = fills.get(color, "#242424")
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=16, fill=fill, outline="white", width=4)
        draw.ellipse((x + 13, y + 32, x + card_w - 13, y + card_h - 32), fill="white")
        label = {"Skip": "SKIP", "Reverse": "REVERSE", "+2": "+2", "+4": "WILD +4", "Wild": "WILD"}.get(value, str(value).upper())
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x + (card_w - tw) / 2, y + (card_h - th) / 2 - bbox[1]), label, fill=fill, font=font)
        draw.text((x + 7, y + 7), f"#{i + 1}", fill="white", font=small)
        draw.text((x + 7, y + card_h - 22), _card_emoji((color, value))[:18], fill="white", font=small)
    output = BytesIO()
    image.save(output, format="PNG")
    return BufferedInputFile(output.getvalue(), filename="alexa_uno_hand.png")


def _hand_picker_keyboard(game):
    """Card buttons select only; a separate confirmation button plays the selection."""
    game_id = game["id"]
    user_id = game["players"][game["current"]]["id"]
    hand = game["hands"].get(user_id, [])
    selected = game.get("selected_card_index")
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    builder = InlineKeyboardBuilder()
    indices = [drawn_index] if drawn_index is not None else list(range(len(hand)))
    for index in indices:
        if index is None or not 0 <= index < len(hand):
            continue
        card = hand[index]
        marker = "✅" if selected == index else "🃏"
        builder.button(text=f"{marker} #{index + 1} {_card_emoji(card)}"[:60], callback_data=f"uno:select:{game_id}:{index}")
    builder.adjust(2)
    builder.button(text="▶️ Play selected card", callback_data=f"uno:confirm:{game_id}")
    if drawn_index is None:
        builder.button(text="➕ Draw a card", callback_data=f"uno:draw:{game_id}")
    else:
        builder.button(text="⏭️ Pass turn", callback_data=f"uno:pass:{game_id}")
    builder.adjust(2, 1, 1)
    _add_uno_call_buttons(builder, game)
    return builder.as_markup()


async def _send_choice_picker(bot, game):
    """Send the active player's card faces plus select-then-play buttons to the group."""
    user = game["players"][game["current"]]
    user_id = user["id"]
    hand = game["hands"].get(user_id, [])
    if not hand:
        game["choice_open"] = False
        await _update_board(bot, game, "Your hand is empty.")
        return
    await _remove_choice_message(bot, game)
    game["choice_open"] = True
    game["selected_card_index"] = None
    await _update_board(bot, game, f"🎴 <b>{escape(user['name'])}</b>, make your choice!")
    try:
        sent = await bot.send_photo(
            chat_id=game["group_id"],
            photo=_render_hand_image(hand),
            caption=(f"🎴 <b>{escape(user['name'])}'s cards</b> — tap a card button to select it, then press <b>Play selected card</b>.\n"
                     "⚠️ Cards are visible to everyone in this group."),
            parse_mode="HTML",
            reply_markup=_hand_picker_keyboard(game),
        )
        game["choice_message_id"] = sent.message_id
    except Exception:
        game["choice_open"] = False
        await _update_board(bot, game, "⚠️ Could not show the card image. Tap Make your choice again.")
        raise


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
    """Draw up to count cards and return the number actually drawn."""
    drawn = 0
    for _ in range(count):
        if not game["deck"]:
            top = game["discard"][-1:]
            recyclable = game["discard"][:-1]
            random.shuffle(recyclable)
            game["deck"] = recyclable
            game["discard"] = top
        if not game["deck"]:
            break
        game["hands"][user_id].append(game["deck"].pop())
        drawn += 1
    return drawn


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


def _add_uno_call_buttons(builder, game):
    for player in game["players"]:
        if player["id"] in game.get("uno_pending", set()):
            builder.button(
                text=f"📣 UNO! — {player['name']}"[:60],
                callback_data=f"uno:call:{game['id']}",
            )


def _group_turn_keyboard(game):
    """Render the active player's hand as public group buttons, never DM buttons."""
    if not game.get("started") or game.get("finished"):
        return None
    if game.get("pending_wild"):
        return _color_keyboard(game["id"])
    player = game["players"][game["current"]]
    user_id = player["id"]
    builder = InlineKeyboardBuilder()
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    hand = game["hands"].get(user_id, [])
    if not game.get("choice_open") and drawn_index is None:
        builder.button(text="🎴 Make your choice", callback_data=f"uno:choose:{game['id']}")
        _add_uno_call_buttons(builder, game)
        builder.adjust(1)
        return builder.as_markup()
    # Hand images and selection buttons appear only after Make your choice is tapped.
    return None


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
        prompt = "👇 Choose a card below." if game.get("choice_open") else "👇 Tap <b>Make your choice</b> to reveal the cards in this group."
        lines.extend(["", f"🎯 Next player: <b>{escape(player['name'])}</b>", prompt])
    return "\n".join(lines)


async def _update_board(bot, game, status=None):
    if not game.get("started") and not game.get("finished"):
        keyboard = _lobby_keyboard(game["id"])
    elif game.get("finished"):
        keyboard = None
    elif game.get("choice_open"):
        keyboard = None
    else:
        keyboard = _group_turn_keyboard(game)
    try:
        await bot.edit_message_text(
            _board_text(game, status), chat_id=game["group_id"],
            message_id=game["group_message_id"], parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        pass


async def _remove_choice_message(bot, game):
    """Delete stale hand-image controls after a move or turn change."""
    message_id = game.pop("choice_message_id", None)
    if message_id:
        try:
            await bot.delete_message(chat_id=game["group_id"], message_id=message_id)
        except Exception:
            pass


async def _refresh_hands(bot, game, status=None):
    """Refresh the single public group board; no private hand messages are sent."""
    await _update_board(bot, game, status)


def _advance(game, steps=1):
    game["current"] = (game["current"] + game["direction"] * steps) % len(game["players"])
    game["choice_open"] = False
    game["selected_card_index"] = None


async def _finish(bot, game, winner_id):
    winner = next(p for p in game["players"] if p["id"] == winner_id)
    game["started"] = False
    game["finished"] = True
    await _update_board(bot, game, f"🏆 <b>{escape(winner['name'])} wins!</b> UNO game finished.")
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
    game["finished"] = False
    game["choice_open"] = False
    game["selected_card_index"] = None
    game["choice_message_id"] = None
    game["uno_pending"] = set()
    game["pending_wild"] = None
    game["drawn_pending"] = {}
    await _update_board(callback.bot, game, "🚀 Game started! Cards and choices are shown in this group.")
    await callback.answer("UNO started in the group!")


async def _create_lobby(message: Message, creator_user=None):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Start UNO in a Telegram group with /uno. Cards and turns are shown inside the group.")
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
        # A lobby board renders the top-card row before the game starts.
        # Seed a harmless placeholder card; _begin_game replaces it at startup.
        "hands": {}, "deck": [], "discard": [("R", "0")], "color": "R", "current": 0,
        "direction": 1, "started": False, "uno_pending": set(),
        "pending_wild": None, "drawn_pending": {},
    }
    sent = await message.answer(
        _board_text(game, "🎮 Join the game, then the creator can start."),
        parse_mode="HTML", reply_markup=_lobby_keyboard(game_id),
    )
    game["group_message_id"] = sent.message_id
    _GAMES[game_id] = game
    _GROUP_GAME[message.chat.id] = game_id


async def _restart_existing_game(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Use /unorestart inside the group where UNO is running.")
        return
    game_id = _GROUP_GAME.get(message.chat.id)
    game = _GAMES.get(game_id) if game_id else None
    if not game:
        await message.reply("There is no active UNO game to restart. Use /uno to create one.")
        return
    if not message.from_user or message.from_user.id != game["creator_id"]:
        await message.reply("Only the player who created this UNO game can restart it.")
        return
    if len(game["players"]) < 2:
        await message.reply("At least 2 players must join before restarting UNO.")
        return

    await _remove_choice_message(message.bot, game)
    game["deck"] = _make_deck()
    game["hands"] = {
        player["id"]: [game["deck"].pop() for _ in range(7)]
        for player in game["players"]
    }
    first_number = next(
        (index for index, card in enumerate(game["deck"])
         if card[0] in COLORS and card[1].isdigit()),
        None,
    )
    game["discard"] = [game["deck"].pop(first_number)] if first_number is not None else [("R", "0")]
    game["color"] = game["discard"][-1][0]
    game["current"] = 0
    game["direction"] = 1
    game["started"] = True
    game["finished"] = False
    game["choice_open"] = False
    game["selected_card_index"] = None
    game["choice_message_id"] = None
    game["uno_pending"] = set()
    game["pending_wild"] = None
    game["drawn_pending"] = {}
    await _update_board(message.bot, game, "🔄 UNO restarted! Same players, fresh cards.")
    

@router.message(Command("uno"))
async def cmd_uno(message: Message, **kwargs):
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].strip().lower() in {"restart", "reset"}:
        await _restart_existing_game(message)
        return
    await _create_lobby(message)


@router.message(Command("unorestart"))
async def cmd_unorestart(message: Message, **kwargs):
    await _restart_existing_game(message)


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
    await callback.answer("Joined UNO! The game will run inside this group.")


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


@router.callback_query(F.data.startswith("uno:choose:"))
@_serialize_game_action
async def choose_uno_cards(callback: CallbackQuery, **kwargs):
    game_id = callback.data.split(":")[2]
    game = _GAMES.get(game_id)
    if not game or not game.get("started"):
        await callback.answer("This UNO game has ended.", show_alert=True)
        return
    if game.get("pending_wild"):
        await callback.answer("Choose a colour first.", show_alert=True)
        return
    if game["players"][game["current"]]["id"] != callback.from_user.id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    try:
        await _send_choice_picker(callback.bot, game)
    except Exception:
        await callback.answer("Could not show cards. Please try again.", show_alert=True)
        return
    await callback.answer("Card pictures shown. Select a card, then tap Play selected card.")



@router.callback_query(F.data.startswith("uno:select:"))
@_serialize_game_action
async def select_uno_card(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid card selection.", show_alert=True)
        return
    game = _GAMES.get(parts[2])
    user_id = callback.from_user.id
    if not game or not game.get("started") or not game.get("choice_open"):
        await callback.answer("Open Make your choice again.", show_alert=True)
        return
    if game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    try:
        index = int(parts[3])
        hand = game["hands"][user_id]
        if not 0 <= index < len(hand):
            raise IndexError
    except (ValueError, IndexError, KeyError):
        await callback.answer("That card is no longer in your hand.", show_alert=True)
        return
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    if drawn_index is not None and index != drawn_index:
        await callback.answer("After drawing, only the drawn card can be selected.", show_alert=True)
        return
    game["selected_card_index"] = index
    try:
        await callback.message.edit_reply_markup(reply_markup=_hand_picker_keyboard(game))
    except Exception:
        await callback.answer("Card selected, but buttons could not refresh. Reopen your cards.", show_alert=True)
        return
    await callback.answer(f"Selected card #{index + 1}. Tap Play selected card to play it.")


@router.callback_query(F.data.startswith("uno:confirm:"))
async def confirm_uno_card(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid play action.", show_alert=True)
        return
    game = _GAMES.get(parts[2])
    user_id = callback.from_user.id
    if not game or not game.get("started") or not game.get("choice_open"):
        await callback.answer("Tap Make your choice to open your cards.", show_alert=True)
        return
    if game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    index = game.get("selected_card_index")
    if index is None:
        await callback.answer("First tap a card to select it.", show_alert=True)
        return
    callback.data = f"uno:play:{game['id']}:{index}"
    await play_uno_card(callback)


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
        await callback.answer("That card is no longer in the current hand. Use the latest group buttons.", show_alert=True)
        return
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    if drawn_index is not None and index != drawn_index:
        await callback.answer("After drawing, you can only play the card you just drew.", show_alert=True)
        return
    if not _legal(game, card, user_id):
        await callback.answer("You can't play that card on the current colour/card.", show_alert=True)
        return
    await _penalize_uno(game, user_id)
    await _remove_choice_message(callback.bot, game)
    game.get("drawn_pending", {}).pop(user_id, None)
    game["selected_card_index"] = None
    game["choice_open"] = False
    game["hands"][user_id].pop(index)
    game["discard"].append(card)
    # Telegram inline buttons cannot contain images, so post the played card as a
    # group photo while keeping the live board and Make your choice buttons intact.
    try:
        await callback.bot.send_photo(
            chat_id=game["group_id"],
            photo=_render_card_image(card),
            caption=f"🃏 <b>{escape(next(p['name'] for p in game['players'] if p['id'] == user_id))}</b> played <b>{escape(_card_name(card))}</b>.",
            parse_mode="HTML",
        )
    except Exception:
        # A missing Pillow install or photo-send failure must never break game turns.
        pass
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
    await _remove_choice_message(callback.bot, game)
    game["choice_open"] = False
    game["selected_card_index"] = None
    before_count = len(game["hands"].get(user_id, []))
    _draw_for(game, user_id, 1)
    if len(game["hands"].get(user_id, [])) == before_count:
        await callback.answer("No cards remain in the draw pile. Try playing a legal card.", show_alert=True)
        return
    drawn_index = len(game["hands"][user_id]) - 1
    if _legal(game, game["hands"][user_id][drawn_index], user_id):
        game.setdefault("drawn_pending", {})[user_id] = drawn_index
        game["choice_open"] = True
        await _update_board(callback.bot, game, f"➕ {escape(next(p['name'] for p in game['players'] if p['id'] == user_id))} drew a playable card.")
        try:
            await _send_choice_picker(callback.bot, game)
        except Exception:
            await callback.answer("Drawn card is ready, but its image picker could not be shown.", show_alert=True)
            return
        await callback.answer("Select the drawn card, then tap Play selected card — or Pass turn.")
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
