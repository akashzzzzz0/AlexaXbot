"""UNO Mini App API; authenticated with Telegram WebApp initData."""
import asyncio
import hashlib
import hmac
import json
import time
from html import escape
from urllib.parse import parse_qsl

from aiohttp import web
from bot.config import settings


def verify_init_data(request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("tma "):
        return None
    try:
        data = dict(parse_qsl(header[4:], keep_blank_values=True))
        supplied = data.pop("hash")
        stamp = int(data.get("auth_date", "0"))
        if not stamp or stamp > time.time() + 60 or time.time() - stamp > 86400:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return None
        user = json.loads(data["user"])
        return int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def setup_uno_web_routes(app):
    from pathlib import Path
    from bot.handlers.uno import GAMES, COLORS, _valid, _board, _turn_prompt, _new_deck
    page = Path(__file__).resolve().parent.parent / "web" / "uno" / "index.html"
    action_lock = asyncio.Lock()

    def get_game(chat_id):
        game = GAMES.get(chat_id)
        if not game or not game.get("active") or not game.get("discard") or len(game.get("players", [])) < 2:
            raise web.HTTPNotFound(text='{"error":"No active UNO game"}', content_type="application/json")
        return game

    def refill(game, uid, count):
        import random
        for _ in range(count):
            if not game["deck"]:
                top = game["discard"][-1]
                game["deck"] = game["discard"][:-1]
                game["discard"] = [top]
                random.shuffle(game["deck"])
            if game["deck"]:
                game["hands"][uid].append(game["deck"].pop())

    def state(game, uid):
        top = game["discard"][-1]
        current = game["players"][game["turn"]]
        return {
            "hand": [dict(color=c["color"], value=c["value"]) for c in game["hands"][uid]],
            "topCard": dict(color=top["color"], value=top["value"]),
            "currentColor": top["color"] or "Wild",
            "direction": game["direction"],
            "isTurn": uid == current,
            "currentPlayerName": game["members"][current]["name"],
            "message": "Your turn — choose a card!" if uid == current else "Waiting for " + game["members"][current]["name"],
            "players": [{"name": game["members"][p]["name"], "cards": len(game["hands"][p]), "isTurn": p == current} for p in game["players"]],
        }

    async def index(request):
        if not page.is_file():
            raise web.HTTPNotFound(text="UNO interface not installed")
        return web.FileResponse(page)

    async def read_state(request):
        uid = verify_init_data(request)
        if uid is None:
            raise web.HTTPUnauthorized(text='{"error":"Open inside Telegram"}', content_type="application/json")
        try:
            chat_id = int(request.query["chat_id"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text='{"error":"Missing chat_id"}', content_type="application/json")
        game = get_game(chat_id)
        if uid not in game["players"]:
            raise web.HTTPForbidden(text='{"error":"Join this game in the group first"}', content_type="application/json")
        return web.json_response(state(game, uid))

    async def _action_unlocked(request):
        bot = request.app.get('telegram_bot')
        if bot is None:
            raise web.HTTPServiceUnavailable(text='Bot is starting')
        uid = verify_init_data(request)
        if uid is None:
            raise web.HTTPUnauthorized(text='{"error":"Telegram verification failed"}', content_type="application/json")
        try:
            body = await request.json()
            chat_id = int(body["chat_id"])
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text='{"error":"Invalid request"}', content_type="application/json")
        game = get_game(chat_id)
        if uid not in game["players"] or game["players"][game["turn"]] != uid:
            raise web.HTTPForbidden(text='{"error":"Not your turn"}', content_type="application/json")
        if request.match_info["action"] == "draw":
            refill(game, uid, 1)
            game["turn"] = (game["turn"] + game["direction"]) % len(game["players"])
        else:
            try:
                idx = int(body["index"])
                hand = game["hands"][uid]
                card = hand[idx]
            except (KeyError, ValueError, TypeError, IndexError):
                raise web.HTTPBadRequest(text='{"error":"Invalid card"}', content_type="application/json")
            if not _valid(card, game["discard"][-1]):
                raise web.HTTPBadRequest(text='{"error":"That card cannot be played"}', content_type="application/json")
            # Official-style Wild Draw Four restriction: no other card in hand
            # may match the currently active color.
            if card["value"] == "+4":
                active_color = game["discard"][-1]["color"]
                if active_color and any(
                    other is not card and other.get("color") == active_color
                    for other in hand
                ):
                    raise web.HTTPBadRequest(
                        text='{"error":"You can play +4 only when you have no card matching the current color"}',
                        content_type="application/json",
                    )
            if card["color"] is None:
                color = body.get("color")
                if color not in COLORS:
                    raise web.HTTPBadRequest(text='{"error":"Choose a Wild color"}', content_type="application/json")
                card = dict(card, color=color)
            elif "color" in body:
                raise web.HTTPBadRequest(text='{"error":"Color can only be selected for a Wild card"}', content_type="application/json")
            hand.pop(idx)
            game["discard"].append(card)
            if not hand:
                game["active"] = False
                old = game.get("turn_message_id")
                if old:
                    try:
                        await bot.delete_message(chat_id, old)
                    except Exception:
                        pass
                await bot.send_message(chat_id, f'🏆 <a href="tg://user?id={uid}">{escape(game["members"][uid]["name"])}</a> won UNO!')
                return web.json_response({"done": True, "message": "You won UNO!"})
            steps = 1
            if card["value"] == "Reverse":
                game["direction"] *= -1
                # In a 2-player game Reverse acts like Skip: same player goes again.
                if len(game["players"]) == 2:
                    steps = 2
            elif card["value"] == "Skip":
                steps = 2
            elif card["value"] == "+2":
                nxt = game["players"][(game["turn"] + game["direction"]) % len(game["players"])]
                refill(game, nxt, 2)
                steps = 2
            elif card["value"] == "+4":
                nxt = game["players"][(game["turn"] + game["direction"]) % len(game["players"])]
                refill(game, nxt, 4)
                steps = 2
            game["turn"] = (game["turn"] + game["direction"] * steps) % len(game["players"])
        await _board(bot, chat_id, game)
        await _turn_prompt(bot, chat_id, game)
        return web.json_response(state(game, uid))

    async def action(request):
        # Prevent two rapid taps/polling requests from consuming the same turn.
        async with action_lock:
            return await _action_unlocked(request)

    app.router.add_get("/uno/", index)
    app.router.add_get("/uno", index)
    app.router.add_get("/uno/api/state", read_state)
    app.router.add_post("/uno/api/{action:play|draw}", action)
