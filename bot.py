import telebot
from telebot import types
import subprocess
import os
import sys
import psutil
import json
import time
import threading

# ----------------- ENVIRONMENT VARIABLES -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_ENV = os.getenv("OWNER_ID")

if not BOT_TOKEN:
    sys.exit("[CRITICAL ERROR] 'BOT_TOKEN' environment variable is missing! Please configure it in Railway.")

if not OWNER_ID_ENV:
    sys.exit("[CRITICAL ERROR] 'OWNER_ID' environment variable is missing! Please configure it in Railway.")

try:
    OWNER_ID = int(OWNER_ID_ENV)
except ValueError:
    sys.exit("[CRITICAL ERROR] 'OWNER_ID' must be a valid integer Telegram user ID!")

bot = telebot.TeleBot(BOT_TOKEN)

USERS_FILE = "allowed_users.json"
CONFIG_FILE = "bot_config.json"
KNOWN_FILE = "known_users.json"      # everyone who ever pressed /start (for stats + broadcast)
MAX_CHANNELS = 10
USERS_PER_PAGE = 8
DIV = "━━━━━━━━━━━━━━━━━━"

current_dir = os.getcwd()
bg_processes = {}
waiting_for_channel_forward = set()  # Owner state waiting for channel forward
waiting_for_broadcast = set()        # Owner state waiting for the broadcast message
pending_broadcast = {}               # owner_id -> (chat_id, message_id) waiting for confirmation
pending_requests = set()             # User IDs with active approval request sent to admin

START_TIME = time.time()

# ----------------- COLOURED BUTTONS (Telegram Bot API 9.4 "style") -----------------
# Official button colours: "primary" = blue, "success" = green, "danger" = red.
# These small subclasses add the `style` field to the button JSON, so they work with
# any pyTelegramBotAPI version (older Telegram apps just show the normal grey button).

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"


class IBtn(types.InlineKeyboardButton):
    """Inline button with an official colour."""
    def __init__(self, text, style=None, **kwargs):
        super().__init__(text, **kwargs)
        self._btn_style = style

    def to_dict(self):
        data = super().to_dict()
        if self._btn_style:
            data["style"] = self._btn_style
        return data


class RBtn(types.KeyboardButton):
    """Reply-keyboard button (the keyboard at the bottom) with an official colour."""
    def __init__(self, text, style=None, **kwargs):
        super().__init__(text, **kwargs)
        self._btn_style = style

    def to_dict(self):
        data = super().to_dict()
        if self._btn_style:
            data["style"] = self._btn_style
        return data

# ----------------- ESCAPE / SANITIZATION HELPERS -----------------

def escape_markdown(text):
    """
    Escapes Telegram legacy Markdown special characters (*, _, `, [).
    Prevents 'can't parse entities' errors when usernames or names contain symbols.
    """
    if not text:
        return ""
    chars = ['*', '_', '`', '[']
    for ch in chars:
        text = text.replace(ch, f"\\{ch}")
    return text

def code_safe(text, limit=60):
    """Makes text safe to place inside a `code span`."""
    text = (text or "").replace("`", "'").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"

def bar(percent, size=10):
    """Little progress bar for CPU / RAM / Disk."""
    filled = int(round(max(0, min(100, percent)) / 100 * size))
    return "▰" * filled + "▱" * (size - filled)

# ----------------- STORAGE HELPERS -----------------

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return {OWNER_ID}
    return {OWNER_ID}

def save_users(users_set):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users_set), f)

def load_config():
    default_config = {"required_channels": []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "required_channel_id" in data and data["required_channel_id"]:
                    data["required_channels"] = [{
                        "id": data["required_channel_id"],
                        "title": data.get("required_channel_title") or "Required Channel",
                        "username": data.get("required_channel_username"),
                        "invite_link": data.get("required_channel_invite_link")
                    }]
                if "required_channels" not in data:
                    data["required_channels"] = []
                return data
        except Exception:
            return default_config
    return default_config

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

def load_known():
    if os.path.exists(KNOWN_FILE):
        try:
            with open(KNOWN_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_known(data):
    with open(KNOWN_FILE, "w") as f:
        json.dump(data, f)

allowed_users = load_users()
bot_config = load_config()
known_users = load_known()

def track_user(user):
    """Remember everyone who talks to the bot (used for stats, user manager and broadcast)."""
    key = str(user.id)
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "User"
    old = known_users.get(key)
    if old and old.get("name") == name and old.get("username") == user.username:
        return
    known_users[key] = {
        "name": name,
        "username": user.username,
        "joined": old.get("joined") if old else int(time.time())
    }
    try:
        save_known(known_users)
    except Exception as e:
        print(f"[!] Could not save known users: {e}")

# ----------------- VERIFICATION HELPERS -----------------

def is_owner(user_id):
    return user_id == OWNER_ID

def is_allowed_user(user_id):
    return user_id in allowed_users or user_id == OWNER_ID

def check_channel_subscriptions(user_id):
    """
    Checks if a user is subscribed to ALL configured channels (up to 10).
    Returns: (is_all_joined: bool, missing_channels: list of dicts)
    """
    if is_owner(user_id):
        return True, []

    channels = bot_config.get("required_channels", [])
    if not channels:
        return True, []

    missing = []
    for ch in channels:
        ch_id = ch.get("id")
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                missing.append(ch)
        except Exception as e:
            print(f"[!] Error checking channel {ch_id}: {e}")
            missing.append(ch)

    return (len(missing) == 0), missing

def get_forward_chat(msg):
    """Works with both the old (forward_from_chat) and new (forward_origin) forward fields."""
    chat = getattr(msg, "forward_from_chat", None)
    if chat:
        return chat
    origin = getattr(msg, "forward_origin", None)
    return getattr(origin, "chat", None) if origin else None

def get_uptime():
    uptime_seconds = int(time.time() - START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    return f"{days}d {hours}h {minutes}m {seconds}s"

def active_engines():
    return sum(1 for info in bg_processes.values() if info['process'].poll() is None)

# ----------------- REPLY KEYBOARD (bottom keyboard) -----------------

BTN_STATUS = "⏳ Status"
BTN_VITALS = "🧬 Vitals"
BTN_RAM = "🧠 RAM"
BTN_DISK = "💽 Disk"
BTN_ENGINES = "⚙️ Engines"
BTN_MYID = "🪪 My ID"
BTN_HELP = "📖 Help"
BTN_ADMIN = "👑 Admin Panel"

MENU_TEXTS = {BTN_STATUS, BTN_VITALS, BTN_RAM, BTN_DISK, BTN_ENGINES, BTN_MYID, BTN_HELP, BTN_ADMIN}

def get_reply_keyboard(user_id):
    rows = [
        [RBtn(BTN_STATUS, style=SUCCESS), RBtn(BTN_VITALS, style=PRIMARY)],
        [RBtn(BTN_RAM, style=PRIMARY), RBtn(BTN_DISK, style=PRIMARY)],
        [RBtn(BTN_ENGINES, style=DANGER), RBtn(BTN_MYID, style=PRIMARY)],
        [RBtn(BTN_HELP, style=SUCCESS)],
    ]
    if is_owner(user_id):
        rows[-1].append(RBtn(BTN_ADMIN, style=DANGER))

    try:
        markup = types.ReplyKeyboardMarkup(
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="⌨️ Type a command or tap a button..."
        )
    except TypeError:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in rows:
        markup.row(*row)
    return markup

# ----------------- INLINE KEYBOARDS -----------------

def get_join_channels_keyboard(missing_channels):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, ch in enumerate(missing_channels, 1):
        link = ch.get("invite_link")
        title = ch.get("title", f"Channel {i}")
        if link:
            markup.add(IBtn(f"📢 Join {title}", style=PRIMARY, url=link))
    markup.add(IBtn("🔄 Verify Membership", style=SUCCESS, callback_data="verify_membership"))
    return markup

def get_request_access_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("📩 Request Access from Admin", style=SUCCESS, callback_data="send_auth_request"))
    return markup

def get_main_menu_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        IBtn("⏳ Status", style=SUCCESS, callback_data="btn_status"),
        IBtn("🧬 Vitals", style=PRIMARY, callback_data="btn_sysinfo")
    )
    markup.add(
        IBtn("🧠 RAM", style=PRIMARY, callback_data="btn_memory"),
        IBtn("💽 Disk", style=PRIMARY, callback_data="btn_disk")
    )
    markup.add(
        IBtn("⚙️ Engines", style=DANGER, callback_data="btn_ps"),
        IBtn("🪪 My ID", style=PRIMARY, callback_data="btn_myid")
    )
    markup.add(IBtn("📖 Help Guide", style=SUCCESS, callback_data="btn_help"))
    if is_owner(user_id):
        markup.add(IBtn("👑 Admin Panel", style=DANGER, callback_data="adm_panel"))
    return markup

def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("🔙 Back to Menu", style=PRIMARY, callback_data="btn_main_menu"))
    return markup

