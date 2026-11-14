"""Group chat activity leaderboard with Today / Overall / Week views."""
import asyncio
import html
import os
from io import BytesIO

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)
from PIL import Image, ImageDraw, ImageFont
import structlog

from bot.services.rankings_store import get_rankings, get_recap_messages

router = Router(name="group_rankings")
logger = structlog.get_logger()
_PERIODS = {
    "overall": ("👑 Overall", "Overall"),
    "today": ("📅 Today", "Today"),
    "week": ("🏆 Week", "Last 7 days"),
    "global": ("🌐 Global", "All Alexa groups · Today"),
}
_HEADER_EMOJI_ID = "6093382540784046658"
_NAME_EMOJI_ID = "6258164063516233396"
_CROWN_EMOJI_ID = "6093382540784046658"


def _keyboard(selected: str) -> InlineKeyboardMarkup:
    def button(key: str) -> InlineKeyboardButton:
        label = _PERIODS[key][0]
        return InlineKeyboardButton(
            text=f"✓ {label}" if key == selected else label,
            callback_data=f"rankings:{key}",
        )

    # Requested layout: Overall alone; Today/Week side by side; Global alone.
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("overall")],
        [button("today"), button("week")],
        [button("global")],
    ])


def _leaderboard_image(period: str) -> BufferedInputFile:
    """Create a compact neon banner locally so /rankings always has an attached image."""
    width, height = 1200, 720
    image = Image.new("RGB", (width, height), (9, 5, 25))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            glow = max(0.0, 1.0 - abs(x - 620) / 850) * max(0.0, 1.0 - y / 900)
            pixels[x, y] = (int(9 + 25 * glow), int(5 + 4 * glow), int(25 + 58 * glow))
    draw = ImageDraw.Draw(image)
    paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    )
    try:
        path = next(p for p in paths if os.path.exists(p))
        title_font = ImageFont.truetype(path, 78)
        tab_font = ImageFont.truetype(path, 34)
        row_font = ImageFont.truetype(path, 25)
    except Exception:
        title_font = tab_font = row_font = ImageFont.load_default()

    draw.rounded_rectangle((22, 22, 1178, 698), radius=42,
                           outline=(174, 65, 255), width=5)
    draw.rounded_rectangle((42, 42, 1158, 230), radius=32,
                           fill=(19, 8, 45), outline=(123, 48, 220), width=3)
    title = "LEADERBOARD"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - bbox[2] + bbox[0]) / 2, 85), title, font=title_font,
              fill=(250, 239, 255), stroke_width=2, stroke_fill=(154, 58, 255))

    def draw_tab(label: str, key: str, bounds):
        left, top, right, bottom = bounds
        active = key == period
        draw.rounded_rectangle(bounds, radius=23,
                               fill=(91, 25, 168) if active else (24, 13, 49),
                               outline=(226, 158, 255) if active else (112, 58, 170), width=4)
        box = draw.textbbox((0, 0), label, font=tab_font)
        draw.text((left + (right - left - box[2] + box[0]) / 2, top + 20), label,
                  font=tab_font, fill=(255, 245, 255))

    # Match the Telegram keyboard: Overall alone, Today/Week paired, Global alone.
    draw_tab("OVERALL", "overall", (55, 255, 1145, 330))
    draw_tab("TODAY", "today", (55, 340, 590, 415))
    draw_tab("WEEK", "week", (610, 340, 1145, 415))
    draw_tab("GLOBAL", "global", (55, 425, 1145, 500))

    medals = ((255, 190, 56), (205, 207, 235), (218, 133, 79))
    for i, color in enumerate(medals):
        top = 535 + i * 48
        draw.rounded_rectangle((65, top, 1135, top + 38), radius=15,
                               fill=(17, 12, 35), outline=color, width=2)
        draw.ellipse((82, top + 3, 117, top + 38), fill=color)
        draw.text((94, top + 4), str(i + 1), font=row_font, fill=(20, 12, 30))
        draw.text((145, top + 6), ("TOP CHATTER" if i == 0 else "GROUP RANKING"),
                  font=row_font, fill=(221, 205, 255))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return BufferedInputFile(output.getvalue(), filename="alexa-leaderboard.png")


