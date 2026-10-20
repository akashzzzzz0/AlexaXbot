"""Offline structural checks for ALEXA. Does not contact Telegram or a database."""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
errors = []

for path in ROOT.rglob("*.py"):
    if any(part in {"__pycache__", ".venv", "venv"} for part in path.parts):
        continue
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        errors.append(f"Syntax/parse error in {path.relative_to(ROOT)}: {exc}")
        continue
    if "bot/keyboards/" in path.as_posix():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_kb"):
                if any(isinstance(child, ast.Return) and isinstance(child.value, ast.Constant) and child.value.value is None for child in ast.walk(node)):
                    errors.append(f"Keyboard stub returns None: {path.relative_to(ROOT)}:{node.name}")

required = {
    "bot/main.py": ["async def main", "async def setup_database_with_retry", "AiohttpSession"],
    "bot/handlers/owner.py": ["async def owner_panel", "async def owner_users", "async def toggle_maintenance", "async def owner_settings", "async def start_group_promotion", "async def process_group_promotion"],
    "bot/middlewares/admin_actions.py": ["class AdminActionMiddleware"],
    "bot/database/connection.py": ["async def switch_to_local_sqlite", "create_async_engine"],
    "bot/services/mission_service.py": ["async def seed_missions", "async def track_progress"],
    "bot/handlers/group_tools.py": ["async def add_filter", "async def add_blocklist", "async def set_rules_finish"],
    "bot/handlers/games.py": ["async def game_wheel", "Free wheel spin reward"],
    "bot/handlers/arena.py": ["async def arena_command", "async def battle_command", "async def arena_callback"],
    "bot/services/arena_service.py": ["def rank_for_xp", "async def award_xp", "def calculate_battle_rewards"],
}
for rel, needles in required.items():
    p = ROOT / rel
    data = p.read_text(encoding="utf-8") if p.exists() else ""
    for needle in needles:
        if needle not in data:
            errors.append(f"Missing required implementation marker: {rel} -> {needle}")

# Router ordering is important: the catch-all group-message handler must be last.
router_init = (ROOT / "bot" / "handlers" / "__init__.py").read_text(encoding="utf-8")
if router_init.find("router.include_router(welcome.router)") < router_init.find("router.include_router(group_tools.router)"):
    errors.append("Catch-all welcome router is registered before specific command routers")

env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
if "OWNER_ID=YOUR_TELEGRAM_USER_ID" in env_example or "BOT_OWNER_ID=YOUR_TELEGRAM_USER_ID" in env_example:
    errors.append(".env.example contains non-numeric owner ID placeholders that break Settings parsing")

main_source = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")
if 'BotCommand(command="owner"' not in main_source or 'BotCommandScopeDefault()' not in main_source:
    errors.append("Owner command scopes are not configured")
if "BotCommandScopeAllPrivateChats()" not in main_source or "BotCommandScopeAllGroupChats()" not in main_source:
    errors.append("Common Telegram command scopes are not reset to hide /owner from non-owners")
if "BotCommandScopeChat(chat_id=owner_id)" not in main_source:
    errors.append("Owner command is not scoped to the configured owner chat")
try:
    main_tree = ast.parse(main_source)
    public_names = set()
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "public_commands" for t in node.targets) and isinstance(node.value, ast.List):
            public_names = {item.elts[0].value for item in node.value.elts if isinstance(item, ast.Tuple) and item.elts and isinstance(item.elts[0], ast.Constant)}
    if "owner" in public_names:
        errors.append("/owner must not appear in the default Telegram command menu")
except Exception as exc:
    errors.append(f"Could not inspect command scopes: {exc}")
owner_source = (ROOT / "bot" / "handlers" / "owner.py").read_text(encoding="utf-8")
if 'message.chat.type != "private"' not in owner_source or "if not is_owner(" not in owner_source:
    errors.append("Owner panel is not guarded as owner/private-only")
for callback in ("owner:stats", "owner:groups", "owner:banall", "owner:activities", "owner:links", "owner:settings", "owner:addgroups", "owner:maintenance", "owner:users", "owner:emergency", "owner:broadcast", "owner:promotion", "owner:menu"):
    if callback not in owner_source:
        errors.append(f"Missing owner-panel action: {callback}")
try:
    owner_tree = ast.parse(owner_source)
    callback_defs = [node.name for node in owner_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(isinstance(d, ast.Call) and ast.unparse(d.func) == "router.callback_query" for d in node.decorator_list)]
    if not callback_defs or callback_defs[-1] != "owner_catch_all":
        errors.append("Owner callback catch-all must be registered after every specific callback")
except Exception as exc:
    errors.append(f"Could not inspect owner callback order: {exc}")