def get_admin_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("🔙 Admin Panel", style=PRIMARY, callback_data="adm_panel"))
    return markup

# ----------------- TEXTS -----------------

def get_welcome_text(user):
    uid = user.id
    name = escape_markdown(user.first_name or "there")
    role = "👑 Owner" if is_owner(uid) else "✅ Approved User"
    return (
        f"⚡️ *DEV X HOST*\n"
        f"_Your personal server, right inside Telegram_\n"
        f"{DIV}\n\n"
        f"👋 Hey *{name}*, welcome!\n\n"
        f"🖥 Run terminal commands & scripts\n"
        f"📤 Upload and 📥 download files\n"
        f"⚙️ Keep your bots running in the background\n"
        f"📊 Watch live server health\n\n"
        f"{DIV}\n"
        f"🪪 *ID:* `{uid}`\n"
        f"🔰 *Access:* {role}\n"
        f"🟢 *Server:* Online · ⏱ `{get_uptime()}`\n"
        f"{DIV}\n"
        f"👇 _Pick an option below or just type a command_"
    )

def get_locked_text(user, joined):
    name = escape_markdown(user.first_name or "there")
    head = (
        f"⚡️ *DEV X HOST*\n"
        f"_Your personal server, right inside Telegram_\n"
        f"{DIV}\n\n"
        f"👋 Hey *{name}*, welcome!\n\n"
        f"This is a private hosting terminal — run scripts, host your bots and manage files, all from Telegram.\n\n"
    )
    if not joined:
        steps = (
            "🔒 *Unlock access in 3 steps*\n"
            "1️⃣ Join the channel(s) below 📢\n"
            "2️⃣ Tap *Verify Membership* 🔄\n"
            "3️⃣ Get approved by the Admin ✅"
        )
    else:
        steps = (
            "✅ *Channels verified!*\n\n"
            "🔒 *One last step*\n"
            "Tap the button below to ask the Admin for access."
        )
    return head + steps

def get_dashboard_text():
    return (
        f"🎛 *DASHBOARD*\n"
        f"{DIV}\n\n"
        f"🟢 *Server:* Online\n"
        f"⏱ *Uptime:* `{get_uptime()}`\n"
        f"⚙️ *Engines:* `{active_engines()}`\n\n"
        f"_Tap an option below_ 👇"
    )

def get_help_text(user_id=None):
    count = len(bot_config.get("required_channels", []))
    text = (
        f"⚡️ *DEV X HOST — COMMAND GUIDE*\n"
        f"{DIV}\n\n"
        f"💻 *Terminal*\n"
        f"• Just type a command — `ls`, `git status`\n"
        f"• `cd <dir>` — change folder 📂\n"
        f"• `pip install <pkg>` — install a package 💉\n"
        f"• `python <script.py>` — run a script 🔥\n\n"
        f"🗂 *Files*\n"
        f"• Send any document — uploads to the current folder 📤\n"
        f"• `/download <filename>` — get a file back 📥\n\n"
        f"⚙️ *Background Engines*\n"
        f"• `/run <cmd>` — start in background 🟢\n"
        f"• `/ps` — list running engines 📊\n"
        f"• `/stop <pid>` — stop an engine 🛑\n\n"
        f"🖥 *System*\n"
        f"• `/status` ⏳  `/sysinfo` 🧬  `/memory` 🧠  `/disk` 💽\n"
        f"• `/myid` — your Telegram ID 🪪\n"
        f"• `/menu` — quick action buttons 🎛\n"
    )
    if user_id is not None and is_owner(user_id):
        text += (
            f"\n👑 *Admin*\n"
            f"• `/admin` — admin panel\n"
            f"• `/broadcast` — message all users 📣\n"
            f"• `/channel` · `/channels` — force-join channels ({count}/{MAX_CHANNELS}) 📢\n"
            f"• `/add <id>` · `/remove <id>` — approve / remove users 👥\n"
        )
    text += f"\n{DIV}\n_SYSTEM READY_ ▍ type a command or use the buttons"
    return text

