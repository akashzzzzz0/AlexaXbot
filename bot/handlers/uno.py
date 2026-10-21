"""Chat-based multiplayer UNO for ALEXA. Active matches live in memory."""
import asyncio
import random
import secrets
import io
from functools import wraps
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQuery, ChosenInlineResult, InlineQueryResultArticle, InputTextMessageContent,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

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


def _add_uno_call_buttons(builder, game):
    for player in game["players"]:
        if player["id"] in game.get("uno_pending", set()):
            builder.button(
                text=f"📣 UNO! — {player['name']}"[:60],
                callback_data=f"uno:call:{game['id']}",
            )


def _group_turn_keyboard(game):
    """Keep card and colour choices private; group shows only the action button."""
    if not game.get("started") or game.get("finished"):
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🎴 Make Your Choice", callback_data=f"uno:hand:{game['id']}")
    _add_uno_call_buttons(builder, game)
    builder.adjust(1)
    return builder.as_markup()


def _render_hand_image(hand):
    """Render the player's actual UNO-style card faces for private viewing."""
    card_w, card_h, gap = 150, 220, 14
    columns = min(4, max(1, len(hand)))
    rows = (len(hand) + columns - 1) // columns
    image = Image.new("RGB", (columns * card_w + (columns + 1) * gap, rows * card_h + (rows + 1) * gap), "#176B48")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        small = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    except OSError:
        font = small = ImageFont.load_default()
    fills = {"R": "#e53935", "G": "#168447", "B": "#1976d2", "Y": "#f4c20d", "W": "#222222"}
    for i, (color, value) in enumerate(hand):
        x, y = gap + (i % columns) * (card_w + gap), gap + (i // columns) * (card_h + gap)
        fill = fills.get(color, "#222222")
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=18, fill=fill, outline="white", width=5)
        draw.ellipse((x + 15, y + 35, x + card_w - 15, y + card_h - 35), fill="white")
        label = "WILD" if value == "Wild" else str(value)
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text((x + (card_w - bbox[2] + bbox[0]) / 2, y + (card_h - bbox[3] + bbox[1]) / 2), label, fill=fill, font=font)
        draw.text((x + 8, y + 8), _card_emoji((color, value)), fill="white", font=small)
        draw.text((x + 8, y + card_h - 25), f"#{i + 1}", fill="white", font=small)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    output.name = "uno_cards.png"
    return output


def _private_hand_keyboard(game, user_id):
    builder = InlineKeyboardBuilder()
    hand = game["hands"].get(user_id, [])
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    for index, card in enumerate(hand):
        if (drawn_index is None or drawn_index == index) and _legal(game, card, user_id):
            builder.button(text=f"▶️ Play {_card_emoji(card)}", callback_data=f"uno:privateplay:{game['id']}:{index}")
    if drawn_index is None:
        builder.button(text="➕ Draw card", callback_data=f"uno:privatedraw:{game['id']}")
    else:
        builder.button(text="⏭️ Pass turn", callback_data=f"uno:privatepass:{game['id']}")
    builder.adjust(1)
    return builder.as_markup()


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
        prompt = "👇 Tap <b>Make your choice</b>; only you can see your cards before playing."
        lines.extend(["", f"🎯 Next player: <b>{escape(player['name'])}</b>", prompt])
    return "\n".join(lines)


async def _update_board(bot, game, status=None):
    if not game.get("started") and not game.get("finished"):
        keyboard = _lobby_keyboard(game["id"])
    elif game.get("finished"):
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


async def _refresh_hands(bot, game, status=None):
    """Refresh the single public group board; no private hand messages are sent."""
    await _update_board(bot, game, status)


def _advance(game, steps=1):
    game["current"] = (game["current"] + game["direction"] * steps) % len(game["players"])
    game["choice_open"] = False


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
    await callback.answer("Use Make your choice to open your private card picker.")


@router.callback_query(F.data.startswith("uno:hand:"))
async def open_private_hand(callback: CallbackQuery):
    parts = callback.data.split(":")
    game = _GAMES.get(parts[2]) if len(parts) == 3 else None
    user_id = callback.from_user.id
    if not game or not game.get("started") or game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn or the game has ended.", show_alert=True)
        return
    try:
        if game.get("pending_wild"):
            pending = game["pending_wild"]
            if pending["user_id"] != user_id:
                await callback.answer("Only the player who played Wild can choose its colour.", show_alert=True)
                return
            builder = InlineKeyboardBuilder()
            for color, (name, _, emoji) in COLORS.items():
                builder.button(text=f"{emoji} {name}", callback_data=f"uno:privatecolor:{game['id']}:{color}")
            builder.adjust(2)
            await callback.bot.send_message(user_id, "🌈 Choose the colour for your Wild card:", reply_markup=builder.as_markup())
        else:
            hand = game["hands"].get(user_id, [])
            await callback.bot.send_photo(user_id, _render_hand_image(hand), caption="🎴 Your UNO cards — tap a Play button below.", reply_markup=_private_hand_keyboard(game, user_id))
        await callback.answer("Private card chooser opened.")
    except Exception:
        await callback.answer("Open ALEXA in private chat and send /start first.", show_alert=True)


