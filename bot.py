import os
import re
import json
import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.types import (
    InputPeerUser,
    UpdateBotChatInviteRequester,
)
from telethon.errors import (
    UserIsBlockedError,
    FloodWaitError,
    SessionPasswordNeededError,
)

# ------------------ ENVIRONMENT ------------------
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ProBot")

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
SESSION_STRING = os.environ.get("SESSION_STRING", "")

def parse_admin_ids(env_string):
    ids = []
    for uid in env_string.split(","):
        uid = uid.strip()
        if uid.lstrip('-').isdigit():
            ids.append(int(uid))
    return ids

ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", "0"))

# ------------------ DATABASE & STATE ------------------
DB_PATH = Path("bot_data.db")

tracked_users: dict[int, int] = {}      
blocked_users: set[int] = set()
saved_messages: dict[int, dict] = {}    
welcome_enabled = True
recently_welcomed: set[int] = set()

admin_chat_state: dict[int, dict] = {}
auto_replies: dict[str, str] = {}
admin_msg_map: dict[tuple, list] = {} 

def init_db():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                access_hash INTEGER NOT NULL,
                blocked INTEGER DEFAULT 0,
                joined_date TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                step INTEGER PRIMARY KEY,
                msg_type TEXT,
                text TEXT,
                msg_id INTEGER,
                from_chat INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS auto_replies (
                keyword TEXT PRIMARY KEY,
                response TEXT
            );
        """)
        return conn

def load_from_db():
    global tracked_users, blocked_users, saved_messages, welcome_enabled, auto_replies
    with sqlite3.connect(str(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, access_hash, blocked FROM users")
        for uid, hsh, blk in cur.fetchall():
            tracked_users[uid] = hsh
            if blk:
                blocked_users.add(uid)
                
        cur.execute("SELECT step, msg_type, text, msg_id, from_chat FROM messages")
        for step, mtype, text, mid, fchat in cur.fetchall():
            saved_messages[step] = {
                "type": mtype, "text": text, "msg_id": mid, "from_chat": fchat
            }
            
        cur.execute("SELECT value FROM settings WHERE key='welcome_enabled'")
        row = cur.fetchone()
        if row:
            welcome_enabled = row[0] == "1"
            
        cur.execute("SELECT keyword, response FROM auto_replies")
        for kw, resp in cur.fetchall():
            auto_replies[kw] = resp
            
    logger.info(f"Loaded {len(tracked_users)} users, {len(auto_replies)} auto-replies.")

def save_user(uid: int, access_hash: int, blocked: bool = False):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date) VALUES (?,?,?,?)",
            (uid, access_hash, int(blocked), datetime.now().isoformat()),
        )

def save_message_step(step: int, data: dict):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)",
            (step, data.get("type"), data.get("text"), data.get("msg_id"), data.get("from_chat")),
        )

def set_setting(key: str, value: str):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

# ------------------ EVENT LOOP & CLIENT (Render Fix) ------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else "bot_session",
    API_ID,
    API_HASH,
    loop=loop
)

# ------------------ WEB SERVER ------------------
async def web_handler(request):
    return web.Response(text="🟢 Bot is alive")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", web_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

# ------------------ DECORATORS ------------------
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("❌ `[Access Denied] Admin privileges required.`")
            return
        await func(event)
    return wrapper

# ------------------ ADMIN ALERTS ------------------
async def notify_admins_new_user(user):
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    
    safe_name = full_name.replace('[', '').replace(']', '').replace('*', '').replace('_', '').replace('`', '')
    if not safe_name.strip():
        safe_name = "Unknown User"
        
    linked_name = f"[{safe_name}](tg://user?id={user.id})"
    username_display = f"@{user.username}" if user.username else "❌ `[No Username]`"
    
    text = (
        f"🚨 **NEW USER CONNECTION** 🚨\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👤 **Name:** {linked_name}\n"
        f"🔗 **Username:** {username_display}\n"
        f"🆔 **UID:** `{user.id}` *(Tap to copy)*\n\n"
        f"💬 **Quick Action:** Reply or send `/send {user.id} <message>`\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖"
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(admin_id, text)
        except Exception:
            pass

# ------------------ WELCOME SYSTEM ------------------
async def remove_welcome_cooldown(uid):
    await asyncio.sleep(30)
    recently_welcomed.discard(uid)

async def send_welcome_sequence(user):
    uid = user.id
    if uid in recently_welcomed:
        return
    recently_welcomed.add(uid)

    if getattr(user, "is_bot", False):
        return

    try:
        full_user = await client.get_input_entity(uid)
        if not isinstance(full_user, InputPeerUser):
            return
        access_hash = full_user.access_hash
    except Exception:
        return

    is_new_user = uid not in tracked_users
    tracked_users[uid] = access_hash
    
    save_user(uid, access_hash, blocked=False)
    blocked_users.discard(uid)

    if is_new_user and uid not in ADMIN_IDS:
        asyncio.create_task(notify_admins_new_user(user))

    if not welcome_enabled or uid in ADMIN_IDS:
        asyncio.create_task(remove_welcome_cooldown(uid))
        return

    for step in sorted(saved_messages.keys()):
        item = saved_messages[step]
        try:
            if item["type"] == "forward":
                try:
                    await client.get_messages(item["from_chat"], ids=item["msg_id"])
                except Exception:
                    continue
                await client.forward_messages(full_user, item["msg_id"], item["from_chat"])
            else:
                await client.send_message(full_user, item["text"])
            await asyncio.sleep(0.5)
        except UserIsBlockedError:
            blocked_users.add(uid)
            save_user(uid, access_hash, blocked=True)
            return
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception:
            continue
            
    asyncio.create_task(remove_welcome_cooldown(uid))

@client.on(events.ChatAction())
async def chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        if user:
            await send_welcome_sequence(user)

@client.on(events.Raw(types=UpdateBotChatInviteRequester))
async def join_request(event):
    try:
        user = await client.get_entity(event.user_id)
        await send_welcome_sequence(user)
    except Exception:
        pass

# ------------------ START COMMAND ------------------
@client.on(events.NewMessage(pattern="(?i)^/start$"))
async def start(event):
    admin_chat_state.pop(event.sender_id, None)
    
    if event.sender_id in blocked_users:
        blocked_users.discard(event.sender_id)
        access_hash = tracked_users.get(event.sender_id, 0)
        save_user(event.sender_id, access_hash, blocked=False)
    
    if event.sender_id in ADMIN_IDS:
        btns = [
            [Button.text("📊 Stats"), Button.text("⚙️ Status")],
            [Button.text("📢 Broadcast"), Button.text("✉️ Send Message")],
            [Button.text("🔢 Set Sequence"), Button.text("🔇 Toggle Welcome")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")],
            [Button.text("🔑 Gen Session"), Button.text("🧹 Cleanup")]
        ]
        admin_panel_text = (
            "👨‍💻 **ADMIN CONTROL PANEL**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "Select an operation below to manage your bot systems seamlessly.\n\n"
            "💡 *Tip: To view active auto-replies, send `/listreplies`*"
        )
        await event.reply(admin_panel_text, buttons=btns)
    else:
        user = await event.get_sender()
        if user:
            await send_welcome_sequence(user)

# 🔥 ------------------ GLOBAL DELETE COMMAND ------------------ 🔥
@client.on(events.NewMessage(pattern=r"^/del$"))
@admin_only
async def global_delete(event):
    if not event.is_reply:
        return await event.reply("❌ `[Error] Please reply to the user's message with /del to wipe it.`")
    
    replied = await event.get_reply_message()
    key = (event.chat_id, replied.id)
    
    if key in admin_msg_map:
        target_msgs = admin_msg_map[key]
        for adm_id, msg_id in target_msgs:
            try:
                await client.delete_messages(adm_id, [msg_id])
            except Exception:
                pass
        
        for adm_id, msg_id in target_msgs:
            admin_msg_map.pop((adm_id, msg_id), None)
            
        del_confirm = await event.respond("✅ `[sys] Message wiped globally from all Admin panels.`")
        try: await event.delete() 
        except: pass
        await asyncio.sleep(3)
        try: await del_confirm.delete() 
        except: pass
    else:
        warn = await event.reply("⚠️ `[Notice] Cannot wipe. Message not found in active global registry.`")
        await asyncio.sleep(3)
        try: await warn.delete()
        except: pass

# 🔥 ------------------ SESSION GENERATOR (IN-BUILT) ------------------ 🔥
@client.on(events.NewMessage(pattern=r"(?i)^(/gensec|🔑 Gen Session)$"))

@admin_only
async def generate_session(event):
    admin_id = event.sender_id
    try:
        async with client.conversation(admin_id, timeout=120) as conv:
            await conv.send_message("📱 **SESSION GENERATOR**\n\n👉 Apna mobile number country code ke sath bhejo\n*(Example: `+919876543210`)*\n*(Cancel karne ke liye /cancel likho)*")
            phone_msg = await conv.get_response()
            phone = phone_msg.text.strip()
            
            if phone.lower() == "/cancel":
                return await conv.send_message("❌ Process cancelled.")

            temp_client = TelegramClient(StringSession(), API_ID, API_HASH, loop=loop)
            await temp_client.connect()
            
            try:
                send_code = await temp_client.send_code_request(phone)
                
                warn_msg = (
                    "✅ **OTP bhej diya gaya hai!**\n\n"
                    "⚠️ **IMPORTANT RULE:** OTP ko space dekar likhna varna Telegram security issue samajh kar account block kar dega!\n"
                    "*(Sahi Tarika: `1 2 3 4 5`)*"
                )
                await conv.send_message(warn_msg)
                
                otp_msg = await conv.get_response()
                if otp_msg.text.strip().lower() == "/cancel":
                    await temp_client.disconnect()
                    return await conv.send_message("❌ Cancelled.")
                    
                otp = otp_msg.text.replace(" ", "").strip()
                
                try:
                    await temp_client.sign_in(phone=phone, code=otp, phone_code_hash=send_code.phone_code_hash)
                except SessionPasswordNeededError:
                    await conv.send_message("🔐 Is account me 2FA (Two-Step Verification) laga hai. Apna password bhejo:")
                    pwd_msg = await conv.get_response()
                    if pwd_msg.text.strip().lower() == "/cancel":
                        await temp_client.disconnect()
                        return await conv.send_message("❌ Cancelled.")
                    await temp_client.sign_in(password=pwd_msg.text.strip())
                
                session_string = temp_client.session.save()
                await temp_client.disconnect()
                
                success_text = (
                    f"✅ **SESSION STRING READY!**\n\n"
                    f"👇 Ise click karke copy karlo:\n\n"
                    f"`{session_string}`\n\n"
                    f"*(Ye account ab Viewbot ya Live joiner banne ke liye 100% ready hai!)*"
                )
                await conv.send_message(success_text)
                
            except asyncio.TimeoutError:
                await conv.send_message("⏳ **Time out!** Aapne OTP daalne me zyada time laga diya. Phir se `/gensec` try karein.")
                await temp_client.disconnect()
            except Exception as e:
                await conv.send_message(f"❌ **Error:** `{e}`")
                await temp_client.disconnect()
                
    except Exception as e:
        await event.reply("⚠️ Ek conversation pehle se chal rahi hai. Kripya thodi der baad try karein.")

# ------------------ ADMIN SMART SEND MESSAGE ------------------
@client.on(events.NewMessage(pattern=r"^✉️ Send Message$"))
@admin_only
async def btn_send_dm_start(event):
    admin_id = event.sender_id
    prompt = await event.reply("👤 **DIRECT MESSAGING**\n➖➖➖➖➖➖➖➖\n👉 Niche target user ki **UID** type karke bhejo:\n*(Cancel karne ke liye `/cancel` likhein)*")
    admin_chat_state[admin_id] = {"step": "waiting_for_id", "target_id": None, "delete_msgs": [event.id, prompt.id]}

@client.on(events.NewMessage(pattern=r"^/cancel$"))
@admin_only
async def cancel_state(event):
    if event.sender_id in admin_chat_state:
        del admin_chat_state[event.sender_id]
        await event.reply("🚫 `[Notice] Operation cancelled successfully.`")

@client.on(events.NewMessage(func=lambda e: e.sender_id in ADMIN_IDS and e.sender_id in admin_chat_state))
async def handle_admin_chat_state(event):
    admin_id = event.sender_id
    state = admin_chat_state[admin_id]
    
    if event.text and event.text.startswith('/'): return 
        
    if state["step"] == "waiting_for_id":
        text = event.text.strip()
        if text.lstrip('-').isdigit():
            target_id = int(text)
            state["target_id"] = target_id
            state["step"] = "waiting_for_msg"
            state["delete_msgs"].append(event.id)
            try: await client.delete_messages(admin_id, state["delete_msgs"])
            except Exception: pass
            prompt = await event.respond(f"✅ **Target UID Locked:** `{target_id}`\n➖➖➖➖➖➖➖➖\n✍️ **Ab apna Message, Photo ya Video bhejo:**\n*(Cancel karne ke liye `/cancel` likhein)*")
            state["delete_msgs"] = [prompt.id] 
        else:
            await event.reply("❌ `[Error] Invalid format.` Sirf numbers allow hain. Phir se UID type karein:")
            
    elif state["step"] == "waiting_for_msg":
        target_id = state["target_id"]
        try:
            try: await client.delete_messages(admin_id, state["delete_msgs"])
            except Exception: pass
            access_hash = tracked_users.get(target_id, 0)
            peer = InputPeerUser(target_id, access_hash) if access_hash else target_id
            
            if event.text: await client.send_message(peer, event.text, file=event.media)
            elif event.media: await client.send_message(peer, file=event.media)
                
            await event.respond(f"✅ **Message successfully delivered to `{target_id}`!**")
            del admin_chat_state[admin_id]
        except UserIsBlockedError:
            blocked_users.add(target_id)
            save_user(target_id, tracked_users.get(target_id, 0), blocked=True)
            await event.respond(f"❌ `[Error] Delivery Failed: User blocked the bot.`")
            del admin_chat_state[admin_id]
        except Exception as e:
            await event.respond(f"❌ `[Error] Delivery Failed:` System issue ya invalid ID.")
            del admin_chat_state[admin_id]
    raise events.StopPropagation 

@client.on(events.NewMessage(pattern=r"^/send\s+(\d+)(?:\s+(.+))?"))
@admin_only
async def send_dm_manual(event):
    target_uid = int(event.pattern_match.group(1))
    text_msg = event.pattern_match.group(2)
    try:
        peer = InputPeerUser(target_uid, tracked_users.get(target_uid, 0))
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            await client.send_message(peer, text_msg or reply_msg.text or "", file=reply_msg.media)
        elif text_msg:
            await client.send_message(peer, text_msg.strip())
        await event.reply(f"✅ **Message successfully delivered to `{target_uid}`!**")
    except UserIsBlockedError:
        blocked_users.add(target_uid)
        save_user(target_uid, tracked_users.get(target_uid, 0), blocked=True)
        await event.reply(f"❌ `[Error] Delivery Failed: User blocked the bot.`")
    except Exception as e:
        await event.reply(f"❌ `[Error] Delivery Failed:` System issue ya invalid ID.")

# ------------------ AUTO-RESPONDER COMMANDS ------------------
@client.on(events.NewMessage(pattern=r"^/setreply\s+(.+?)\s*\|\s*(.+)"))
@admin_only
async def set_auto_reply(event):
    keyword = event.pattern_match.group(1).strip().lower()
    response = event.pattern_match.group(2).strip()
    auto_replies[keyword] = response
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?, ?)", (keyword, response))
    await event.reply(f"✅ **Auto-Reply Saved!**\n➖➖➖➖➖➖➖➖\n🔑 **Keyword:** `{keyword}`\n🤖 **Response:**\n{response}")

@client.on(events.NewMessage(pattern=r"^/delreply\s+(.+)"))
@admin_only
async def del_auto_reply(event):
    keyword = event.pattern_match.group(1).strip().lower()
    if keyword in auto_replies:
        del auto_replies[keyword]
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM auto_replies WHERE keyword=?", (keyword,))
        await event.reply(f"🗑 **Auto-reply deleted** for keyword: `{keyword}`")
    else:
        await event.reply(f"⚠️ Keyword `{keyword}` database mein nahi mila.")

@client.on(events.NewMessage(pattern=r"^/listreplies$"))
@admin_only
async def list_auto_replies(event):
    if not auto_replies: return await event.reply("📭 Abhi koi auto-replies set nahi hain.")
    msg = "🤖 **ACTIVE AUTO-REPLIES:**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    for kw, resp in auto_replies.items():
        short_resp = resp[:25] + "..." if len(resp) > 25 else resp
        msg += f"🔹 `{kw}` ➡ {short_resp}\n"
    await event.reply(msg)

# ------------------ STATS / STATUS / BROADCAST ------------------
@client.on(events.NewMessage(pattern=r"^(/stats|📊 Stats)$"))
@admin_only
async def stats(event):
    msg = (
        "📊 **BOT STATISTICS & METRICS**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👥 **Active Users:** `{len(tracked_users)}`\n"
        f"🚫 **Blocked Users:** `{len(blocked_users)}`\n"
        f"🤖 **Auto-Replies:** `{len(auto_replies)}`\n"
        f"🔊 **Welcome Status:** `{'🟢 ON' if welcome_enabled else '🔴 OFF'}`\n\n"
        f"📁 **CONFIGURED MESSAGES:**\n"
        f" ├ Sequence Steps: `{len(saved_messages)}`\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖"
    )
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^(/status|⚙️ Status)$"))
@admin_only
async def status(event):
    steps = "\n".join(f"  {s}: {d['type']}" for s, d in sorted(saved_messages.items()))
    await event.reply(f"⚙️ **System Status**\nUsers: `{len(tracked_users)}`\nBlocked: `{len(blocked_users)}`\nSequence:\n{steps if steps else 'none'}")

@client.on(events.NewMessage(pattern=r"^/broadcast"))
@admin_only
async def broadcast(event):
    if not tracked_users: return await event.reply("❌ Koi active user nahi hai.")
    text = event.text.replace("/broadcast", "").strip()
    if not text and not event.is_reply: return await event.reply("❌ Message text dein ya kisi message par reply karein.")

    status_msg = await event.reply(f"📢 Broadcasting to {len(tracked_users)} users...")
    success = fail = skip = 0

    for uid, old_hash in list(tracked_users.items()):
        if uid in blocked_users: skip += 1; continue
        try:
            peer = await client.get_input_entity(uid)
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(peer, reply_msg.id, reply_msg.chat_id)
            else:
                await client.send_message(peer, text)
            success += 1
            await asyncio.sleep(0.3)
        except UserIsBlockedError:
            blocked_users.add(uid)
            save_user(uid, old_hash, blocked=True)
            fail += 1
        except Exception:
            fail += 1

    await status_msg.edit(f"✅ **Broadcast Completed**\n➖➖➖➖➖➖➖➖\n✅ Delivered: `{success}`\n❌ Failed: `{fail}`\n⏭ Skipped: `{skip}`")

@client.on(events.NewMessage(pattern=r"^/setmsg(\d+)(?:\s+(.+))?"))
@admin_only
async def setmsg(event):
    step = int(event.pattern_match.group(1))
    extra = event.pattern_match.group(2)
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        data = {"type": "forward", "msg_id": reply_msg.id, "from_chat": event.chat_id}
    elif extra is not None:
        data = {"type": "text", "text": extra.strip()}
    else:
        if step in saved_messages:
            del saved_messages[step]
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("DELETE FROM messages WHERE step=?", (step,))
            return await event.reply(f"🗑 Sequence step {step} hata diya gaya hai.")
        else:
            return await event.reply("❌ Text provide karein ya kisi message par reply karein.")

    saved_messages[step] = data
    save_message_step(step, data)
    await event.reply(f"✅ Sequence Step {step} save ho gaya hai.")

@client.on(events.NewMessage(pattern=r"^(/togglewelcome|🔇 Toggle Welcome)$"))
@admin_only
async def toggle_welcome(event):
    global welcome_enabled
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    await event.reply(f"🔊 Welcome protocol is now **{'🟢 ON' if welcome_enabled else '🔴 OFF'}**.")

@client.on(events.NewMessage(pattern=r"^(/backup|📁 Backup)$"))
@admin_only
async def backup(event):
    data = {
        "tracked": tracked_users,
        "blocked": list(blocked_users),
        "messages": {str(k): v for k, v in saved_messages.items()},
        "welcome": welcome_enabled,
        "auto_replies": auto_replies
    }
    file = "backup.json"
    with open(file, "w") as f: json.dump(data, f)
    await client.send_file(event.chat_id, file, caption="📁 `[System Backup File]`")
    os.remove(file)

@client.on(events.NewMessage(pattern=r"^/restore"))
@admin_only
async def restore(event):
    if not event.is_reply: return await event.reply("❌ Please reply to a `.json` backup file.")
    rep = await event.get_reply_message()
    if not rep.file or not rep.file.name.endswith(".json"): return await event.reply("❌ Invalid file format.")
    path = await client.download_media(rep.media)
    try:
        with open(path, "r") as f: data = json.load(f)
        global tracked_users, blocked_users, saved_messages, welcome_enabled, auto_replies
        tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
        blocked_users = set(int(u) for u in data.get("blocked", []))
        saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
        welcome_enabled = data.get("welcome", True)
        auto_replies = data.get("auto_replies", {})

        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM auto_replies")
            
            for uid, hsh in tracked_users.items():
                conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", (uid, hsh, int(uid in blocked_users)))
            for step, d in saved_messages.items():
                conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)", (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
            for kw, resp in auto_replies.items():
                conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?,?)", (kw, resp))
                
        set_setting("welcome_enabled", "1" if welcome_enabled else "0")
        await event.reply(f"✅ **Restore Successful:** Loaded `{len(tracked_users)}` users.")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path): os.remove(path)

@client.on(events.NewMessage(pattern=r"^(📢 Broadcast|🔢 Set Sequence)$"))
@admin_only
async def helper_buttons(event):
    text = event.text
    if text == "📢 Broadcast": await event.reply("📢 **How to Broadcast:**\n👉 Send `/broadcast <text>` or reply to a message.")
    elif text == "🔢 Set Sequence": await event.reply("🔢 **How to Set Sequence:**\n👉 Send `/setmsg1 <text>` or reply to a message.")

@client.on(events.NewMessage(pattern=r"^(📁 Backup|🔄 Restore)$"))
@admin_only
async def backup_restore_buttons(event):
    if event.text == "📁 Backup":
        await backup(event)
    elif event.text == "🔄 Restore":
        if os.path.exists("auto_backup.json"):
            try:
                with open("auto_backup.json", "r") as f: data = json.load(f)
                global tracked_users, blocked_users, saved_messages, welcome_enabled, auto_replies
                tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
                blocked_users = set(int(u) for u in data.get("blocked", []))
                saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
                welcome_enabled = data.get("welcome", True)
                auto_replies = data.get("auto_replies", {})
                
                with sqlite3.connect(str(DB_PATH)) as conn:
                    conn.execute("DELETE FROM users")
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM auto_replies")
                    for uid, hsh in tracked_users.items(): conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", (uid, hsh, int(uid in blocked_users)))
                    for step, d in saved_messages.items(): conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)", (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
                    for kw, resp in auto_replies.items(): conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?,?)", (kw, resp))
                        
                set_setting("welcome_enabled", "1" if welcome_enabled else "0")
                await event.reply(f"✅ Restored from auto-backup successfully. Active users: `{len(tracked_users)}`")
            except Exception as e:
                await event.reply(f"❌ Auto-restore failed: {e}")
        else:
            await event.reply("🔄 **Restore Guide:**\n1. Generate a backup using `/backup`.\n2. Reply to that `.json` file with `/restore`.")

@client.on(events.NewMessage(pattern=r"^(/cleanup|🧹 Cleanup)$"))
@admin_only
async def cleanup(event):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM users WHERE blocked=1")
    for uid in list(blocked_users):
        tracked_users.pop(uid, None)
    blocked_users.clear()
    await event.reply("🧹 Database cleaned. Dead blocked users removed.")

# ------------------ TWO-WAY CHAT & AUTO-REPLY ------------------
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def seamless_chat_handler(event):
    if event.sender_id in admin_chat_state: return

    text = event.raw_text.lower() if event.raw_text else ""
    
    if text.startswith('/') or "✉️ send message" in text or "📢 broadcast" in text or "📊 stats" in text or "⚙️ status" in text or "🔢 set sequence" in text or "🔇 toggle welcome" in text or "📁 backup" in text or "🔄 restore" in text or "🧹 cleanup" in text or "🔑 gen session" in text:
        return

    if event.sender_id not in ADMIN_IDS:
        bot_auto_replied = False
        for kw, resp in auto_replies.items():
            if kw in text:
                try:
                    await client.send_message(event.sender_id, resp)
                    bot_auto_replied = True
                    break 
                except Exception:
                    pass

        user = await event.get_sender()
        name = user.first_name or "User"
        auto_tag = "🤖 *(AI Auto-Responded)*\n" if bot_auto_replied else ""
        
        caption = (
            f"💬 **SUPPORT INTERCEPT** 💬\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"{auto_tag}"
            f"👤 **From:** [{name}](tg://user?id={event.sender_id})\n"
            f"🆔 `{event.sender_id}`\n"
        )
        
        if event.raw_text: caption += f"\n📝 **Message:**\n{event.raw_text}"
        caption += "\n\n👇 *(Reply or use /del to wipe this)*"
        
        sent_admin_msgs = []
        for admin_id in ADMIN_IDS:
            try: 
                sent_msg = await client.send_message(admin_id, caption, file=event.media)
                sent_admin_msgs.append((admin_id, sent_msg.id))
            except Exception: 
                pass
        
        if sent_admin_msgs:
            for adm_id, msg_id in sent_admin_msgs:
                admin_msg_map[(adm_id, msg_id)] = sent_admin_msgs
                
        try:
            if not bot_auto_replied:
                feedback_msg = await event.reply("✅ *Message delivered to Admin.*")
                await asyncio.sleep(3)
                await feedback_msg.delete()
        except Exception:
            pass
            
    else:
        # --- ADMIN TO USER ---
        if event.is_reply:
            replied_msg = await event.get_reply_message()
            replied_text = replied_msg.raw_text or ""
            
            match = re.search(r"🆔 `(\d+)`", replied_text)
            if match:
                target_uid = int(match.group(1))
                try:
                    access_hash = tracked_users.get(target_uid, 0)
                    peer = InputPeerUser(target_uid, access_hash) if access_hash else target_uid 
                        
                    if event.raw_text: await client.send_message(peer, event.raw_text, file=event.media)
                    elif event.media: await client.send_message(peer, file=event.media)
                        
                    admin_feedback = await event.reply("✅ **Sent!**")
                    await asyncio.sleep(2)
                    try: await admin_feedback.delete()
                    except Exception: pass
                        
                except UserIsBlockedError:
                    blocked_users.add(target_uid)
                    save_user(target_uid, tracked_users.get(target_uid, 0), blocked=True)
                    await event.reply(f"❌ `[Error] Transmission failed: User blocked the bot.`")
                except Exception as e:
                    await event.reply(f"❌ **Error:** {e}")

# ------------------ AUTO BACKUP (EVERY 6 HOURS & SEND TO ADMINS) ------------------
async def periodic_backup():
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            data = {
                "tracked": tracked_users,
                "blocked": list(blocked_users),
                "messages": {str(k): v for k, v in saved_messages.items()},
                "welcome": welcome_enabled,
                "auto_replies": auto_replies
            }
            file = "auto_backup.json"
            with open(file, "w") as f:
                json.dump(data, f)
            
            caption = f"📁 `[Automatic 6-Hour Database Backup]`\n📊 Total Active Users: `{len(tracked_users)}`"
            for admin_id in ADMIN_IDS:
                try:
                    await client.send_file(admin_id, file, caption=caption)
                except Exception as e:
                    logger.error(f"Failed to send periodic backup to admin {admin_id}: {e}")
                    
            logger.info("Automatic 6-hour backup generated and sent to admins.")
        except Exception as e:
            logger.error(f"Periodic backup error: {e}")

# ------------------ MAIN ------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    init_db()
    load_from_db()
    await start_web()
    asyncio.create_task(periodic_backup())
    logger.info("Bot is running smoothly...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try: 
        loop.run_until_complete(main())
    except KeyboardInterrupt: 
        logger.info("Bot stopped.")
    except Exception as e: 
        logger.error(f"Fatal error: {e}")