def get_status_text():
    return (
        f"🟢 *SERVER STATUS*\n"
        f"{DIV}\n\n"
        f"📡 *State:* `ONLINE`\n"
        f"⏳ *Runtime:* `{get_uptime()}`\n"
        f"⚙️ *Engines:* `{active_engines()}`\n"
        f"🔐 *Connection:* `SECURE`"
    )

def get_vitals_text():
    cpu = psutil.cpu_percent(interval=0.4)
    ram = psutil.virtual_memory().percent
    return (
        f"🧬 *SYSTEM VITALS*\n"
        f"{DIV}\n\n"
        f"🔥 *CPU:* `{cpu}%`\n`{bar(cpu)}`\n\n"
        f"🧠 *RAM:* `{ram}%`\n`{bar(ram)}`"
    )

def get_memory_text():
    mem = psutil.virtual_memory()
    return (
        f"🧠 *MEMORY CORE*\n"
        f"{DIV}\n\n"
        f"`{bar(mem.percent)}` *{mem.percent}%*\n\n"
        f"*Total:* `{mem.total / (1024**3):.2f} GB`\n"
        f"*Used:* `{mem.used / (1024**3):.2f} GB`\n"
        f"*Free:* `{mem.available / (1024**3):.2f} GB`"
    )

def get_disk_text():
    disk = psutil.disk_usage('/')
    return (
        f"💽 *STORAGE VAULT*\n"
        f"{DIV}\n\n"
        f"`{bar(disk.percent)}` *{disk.percent}%*\n\n"
        f"*Total:* `{disk.total / (1024**3):.2f} GB`\n"
        f"*Used:* `{disk.used / (1024**3):.2f} GB`\n"
        f"*Free:* `{disk.free / (1024**3):.2f} GB`"
    )

def get_myid_text(user_id):
    return f"🪪 *YOUR TELEGRAM ID*\n{DIV}\n\n`{user_id}`\n\n_Tap the number to copy_ ⚡️"

def get_engines_view(with_back=False):
    """Returns (text, markup) for the running engines list."""
    alive = {}
    for pid, info in list(bg_processes.items()):
        if info['process'].poll() is None:
            alive[pid] = info
        else:
            del bg_processes[pid]

    markup = types.InlineKeyboardMarkup(row_width=1)
    if not alive:
        text = (
            f"💤 *SYSTEM IDLE*\n"
            f"{DIV}\n\n"
            f"No background engines running.\n"
            f"Start one with `/run python app.py`"
        )
    else:
        text = f"⚙️ *ACTIVE ENGINES* ({len(alive)})\n{DIV}\n\n"
        for pid, info in alive.items():
            text += f"🟢 `{pid}` — `{code_safe(info['cmd'])}`\n"
            markup.add(IBtn(f"🛑 Kill PID {pid}", style=DANGER, callback_data=f"kill_{pid}"))
    if with_back:
        markup.add(IBtn("🔙 Back to Menu", style=PRIMARY, callback_data="btn_main_menu"))
    return text, markup

# ----------------- SAFE EDIT HELPERS -----------------

def safe_edit(chat_id, message_id, text, markup=None):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        if "not modified" not in str(e):
            print(f"[!] Edit failed: {e}")

def edit(call, text, markup=None):
    safe_edit(call.message.chat.id, call.message.message_id, text, markup)

# ----------------- ACCESS GATES -----------------

def access_ok(message):
    """Used by every protected command: whitelisted + joined all channels."""
    uid = message.from_user.id
    if not is_allowed_user(uid):
        bot.reply_to(message, "⛔️ *ACCESS RESTRICTED*\nSend /start to request access.", parse_mode="Markdown")
        return False
    is_all_joined, missing = check_channel_subscriptions(uid)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels first.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return False
    return True

def entry_gate(message):
    """
    Used by /start, /menu, /help. Returns True if the user may continue,
    otherwise sends the right prompt (join channels / ask the admin) and returns False.
    """
    user = message.from_user
    uid = user.id
    if is_owner(uid):
        return True

    is_all_joined, missing = check_channel_subscriptions(uid)
    if not is_all_joined:
        bot.send_message(message.chat.id, get_locked_text(user, joined=False), parse_mode="Markdown", reply_markup=get_join_channels_keyboard(missing))
        return False

    if not is_allowed_user(uid):
        bot.send_message(message.chat.id, get_locked_text(user, joined=True), parse_mode="Markdown", reply_markup=get_request_access_keyboard())
        return False

    return True

# ----------------- COMMAND MENU (the "/" menu next to the typing box) -----------------

USER_COMMANDS = [
    ("start", "🚀 Open your dashboard"),
    ("menu", "🎛 Quick action buttons"),
    ("help", "📖 Full command guide"),
    ("status", "⏳ Server status & uptime"),
    ("sysinfo", "🧬 CPU & RAM usage"),
    ("memory", "🧠 Memory details"),
    ("disk", "💽 Storage details"),
    ("ps", "⚙️ Running background engines"),
    ("run", "🟢 Start a background engine"),
    ("stop", "🛑 Stop an engine by PID"),
    ("download", "📥 Download a file from the server"),
    ("myid", "🪪 Show your Telegram ID"),
]

OWNER_COMMANDS = USER_COMMANDS + [
    ("admin", "👑 Open the admin panel"),
    ("broadcast", "📣 Send a message to all users"),
    ("channels", "📢 Manage force-join channels"),
    ("channel", "➕ Add a force-join channel"),
    ("add", "✅ Approve a user by ID"),
    ("remove", "🗑 Remove a user by ID"),
    ("cancel", "❌ Cancel the current action"),
]

def setup_command_menu():
    try:
        bot.set_my_commands([types.BotCommand(c, d) for c, d in USER_COMMANDS])
        bot.set_chat_menu_button(menu_button=types.MenuButtonCommands())
    except Exception as e:
        print(f"[!] Could not set command menu: {e}")
    set_owner_commands()

def set_owner_commands():
    try:
        bot.set_my_commands(
            [types.BotCommand(c, d) for c, d in OWNER_COMMANDS],
            scope=types.BotCommandScopeChat(OWNER_ID)
        )
    except Exception as e:
        print(f"[!] Could not set owner command menu (open the bot and send /start once): {e}")

# ----------------- ADMIN PANEL -----------------