def _render(rows, total: int, period: str, recap: str) -> str:
    label = _PERIODS[period][1]
    lines = [
        f'<tg-emoji emoji-id="{_HEADER_EMOJI_ID}">👑</tg-emoji> <b>LEADERBOARD</b>',
        f"<i>{html.escape(label)} · TOP 10 CHATTERS</i>",
        "",
    ]
    if rows:
        for index, row in enumerate(rows, 1):
            name = html.escape(row["first_name"] or "Member")
            if row["username"]:
                name = f'<a href="https://t.me/{html.escape(row["username"], quote=True)}">{name}</a>'
            else:
                name = f'<a href="tg://user?id={int(row["user_id"])}">{name}</a>'
            sticker = f'<tg-emoji emoji-id="{_NAME_EMOJI_ID}">👑</tg-emoji>'
            crown = f' <tg-emoji emoji-id="{_CROWN_EMOJI_ID}">👑</tg-emoji>' if index == 1 else ""
            lines.append(f"{index}. {sticker}{crown} {name} • <b>{int(row['message_count']):,}</b>")
    else:
        lines.append("<i>No messages recorded for this period yet.</i>")
    lines.extend(["", f"✉️ <b>Total messages:</b> {total:,}"])
    if period != "global":
        lines.extend(["", "✨ <b>ChatFight AI Summary</b>"])
        if recap:
            lines.append(html.escape(recap[:180]))
        else:
            lines.append("<i>Not enough text messages for a recap yet.</i>")
    if period == "overall":
        lines.append("<i>Overall ranking is all-time for this group.</i>")
    elif period == "global":
        lines.append("<i>Daily ranking combines messages from all Alexa groups.</i>")
    # Telegram applies its caption limit to the rendered text, not the raw
    # HTML markup. Slicing the raw HTML at 1024 can cut an entity/tag in half
    # and make Telegram reject the entire leaderboard message.
    return "\n".join(lines)


async def _build_view(group_id: int, period: str):
    rows, total = await get_rankings(group_id, period)
    messages = [] if period == "global" else await get_recap_messages(group_id, period)
    recap = ""
    if messages:
        from bot.services.ollama_ai import ollama_chat
        transcript = "\n".join(f"{name}: {body}" for name, body in messages)
        try:
            recap = await asyncio.wait_for(
                ollama_chat(
                    [{"role": "user", "content":
                        "Summarize the key topics and notable moments from this group chat. "
                        "Use 2-3 concise bullet points in the same language as the messages. "
                        "Do not invent details or identify private individuals beyond their displayed names.\n\n"
                        + transcript[:10000]
                    }],
                    system="You create concise, neutral group chat recaps from supplied messages.",
                    temperature=0.2,
                    max_tokens=220,
                ),
                timeout=45,
            )
        except Exception:
            recap = "AI summary is temporarily unavailable. Message ranking is still working."
    return _render(rows, total, period, recap)


@router.message(Command("rankings"))
async def rankings_command(message: Message):
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("📈 Use /rankings inside a group to view its chat leaderboard.")
        return
    try:
        caption = await _build_view(message.chat.id, "today")
        await message.answer_photo(
            photo=_leaderboard_image("today"),
            caption=caption,
            reply_markup=_keyboard("today"),
        )
    except Exception:
        logger.exception("Could not load group leaderboard", group_id=message.chat.id)
        await message.answer("⚠️ Alexa could not load the leaderboard right now. Please try again.")


@router.callback_query(F.data.startswith("rankings:"))
async def rankings_period_callback(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("Open rankings inside a group.", show_alert=True)
        return
    period = (callback.data or "").split(":", 1)[1]
    if period not in _PERIODS:
        await callback.answer("Unknown ranking period.", show_alert=True)
        return
    await callback.answer()
    try:
        caption = await _build_view(callback.message.chat.id, period)
        await callback.message.edit_media(
            media=InputMediaPhoto(media=_leaderboard_image(period), caption=caption),
            reply_markup=_keyboard(period),
        )
    except Exception:
        logger.exception("Could not refresh group leaderboard", group_id=callback.message.chat.id, period=period)
        await callback.message.answer("⚠️ Could not refresh rankings. Please try again.")