@router.callback_query(F.data.startswith("uno:privateplay:"))
@_serialize_game_action
async def play_private_uno_card(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    game = _GAMES.get(parts[2]) if len(parts) == 4 else None
    user_id = callback.from_user.id
    if not game or not game.get("started") or game["players"][game["current"]]["id"] != user_id or game.get("pending_wild"):
        await callback.answer("It's not your turn or the game has ended.", show_alert=True)
        return
    try:
        index = int(parts[3])
        card = game["hands"][user_id][index]
    except (ValueError, IndexError, KeyError):
        await callback.answer("Card changed; open the chooser again.", show_alert=True)
        return
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    if (drawn_index is not None and index != drawn_index) or not _legal(game, card, user_id):
        await callback.answer("That card cannot be played now.", show_alert=True)
        return
    player = next(p for p in game["players"] if p["id"] == user_id)
    await _penalize_uno(game, user_id)
    game.get("drawn_pending", {}).pop(user_id, None)
    game["hands"][user_id].pop(index)
    game["discard"].append(card)
    if not game["hands"][user_id]:
        await _finish(callback.bot, game, user_id)
        await callback.answer("You won UNO!")
        return
    if len(game["hands"][user_id]) == 1:
        game["uno_pending"].add(user_id)
    if card[0] == "W":
        game["pending_wild"] = {"user_id": user_id, "value": card[1]}
        await _update_board(callback.bot, game, f"🌈 {escape(player['name'])} played {_card_emoji(card)}; choose a colour privately.")
        builder = InlineKeyboardBuilder()
        for color, (name, _, emoji) in COLORS.items():
            builder.button(text=f"{emoji} {name}", callback_data=f"uno:privatecolor:{game['id']}:{color}")
        builder.adjust(2)
        await callback.bot.send_message(user_id, "🌈 Choose the colour for your Wild card:", reply_markup=builder.as_markup())
        await callback.answer("Wild card played.")
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
    await _update_board(callback.bot, game, f"🃏 {escape(player['name'])} played {_card_emoji(card)}.")
    await callback.answer("Card played!")
    try:
        await callback.message.edit_caption(caption="✅ Card played. Check the UNO board in your group.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("uno:privatecolor:"))
@_serialize_game_action
async def choose_private_uno_color(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    game = _GAMES.get(parts[2]) if len(parts) == 4 else None
    color, user_id = parts[3] if len(parts) == 4 else "", callback.from_user.id
    pending = game.get("pending_wild") if game else None
    if not game or not game.get("started") or not pending or pending["user_id"] != user_id or color not in COLORS:
        await callback.answer("Colour choice is no longer available.", show_alert=True)
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
    try:
        await callback.message.edit_text(f"✅ Colour selected: {COLORS[color][0]}. Check the group board.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("uno:privatedraw:"))
@_serialize_game_action
async def draw_private_uno_card(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    game = _GAMES.get(parts[2]) if len(parts) == 3 else None
    user_id = callback.from_user.id
    if not game or not game.get("started") or game["players"][game["current"]]["id"] != user_id or game.get("pending_wild"):
        await callback.answer("It's not your turn.", show_alert=True)
        return
    await _penalize_uno(game, user_id)
    _draw_for(game, user_id, 1)
    drawn_index = len(game["hands"][user_id]) - 1
    if _legal(game, game["hands"][user_id][drawn_index], user_id):
        game.setdefault("drawn_pending", {})[user_id] = drawn_index
        await _update_board(callback.bot, game, "➕ A playable card was drawn; play it or pass.")
    else:
        _advance(game)
        await _update_board(callback.bot, game, "➕ A card was drawn; turn passed.")
    await callback.answer("Card drawn.")
    try:
        await callback.bot.send_photo(user_id, _render_hand_image(game["hands"].get(user_id, [])), caption="🎴 Your updated UNO cards:", reply_markup=_private_hand_keyboard(game, user_id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("uno:privatepass:"))
@_serialize_game_action
async def pass_private_uno_turn(callback: CallbackQuery, **kwargs):
    parts = callback.data.split(":")
    game = _GAMES.get(parts[2]) if len(parts) == 3 else None
    user_id = callback.from_user.id
    if not game or not game.get("started") or game["players"][game["current"]]["id"] != user_id:
        await callback.answer("It's not your turn.", show_alert=True)
        return
    if game.get("drawn_pending", {}).pop(user_id, None) is None:
        await callback.answer("No drawn card to pass.", show_alert=True)
        return
    _advance(game)
    await _update_board(callback.bot, game, "⏭️ Player passed after drawing.")
    await callback.answer("Turn passed.")
    try:
        await callback.message.edit_caption(caption="⏭️ Turn passed. Check the UNO board in your group.")
    except Exception:
        pass


@router.inline_query()
async def uno_private_card_picker(query: InlineQuery):
    """Return only the current player's cards in their personal inline results."""
    parts = (query.query or "").strip().split("_")
    if len(parts) != 3 or parts[0] != "uno":
        return
    game = _GAMES.get(parts[1])
    try:
        user_id = int(parts[2])
    except ValueError:
        return
    if not game or not game.get("started") or user_id != query.from_user.id:
        return
    if game.get("pending_wild") or game["players"][game["current"]]["id"] != user_id:
        return
    hand = game["hands"].get(user_id, [])
    drawn_index = game.get("drawn_pending", {}).get(user_id)
    results = []
    for index, card in enumerate(hand):
        if drawn_index is not None and index != drawn_index:
            continue
        if not _legal(game, card, user_id):
            continue
        label = _card_emoji(card)
        results.append(InlineQueryResultArticle(
            id=f"{game['id']}:{user_id}:{index}:{secrets.token_hex(3)}",
            title=f"Play {label}",
            description="Only you see this card picker",
            input_message_content=InputTextMessageContent(
                message_text=f"🃏 {escape(query.from_user.full_name or query.from_user.first_name or 'Player')} played {escape(label)}"
            ),
        ))
    if drawn_index is not None:
        results.append(InlineQueryResultArticle(
            id=f"{game['id']}:{user_id}:pass:{secrets.token_hex(3)}",
            title="⏭️ Pass turn",
            description="Keep the card you drew and end your turn",
            input_message_content=InputTextMessageContent(
                message_text=f"⏭️ {escape(query.from_user.full_name or query.from_user.first_name or 'Player')} passed"
            ),
        ))
    if not results:
        results.append(InlineQueryResultArticle(
            id=f"{game['id']}:{user_id}:draw:{secrets.token_hex(3)}",
            title="➕ Draw a card",
            description="No playable card in your hand",
            input_message_content=InputTextMessageContent(
                message_text=f"➕ {escape(query.from_user.full_name or query.from_user.first_name or 'Player')} draws a card"
            ),
        ))
    await query.answer(results, cache_time=0, is_personal=True, switch_pm_text="Open ALEXA", switch_pm_parameter="start")


@router.chosen_inline_result()
async def uno_card_chosen(chosen: ChosenInlineResult, bot):
    """Apply a selected private-picker result after Telegram posts it to the group."""
    parts = (chosen.result_id or "").split(":")
    if len(parts) != 4:
        return
    game_id, raw_user, action, nonce = parts
    try:
        user_id = int(raw_user)
    except ValueError:
        return
    game = _GAMES.get(game_id)
    if not game:
        return
    lock = game.setdefault("_action_lock", asyncio.Lock())
    async with lock:
        if not game.get("started") or chosen.from_user.id != user_id:
            return
        if game["players"][game["current"]]["id"] != user_id or game.get("pending_wild"):
            return
        player = next((p for p in game["players"] if p["id"] == user_id), None)
        if not player:
            return
        if action == "draw":
            await _penalize_uno(game, user_id)
            _draw_for(game, user_id, 1)
            drawn_index = len(game["hands"][user_id]) - 1
            if _legal(game, game["hands"][user_id][drawn_index], user_id):
                game.setdefault("drawn_pending", {})[user_id] = drawn_index
                await _update_board(bot, game, f"➕ {escape(player['name'])} drew a playable card.")
            else:
                game.get("drawn_pending", {}).pop(user_id, None)
                _advance(game)
                await _update_board(bot, game, f"➕ {escape(player['name'])} drew a card and passed.")
            return
        if action == "pass":
            if user_id not in game.get("drawn_pending", {}):
                return
            game["drawn_pending"].pop(user_id, None)
            _advance(game)
            await _update_board(bot, game, f"⏭️ {escape(player['name'])} passed.")
            return
        try:
            index = int(action)
            card = game["hands"][user_id][index]
        except (ValueError, IndexError, KeyError):
            return
        drawn_index = game.get("drawn_pending", {}).get(user_id)
        if drawn_index is not None and index != drawn_index:
            return
        if not _legal(game, card, user_id):
            return
        await _penalize_uno(game, user_id)
        game.get("drawn_pending", {}).pop(user_id, None)
        game["hands"][user_id].pop(index)
        game["discard"].append(card)
        if not game["hands"][user_id]:
            await _finish(bot, game, user_id)
            return
        if len(game["hands"][user_id]) == 1:
            game["uno_pending"].add(user_id)
        if card[0] == "W":
            game["pending_wild"] = {"user_id": user_id, "value": card[1]}
            await _update_board(bot, game, f"🌈 {escape(player['name'])} played {escape(_card_name(card))}; choose a colour.")
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
        await _update_board(bot, game, f"🃏 {escape(player['name'])} played {_card_emoji(card)}.")


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
        game["choice_open"] = True
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