def get_admin_panel_text():
    total = len(known_users)
    approved = len(allowed_users - {OWNER_ID})
    channels = len(bot_config.get("required_channels", []))
    return (
        f"👑 *ADMIN PANEL*\n"
        f"{DIV}\n\n"
        f"👥 *Users started:* `{total}`\n"
        f"✅ *Approved:* `{approved}`\n"
        f"📢 *Force-join channels:* `{channels}/{MAX_CHANNELS}`\n"
        f"⚙️ *Engines running:* `{active_engines()}`\n"
        f"⏱ *Uptime:* `{get_uptime()}`\n\n"
        f"{DIV}\n"
        f"_Choose an action below_"
    )

def get_admin_panel_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        IBtn("📣 Broadcast", style=SUCCESS, callback_data="adm_broadcast"),
        IBtn("👥 Users", style=PRIMARY, callback_data="adm_users_0")
    )
    markup.add(
        IBtn("📢 Channels", style=PRIMARY, callback_data="btn_channel_info"),
        IBtn("🔄 Refresh", callback_data="adm_panel")
    )
    markup.add(IBtn("🔙 Main Menu", style=PRIMARY, callback_data="btn_main_menu"))
    return markup

def show_admin_panel(chat_id, message_id=None):
    if message_id:
        safe_edit(chat_id, message_id, get_admin_panel_text(), get_admin_panel_keyboard())
    else:
        bot.send_message(chat_id, get_admin_panel_text(), parse_mode="Markdown", reply_markup=get_admin_panel_keyboard())

