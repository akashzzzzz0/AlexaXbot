"""Group chat activity leaderboard with Today / Overall / Week views."""
import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.rankings_store import get_rankings, get_recap_messages

router = Router(name="group_rankings")
_PERIODS = {
    "today": ("📅 Today", "Today"),
    "overall": ("🌍 Overall", "Overall"),
    "week": ("📊 Week", "Last 7 days"),
}


def _keyboard(selected: str) -> InlineKeyboardMarkup:
    buttons = []
    for key, (label, _) in _PERIODS.items():
        shown = f"✓ {label}" if key == selected else label
        buttons.append(InlineKeyboardButton(text=shown, callback_data=f"rankings:{key}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _render(rows, total: int, period: str, recap: str) -> str:
    label = _PERIODS[period][1]
    medals = ("🥇", "🥈", "🥉")
    lines = [
        "📈 <b>LEADERBOARD</b>",
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
            rank = medals[index - 1] if index <= 3 else f"{index}."
            lines.append(f"{rank} {name} · <b>{int(row['message_count']):,}</b>")
    else:
        lines.append("<i>No messages recorded for this period yet.</i>")
    lines.extend(["", f"✉️ <b>Total messages:</b> {total:,}", "", "✨ <b>ChatFight AI Summary</b>"])
    if recap:
        lines.append(html.escape(recap))
    else:
        lines.append("<i>Not enough text messages for a recap yet.</i>")
    if period == "overall":
        lines.append("<i>AI recap uses recent messages from the last 7 days; Overall ranking is all-time.</i>")
    lines.append("")
    lines.append("<i>AI recap text is kept in a separate database for up to 7 days; all-time counts are retained.</i>")
    return "\n".join(lines)


async def _build_view(group_id: int, period: str):
    rows, total = await get_rankings(group_id, period)
    messages = await get_recap_messages(group_id, period)
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
        text = await _build_view(message.chat.id, "today")
        await message.answer(text, reply_markup=_keyboard("today"), disable_web_page_preview=True)
    except Exception:
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
        text = await _build_view(callback.message.chat.id, period)
        await callback.message.edit_text(
            text, reply_markup=_keyboard(period), disable_web_page_preview=True
        )
    except Exception:
        await callback.message.answer("⚠️ Could not refresh rankings. Please try again.")