# Verify every content-lock field is enforced and /lock all controls every field.
security_service = (ROOT / "bot" / "services" / "security_service.py").read_text(encoding="utf-8")
lock_fields = (
    "lock_text", "lock_links", "lock_photos", "lock_videos", "lock_gifs",
    "lock_audio", "lock_voice", "lock_documents", "lock_polls", "lock_games",
    "lock_bots", "lock_forwards", "lock_contacts", "lock_locations",
    "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags",
)
for field in lock_fields:
    if field not in security_service:
        errors.append(f"Missing content-lock enforcement: {field}")
security_handler = (ROOT / "bot" / "handlers" / "security.py").read_text(encoding="utf-8")
if "if lock_type == 'all':" not in security_handler or 'await db_session.commit()' not in security_handler:
    errors.append("/lock all must set all lock fields and commit settings before confirming")
for field in lock_fields:
    if field not in security_handler:
        errors.append(f"/lock handler does not expose field: {field}")
welcome_source = (ROOT / "bot" / "handlers" / "welcome.py").read_text(encoding="utf-8")
if "if group_security.lock_bots:" not in welcome_source:
    errors.append("Bot-message lock is not enforced for bot accounts")
if 'captcha_type not in {"math", "button"}' not in (ROOT / "bot" / "handlers" / "security.py").read_text(encoding="utf-8"):
    errors.append("Captcha menu exposes an unsupported challenge type")
if 'if any(reason.startswith("locked_") for reason in violations):' not in welcome_source:
    errors.append("Content locks do not force delete action")

# Mutating group-tools commands must be guarded by the admin middleware.
group_tools_source = (ROOT / "bot" / "handlers" / "group_tools.py").read_text(encoding="utf-8")
for command in ("setrules", "filter", "stopfilter", "save", "clear", "addblocklist", "rmblocklist", "disablecmd", "enablecmd"):
    if f'"{command}"' not in group_tools_source.split("router.message.outer_middleware", 1)[-1].split("\n", 1)[0]:
        errors.append(f"Group-tools admin middleware missing command: {command}")

# Match keyboard callbacks to actual callback-query filters (exact, in-set, or prefix).
keyboard_callbacks = set()
for path in (ROOT / "bot" / "keyboards").glob("*.py"):
    data = path.read_text(encoding="utf-8")
    keyboard_callbacks.update(re.findall(r"callback_data\s*=\s*[\"']([^\"']+)[\"']", data))
    keyboard_callbacks.update(re.findall(r"[\"']((?:menu|sec|lock|captcha|settings|welcome|goodbye|logs|log|gs|slowmode|autodel|mod|mute_duration|ban_duration|warn_action|ban_confirm|game|dice|rps|roulette|quiz_ans|trn|duel|eco|shop|mission|missions|rewards|transfer|owner|lang|stats|lb|arena):[^\"']+)[\"']", data))
exact_callbacks = set()
callback_prefixes = set()
for path in (ROOT / "bot" / "handlers").glob("*.py"):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            if ast.unparse(decorator.func) != "router.callback_query":
                continue
            filt = ast.unparse(decorator.args[0])
            if "F.data.startswith(" in filt and isinstance(decorator.args[0], ast.Call):
                call = decorator.args[0]
                if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                    callback_prefixes.add(call.args[0].value)
            elif "F.data.in_(" in filt and isinstance(decorator.args[0], ast.Call):
                call = decorator.args[0]
                if call.args and isinstance(call.args[0], (ast.List, ast.Tuple, ast.Set)):
                    exact_callbacks.update(e.value for e in call.args[0].elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
            else:
                for constant in ast.walk(decorator.args[0]):
                    if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                        if constant.value.startswith(("menu:", "sec:", "lock:", "captcha:", "settings:", "welcome:", "goodbye:", "logs:", "log:", "gs:", "slowmode:", "autodel:", "mod:", "mute_duration:", "ban_duration:", "warn_action:", "ban_confirm:", "game:", "dice:", "rps:", "roulette:", "quiz_ans:", "trn:", "duel:", "duel_accept:", "duel_decline:", "eco:", "shop:", "mission:", "missions:", "rewards:", "transfer:", "owner:", "lang:", "stats:", "lb:", "arena:")):
                            exact_callbacks.add(constant.value)
for callback in keyboard_callbacks:
    if callback not in exact_callbacks and not any(callback.startswith(prefix) for prefix in callback_prefixes):
        errors.append(f"Keyboard callback has no matching callback-query handler: {callback}")

if errors:
    print("SMOKE CHECK FAILED")
    print("\n".join("- " + e for e in errors))
    sys.exit(1)
print("SMOKE CHECK PASSED")
print("- All Python files parse")
print("- No keyboard helper returns None")
print("- Critical owner, middleware, database, and startup implementations are present")
print("Note: this offline check does not verify installed dependencies or live Telegram behavior.")