def show_users_manager(chat_id, message_id, page=0):
    ids = sorted(allowed_users - {OWNER_ID})
    total_pages = max(1, (len(ids) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = ids[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]

    markup = types.InlineKeyboardMarkup(row_width=1)
    if not ids:
        text = (
            f"👥 *USER MANAGER*\n"
            f"{DIV}\n\n"
            f"No approved users yet.\n"
            f"New access requests will appear in this chat."
        )
    else:
        text = (
            f"👥 *USER MANAGER* — {len(ids)} approved\n"
            f"{DIV}\n"
            f"_Tap a user to remove their access_\n\n"
        )
        for uid in chunk:
            info = known_users.get(str(uid), {})
            name = info.get("name") or "User"
            uname = info.get("username")
            line = f"▪️ {escape_markdown(name)} — `{uid}`"
            if uname:
                line += f" (@{escape_markdown(uname)})"
            text += line + "\n"
            markup.add(IBtn(f"🗑 Remove {name[:18]}", style=DANGER, callback_data=f"adm_rm_{uid}_{page}"))

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(IBtn("⬅️ Prev", style=PRIMARY, callback_data=f"adm_users_{page - 1}"))
            if page < total_pages - 1:
                nav.append(IBtn("Next ➡️", style=PRIMARY, callback_data=f"adm_users_{page + 1}"))
            markup.row(*nav)
            text += f"\n_Page {page + 1}/{total_pages}_"

    markup.add(IBtn("🔙 Admin Panel", style=PRIMARY, callback_data="adm_panel"))
    safe_edit(chat_id, message_id, text, markup)

def broadcast_prompt_text():
    return (
        f"📣 *BROADCAST*\n"
        f"{DIV}\n\n"
        f"Send me the message you want to deliver to *all users* ({len([u for u in known_users if int(u) != OWNER_ID])} people).\n\n"
        f"Text, photo, video, file — anything works.\n"
        f"_You'll get a confirmation before it is sent._"
    )

def broadcast_cancel_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("❌ Cancel", style=DANGER, callback_data="bc_cancel"))
    return markup

def run_broadcast(owner_id, chat_id, status_message_id):
    source = pending_broadcast.pop(owner_id, None)
    if not source:
        return
    src_chat, src_msg = source
    targets = [int(u) for u in known_users if int(u) != OWNER_ID]
    sent = 0
    failed = 0
    for target in targets:
        try:
            bot.copy_message(target, src_chat, src_msg)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # stay under Telegram's flood limits

    safe_edit(
        chat_id, status_message_id,
        f"✅ *BROADCAST COMPLETE*\n{DIV}\n\n📨 *Delivered:* `{sent}`\n🚫 *Failed:* `{failed}`\n\n_Failed = users who blocked the bot._",
        get_admin_back_keyboard()
    )

# ----------------- OWNER CHANNEL MANAGEMENT -----------------

@bot.message_handler(commands=['channel'])
def set_channel_prompt(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
        return

    current_count = len(bot_config.get("required_channels", []))
    if current_count >= MAX_CHANNELS:
        bot.reply_to(
            message,
            f"⚠️ *LIMIT REACHED*\nYou already have `{MAX_CHANNELS}` channels added!\n"
            f"Use `/channels` or `/channel_del <id>` to remove one first.",
            parse_mode="Markdown"
        )
        return

    waiting_for_channel_forward.add(message.from_user.id)
    text = (
        f"📢 *ADD CHANNEL* ({current_count}/{MAX_CHANNELS})\n"
        f"{DIV}\n\n"
        f"1️⃣ Add this bot as an *Administrator* in your channel (with invite link permission).\n"
        f"2️⃣ *Forward any post from that channel to this chat right now.*\n\n"
        f"_Send /cancel any time to stop._"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("❌ Cancel Setup", style=DANGER, callback_data="cancel_channel_setup"))
    bot.reply_to(message, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['channels'])
def list_channels_cmd(message):
    if not is_owner(message.from_user.id):
        return
    show_channel_manager(message.chat.id, None)

@bot.message_handler(commands=['channel_del'])
def delete_channel_by_arg(message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split(" ")
    if len(args) < 2:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse `/channel_del <channel_id>` or `/channels` to manage with buttons.", parse_mode="Markdown")
        return
    try:
        del_id = int(args[1])
        channels = bot_config.get("required_channels", [])
        before = len(channels)
        bot_config["required_channels"] = [c for c in channels if c.get("id") != del_id]
        if len(bot_config["required_channels"]) < before:
            save_config(bot_config)
            bot.reply_to(message, f"✅ Removed channel `{del_id}`.", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Channel ID `{del_id}` not found in list.", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "⚠️ Channel ID must be a numeric integer.")

def show_channel_manager(chat_id, message_id=None):
    channels = bot_config.get("required_channels", [])
    markup = types.InlineKeyboardMarkup(row_width=1)

    if not channels:
        text = (
            f"📢 *CHANNEL MANAGER* (0/{MAX_CHANNELS})\n"
            f"{DIV}\n\n"
            f"No required channels yet.\n"
            f"Tap *Add New Channel* to set one up!"
        )
    else:
        text = f"📢 *CHANNEL MANAGER* ({len(channels)}/{MAX_CHANNELS})\n{DIV}\n\n"
        for i, ch in enumerate(channels, 1):
            clean_title = escape_markdown(ch.get('title', 'Channel'))
            link = escape_markdown(ch.get('invite_link') or 'None')
            text += f"{i}. *{clean_title}*\n   🆔 `{ch.get('id')}`\n   🔗 {link}\n\n"
            markup.add(IBtn(f"🗑 Remove: {ch.get('title', 'Channel')[:25]}", style=DANGER, callback_data=f"del_ch_{ch.get('id')}"))

    if len(channels) < MAX_CHANNELS:
        markup.add(IBtn("➕ Add New Channel", style=SUCCESS, callback_data="add_new_channel_btn"))
    markup.add(IBtn("🔙 Admin Panel", style=PRIMARY, callback_data="adm_panel"))

    if message_id:
        safe_edit(chat_id, message_id, text, markup)
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['cancel'])
def cancel_action(message):
    uid = message.from_user.id
    cancelled = False
    if uid in waiting_for_channel_forward:
        waiting_for_channel_forward.discard(uid)
        cancelled = True
    if uid in waiting_for_broadcast:
        waiting_for_broadcast.discard(uid)
        cancelled = True
    if pending_broadcast.pop(uid, None):
        cancelled = True
    if cancelled:
        bot.reply_to(message, "❌ Cancelled.")

@bot.message_handler(commands=['add'])
def add_user(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
        return
    try:
        new_id = int(message.text.split(" ")[1])
        allowed_users.add(new_id)
        save_users(allowed_users)
        bot.reply_to(message, f"⚡️ *ACCESS GRANTED*\nUser `{new_id}` added to the authorized list! 🚀", parse_mode="Markdown")
        try:
            bot.send_message(
                new_id,
                "🎉 *ACCESS APPROVED*\n\nThe Admin approved your access! Your keyboard is ready below 👇",
                parse_mode="Markdown",
                reply_markup=get_reply_keyboard(new_id)
            )
        except Exception:
            pass
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/add <userid>`", parse_mode="Markdown")

@bot.message_handler(commands=['remove'])
def remove_user(message):
    if not is_owner(message.from_user.id):
        return
    try:
        del_id = int(message.text.split(" ")[1])
        if del_id == OWNER_ID:
            bot.reply_to(message, "👑 *[SYSTEM ERROR]* Cannot disconnect the master owner! 🧠", parse_mode="Markdown")
            return
        if del_id in allowed_users:
            allowed_users.remove(del_id)
            save_users(allowed_users)
            bot.reply_to(message, f"🗑 *USER REMOVED*\nUser `{del_id}` removed from the authorized list! 🔌", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ User ID not found in whitelist.")
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/remove <userid>`", parse_mode="Markdown")

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
        return
    show_admin_panel(message.chat.id)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
        return
    waiting_for_broadcast.add(message.from_user.id)
    bot.send_message(message.chat.id, broadcast_prompt_text(), parse_mode="Markdown", reply_markup=broadcast_cancel_keyboard())

# ----------------- CHANNEL FORWARD CAPTURE (UP TO 10) -----------------

FORWARD_TYPES = ['text', 'photo', 'video', 'document', 'audio', 'animation', 'voice']

@bot.message_handler(
    func=lambda msg: msg.from_user.id in waiting_for_channel_forward and get_forward_chat(msg) is not None,
    content_types=FORWARD_TYPES
)
def handle_channel_forward(message):
    user_id = message.from_user.id
    chat = get_forward_chat(message)
    waiting_for_channel_forward.discard(user_id)

    if chat.type != 'channel':
        bot.reply_to(message, "❌ The forwarded message must be from a *Channel*. Setup cancelled.", parse_mode="Markdown")
        return

    channels = bot_config.get("required_channels", [])
    if len(channels) >= MAX_CHANNELS:
        bot.reply_to(message, f"⚠️ Maximum limit of {MAX_CHANNELS} channels reached. Remove one first using `/channels`.", parse_mode="Markdown")
        return

    channel_id = chat.id
    channel_title = chat.title or "Required Channel"
    channel_username = chat.username

    if any(c.get("id") == channel_id for c in channels):
        bot.reply_to(message, f"⚠️ Channel *{escape_markdown(channel_title)}* (`{channel_id}`) is already in your required list!", parse_mode="Markdown")
        return

    invite_link = None
    try:
        chat_info = bot.get_chat(channel_id)
        if chat_info.invite_link:
            invite_link = chat_info.invite_link
        elif channel_username:
            invite_link = f"https://t.me/{channel_username}"
        else:
            link_obj = bot.create_chat_invite_link(channel_id)
            invite_link = link_obj.invite_link
    except Exception as e:
        if channel_username:
            invite_link = f"https://t.me/{channel_username}"
        print(f"[!] Could not create or fetch invite link: {e}")

    new_channel = {
        "id": channel_id,
        "title": channel_title,
        "username": channel_username,
        "invite_link": invite_link
    }
    channels.append(new_channel)
    bot_config["required_channels"] = channels
    save_config(bot_config)

    clean_title = escape_markdown(channel_title)
    link_text = escape_markdown(invite_link) if invite_link else "No link generated"
    markup = types.InlineKeyboardMarkup()
    markup.add(IBtn("📢 Open Channel Manager", style=PRIMARY, callback_data="btn_channel_info"))
    success_text = (
        f"✅ *CHANNEL #{len(channels)} ADDED*\n"
        f"{DIV}\n\n"
        f"📌 *Title:* {clean_title}\n"
        f"🆔 *ID:* `{channel_id}`\n"
        f"🔗 *Link:* {link_text}\n\n"
        f"Active channels: `{len(channels)}/{MAX_CHANNELS}`\n"
        f"_Users must now join all active channels before using the bot!_"
    )
    bot.reply_to(message, success_text, parse_mode="Markdown", reply_markup=markup)

# ----------------- BROADCAST CAPTURE (OWNER) -----------------

BROADCAST_TYPES = ['text', 'photo', 'video', 'document', 'audio', 'animation', 'voice', 'sticker']

@bot.message_handler(
    func=lambda msg: msg.from_user.id in waiting_for_broadcast and not (msg.text or "").startswith("/"),
    content_types=BROADCAST_TYPES
)
def capture_broadcast(message):
    uid = message.from_user.id
    waiting_for_broadcast.discard(uid)
    pending_broadcast[uid] = (message.chat.id, message.message_id)

    count = len([u for u in known_users if int(u) != OWNER_ID])
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        IBtn(f"✅ Send to {count} users", style=SUCCESS, callback_data="bc_confirm"),
        IBtn("❌ Cancel", style=DANGER, callback_data="bc_cancel")
    )
    bot.reply_to(
        message,
        f"📣 *READY TO SEND*\n{DIV}\n\nThe message above will be delivered to *{count}* users.\nConfirm?",
        parse_mode="Markdown",
        reply_markup=markup
    )

# ----------------- START / MENU / HELP -----------------

@bot.message_handler(commands=['start'])
def send_start(message):
    user = message.from_user
    track_user(user)
    if is_owner(user.id):
        set_owner_commands()
    if not entry_gate(message):
        return
    bot.send_message(message.chat.id, get_welcome_text(user), parse_mode="Markdown", reply_markup=get_reply_keyboard(user.id))

@bot.message_handler(commands=['menu'])
def send_menu(message):
    track_user(message.from_user)
    if not entry_gate(message):
        return
    bot.send_message(message.chat.id, get_dashboard_text(), parse_mode="Markdown", reply_markup=get_main_menu_keyboard(message.from_user.id))

@bot.message_handler(commands=['help', 'commands'])
def send_help(message):
    track_user(message.from_user)
    if not entry_gate(message):
        return
    uid = message.from_user.id
    bot.send_message(message.chat.id, get_help_text(uid), parse_mode="Markdown", reply_markup=get_main_menu_keyboard(uid))

@bot.message_handler(commands=['myid'])
def my_id(message):
    bot.reply_to(message, get_myid_text(message.from_user.id), parse_mode="Markdown")

# ----------------- SYSTEM INFO / VITALS -----------------

@bot.message_handler(commands=['status'])
def server_status(message):
    if not access_ok(message):
        return
    bot.reply_to(message, get_status_text(), parse_mode="Markdown", reply_markup=get_main_menu_keyboard(message.from_user.id))

@bot.message_handler(commands=['sysinfo'])
def sysinfo_cmd(message):
    if not access_ok(message):
        return
    bot.reply_to(message, get_vitals_text(), parse_mode="Markdown")

@bot.message_handler(commands=['memory'])
def memory_cmd(message):
    if not access_ok(message):
        return
    bot.reply_to(message, get_memory_text(), parse_mode="Markdown")

@bot.message_handler(commands=['disk'])
def disk_cmd(message):
    if not access_ok(message):
        return
    bot.reply_to(message, get_disk_text(), parse_mode="Markdown")

# ----------------- BACKGROUND ENGINES (RUN, PS, STOP) -----------------

@bot.message_handler(commands=['run'])
def run_bg(message):
    if not access_ok(message):
        return

    global current_dir
    cmd = message.text.replace('/run', '', 1).strip()
    if not cmd:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/run <command>` (e.g. `/run python app.py`)", parse_mode="Markdown")
        return

    try:
        # DEVNULL (not PIPE): nobody reads the pipe, so a chatty script would freeze once it filled up.
        proc = subprocess.Popen(cmd, shell=True, cwd=current_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bg_processes[proc.pid] = {'process': proc, 'cmd': cmd}
        bot.reply_to(
            message,
            f"🟢 *ENGINE STARTED*\n{DIV}\n\n⚙️ *PID:* `{proc.pid}`\n💻 *CMD:* `{code_safe(cmd)}`\n\n_Running in background..._ 🥷",
            parse_mode="Markdown"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ *CRASH* Engine failed: {e}", parse_mode=None)

@bot.message_handler(commands=['ps'])
def list_ps(message):
    if not access_ok(message):
        return
    text, markup = get_engines_view()
    bot.reply_to(message, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['stop'])
def stop_ps(message):
    if not access_ok(message):
        return

    try:
        pid = int(message.text.split(" ")[1])
        if pid in bg_processes:
            bg_processes[pid]['process'].terminate()
            del bg_processes[pid]
            bot.reply_to(message, f"🛑 *ENGINE KILLED*\nPID `{pid}` terminated successfully! 💀", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ *NOT FOUND*\nTarget PID does not exist in active processes.", parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/stop <pid>`", parse_mode="Markdown")

# ----------------- FILE OPERATIONS -----------------

@bot.message_handler(commands=['download'])
def download_file(message):
    if not access_ok(message):
        return

    global current_dir
    filename = message.text.replace('/download', '', 1).strip()
    if not filename:
        bot.reply_to(message, "⚠️ *Usage:* `/download <filename>`", parse_mode="Markdown")
        return

    filepath = os.path.join(current_dir, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        bot.reply_to(message, "📥 *EXTRACTING*\nTransmitting file... ⏳", parse_mode="Markdown")
        try:
            with open(filepath, 'rb') as f:
                bot.send_document(message.chat.id, f)
        except Exception as e:
            bot.reply_to(message, f"❌ Failed to send file: {e}")
    else:
        bot.reply_to(message, "❌ *404* File not found in current directory! 🔍", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_upload(message):
    if not access_ok(message):
        return

    global current_dir
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        safe_name = os.path.basename(message.document.file_name or "file")
        filepath = os.path.join(current_dir, safe_name)
        with open(filepath, 'wb') as new_file:
            new_file.write(downloaded_file)
        bot.reply_to(message, f"📤 *UPLOAD COMPLETE*\nFile secured at:\n`{code_safe(filepath, 200)}` 🔒", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ *UPLOAD FAILED* Error: {e}", parse_mode=None)

# ----------------- BOTTOM KEYBOARD BUTTONS -----------------
# Must stay ABOVE the terminal handler, otherwise button texts would be run as shell commands.

@bot.message_handler(func=lambda message: message.text in MENU_TEXTS)
def handle_menu_buttons(message):
    uid = message.from_user.id
    text = message.text

    if text == BTN_ADMIN:
        if not is_owner(uid):
            bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
            return
        show_admin_panel(message.chat.id)
        return

    if not access_ok(message):
        return

    if text == BTN_STATUS:
        bot.send_message(message.chat.id, get_status_text(), parse_mode="Markdown")
    elif text == BTN_VITALS:
        bot.send_message(message.chat.id, get_vitals_text(), parse_mode="Markdown")
    elif text == BTN_RAM:
        bot.send_message(message.chat.id, get_memory_text(), parse_mode="Markdown")
    elif text == BTN_DISK:
        bot.send_message(message.chat.id, get_disk_text(), parse_mode="Markdown")
    elif text == BTN_ENGINES:
        view_text, markup = get_engines_view()
        bot.send_message(message.chat.id, view_text, parse_mode="Markdown", reply_markup=markup)
    elif text == BTN_MYID:
        bot.send_message(message.chat.id, get_myid_text(uid), parse_mode="Markdown")
    elif text == BTN_HELP:
        bot.send_message(message.chat.id, get_help_text(uid), parse_mode="Markdown")

# ----------------- DIRECT TERMINAL COMMANDS -----------------

@bot.message_handler(func=lambda message: message.text is not None and not message.text.startswith('/'))
def direct_terminal(message):
    if not access_ok(message):
        return

    global current_dir
    cmd = message.text.strip()

    if cmd.startswith("cd "):
        new_dir = cmd[3:].strip()
        target_path = os.path.abspath(os.path.join(current_dir, new_dir))
        if os.path.exists(target_path) and os.path.isdir(target_path):
            current_dir = target_path
            bot.reply_to(message, f"📂 *DIR CHANGED*\nNow in:\n`{code_safe(current_dir, 200)}` ⚡️", parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ *404* Directory does not exist!", parse_mode="Markdown")
        return

    try:
        bot.send_chat_action(message.chat.id, 'typing')
        result = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT, cwd=current_dir)

        if not result.strip():
            bot.reply_to(message, "✅ *EXECUTED*\nCommand succeeded with empty output. 🥷", parse_mode="Markdown")
        else:
            if len(result) > 4000:
                out_path = "output.txt"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(result)
                with open(out_path, "rb") as f:
                    bot.send_document(message.chat.id, f, caption="⚠️ Output is too long — sent as a file.")
            else:
                bot.reply_to(message, f"```\n{result.replace('```', chr(39) * 3)}\n```", parse_mode="Markdown")
    except subprocess.CalledProcessError as e:
        out = (e.output or "").replace('```', chr(39) * 3)
        if len(out) > 3900:
            out = out[-3900:]
        bot.reply_to(message, f"🛑 *COMMAND ERROR*\n```\n{out}\n```", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ *SYSTEM ERROR*: {e}", parse_mode=None)

# ----------------- INLINE CALLBACK HANDLERS -----------------

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    # 1. Verification of Channel Subscriptions
    if data == "verify_membership":
        is_all_joined, missing = check_channel_subscriptions(user_id)
        if is_all_joined:
            if is_allowed_user(user_id):
                bot.answer_callback_query(call.id, "✅ Verified! Welcome back.")
                edit(call, "✅ *VERIFIED*\n\nAll channels joined — you're good to go! 🚀")
                bot.send_message(call.message.chat.id, get_welcome_text(call.from_user), parse_mode="Markdown", reply_markup=get_reply_keyboard(user_id))
            else:
                bot.answer_callback_query(call.id, "✅ All channels verified!")
                edit(call, get_locked_text(call.from_user, joined=True), get_request_access_keyboard())
        else:
            bot.answer_callback_query(call.id, f"❌ You still have {len(missing)} channel(s) left to join!", show_alert=True)
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=get_join_channels_keyboard(missing)
                )
            except Exception:
                pass
        return

    # 2. User Sends Auth Request to Admin
    if data == "send_auth_request":
        is_all_joined, missing = check_channel_subscriptions(user_id)
        if not is_all_joined:
            bot.answer_callback_query(call.id, "❌ You must join all channels first!", show_alert=True)
            return

        if is_allowed_user(user_id):
            bot.answer_callback_query(call.id, "✅ You are already authorized!")
            edit(call, "✅ *You already have access!*\n\nSend /start to open your dashboard.")
            bot.send_message(call.message.chat.id, get_welcome_text(call.from_user), parse_mode="Markdown", reply_markup=get_reply_keyboard(user_id))
            return

        if user_id in pending_requests:
            bot.answer_callback_query(call.id, "⏳ Your request is already with the Admin.", show_alert=True)
            return

        track_user(call.from_user)

        # Sanitize names to prevent Markdown parse error
        raw_full_name = f"{call.from_user.first_name} {call.from_user.last_name or ''}".strip()
        safe_full_name = escape_markdown(raw_full_name)

        if call.from_user.username:
            safe_username = "@" + escape_markdown(call.from_user.username)
        else:
            safe_username = "None"

        admin_markup = types.InlineKeyboardMarkup(row_width=2)
        admin_markup.add(
            IBtn("✅ Allow", style=SUCCESS, callback_data=f"auth_allow_{user_id}"),
            IBtn("❌ Deny", style=DANGER, callback_data=f"auth_deny_{user_id}")
        )

        channels_count = len(bot_config.get("required_channels", []))
        admin_text = (
            f"🔔 *NEW ACCESS REQUEST*\n"
            f"{DIV}\n\n"
            f"👤 *Name:* {safe_full_name}\n"
            f"🔗 *Username:* {safe_username}\n"
            f"🆔 *User ID:* `{user_id}`\n"
            f"📢 *Channels:* Joined all `{channels_count}` ✅\n\n"
            f"Grant terminal access to this user?"
        )
        user_wait_text = (
            "⏳ *REQUEST SENT*\n\n"
            "Your request is with the Admin now.\n"
            "You'll get a message here as soon as it's approved! 🚀"
        )

        try:
            bot.send_message(OWNER_ID, admin_text, parse_mode="Markdown", reply_markup=admin_markup)
        except Exception:
            # Fallback to plain text if Markdown still hits a formatting conflict
            try:
                plain_text = (
                    f"🔔 NEW ACCESS REQUEST\n\n"
                    f"Name: {raw_full_name}\n"
                    f"Username: @{call.from_user.username if call.from_user.username else 'None'}\n"
                    f"User ID: {user_id}\n"
                    f"Channels: Joined all {channels_count}\n\n"
                    f"Grant terminal access to this user?"
                )
                bot.send_message(OWNER_ID, plain_text, reply_markup=admin_markup)
            except Exception as e2:
                bot.answer_callback_query(call.id, f"Error reaching Admin: {e2}", show_alert=True)
                return

        pending_requests.add(user_id)
        bot.answer_callback_query(call.id, "📨 Request sent to Admin!")
        edit(call, user_wait_text)
        return

    # 3. Admin Decision: ALLOW
    if data.startswith("auth_allow_"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only.", show_alert=True)
            return
        target_uid = int(data.replace("auth_allow_", ""))
        allowed_users.add(target_uid)
        save_users(allowed_users)
        pending_requests.discard(target_uid)

        bot.answer_callback_query(call.id, f"User {target_uid} approved!")
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=call.message.text + f"\n\n🟢 DECISION: Allowed on {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception:
            pass

        try:
            bot.send_message(
                target_uid,
                "🎉 *ACCESS APPROVED*\n\nThe Admin approved your request — you now have full terminal access.\nYour keyboard is ready below 👇",
                parse_mode="Markdown",
                reply_markup=get_reply_keyboard(target_uid)
            )
        except Exception as e:
            print(f"[!] Notification to user {target_uid} failed: {e}")
        return

    # 4. Admin Decision: DENY
    if data.startswith("auth_deny_"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only.", show_alert=True)
            return
        target_uid = int(data.replace("auth_deny_", ""))
        pending_requests.discard(target_uid)

        bot.answer_callback_query(call.id, f"User {target_uid} denied.")
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=call.message.text + f"\n\n🔴 DECISION: Denied on {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception:
            pass

        try:
            bot.send_message(target_uid, "🚫 *ACCESS DENIED*\nThe Admin has rejected your request.", parse_mode="Markdown")
        except Exception:
            pass
        return

    # ----------------- OWNER-ONLY ACTIONS -----------------
    owner_actions = (
        "adm_panel", "btn_channel_info", "add_new_channel_btn", "cancel_channel_setup",
        "adm_broadcast", "bc_cancel", "bc_confirm"
    )
    if data in owner_actions or data.startswith(("adm_users_", "adm_rm_", "del_ch_")):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only.", show_alert=True)
            return

        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        if data == "adm_panel":
            waiting_for_broadcast.discard(user_id)
            bot.answer_callback_query(call.id)
            show_admin_panel(chat_id, msg_id)

        elif data.startswith("adm_users_"):
            try:
                page = int(data.replace("adm_users_", ""))
            except ValueError:
                page = 0
            bot.answer_callback_query(call.id)
            show_users_manager(chat_id, msg_id, page)

        elif data.startswith("adm_rm_"):
            try:
                _, _, uid_s, page_s = data.split("_")
                target = int(uid_s)
                page = int(page_s)
            except ValueError:
                bot.answer_callback_query(call.id)
                return
            if target == OWNER_ID:
                bot.answer_callback_query(call.id, "👑 You can't remove the owner.", show_alert=True)
                return
            if target in allowed_users:
                allowed_users.discard(target)
                save_users(allowed_users)
                bot.answer_callback_query(call.id, "🗑 Access removed")
            else:
                bot.answer_callback_query(call.id, "User already removed.")
            show_users_manager(chat_id, msg_id, page)

        elif data == "adm_broadcast":
            waiting_for_broadcast.add(user_id)
            bot.answer_callback_query(call.id)
            safe_edit(chat_id, msg_id, broadcast_prompt_text(), broadcast_cancel_keyboard())

        elif data == "bc_cancel":
            waiting_for_broadcast.discard(user_id)
            pending_broadcast.pop(user_id, None)
            bot.answer_callback_query(call.id, "Cancelled")
            safe_edit(chat_id, msg_id, "❌ *Broadcast cancelled.*", get_admin_back_keyboard())

        elif data == "bc_confirm":
            if user_id not in pending_broadcast:
                bot.answer_callback_query(call.id, "Nothing to send.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "📤 Sending...")
            safe_edit(chat_id, msg_id, "📤 *Sending broadcast...*\n_Please wait_")
            threading.Thread(target=run_broadcast, args=(user_id, chat_id, msg_id), daemon=True).start()

        elif data == "btn_channel_info":
            bot.answer_callback_query(call.id)
            show_channel_manager(chat_id, msg_id)

        elif data == "add_new_channel_btn":
            current_count = len(bot_config.get("required_channels", []))
            if current_count >= MAX_CHANNELS:
                bot.answer_callback_query(call.id, f"Limit reached ({MAX_CHANNELS} channels maximum).", show_alert=True)
                return
            waiting_for_channel_forward.add(user_id)
            text = (
                f"📢 *ADD CHANNEL* ({current_count}/{MAX_CHANNELS})\n"
                f"{DIV}\n\n"
                f"Make sure the bot is *admin* in the channel.\n"
                f"Now *forward any post from that channel* here.\n\n"
                f"_Send /cancel to stop._"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(IBtn("❌ Cancel", style=DANGER, callback_data="cancel_channel_setup"))
            safe_edit(chat_id, msg_id, text, markup)
            bot.answer_callback_query(call.id)

        elif data.startswith("del_ch_"):
            target_ch_id = int(data.replace("del_ch_", ""))
            channels = bot_config.get("required_channels", [])
            bot_config["required_channels"] = [c for c in channels if c.get("id") != target_ch_id]
            save_config(bot_config)
            bot.answer_callback_query(call.id, "Channel removed!")
            show_channel_manager(chat_id, msg_id)

        elif data == "cancel_channel_setup":
            waiting_for_channel_forward.discard(user_id)
            bot.answer_callback_query(call.id)
            safe_edit(chat_id, msg_id, "❌ *Channel setup cancelled.*", get_admin_back_keyboard())
        return

    # Check authorization for other controls
    if not is_allowed_user(user_id):
        bot.answer_callback_query(call.id, "⛔ Access denied. Unauthorized.", show_alert=True)
        return

    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.answer_callback_query(call.id, "⚠️ Channel membership required!", show_alert=True)
        return

    # Kill Process Callback
    if data.startswith("kill_"):
        pid = int(data.replace("kill_", ""))
        if pid in bg_processes:
            try:
                bg_processes[pid]['process'].terminate()
                del bg_processes[pid]
                bot.answer_callback_query(call.id, f"Engine {pid} killed!")
                bot.send_message(call.message.chat.id, f"🛑 *ENGINE KILLED*\nPID `{pid}` successfully terminated.", parse_mode="Markdown")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "PID already stopped or not found.")
        return

    # Dashboard Actions
    if data == "btn_main_menu":
        edit(call, get_dashboard_text(), get_main_menu_keyboard(user_id))
        bot.answer_callback_query(call.id)

    elif data == "btn_status":
        edit(call, get_status_text(), get_back_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "btn_sysinfo":
        edit(call, get_vitals_text(), get_back_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "btn_memory":
        edit(call, get_memory_text(), get_back_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "btn_disk":
        edit(call, get_disk_text(), get_back_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "btn_ps":
        text, markup = get_engines_view(with_back=True)
        edit(call, text, markup)
        bot.answer_callback_query(call.id)

    elif data == "btn_myid":
        edit(call, get_myid_text(call.from_user.id), get_back_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "btn_help":
        edit(call, get_help_text(user_id), get_back_keyboard())
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id)

# ----------------- START THE BOT -----------------

setup_command_menu()
print("⚡️ DEV X HOST is online and waiting...")
bot.infinity_polling()
