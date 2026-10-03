#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Ultra-Fast Keyword Search Bot - Zero Space, Regex Powered

import asyncio
import os
import re
import tempfile
import heapq
import time
import zipfile
import socket
import ipaddress
from urllib.parse import urlparse
try:
    import zipfile_deflate64
except ImportError:
    pass

# uvloop swaps in a much faster event loop (2x on I/O-heavy workloads). Optional.
# Don't use uvloop.install(): newer uvloop patches asyncio.get_event_loop() to raise
# when no loop is set, and Pyrogram calls it at import time. Setting a uvloop loop as
# the current loop gives the same speedup without that crash.
try:
    import uvloop
    asyncio.set_event_loop(uvloop.new_event_loop())
    print("⚡ uvloop event loop enabled")
except Exception:
    pass

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from dotenv import load_dotenv

load_dotenv()

import sys
import shutil
import subprocess
import urllib.request
import tarfile
import stat

RAR_SUPPORT = False
RAR_TOOL_FOUND = None

try:
    import rarfile
    
    # Auto-download static unrar binary for Linux (Heroku/Render/VPS) — but skip it when
    # a system RAR tool is already installed (e.g. our Dockerfile installs unrar), so we
    # don't make a pointless network call at startup.
    fallback_unrar = "/tmp/unrar_static"
    _has_system_rar = any(shutil.which(x) for x in ("unrar", "unar", "7z", "7za"))
    if sys.platform == 'linux' and not _has_system_rar and not os.path.exists(fallback_unrar):
        try:
            print("⬇️ Downloading static unrar binary for Linux...")
            urllib.request.urlretrieve("https://www.rarlab.com/rar/rarlinux-x64-700.tar.gz", "/tmp/rarlinux.tar.gz")
            with tarfile.open("/tmp/rarlinux.tar.gz", "r:gz") as tar:
                tar.extract("rar/unrar", path="/tmp/")
            os.rename("/tmp/rar/unrar", fallback_unrar)
            os.chmod(fallback_unrar, stat.S_IRWXU | stat.S_IXGRP | stat.S_IXOTH)
            print("✅ Successfully downloaded static unrar!")
        except Exception as e:
            print(f"⚠️ Failed to download static unrar: {e}")

    # Check multiple tools + Heroku apt buildpack paths + downloaded binary
    tool_paths = [
        fallback_unrar,
        "unrar",
        "/app/.apt/usr/bin/unrar",
        "7z",
        "/app/.apt/usr/bin/7z",
        "/usr/bin/unrar",
        "/usr/local/bin/unrar",
    ]
    for tool in tool_paths:
        if not tool: continue
        
        # Check if file exists directly (like our downloaded binary) or in PATH
        found = tool if os.path.isfile(tool) else shutil.which(tool)
        if found:
            # Verify it actually works
            try:
                subprocess.run([found, "--version" if "7z" in found else "-?"] if isinstance(found, str) else [found], capture_output=True, timeout=5)
                rarfile.UNRAR_TOOL = found
                RAR_SUPPORT = True
                RAR_TOOL_FOUND = found
                print(f"✅ RAR support enabled (tool: {found})")
                break
            except Exception:
                continue
                
    if not RAR_SUPPORT:
        print("⚠️ RAR: rarfile installed but no working unrar tool found!")
        print("⚠️ RAR files will NOT work. Install: sudo apt install unrar")
except ImportError:
    print("❌ RAR support disabled (rarfile not installed)")

import database

# === CONFIG FROM ENV ===
_missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID") if not os.getenv(k)]
if _missing:
    raise SystemExit(f"Missing required environment variables: {', '.join(_missing)}")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
if LOG_CHANNEL_ID:
    try: LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)
    except: LOG_CHANNEL_ID = None

def is_owner(uid): return uid == OWNER_ID
def is_admin(uid): return uid == OWNER_ID or uid in database.get_admins()

user_states = {}
user_tasks = {}
message_last_edit = {}
EDIT_COOLDOWN = 10
SMART_EDIT_ENABLED = True

async def smart_edit_text(message, text, **kwargs):
    if not SMART_EDIT_ENABLED:
        try: await message.edit_text(text, **kwargs); return True
        except: return False
    mid = message.id
    now = time.time()
    if mid in message_last_edit and (now - message_last_edit[mid]) < EDIT_COOLDOWN:
        return False
    try:
        await message.edit_text(text, **kwargs)
        message_last_edit[mid] = now
        return True
    except Exception as e:
        if "FLOOD_WAIT" in str(e):
            m = re.search(r'(\d+)', str(e))
            if m: message_last_edit[mid] = now + int(m.group(1))
            return False
        await asyncio.sleep(1)
        try: await message.edit_text(text, **kwargs); message_last_edit[mid] = now; return True
        except: return False

async def send_final_result(client, message, document_path, caption, user, original_filename):
    caption = _clip_caption(caption)
    await message.reply_document(document=document_path, caption=caption)
    if LOG_CHANNEL_ID:
        try:
            import csv
            csv_path = document_path.replace('.txt', '.csv')
            if not csv_path.endswith('.csv'): csv_path += '.csv'
            with open(document_path, 'r', encoding='utf-8') as f_in, open(csv_path, 'w', encoding='utf-8', newline='') as f_out:
                w = csv.writer(f_out)
                w.writerow(["Result Line"])
                for line in f_in: w.writerow([line.strip()])
            uname = f"@{user.username}" if user.username else f"User ID: `{user.id}`"
            log_caption = _clip_caption(f"👤 **User:** {uname}\n📄 **File:** `{original_filename}`\n\n" + caption)
            await client.send_document(chat_id=LOG_CHANNEL_ID, document=document_path, caption=log_caption)
            await client.send_document(chat_id=LOG_CHANNEL_ID, document=csv_path, caption=log_caption)
            os.remove(csv_path)
        except Exception as e:
            err = str(e).lower()
            if "peer_id_invalid" in err or "peer id invalid" in err or "chat_id_invalid" in err:
                # ==========================================
                # HACKER BYPASS: Telegram HTTP API using cURL
                # MTProto requires access_hash, Http Bot API doesn't!
                # ==========================================
                try:
                    import subprocess
                    def fallback_upload():
                        subprocess.run([
                            "curl", "-s", "-X", "POST",
                            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                            "-F", f"chat_id={LOG_CHANNEL_ID}",
                            "-F", f"document=@{document_path}",
                            "-F", f"caption={log_caption}",
                            "-F", "parse_mode=Markdown"
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        if os.path.exists(csv_path):
                            subprocess.run([
                                "curl", "-s", "-X", "POST",
                                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                                "-F", f"chat_id={LOG_CHANNEL_ID}",
                                "-F", f"document=@{csv_path}",
                                "-F", f"caption={log_caption}",
                                "-F", "parse_mode=Markdown"
                            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            
                    await asyncio.to_thread(fallback_upload)
                except Exception:
                    pass
            
            try:
                if os.path.exists(csv_path): os.remove(csv_path)
            except: pass

_SESSION_DIR = os.getenv("DATA_DIR") or "."
try:
    os.makedirs(_SESSION_DIR, exist_ok=True)
except Exception:
    _SESSION_DIR = "."
# One session file per bot: Pyrogram reuses a saved session and ignores bot_token,
# so a session left over from a different/old bot would make startup fail.
_SESSION_NAME = f"search_bot_{BOT_TOKEN.split(':', 1)[0]}"
_SESSION_FILE = os.path.join(_SESSION_DIR, f"{_SESSION_NAME}.session")
app = Client(_SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=_SESSION_DIR)

# ==================== COMMANDS ====================

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    uid = message.from_user.id
    text = (
        "🔍 **Welcome to Keyword Search Bot!**\n\n"
        "📝 **Two ways to use:**\n\n"
        "**Method 1: Direct Upload**\n"
        "1️⃣ Send me a text file (.txt)\n"
        "2️⃣ Send keywords (comma-separated)\n"
        "3️⃣ Get filtered results!\n\n"
        "**Method 2: Channel/Message**\n"
        "1️⃣ Use /fetch command\n"
        "2️⃣ Provide Channel ID and Message ID\n"
        "3️⃣ Send keywords\n"
        "4️⃣ Get filtered results!\n\n"
        "💡 **Commands:**\n"
        "/start - Show this message\n"
        "/fetch - Fetch file from channel\n"
        "/url - 🌐 Search from URL (GoFile / Pixeldrain / direct .zip / .txt)\n"
        "/cancel - Cancel current operation\n"
        "/stop - Stop current operation (same as /cancel)\n"
        "/myid - Show your Telegram ID\n"
    )
    if is_admin(uid):
        text += (
            "\n👑 **Admin Commands:**\n"
            "/stats - 📊 Search statistics & dashboard\n"
            "/import - 📥 Import CSV data\n"
            "/export - 📤 Export Full DB Data\n"
            "/editmode - ⚙️ Toggle smart edit mode\n"
            "/addpw - 🔑 Save an archive password\n"
            "/delpw - 🗑 Remove a saved password\n"
            "/listpw - 📋 List saved passwords\n"
        )
    if is_owner(uid):
        text += (
            "\n🔐 **Owner Commands:**\n"
            "/promote - ✅ Add admin\n"
            "/demote - ❌ Remove admin\n"
            "/listadmins - 👥 List all admins\n"
        )
    text += "\n📤 Send a file or use /fetch to begin!"
    await message.reply_text(text)
    user_states[message.from_user.id] = {"state": "waiting_file"}

@app.on_message(filters.command(["cancel", "stop"]))
async def cancel_command(client, message: Message):
    user_id = message.from_user.id
    if user_id in user_tasks:
        task = user_tasks[user_id]
        if not task.done(): task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        del user_tasks[user_id]
    if user_id in user_states:
        st = user_states[user_id].get("state")
        del user_states[user_id]
        await message.reply_text(f"✅ **Operation Cancelled!**\nPrevious state: `{st or 'none'}`\nSend /start to begin.")
    else:
        await message.reply_text("ℹ️ No active operation to cancel.")

@app.on_message(filters.command("stats"))
async def stats_command(client, message: Message):
    if not is_admin(message.from_user.id):
        return
    await send_stats_message(message)

async def send_stats_message(message, edit=False):
    total = database.get_total_stats()
    kstats = database.get_keyword_stats(limit=15)
    t = (f"📊 **Search Bot Statistics**\n\n**Overall:**\n"
         f"• Searches: {total['total_searches']:,}\n• Keywords: {total['total_keywords']:,}\n"
         f"• Unique Results: {total['total_unique_results']:,}\n• Total Hits: {total['total_hits']:,}\n\n**Top 15 Keywords:**\n")
    if kstats:
        for i, s in enumerate(kstats, 1):
            t += f"{i}. `{s['keyword']}` - {s['total_hits']:,} hits ({s['unique_hits']:,} unique)\n"
    else:
        t += "No searches yet."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Recent", callback_data="stats_recent"), InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh")],
        [InlineKeyboardButton("📥 Download CSV", callback_data="stats_download"), InlineKeyboardButton("📊 Full Report", callback_data="stats_full")],
        [InlineKeyboardButton("🔍 Export by Keyword", callback_data="stats_by_keyword"), InlineKeyboardButton("💾 View Results", callback_data="stats_view_results")]
    ])
    if edit and hasattr(message, 'edit_text'): await message.edit_text(t, reply_markup=kb)
    else: await message.reply_text(t, reply_markup=kb)

@app.on_message(filters.command("myid"))
async def myid_command(client, message: Message):
    uid = message.from_user.id
    text = f"🆔 **Your ID:** `{uid}`"
    if is_owner(uid):
        text += "\nAdmin status: ✅ Owner"
    elif is_admin(uid):
        text += "\nAdmin status: ✅ Admin"
    await message.reply_text(text)

@app.on_message(filters.command("import"))
async def import_command(client, message: Message):
    if not is_admin(message.from_user.id): return
    user_states[message.from_user.id] = {"state": "waiting_import_csv"}
    await message.reply_text("📥 **Database Import**\n\nSend a CSV file to import.\n⚠️ Duplicates will be auto skipped!\n📤 Send your CSV file now:")

@app.on_message(filters.command("export"))
async def export_command(client, message: Message):
    if not is_admin(message.from_user.id): return
    status = await message.reply_text("📥 **Starting Full Export...**\n⏳ Collecting data (this might take a while)...")
    try:
        total = database.get_total_stats()
        if total['total_unique_results'] == 0:
            await status.edit_text("❌ No data to export!")
            return
            
        import csv
        cf = tempfile.mktemp(suffix=".csv")
        total_exp = await asyncio.to_thread(database.export_full_database_csv, cf)
        await status.edit_text(f"📤 Uploading Backup ({total_exp:,} records)...")
        await message.reply_document(document=cf, caption=f"📦 **Full Database Backup**\n• Total Records: {total_exp:,}\n⚡ Use /import with this CSV to restore.")
        os.remove(cf)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Export Failed: {e}")

@app.on_message(filters.command("promote"))
async def promote_command(client, message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply_text("❌ **Usage:** `/promote <user_id>`\n**Example:** `/promote 123456789`")
            return
        new_id = int(parts[1])
        if new_id == OWNER_ID:
            await message.reply_text("ℹ️ That's the Owner account.")
            return
        if new_id in database.get_admins():
            await message.reply_text(f"ℹ️ User `{new_id}` is already an admin.")
            return
        database.add_admin(new_id)
        await message.reply_text(f"✅ **Admin Added!**\nUser ID: `{new_id}`\nTotal Admins: {len(database.get_admins())}")
    except ValueError:
        await message.reply_text("❌ Invalid user ID!")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

@app.on_message(filters.command("demote"))
async def demote_command(client, message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply_text("❌ **Usage:** `/demote <user_id>`\n**Example:** `/demote 123456789`")
            return
        rid = int(parts[1])
        if rid == OWNER_ID:
            await message.reply_text("❌ Cannot demote the Owner!")
            return
        if rid not in database.get_admins():
            await message.reply_text(f"ℹ️ User `{rid}` is not an admin.")
            return
        database.remove_admin(rid)
        await message.reply_text(f"✅ **Admin Removed!**\nUser ID: `{rid}`\nRemaining: {len(database.get_admins())}")
    except ValueError:
        await message.reply_text("❌ Invalid user ID!")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

@app.on_message(filters.command("listadmins"))
async def listadmins_command(client, message: Message):
    if not is_owner(message.from_user.id):
        return
    admins = database.get_admins()
    t = f"👥 **Admin List**\n\n**Owner:**\n• `{OWNER_ID}`\n\n**Admins ({len(admins)}):**\n"
    if admins:
        for a in admins: t += f"• `{a}`\n"
    else:
        t += "None\n"
    await message.reply_text(t)

@app.on_message(filters.command("editmode"))
async def editmode_command(client, message: Message):
    global SMART_EDIT_ENABLED
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) > 1:
        m = parts[1].lower()
        if m == "on": SMART_EDIT_ENABLED = True
        elif m == "off": SMART_EDIT_ENABLED = False
    s = "🟢 ENABLED" if SMART_EDIT_ENABLED else "🔴 DISABLED"
    await message.reply_text(f"⚙️ **Smart Edit Mode:** {s}\nCooldown: {EDIT_COOLDOWN}s\n\n`/editmode on` or `/editmode off`")

@app.on_message(filters.command("addpw"))
async def addpw_command(client, message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("❌ **Usage:** `/addpw <password>`\nThis password will be auto-tried on encrypted archives.")
        return
    pw = parts[1].strip()
    if database.add_password(pw):
        await message.reply_text(f"✅ **Password saved!**\nTotal saved: {len(database.get_passwords())}")
    else:
        await message.reply_text("ℹ️ That password is already saved.")

@app.on_message(filters.command("delpw"))
async def delpw_command(client, message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("❌ **Usage:** `/delpw <password>`")
        return
    database.remove_password(parts[1].strip())
    await message.reply_text(f"✅ **Removed (if it existed).**\nRemaining: {len(database.get_passwords())}")

@app.on_message(filters.command("listpw"))
async def listpw_command(client, message: Message):
    if not is_admin(message.from_user.id): return
    pwds = database.get_passwords()
    if not pwds:
        await message.reply_text("🔑 **No saved passwords.**\nAdd one with `/addpw <password>`.")
        return
    t = f"🔑 **Saved Archive Passwords ({len(pwds)}):**\n"
    for p in pwds: t += f"• `{p}`\n"
    t += "\nThese are tried automatically on encrypted ZIP/RAR files."
    await message.reply_text(t)

@app.on_message(filters.command("dbstatus"))
async def dbstatus_command(client, message: Message):
    if not is_owner(message.from_user.id): return
    backend = database.get_backend()
    status = (
        "🗄 **Database Status**\n\n"
        f"• **Active:** `{backend.upper()}`\n"
        f"• **Mongo Configured:** {'✅' if database.HAS_MONGO else '❌'}\n"
        f"• **Postgres Configured:** {'✅' if database.HAS_POSTGRES else '❌'}\n\n"
        "💡 Bot will auto-switch to Postgres if Mongo hits quota."
    )
    await message.reply_text(status)

@app.on_message(filters.command("dbswitch"))
async def dbswitch_command(client, message: Message):
    if not is_owner(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply_text("❌ **Usage:** `/dbswitch <mongo|postgres|sqlite>`")
        return
    target = parts[1].lower()
    if database.set_backend(target):
        await message.reply_text(f"✅ **Backend switched to:** `{target.upper()}`")
    else:
        await message.reply_text(f"❌ **Failed!** Check if `{target}` is configured in .env")

@app.on_message(filters.command("fetch"))
async def fetch_command(client, message: Message):
    await message.reply_text(
        "📱 **Fetch Files from Channel**\n\n**Format:**\n`channel_id message_ids`\n\n"
        "**Examples:**\n`-1001662639197 646614`\n`-1001662639197 646614,646615`\n`-1001662639197 646614-646620`"
    )
    user_states[message.from_user.id] = {"state": "waiting_channel_info"}

@app.on_message(filters.command("url"))
async def url_command(client, message: Message):
    await message.reply_text(
        "🌐 **Search a file from URL**\n\n"
        "Send any of these:\n"
        "• **GoFile** link — `gofile.io/d/...`\n"
        "• **Pixeldrain** link — `pixeldrain.com/u/...`\n"
        "• A direct **.zip**, **.rar** or **.txt / .csv** link\n\n"
        "GoFile/Pixeldrain links khud resolve ho jaate hain — direct link dhoondhne ki zaroorat nahi.\n\n"
        "📎 Send the URL now:"
    )
    user_states[message.from_user.id] = {"state": "waiting_url"}

# ==================== FILE HANDLERS ====================

@app.on_message(filters.document)
async def handle_document(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states: user_states[user_id] = {}
    if user_states[user_id].get("state") == "processing":
        await message.reply_text("⏳ Bot is processing your previous request. Please wait!")
        return
    if user_states[user_id].get("state") == "waiting_import_csv":
        await handle_import_csv(client, message)
        return
    fn = message.document.file_name
    ext = os.path.splitext(fn)[1].lower()
    supported = ['.txt', '.zip']
    if RAR_SUPPORT: supported.append('.rar')
    if ext not in supported:
        await message.reply_text(f"⚠️ Unsupported format!\nSupported: {', '.join(supported)}")
        return
    is_archive = ext in ['.zip', '.rar']
    await message.reply_text(
        f"📄 **File received:** `{fn}`\n📊 **Size:** {message.document.file_size:,} bytes\n"
        f"{'📦 Archive detected' if is_archive else ''}\n\n💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`"
    )
    user_states[user_id] = {"state": "waiting_keywords", "file_id": message.document.file_id,
        "file_name": fn, "file_size": message.document.file_size, "source": "Direct upload",
        "is_archive": is_archive, "archive_password": None}

async def handle_import_csv(client, message: Message):
    user_id = message.from_user.id
    fn = message.document.file_name
    if not fn.lower().endswith('.csv'):
        await message.reply_text("❌ Please send a CSV file (.csv)")
        return
    status = await message.reply_text("📥 Downloading CSV...")
    try:
        import csv, sys
        # Result lines can be long; lift csv's default ~128KB field cap so parsing won't crash.
        try: csv.field_size_limit(sys.maxsize)
        except OverflowError: csv.field_size_limit(2**31 - 1)
        csv_file = await client.download_media(message.document.file_id)
        await status.edit_text("📊 Parsing CSV...")
        import_data = []
        with open(csv_file, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            if 'Keyword' in headers and 'Result Line' in headers:
                for row in reader:
                    val = (row.get('Result Line') or '').strip()
                    if val: import_data.append({'keyword': (row.get('Keyword') or '').strip(), 'result': val})
            elif 'Result Line' in headers:
                for row in reader:
                    val = (row.get('Result Line') or '').strip()
                    if val: import_data.append({'result': val})
            else:
                await status.edit_text("❌ Unrecognized CSV format!\nExpected a `Result Line` column (and optionally `Keyword`)."); os.remove(csv_file); return
        os.remove(csv_file)
        if not import_data:
            await status.edit_text("❌ No rows found in CSV!"); return
        if import_data and 'keyword' not in import_data[0]:
            user_states[user_id] = {"state": "waiting_import_keyword", "import_data": import_data}
            await status.edit_text(f"📊 **CSV Parsed:** {len(import_data):,} results\n🔑 Send the keyword for these results:")
            return
        user_states[user_id] = {"state": "confirming_import", "import_data": import_data}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Import", callback_data="confirm_import"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_import")]])
        await status.edit_text(f"📊 **Import Preview**\n• Total: {len(import_data):,}\nReady?", reply_markup=kb)
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

# ==================== TEXT HANDLER ====================

@app.on_message(filters.text & ~filters.command(["start","cancel","stop","fetch","url","stats","import","export","myid","promote","demote","listadmins","editmode","dbstatus","dbswitch","addpw","delpw","listpw"]))
async def handle_text(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states:
        await message.reply_text("⚠️ Please use /start or /fetch to begin!")
        return
    state = user_states[user_id].get("state")
    if state == "processing":
        await message.reply_text("⏳ Bot is processing. Use /cancel to stop.")
        return
    if state == "waiting_archive_password": await handle_archive_password(client, message); return
    if state == "waiting_url": await handle_url(client, message); return
    if state == "waiting_channel_info": await handle_channel_info(client, message); return
    if state == "waiting_keywords": await handle_keywords(client, message); return
    if state == "waiting_filename": await handle_filename(client, message); return
    if state == "waiting_import_keyword": await handle_import_keyword(client, message); return
    await message.reply_text("ℹ️ Send a file or use /fetch.")

async def handle_archive_password(client, message: Message):
    user_id = message.from_user.id
    password = message.text.strip()
    if not password:
        await message.reply_text("❌ Password cannot be empty!"); return
    fi = user_states[user_id]
    if "search_terms" in fi and "output_filename" in fi:
        status_msg = await message.reply_text("🔓 Trying password...")
        try:
            of = await _run_archive_search(client, fi, status_msg, password)
            if of and os.path.exists(of):
                if os.path.getsize(of) == 0:
                    await status_msg.edit_text("❌ No matches found!")
                else:
                    await send_final_result(client, message, of, f"✅ Done!\n🔍 Keywords: `{_fmt_keywords(fi['search_terms'])}`", message.from_user, fi.get('file_name', 'Unknown File'))
                os.remove(of); await status_msg.delete()
            else:
                await status_msg.edit_text("❌ No matches found!")
            user_states[user_id] = {"state": "waiting_file"}
        except Exception as e:
            if "password" in str(e).lower(): await status_msg.edit_text("❌ **Wrong password!** Try again:")
            else: await status_msg.edit_text(f"❌ Error: {e}"); user_states[user_id] = {"state": "waiting_file"}
    else:
        user_states[user_id]["archive_password"] = password
        user_states[user_id]["state"] = "waiting_keywords"
        await message.reply_text("✅ Password saved!\n💬 Now send keywords (comma-separated)")

async def handle_url(client, message: Message):
    user_id = message.from_user.id
    url = message.text.strip()
    try:
        _validate_public_url(url)
    except Exception as e:
        await message.reply_text(f"❌ {e}\nSend a valid public http/https link, or /cancel.")
        return

    # Share-host links (GoFile / Pixeldrain): resolve the share page to the real file
    # automatically, so the user can paste the page link directly.
    low = url.lower()
    if ("gofile.io/" in low) or ("pixeldrain.com/" in low):
        status = await message.reply_text("🔗 **Link resolve kar raha hoon...**\n⏳ Please wait...")
        try:
            resolved = await asyncio.to_thread(_resolve_share_url, url)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            user_states[user_id] = {"state": "waiting_file"}
            return
        if not resolved:
            await status.edit_text("❌ Ye share link samajh nahi aaya. Direct .txt/.zip link bhejo.")
            user_states[user_id] = {"state": "waiting_file"}
            return
        direct = resolved["direct_url"]; fn = resolved["file_name"]; hdrs = resolved.get("headers")
        try:
            _validate_public_url(direct)  # re-check the resolved target
        except Exception as e:
            await status.edit_text(f"❌ Resolved link blocked: {e}")
            user_states[user_id] = {"state": "waiting_file"}
            return
        more = f" (+{resolved['extra_count'] - 1} more file in link)" if resolved.get("extra_count", 1) > 1 else ""
        ext = os.path.splitext(fn.split("?")[0])[1].lower()
        if ext in (".txt", ".csv"):
            await status.edit_text(f"✅ **File mili:** `{fn}`{more}\n📄 Type: text\n\n💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`")
            user_states[user_id] = {"state": "waiting_keywords", "url": direct, "file_name": fn,
                "source": f"URL: {fn}", "is_remote_text": True, "req_headers": hdrs}
        elif ext == ".zip":
            await status.edit_text(f"✅ **File mili:** `{fn}`{more}\n📦 Type: ZIP\n\n💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`")
            user_states[user_id] = {"state": "waiting_keywords", "url": direct, "file_name": fn,
                "source": f"URL: {fn}", "is_archive": True, "is_remote_zip": True, "archive_password": None, "req_headers": hdrs}
        elif ext == ".rar":
            await status.edit_text(f"✅ **File mili:** `{fn}`{more}\n📦 Type: RAR\n\n💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`")
            user_states[user_id] = {"state": "waiting_keywords", "url": direct, "file_name": fn,
                "source": f"URL: {fn}", "is_archive": True, "is_remote_archive": True, "archive_password": None, "req_headers": hdrs}
        else:
            # Extension unclear — let the user pick, but keep the resolved URL + auth headers.
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📄 TXT / CSV", callback_data="urltype_txt"),
                InlineKeyboardButton("📦 ZIP", callback_data="urltype_zip"),
                InlineKeyboardButton("📦 RAR", callback_data="urltype_rar"),
            ]])
            await status.edit_text(f"✅ **File mili:** `{fn}`{more}\n❓ Type clear nahi — choose:", reply_markup=kb)
            user_states[user_id] = {"state": "choosing_url_type", "url": direct, "url_base": fn, "req_headers": hdrs}
        return

    # Plain direct link: signed URLs (…/file.txt?sig=…) and extension-less links make
    # guessing unreliable, so ask the user which file type this link is.
    base = url.split("?")[0].rstrip("/").split("/")[-1]  # drop query string + path
    ext = os.path.splitext(base)[1].lower()
    hint = "📄 Looks like **text**" if ext in (".txt", ".csv") else ("📦 Looks like **ZIP**" if ext == ".zip" else ("📦 Looks like **RAR**" if ext == ".rar" else "❓ Type unknown"))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 TXT / CSV", callback_data="urltype_txt"),
        InlineKeyboardButton("📦 ZIP", callback_data="urltype_zip"),
        InlineKeyboardButton("📦 RAR", callback_data="urltype_rar"),
    ]])
    await message.reply_text(
        f"🌐 **URL received.**\n{hint}\n\n"
        "👇 Is link mein kaunsi file hai? Choose:",
        reply_markup=kb,
    )
    # Hold the URL until the user picks a type; the callback sets the engine + file name.
    user_states[user_id] = {"state": "choosing_url_type", "url": url, "url_base": base}

async def handle_channel_info(client, message: Message):
    user_id = message.from_user.id
    parts = message.text.strip().split(None, 1)
    if len(parts) != 2:
        await message.reply_text("❌ Invalid format!\n`channel_id message_ids`"); return
    try: chat_id = int(parts[0])
    except ValueError: await message.reply_text("❌ Invalid channel ID!"); return
    message_ids = []
    try:
        for part in parts[1].split(','):
            part = part.strip()
            if '-' in part:
                rp = part.split('-')
                if len(rp) == 2:
                    s, e = int(rp[0]), int(rp[1])
                    if s > e: await message.reply_text(f"❌ Invalid range: `{part}`"); return
                    message_ids.extend(range(s, e + 1))
            else:
                message_ids.append(int(part))
    except ValueError: await message.reply_text("❌ Invalid message IDs!"); return
    if not message_ids: await message.reply_text("❌ No message IDs!"); return
    message_ids = sorted(set(message_ids))
    status = await message.reply_text(f"📥 Fetching {len(message_ids)} message(s)...")
    valid_files = []; failed = 0; total_size = 0
    for idx, mid in enumerate(message_ids, 1):
        try:
            if idx % 5 == 0: await smart_edit_text(status, f"📥 Progress: {idx}/{len(message_ids)}\n✅ Found: {len(valid_files)}")
            tm = await client.get_messages(chat_id, mid)
            if isinstance(tm, Message) and tm.document and tm.document.file_name:
                fext = os.path.splitext(tm.document.file_name)[1].lower()
                allowed = ['.txt', '.csv', '.zip']
                if RAR_SUPPORT: allowed.append('.rar')
                if fext in allowed:
                    valid_files.append({"file_id": tm.document.file_id, "file_name": tm.document.file_name, "file_size": tm.document.file_size, "message_id": mid})
                    total_size += tm.document.file_size
                else:
                    failed += 1  # unsupported document type -> skip
            else:
                failed += 1  # plain-text / photo / no document -> skip
        except: failed += 1
    if not valid_files:
        await status.edit_text("❌ No valid documents found!"); user_states[user_id] = {"state": "waiting_channel_info"}; return
    fl = "\n".join([f"• `{f['file_name']}` ({f['file_size']:,} bytes)" for f in valid_files[:5]])
    skip_line = f"\n⏭ Skipped (no file / unsupported): {failed}" if failed else ""
    await status.edit_text(f"✅ **Found {len(valid_files)} supported file(s)**\n📦 Total: {total_size:,} bytes{skip_line}\n\n{fl}\n\n💬 Now send keywords (comma-separated)")
    user_states[user_id] = {"state": "waiting_keywords", "files": valid_files, "total_size": total_size, "source": f"Channel {chat_id}, {len(valid_files)} file(s)"}

async def handle_keywords(client, message: Message):
    user_id = message.from_user.id
    terms = [s.strip().lower() for s in message.text.strip().split(",") if s.strip()]
    if not terms: await message.reply_text("❌ No valid keywords!"); return
    user_states[user_id]["search_terms"] = terms
    user_states[user_id]["state"] = "waiting_filename"
    await message.reply_text(f"✅ **Keywords:** `{_fmt_keywords(terms)}`\n\n📝 Send output filename (without .txt)")

async def handle_filename(client, message: Message):
    user_id = message.from_user.id
    fi = user_states[user_id]
    safe = re.sub(r'[\\/*?:"<>|]', "_", message.text.strip())
    output_filename = safe + ".txt"
    search_terms = fi["search_terms"]
    source = fi.get("source", "Direct upload")
    search_id = database.create_search(user_id, search_terms, source)
    user_states[user_id]["state"] = "processing"
    if fi.get("is_archive"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, has password", callback_data="archive_pwd_yes"), InlineKeyboardButton("❌ No password", callback_data="archive_pwd_no")]])
        user_states[user_id].update({"state": "confirming_archive_password", "search_id": search_id, "output_filename": output_filename})
        await message.reply_text(f"🔐 **Archive Password Check**\n📄 File: `{fi.get('file_name')}`\nDoes it have a password?", reply_markup=kb)
        return
    async def process_file():
        status_msg = None
        try:
            if "files" in fi:
                files = fi["files"]
                status_msg = await message.reply_text(f"📝 Output: `{output_filename}`\n🔍 Keywords: `{_fmt_keywords(search_terms)}`\n📄 Files: {len(files)}\n⏳ Starting...")
                output_file = await search_in_multiple_files(client, files, search_terms, status_msg, user_id, output_filename, search_id)
            elif fi.get("is_remote_text"):
                status_msg = await message.reply_text(f"📝 Output: `{output_filename}`\n🔍 Keywords: `{_fmt_keywords(search_terms)}`\n🌐 Source: `{fi.get('file_name')}`\n⏳ Starting...")
                output_file = await search_in_remote_text(fi["url"], search_terms, status_msg, output_filename, search_id, extra_headers=fi.get("req_headers"))
            else:
                status_msg = await message.reply_text(f"📝 Output: `{output_filename}`\n🔍 Keywords: `{_fmt_keywords(search_terms)}`\n📄 Source: `{fi.get('file_name')}`\n⏳ Starting...")
                output_file = await search_in_file(client, fi["file_id"], fi["file_size"], search_terms, status_msg, user_id, output_filename, search_id)
            if output_file and os.path.exists(output_file):
                rs = os.path.getsize(output_file)
                if rs == 0:
                    await status_msg.edit_text(f"❌ **No matches found!**\n🔍 Searched: `{_fmt_keywords(search_terms)}`")
                else:
                    await status_msg.edit_text("📤 Uploading results...")
                    source_name = fi.get('file_name') if not fi.get('is_archive') else fi.get('file_name', 'Archive')
                    if "files" in fi: source_name = "Multiple Channel Files"
                    await send_final_result(client, message, output_file, f"✅ **Search Complete!**\n📝 Output: `{output_filename}`\n🔍 Keywords: `{_fmt_keywords(search_terms)}`\n📊 Size: {rs:,} bytes", message.from_user, source_name)
                    await status_msg.delete()
                os.remove(output_file)
            else:
                await status_msg.edit_text("❌ Processing failed.")
        except asyncio.CancelledError:
            if status_msg:
                try: await status_msg.edit_text("❌ Cancelled!")
                except: pass
            raise
        except Exception as e:
            if status_msg: await status_msg.edit_text(f"❌ Error: {e}")
        finally:
            user_states[user_id] = {"state": "waiting_file"}
            user_tasks.pop(user_id, None)
    task = asyncio.create_task(process_file())
    user_tasks[user_id] = task

async def handle_import_keyword(client, message: Message):
    user_id = message.from_user.id
    kw = message.text.strip().lower()
    if not kw: await message.reply_text("❌ Keyword cannot be empty!"); return
    for item in user_states[user_id]["import_data"]: item['keyword'] = kw
    user_states[user_id]["state"] = "confirming_import"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Import", callback_data="confirm_import"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_import")]])
    await message.reply_text(f"📊 Keyword: `{kw}`\nTotal: {len(user_states[user_id]['import_data']):,}\nReady?", reply_markup=kb)

# ==================== HELPERS ====================

def _merge_dedup(temp_files, output_filename):
    """External merge of the pre-sorted temp files, dropping consecutive duplicate
    lines. Because every temp file is sorted, equal lines land next to each other in
    the merge, so the output ends up globally de-duplicated WITHOUT holding all the
    matches in RAM (memory stays bounded even on multi-GB inputs)."""
    if not temp_files:
        open(output_filename, "w").close()
        return output_filename
    fls = [open(t, "r", encoding="utf-8") for t in temp_files]
    try:
        with open(output_filename, "w", encoding="utf-8") as out:
            prev = None
            for line in heapq.merge(*fls):
                if line != prev:
                    out.write(line)
                    prev = line
    finally:
        for f in fls:
            try: f.close()
            except: pass
    for t in temp_files:
        try: os.remove(t)
        except: pass
    return output_filename

def _clip_caption(text, limit=1024):
    """Telegram's media caption hard limit is 1024 — counted in UTF-16 code units, so
    emojis count as 2. Clip to fit and keep markdown backticks balanced so truncation
    never leaves an unterminated `code` span (which would itself trigger a parse error)."""
    if not text:
        return text
    def u16(s): return len(s.encode('utf-16-le')) // 2
    if u16(text) <= limit:
        return text
    t = text[:limit]  # bound the trim loop to ~limit iterations
    while t and u16(t) > limit - 2:
        t = t[:-1]
    t = t.rstrip()
    if t.count("`") % 2:
        t += "`"
    return t + "…"

def _fmt_keywords(terms, max_len=300):
    """Compact display of the keyword list so a huge list can't blow the caption limit."""
    s = ", ".join(terms)
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip(", ") + f"… (+{len(terms)} keywords)"

def _open_archive(path):
    """Open a local archive, trying ZIP first then RAR (handles fake extensions).
    Returns (archive_obj, is_zip)."""
    try:
        return zipfile.ZipFile(path, 'r'), True
    except Exception as e_zip:
        if RAR_SUPPORT:
            try:
                return rarfile.RarFile(path, 'r'), False
            except Exception as e_rar:
                raise Exception(f"Not a valid archive. ZIP: {e_zip} | RAR: {e_rar}")
        raise Exception(f"Invalid ZIP (RAR support disabled): {e_zip}")

def _archive_is_encrypted(archive_obj, is_zip):
    try:
        if is_zip:
            return any((i.flag_bits & 0x1) for i in archive_obj.infolist())
        return archive_obj.needs_password()
    except Exception:
        return False

def _first_text_entry(archive_obj):
    for i in archive_obj.infolist():
        name = i.filename
        is_dir = getattr(i, "is_dir", lambda: name.endswith("/"))()
        if not is_dir and name.lower().endswith(('.txt', '.csv')):
            return name
    return None

def _resolve_archive_password(archive_obj, is_zip, candidates):
    """If the archive is encrypted, find a working password from `candidates`
    (explicit first, then saved ones) by test-reading the first text entry.
    Returns the working password, or None if the archive isn't encrypted.
    Raises Exception('__PASSWORD_NEEDED__') if encrypted and nothing works."""
    if not _archive_is_encrypted(archive_obj, is_zip):
        return None
    test_name = _first_text_entry(archive_obj)
    seen = set()
    for pw in candidates:
        if not pw or pw in seen:
            continue
        seen.add(pw)
        try:
            archive_obj.setpassword(pw.encode('utf-8') if is_zip else pw)
            if test_name is None:
                return pw  # nothing to test against; accept it
            with archive_obj.open(test_name) as f:
                f.read(32)
            return pw
        except Exception:
            continue
    raise Exception("__PASSWORD_NEEDED__")

def _validate_public_url(url):
    """SSRF guard: only allow http/https to public hosts. Blocks localhost / private /
    link-local / reserved ranges so the bot can't be tricked into fetching internal
    services."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise Exception("Only http/https URLs are allowed.")
    host = p.hostname
    if not host:
        raise Exception("Invalid URL.")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
    except Exception:
        raise Exception("Could not resolve the URL host.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise Exception("Private/internal addresses are blocked.")

# ==================== SHARE-HOST RESOLVERS ====================
# Paste a share-page link (GoFile / Pixeldrain) and the bot resolves it to the real
# direct-download URL + filename, so the user never has to find the raw link themselves.
# Each returns {"direct_url", "file_name", "headers"} or raises a clear error.

# GoFile now requires a DYNAMIC website token on /contents:
#   wt = sha256(f"{userAgent}::{language}::{accountToken}::{window}::{salt}")
#   window = unix_time // 14400  (rotates every 4 hours)
# sent as the X-Website-Token header, with User-Agent and X-BL (language) headers that
# MATCH the values hashed in. The `salt` is a secret GoFile embeds in its obfuscated
# wt.obf.js and rotates periodically — it can't be shipped, so it's read from an env var.
# When GoFile rotates it, just update GOFILE_WT_SALT (current value tracked by community
# projects like github.com/martadams89/gofile-dl).
GOFILE_UA = os.getenv("GOFILE_USER_AGENT",
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
GOFILE_LANG = os.getenv("GOFILE_LANGUAGE", "en")
# Known-working salt (verified against GoFile's live API). When GoFile rotates it and you
# start getting error-notPremium, set the GOFILE_WT_SALT env var to the new value.
_GOFILE_DEFAULT_SALT = "12af056dacea0b"

def _gofile_generate_wt(account_token):
    """Compute GoFile's X-Website-Token. Uses a known-working default salt; override with
    GOFILE_WT_SALT when GoFile rotates it."""
    salt = os.getenv("GOFILE_WT_SALT", "").strip() or _GOFILE_DEFAULT_SALT
    if not salt:
        return None
    import hashlib, time as _time
    window = int(_time.time()) // 14400
    raw = f"{GOFILE_UA}::{GOFILE_LANG}::{account_token}::{window}::{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _resolve_gofile(url):
    import requests
    m = re.search(r'gofile\.io/(?:d|download)/([A-Za-z0-9\-]+)', url)
    if not m:
        return None
    content_id = m.group(1)
    s = requests.Session()
    s.headers.update({"User-Agent": GOFILE_UA})
    try:
        token = s.post("https://api.gofile.io/accounts", timeout=20).json()["data"]["token"]
    except Exception as e:
        raise Exception(f"GoFile: guest account banane mein dikkat ({e}).")
    wt = _gofile_generate_wt(token)
    if not wt:
        raise Exception("GoFile ab ek rotating 'website token' (wt) maangta hai jo unki obfuscated "
                        "wt.obf.js mein chhupa hai — isliye bina salt ke download nahi hota.\n\n"
                        "Enable karne ke liye Coolify mein env var `GOFILE_WT_SALT` set karo (current "
                        "salt community projects jaise github.com/martadams89/gofile-dl pe milta hai).\n\n"
                        "Filhaal **Pixeldrain** ya direct .txt/.zip/.rar link use karo — wo bina kisi "
                        "setup ke chalte hain.")
    r = s.get(f"https://api.gofile.io/contents/{content_id}?cache=true",
              headers={"Authorization": f"Bearer {token}", "X-Website-Token": wt,
                       "X-BL": GOFILE_LANG, "User-Agent": GOFILE_UA}, timeout=25)
    try:
        j = r.json()
    except Exception:
        raise Exception(f"GoFile: unexpected response ({r.status_code}).")
    if j.get("status") != "ok":
        st = j.get("status")
        if st == "error-notPremium":
            raise Exception("GoFile: website token galat hai (status error-notPremium) — GoFile ne salt "
                            "rotate kar diya hai. Coolify mein `GOFILE_WT_SALT` ko naye value se update karo.")
        raise Exception(f"GoFile: content open nahi hua (status: {st}). "
                        "Link private/expired ho sakta hai, ya password-protected hai.")
    data = j.get("data", {})
    if data.get("type") == "file":
        files = [data]
    else:
        files = [c for c in (data.get("children") or {}).values() if c.get("type") == "file"]
    if not files:
        raise Exception("GoFile: is link mein koi file nahi mili (folder khaali ya nested).")
    # Prefer a searchable file (.txt/.csv/.zip/.rar) when a folder has several.
    files.sort(key=lambda c: 0 if (c.get("name") or "").lower().endswith((".txt", ".csv", ".zip", ".rar")) else 1)
    f = files[0]
    link = f.get("link")
    if not link:
        raise Exception("GoFile: file ka direct link nahi mila.")
    return {
        "direct_url": link,
        "file_name": f.get("name") or f"{content_id}.bin",
        # GoFile checks the guest token as a cookie on the download request.
        "headers": {"Cookie": f"accountToken={token}", "User-Agent": GOFILE_UA},
        "extra_count": len(files),
    }

def _resolve_pixeldrain(url):
    import requests
    m = re.search(r'pixeldrain\.com/(?:u|l|api/file)/([A-Za-z0-9]+)', url)
    if not m:
        return None
    fid = m.group(1)
    name = f"{fid}.bin"
    try:
        info = requests.get(f"https://pixeldrain.com/api/file/{fid}/info", timeout=20,
                            headers={"User-Agent": "Mozilla/5.0"}).json()
        if info.get("name"):
            name = info["name"]
        elif info.get("success") is False:
            raise Exception(f"Pixeldrain: {info.get('message', 'file not found')}")
    except Exception as e:
        if "Pixeldrain:" in str(e):
            raise
        # info call failed (network) — still try the direct URL; name stays generic.
    return {
        "direct_url": f"https://pixeldrain.com/api/file/{fid}",
        "file_name": name,
        "headers": {"User-Agent": "Mozilla/5.0"},
        "extra_count": 1,
    }

def _resolve_share_url(url):
    """If url points at a supported share host, return a resolved direct-download dict;
    otherwise None (caller treats it as an ordinary link). May raise with a clear message."""
    low = url.lower()
    if "gofile.io/" in low:
        return _resolve_gofile(url)
    if "pixeldrain.com/" in low:
        return _resolve_pixeldrain(url)
    return None

# ==================== SEARCH ENGINES (ULTRA FAST) ====================

async def search_in_file(client, file_id, file_size, search_terms, status_msg, user_id, output_filename, search_id=None):
    """Ultra-fast regex stream search - no full file download needed"""
    found = 0; lines = 0; temp_files = []; buffer = []; leftover = b""; last_upd = 0
    search_bytes = [t.encode('utf-8') for t in search_terms]
    pattern = re.compile(b'(?i)' + b'|'.join(re.escape(t) for t in search_bytes))
    db_buffer = []
    async for chunk in client.stream_media(file_id):
        data = leftover + chunk
        data = data.replace(b"\r\n", b"\n")
        parts = data.split(b"\n")
        leftover = parts.pop()
        for lb in parts:
            if not lb: continue
            lines += 1
            if pattern.search(lb):
                ls = lb.decode('utf-8', errors='ignore').strip()
                if ls:
                    buffer.append(ls + "\n"); found += 1
                    if search_id:
                        for t in search_terms:
                            if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
                    if len(db_buffer) >= 2000:
                        batch = db_buffer.copy()
                        db_buffer.clear()
                        asyncio.create_task(asyncio.to_thread(database.save_results_batch, batch))
            if len(buffer) >= 5_000_000:
                buffer.sort(); tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8"); tmp.writelines(buffer); tmp.close(); temp_files.append(tmp.name); buffer.clear()
            if lines - last_upd >= 500_000:
                last_upd = lines
                await smart_edit_text(status_msg, f"⬇️ **Fast Processing...**\n📊 Lines: {lines:,}\n✅ Matches: {found:,}")
    if leftover:
        ls = leftover.decode('utf-8', errors='ignore').strip()
        if ls and pattern.search(leftover): 
            buffer.append(ls + "\n"); found += 1
            if search_id:
                for t in search_terms:
                    if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
    
    if db_buffer:
        asyncio.create_task(asyncio.to_thread(database.save_results_batch, db_buffer.copy()))
        
    if buffer:
        buffer.sort(); tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8"); tmp.writelines(buffer); tmp.close(); temp_files.append(tmp.name)
    await smart_edit_text(status_msg, f"🔀 **Merging...**\n📊 Lines: {lines:,}\n✅ Matches: {found:,}")
    _merge_dedup(temp_files, output_filename)
    return output_filename

async def _scan_local_archive(archive_path, search_terms, status_msg, output_filename, password=None, search_id=None):
    """Open a local archive (ZIP or RAR), find its .txt/.csv members and search them, then
    delete the archive. The blocking work (unrar/zip extraction + reads) runs in a worker
    THREAD so the bot's event loop never freezes (prevents Pyrogram ping-timeout drops on
    big RAR/ZIP), while an updater coroutine shows live progress every few seconds."""
    pattern = re.compile(b'(?i)' + b'|'.join(re.escape(t.encode('utf-8')) for t in search_terms))
    temp_files = []
    progress = {"phase": "opening", "file": "", "lines": 0, "found": 0, "files": 0, "done": False, "error": None}

    def work():
        buffer = []; db_buffer = []
        try:
            try:
                archive_obj, is_zip = _open_archive(archive_path)
            except Exception as e:
                progress["error"] = f"open_failed:{e}"; return
            try:
                # Explicit password first, then saved ones. Raises __PASSWORD_NEEDED__ if
                # encrypted and nothing works, so the caller can prompt the user.
                candidates = ([password] if password else []) + database.get_passwords()
                _resolve_archive_password(archive_obj, is_zip, candidates)
            except Exception as e:
                progress["error"] = "__PASSWORD_NEEDED__" if "__PASSWORD_NEEDED__" in str(e) else f"pw:{e}"
                return
            progress["phase"] = "extracting"  # for RAR, the first read can take a while
            for info in archive_obj.infolist():
                name = info.filename
                if not (name.lower().endswith('.txt') or name.lower().endswith('.csv')):
                    continue
                progress["files"] += 1; progress["file"] = name
                try:
                    with archive_obj.open(name) as f:
                        progress["phase"] = "scanning"
                        while True:
                            chunk_lines = f.readlines(10 * 1024 * 1024)  # 10MB reads
                            if not chunk_lines:
                                break
                            for lb in chunk_lines:
                                progress["lines"] += 1
                                if pattern.search(lb):
                                    ls = lb.decode('utf-8', errors='ignore').strip()
                                    if ls:
                                        buffer.append(ls + "\n"); progress["found"] += 1
                                        if search_id:
                                            for t in search_terms:
                                                if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
                                        if len(db_buffer) >= 2000:
                                            database.save_results_batch(db_buffer); db_buffer = []
                            if len(buffer) >= 5_000_000:
                                buffer.sort()
                                tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                                tmp.writelines(buffer); tmp.close(); temp_files.append(tmp.name); buffer = []
                except Exception as e:
                    es = str(e).lower()
                    if "password" in es or "encrypted" in es or "bad password" in es:
                        progress["error"] = "__PASSWORD_NEEDED__"
                        try: archive_obj.close()
                        except: pass
                        return
                    continue  # skip an unreadable member, keep going
            try: archive_obj.close()
            except: pass
            if db_buffer:
                database.save_results_batch(db_buffer)
            if buffer:
                buffer.sort()
                tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                tmp.writelines(buffer); tmp.close(); temp_files.append(tmp.name)
        except Exception as e:
            progress["error"] = str(e)

    async def updater():
        while not progress["done"]:
            if progress["phase"] == "opening":
                txt = f"📦 **Archive open ho raha hai...**\n{'🔑 password try kar raha hoon' if password else '⏳ please wait'}"
            elif progress["phase"] == "extracting" and progress["lines"] == 0:
                txt = (f"⛏ **Extracting archive...**\n📂 `{progress['file'] or '...'}`\n"
                       "⏳ Bade RAR mein ismein time lagta hai (unrar poori file nikaalta hai).")
            else:
                txt = (f"🔍 **Searching archive...**\n"
                       f"📂 File {progress['files']}: `{progress['file']}`\n"
                       f"📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}")
            await smart_edit_text(status_msg, txt)
            await asyncio.sleep(3)

    upd = asyncio.create_task(updater())
    try:
        await asyncio.to_thread(work)
    finally:
        progress["done"] = True
        try: await upd
        except: pass
    try:
        if os.path.exists(archive_path): os.remove(archive_path)
    except: pass

    if progress["error"]:
        err = progress["error"]
        if err == "__PASSWORD_NEEDED__" or err.startswith("pw:"):
            raise Exception("❌ Wrong password or password required!")
        if err.startswith("open_failed:"):
            raise Exception(f"Extraction failed: {err.split(':', 1)[1]}")
        raise Exception(err)
    if progress["files"] == 0:
        raise Exception("No .txt files found in archive!")
    await smart_edit_text(status_msg, f"🔀 **Merging...**\n📁 Files: {progress['files']}\n📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}")
    _merge_dedup(temp_files, output_filename)
    return output_filename

async def extract_and_search_archive(client, file_id, file_name, search_terms, status_msg, output_filename, password=None, search_id=None):
    """Uploaded archive: download from Telegram to a temp file, then scan it."""
    await status_msg.edit_text(f"📥 **Downloading archive...**\n📦 `{file_name}`")
    archive_path = tempfile.mktemp(suffix=os.path.splitext(file_name)[1])
    try:
        await client.download_media(file_id, file_name=archive_path)
    except Exception as e:
        if os.path.exists(archive_path): os.remove(archive_path)
        raise Exception(f"Download failed: {e}")
    await status_msg.edit_text(f"📦 **Scanning archive...**\n{'🔑 Using password...' if password else '⏳ Please wait...'}")
    return await _scan_local_archive(archive_path, search_terms, status_msg, output_filename, password, search_id)

async def search_in_remote_archive(url, file_name, search_terms, status_msg, output_filename, search_id=None, password=None, extra_headers=None):
    """Remote archive (.rar / .zip): download the whole file to disk with resume-on-drop,
    then scan it. RAR must be downloaded fully because unrar needs a real file on disk
    (unlike ZIP, which the range engine can read remotely)."""
    try:
        import requests
    except ImportError:
        raise Exception("requests not installed — add `requests` to requirements.txt")
    _validate_public_url(url)
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if extra_headers:
        req_headers.update(extra_headers)
    suffix = os.path.splitext(file_name)[1] or ".bin"
    archive_path = tempfile.mktemp(suffix=suffix)
    progress = {"bytes": 0, "done": False, "error": None, "retry": 0}
    MAX_ATTEMPTS = 8

    def downloader():
        pos = 0; attempts = 0
        try:
            with open(archive_path, "wb") as out:
                while True:
                    hdrs = dict(req_headers)
                    if pos > 0:
                        hdrs["Range"] = f"bytes={pos}-"
                    try:
                        with requests.get(url, stream=True, timeout=(30, 180), headers=hdrs) as r:
                            if pos > 0 and r.status_code == 200:
                                # server ignored Range -> restart file from scratch
                                out.seek(0); out.truncate(); pos = 0
                            r.raise_for_status()
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                if not chunk:
                                    continue
                                out.write(chunk); pos += len(chunk); progress["bytes"] = pos
                        break  # finished
                    except Exception as e:
                        attempts += 1
                        if attempts > MAX_ATTEMPTS:
                            progress["error"] = f"Download {MAX_ATTEMPTS} retries ke baad bhi ruka ({e})"; return
                        progress["retry"] = attempts
                        time.sleep(min(5 * attempts, 30))
                        continue
        except Exception as e:
            progress["error"] = str(e)

    async def updater():
        while not progress["done"]:
            mb = progress["bytes"] / (1024 * 1024)
            rt = f"\n🔁 Reconnect #{progress['retry']} (resume)" if progress["retry"] else ""
            await smart_edit_text(status_msg, f"📥 **Downloading archive...**\n📦 `{file_name}`\n⬇️ {mb:,.1f} MB{rt}")
            await asyncio.sleep(3)

    await status_msg.edit_text(f"📥 **Downloading archive...**\n📦 `{file_name}`")
    upd = asyncio.create_task(updater())
    try:
        await asyncio.to_thread(downloader)
    finally:
        progress["done"] = True
        try: await upd
        except: pass
    if progress["error"]:
        try:
            if os.path.exists(archive_path): os.remove(archive_path)
        except: pass
        raise Exception(progress["error"])
    await status_msg.edit_text(f"📦 **Scanning archive...**\n{'🔑 Using password...' if password else '⏳ Please wait...'}")
    return await _scan_local_archive(archive_path, search_terms, status_msg, output_filename, password, search_id)

async def search_in_multiple_files(client, files, search_terms, status_msg, user_id, output_filename, search_id=None):
    """Search multiple channel files, handling each by type:
      • .txt / .csv (or no extension) -> streamed line search
      • .zip / .rar                   -> downloaded + archive-scanned (saved passwords auto-tried)
      • anything else                 -> skipped
    A file that fails (plain-text message with no doc never reaches here; bad archive,
    no matching password, download/read error) is SKIPPED and reported, so one bad file
    in a range never breaks the whole job."""
    total_found = 0; total_lines = 0; files_done = 0; ok_files = 0; all_temps = []
    skipped = []  # list of (filename, reason)
    search_bytes = [t.encode('utf-8') for t in search_terms]
    pattern = re.compile(b'(?i)' + b'|'.join(re.escape(t) for t in search_bytes))

    def _flush_db(db_buffer):
        if db_buffer:
            asyncio.create_task(asyncio.to_thread(database.save_results_batch, db_buffer.copy()))
            db_buffer.clear()

    def _spill(buffer, temps):
        buffer.sort()
        tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
        tmp.writelines(buffer); tmp.close(); temps.append(tmp.name); buffer.clear()

    for finfo in files:
        fid = finfo["file_id"]; fname = finfo["file_name"]; fsize = finfo["file_size"]
        ext = os.path.splitext(fname)[1].lower()
        files_done += 1; found = 0; lc = 0; buffer = []; temps = []; last_upd = 0; db_buffer = []

        # ---------------- TEXT FILES ----------------
        if ext in ('.txt', '.csv', ''):
            leftover = b""
            await smart_edit_text(status_msg, f"⬇️ **File {files_done}/{len(files)}**\n📂 `{fname}`\n📊 Size: {fsize:,} bytes\n✅ Total matches: {total_found:,}")
            try:
                async for chunk in client.stream_media(fid):
                    data = leftover + chunk; data = data.replace(b"\r\n", b"\n"); parts = data.split(b"\n"); leftover = parts.pop()
                    for lb in parts:
                        if not lb: continue
                        lc += 1
                        if pattern.search(lb):
                            ls = lb.decode('utf-8', errors='ignore').strip()
                            if ls:
                                buffer.append(ls + "\n"); found += 1
                                if search_id:
                                    for t in search_terms:
                                        if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
                                if len(db_buffer) >= 2000: _flush_db(db_buffer)
                        if len(buffer) >= 5_000_000: _spill(buffer, temps)
                        if lc - last_upd >= 500_000:
                            last_upd = lc
                            await smart_edit_text(status_msg, f"⬇️ **File {files_done}/{len(files)}: `{fname}`**\n📊 Lines: {lc:,}\n✅ Matches: {found:,}")
                if leftover:
                    ls = leftover.decode('utf-8', errors='ignore').strip()
                    if ls and pattern.search(leftover):
                        buffer.append(ls + "\n"); found += 1
                        if search_id:
                            for t in search_terms:
                                if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
            except Exception as e:
                for t in temps:
                    try: os.remove(t)
                    except: pass
                skipped.append((fname, f"read error: {e}")); continue

        # ---------------- ARCHIVES ----------------
        elif ext in ('.zip', '.rar'):
            await smart_edit_text(status_msg, f"📦 **File {files_done}/{len(files)} (archive)**\n📂 `{fname}`\n📊 Size: {fsize:,} bytes\n⏳ Downloading & scanning...")
            archive_path = tempfile.mktemp(suffix=ext)
            try:
                await client.download_media(fid, file_name=archive_path)
                archive_obj, is_zip = _open_archive(archive_path)
                _resolve_archive_password(archive_obj, is_zip, database.get_passwords())
                had_text = False
                for info in archive_obj.infolist():
                    if not (info.filename.lower().endswith('.txt') or info.filename.lower().endswith('.csv')):
                        continue
                    had_text = True
                    with archive_obj.open(info.filename) as f:
                        while True:
                            chunk_lines = f.readlines(10 * 1024 * 1024)
                            if not chunk_lines: break
                            for lb in chunk_lines:
                                lc += 1
                                if pattern.search(lb):
                                    ls = lb.decode('utf-8', errors='ignore').strip()
                                    if ls:
                                        buffer.append(ls + "\n"); found += 1
                                        if search_id:
                                            for t in search_terms:
                                                if t in ls.lower(): db_buffer.append((search_id, ls, t)); break
                                        if len(db_buffer) >= 2000: _flush_db(db_buffer)
                            await asyncio.sleep(0)  # keep the event loop responsive
                            if len(buffer) >= 5_000_000: _spill(buffer, temps)
                            if lc - last_upd >= 500_000:
                                last_upd = lc
                                await smart_edit_text(status_msg, f"📦 **File {files_done}/{len(files)}: `{fname}`**\n📊 Lines: {lc:,}\n✅ Matches: {found:,}")
                archive_obj.close()
                try: os.remove(archive_path)
                except: pass
                if not had_text:
                    for t in temps:
                        try: os.remove(t)
                        except: pass
                    skipped.append((fname, "no .txt/.csv inside archive")); continue
            except Exception as e:
                try:
                    if os.path.exists(archive_path): os.remove(archive_path)
                except: pass
                for t in temps:
                    try: os.remove(t)
                    except: pass
                reason = "password needed (no saved password matched)" if "__PASSWORD_NEEDED__" in str(e) else str(e)
                skipped.append((fname, reason)); continue

        # ---------------- UNSUPPORTED ----------------
        else:
            skipped.append((fname, f"unsupported type ({ext or 'no extension'})")); continue

        # ---------------- COMMON TAIL (file processed OK) ----------------
        _flush_db(db_buffer)
        if buffer: _spill(buffer, temps)
        total_found += found; total_lines += lc; ok_files += 1
        if temps and found > 0:
            ind_out = f"{fname}_{output_filename}"
            fls = [open(t, "r", encoding="utf-8") for t in temps]
            try:
                with open(ind_out, "w", encoding="utf-8") as out:  # per-file preview, de-duplicated
                    prev = None
                    for line in heapq.merge(*fls):
                        if line != prev: out.write(line); prev = line
            finally:
                for f in fls:
                    try: f.close()
                    except: pass
            try: await client.send_document(chat_id=user_id, document=ind_out, caption=_clip_caption(f"✅ **File {files_done}/{len(files)}**\n📂 `{fname}`\n📊 Lines: {lc:,}\n✅ Matches: {found:,}"))
            except: pass
            try: os.remove(ind_out)
            except: pass
        all_temps.extend(temps)

    tail = f"\n⏭ Skipped: {len(skipped)}" if skipped else ""
    await smart_edit_text(status_msg, f"🔀 **Merging All...**\n📊 Lines: {total_lines:,}\n✅ Matches: {total_found:,}\n📁 Processed: {ok_files}/{files_done}{tail}")
    _merge_dedup(all_temps, output_filename)
    if skipped:
        det = "\n".join([f"• `{n}` — {r}" for n, r in skipped[:10]])
        more = f"\n…and {len(skipped) - 10} more" if len(skipped) > 10 else ""
        try: await client.send_message(user_id, f"⏭ **Skipped {len(skipped)} file(s):**\n{det}{more}")
        except: pass
    return output_filename

async def search_in_remote_text(url, search_terms, status_msg, output_filename, search_id=None, extra_headers=None):
    """Read a plain .txt/.csv straight from a URL and search it line by line while it
    streams in — nothing is saved to disk. Mirrors search_in_file's buffering + external
    merge, so memory stays bounded even on multi-GB text files. The blocking HTTP read
    runs in a worker thread; a tiny async task refreshes the status message.
    extra_headers: optional auth headers/cookies for resolved share-host links."""
    try:
        import requests
    except ImportError:
        raise Exception("requests not installed — add `requests` to requirements.txt")

    _validate_public_url(url)  # SSRF guard (host must be public)
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if extra_headers:
        req_headers.update(extra_headers)

    pattern = re.compile(b'(?i)' + b'|'.join(re.escape(t.encode('utf-8')) for t in search_terms))
    progress = {"lines": 0, "found": 0, "bytes": 0, "done": False, "error": None, "retry": 0}
    temp_files = []
    MAX_ATTEMPTS = 8  # survive transient drops on very large (e.g. 20GB) downloads

    def worker():
        # These persist ACROSS reconnects so a resumed download continues cleanly:
        buffer = []
        db_buffer = []
        leftover = b""
        pos = 0          # bytes consumed so far == where to resume with a Range request
        attempts = 0
        try:
            while True:
                hdrs = dict(req_headers)
                if pos > 0:
                    hdrs["Range"] = f"bytes={pos}-"  # resume from where we dropped
                try:
                    # timeout=(connect, read): a >180s stall counts as a drop and is retried.
                    with requests.get(url, stream=True, timeout=(30, 180), headers=hdrs) as r:
                        if pos > 0 and r.status_code == 200:
                            # Server ignored our Range and restarted from 0 — can't safely
                            # resume a 20GB stream, so bail with a clear message.
                            raise Exception("__NO_RESUME__")
                        r.raise_for_status()
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            pos += len(chunk)
                            progress["bytes"] = pos
                            data = (leftover + chunk).replace(b"\r\n", b"\n")
                            parts = data.split(b"\n")
                            leftover = parts.pop()
                            for lb in parts:
                                if not lb:
                                    continue
                                progress["lines"] += 1
                                if pattern.search(lb):
                                    ls = lb.decode('utf-8', errors='ignore').strip()
                                    if ls:
                                        buffer.append(ls + "\n")
                                        progress["found"] += 1
                                        if search_id:
                                            for t in search_terms:
                                                if t in ls.lower():
                                                    db_buffer.append((search_id, ls, t)); break
                                        if len(db_buffer) >= 2000:
                                            database.save_results_batch(db_buffer); db_buffer = []
                                if len(buffer) >= 5_000_000:
                                    buffer.sort()
                                    tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                                    tmp.writelines(buffer); tmp.close()
                                    temp_files.append(tmp.name); buffer = []
                    break  # stream finished to the end — done
                except Exception as e:
                    if "__NO_RESUME__" in str(e):
                        progress["error"] = ("Server resume (HTTP Range) support nahi karta, isliye "
                                             "itni badi file beech se continue nahi ho sakti.")
                        return
                    attempts += 1
                    if attempts > MAX_ATTEMPTS:
                        progress["error"] = f"Download {MAX_ATTEMPTS} retries ke baad bhi ruka (last: {e})"
                        return
                    progress["retry"] = attempts
                    time.sleep(min(5 * attempts, 30))  # back off, then resume via Range
                    continue
            # final partial line (file not ending in newline)
            if leftover:
                progress["lines"] += 1
                if pattern.search(leftover):
                    ls = leftover.decode('utf-8', errors='ignore').strip()
                    if ls:
                        buffer.append(ls + "\n")
                        progress["found"] += 1
                        if search_id:
                            for t in search_terms:
                                if t in ls.lower():
                                    db_buffer.append((search_id, ls, t)); break
            if db_buffer:
                database.save_results_batch(db_buffer)
            if buffer:
                buffer.sort()
                tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                tmp.writelines(buffer); tmp.close()
                temp_files.append(tmp.name)
        except Exception as e:
            progress["error"] = str(e)

    async def updater():
        while not progress["done"]:
            mb = progress["bytes"] / (1024 * 1024)
            retry_line = f"\n🔁 Reconnect #{progress['retry']} (resume)" if progress["retry"] else ""
            await smart_edit_text(status_msg,
                f"🌐 **Reading text from URL...**\n"
                f"📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}\n"
                f"⬇️ Downloaded: {mb:,.1f} MB{retry_line}")
            await asyncio.sleep(3)

    await status_msg.edit_text("🌐 **Connecting to URL...**\n⏳ Starting download...")
    upd = asyncio.create_task(updater())
    try:
        await asyncio.to_thread(worker)
    finally:
        progress["done"] = True
        try: await upd
        except: pass

    if progress["error"]:
        raise Exception(progress["error"])

    await smart_edit_text(status_msg, f"🔀 **Merging...**\n📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}")
    return _merge_dedup(temp_files, output_filename)

async def search_in_remote_zip(url, search_terms, status_msg, output_filename, search_id=None, password=None, extra_headers=None):
    """Option B — read a .zip straight from a URL via HTTP Range requests (remotezip),
    without downloading the whole archive. Only the ZIP index + the members we actually
    read are fetched. The blocking read runs in a worker thread; a tiny async task
    updates the status message every few seconds.
    extra_headers: optional auth headers/cookies for resolved share-host links."""
    try:
        from remotezip import RemoteZip
        import requests
    except ImportError:
        raise Exception("remotezip not installed — add `remotezip` to requirements.txt")

    _validate_public_url(url)  # SSRF guard (host must be public)
    # A pre-configured session carries auth headers/cookies onto every Range request.
    _sess = requests.Session()
    _sess.headers.update({"User-Agent": "Mozilla/5.0"})
    if extra_headers:
        _sess.headers.update(extra_headers)

    pattern = re.compile(b'(?i)' + b'|'.join(re.escape(t.encode('utf-8')) for t in search_terms))
    progress = {"lines": 0, "found": 0, "file": "", "files": 0, "done": False, "error": None}
    temp_files = []

    def worker():
        buffer = []
        db_buffer = []
        try:
            with RemoteZip(url, session=_sess) as z:
                names = [i.filename for i in z.infolist()
                         if i.filename.lower().endswith(('.txt', '.csv'))]
                if not names:
                    progress["error"] = "No .txt/.csv files found in archive!"
                    return
                # explicit password first, then saved passwords (RemoteZip is a ZipFile)
                candidates = ([password] if password else []) + database.get_passwords()
                try:
                    resolved = _resolve_archive_password(z, True, candidates)
                except Exception:
                    progress["error"] = "__PASSWORD_NEEDED__"
                    return
                pwd = resolved.encode('utf-8') if resolved else None
                for name in names:
                    progress["files"] += 1
                    progress["file"] = name
                    with z.open(name, pwd=pwd) as f:
                        while True:
                            # 10MB chunked line reads, same trick as the file engine
                            chunk_lines = f.readlines(10 * 1024 * 1024)
                            if not chunk_lines:
                                break
                            for lb in chunk_lines:
                                progress["lines"] += 1
                                if pattern.search(lb):
                                    ls = lb.decode('utf-8', errors='ignore').strip()
                                    if ls:
                                        buffer.append(ls + "\n")
                                        progress["found"] += 1
                                        if search_id:
                                            for t in search_terms:
                                                if t in ls.lower():
                                                    db_buffer.append((search_id, ls, t)); break
                                        if len(db_buffer) >= 2000:
                                            database.save_results_batch(db_buffer); db_buffer = []
                            if len(buffer) >= 5_000_000:
                                buffer.sort()
                                tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                                tmp.writelines(buffer); tmp.close()
                                temp_files.append(tmp.name); buffer = []
            if db_buffer:
                database.save_results_batch(db_buffer)
            if buffer:
                buffer.sort()
                tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8")
                tmp.writelines(buffer); tmp.close()
                temp_files.append(tmp.name)
        except Exception as e:
            progress["error"] = str(e)

    async def updater():
        while not progress["done"]:
            await smart_edit_text(status_msg,
                f"🌐 **Reading ZIP from URL (Range mode)...**\n"
                f"📂 File {progress['files']}: `{progress['file']}`\n"
                f"📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}")
            await asyncio.sleep(3)

    await status_msg.edit_text("🌐 **Connecting to URL...**\n⏳ Reading archive index...")
    upd = asyncio.create_task(updater())
    try:
        await asyncio.to_thread(worker)
    finally:
        progress["done"] = True
        try: await upd
        except: pass

    if progress["error"]:
        es = progress["error"].lower()
        if "password" in es or "encrypted" in es or "bad password" in es:
            raise Exception("❌ Wrong password or password required!")
        raise Exception(progress["error"])

    await smart_edit_text(status_msg, f"🔀 **Merging...**\n📊 Lines: {progress['lines']:,}\n✅ Matches: {progress['found']:,}")
    return _merge_dedup(temp_files, output_filename)

async def _run_archive_search(client, fi, status_msg, password):
    """Dispatch to the right archive engine: remote URL zip vs. uploaded archive file."""
    if fi.get("is_remote_archive"):
        return await search_in_remote_archive(fi["url"], fi.get("file_name", "remote.rar"), fi["search_terms"],
                                              status_msg, fi["output_filename"], search_id=fi.get("search_id"),
                                              password=password, extra_headers=fi.get("req_headers"))
    if fi.get("is_remote_zip"):
        return await search_in_remote_zip(fi["url"], fi["search_terms"], status_msg,
                                          fi["output_filename"], search_id=fi.get("search_id"),
                                          password=password, extra_headers=fi.get("req_headers"))
    return await extract_and_search_archive(client, fi["file_id"], fi["file_name"], fi["search_terms"],
                                            status_msg, fi["output_filename"], password=password, search_id=fi.get("search_id"))

# ==================== CALLBACK HANDLERS ====================

@app.on_callback_query(filters.regex("^stats_"))
async def handle_stats_callback(client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id): await cq.answer("🔒 Admin only!", show_alert=True); return
    d = cq.data
    if d == "stats_refresh": await cq.answer("🔄"); await send_stats_message(cq.message, edit=True)
    elif d == "stats_recent":
        recent = database.get_recent_searches(limit=10)
        t = "📈 **Recent Searches:**\n\n"
        if recent:
            for i, s in enumerate(recent, 1): t += f"{i}. User {s['user_id']} - `{s['keywords']}` ({s['source']})\n   {s['timestamp']}\n\n"
        else: t += "No searches yet."
        await cq.message.edit_text(t, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="stats_refresh")]])); await cq.answer()
    elif d == "stats_download":
        await cq.answer("📥 Generating...")
        try:
            total = await asyncio.to_thread(database.get_total_stats)
            if total['total_unique_results'] == 0:
                await cq.message.reply_text("❌ No data!"); return
            # Single streaming pass in a thread (no N+1 per-keyword queries, no event-loop block).
            cf = tempfile.mktemp(suffix=".csv")
            total_exp = await asyncio.to_thread(database.export_full_database_csv, cf)
            await cq.message.reply_document(document=cf, caption=f"📊 **Full Export**\n• Results: {total_exp:,}")
            os.remove(cf)
        except Exception as e: await cq.message.reply_text(f"❌ Error: {e}")
    elif d == "stats_full":
        kstats = database.get_keyword_stats(limit=50); total = database.get_total_stats()
        t = f"📊 **Full Report**\n\nSearches: {total['total_searches']:,}\nKeywords: {total['total_keywords']:,}\nUnique: {total['total_unique_results']:,}\nHits: {total['total_hits']:,}\n\n**Top 50:**\n"
        if kstats:
            for i, s in enumerate(kstats, 1): t += f"{i}. `{s['keyword']}` - {s['total_hits']:,} ({s['unique_hits']:,})\n"
        await cq.message.edit_text(t, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="stats_refresh")]])); await cq.answer()
    elif d.startswith("stats_by_keyword"):
        page = 1
        if "_" in d and d.split("_")[-1].isdigit(): page = int(d.split("_")[-1])
        kws = database.get_all_keywords_list()
        if not kws: await cq.answer("No keywords!", show_alert=True); return
        pp = 15; tp = (len(kws) + pp - 1) // pp; si = (page - 1) * pp; ei = si + pp
        btns = []
        for kw in kws[si:ei]: btns.append([InlineKeyboardButton(f"📥 {kw}", callback_data=f"export_kw_{kw}"[:64])])
        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"stats_by_keyword_{page-1}"))
        if page < tp: nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"stats_by_keyword_{page+1}"))
        if nav: btns.append(nav)
        btns.append([InlineKeyboardButton("◀️ Back", callback_data="stats_refresh")])
        try: await cq.message.edit_text(f"🔍 **Export by Keyword** (Page {page}/{tp})\nTotal: {len(kws)}", reply_markup=InlineKeyboardMarkup(btns)); await cq.answer()
        except: await cq.answer("⏳ Wait a moment.", show_alert=True)
    elif d == "stats_view_results":
        total = database.get_total_stats()
        await cq.message.edit_text(f"💾 **Results**\nUnique: {total['total_unique_results']:,}\nHits: {total['total_hits']:,}\n\n📥 Use 'Export by Keyword' to download!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="stats_refresh")]])); await cq.answer()

@app.on_callback_query(filters.regex("^export_kw_"))
async def handle_keyword_export(client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id): await cq.answer("🔒 Admin only!", show_alert=True); return
    kw = cq.data.replace("export_kw_", "")
    kstats = database.get_keyword_stats(limit=1000)
    stat = next((s for s in kstats if s['keyword'] == kw), None)
    if not stat: await cq.answer("No data!", show_alert=True); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✨ Unique ({stat['unique_hits']:,})", callback_data=f"download_unique_{kw}")],
        [InlineKeyboardButton(f"📦 All ({stat['total_hits']:,})", callback_data=f"download_all_{kw}")],
        [InlineKeyboardButton("◀️ Back", callback_data="stats_by_keyword")]
    ])
    try: await cq.message.edit_text(f"📥 **Export: `{kw}`**\nHits: {stat['total_hits']:,}\nUnique: {stat['unique_hits']:,}", reply_markup=kb); await cq.answer()
    except: await cq.answer("⏳ Wait.", show_alert=True)

@app.on_callback_query(filters.regex("^download_(unique|all)_"))
async def handle_keyword_download(client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id): await cq.answer("🔒 Admin only!", show_alert=True); return
    d = cq.data; unique = d.startswith("download_unique_"); kw = d.replace("download_unique_", "").replace("download_all_", "")
    await cq.answer(f"📥 Generating...")
    try:
        import csv
        cf = tempfile.mktemp(suffix=".csv"); tf = tempfile.mktemp(suffix=".txt")
        def _build():
            results = database.get_results_by_keyword(kw, limit=50000, unique_only=unique)
            with open(cf, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f); w.writerow(['Result Line', 'Timestamp'])
                for r in results: w.writerow([r['result_line'], r['timestamp']])
            with open(tf, 'w', encoding='utf-8') as f:
                for r in results: f.write(r['result_line'] + '\n')
            return len(results)
        n = await asyncio.to_thread(_build)  # DB read + file writes off the event loop
        if not n:
            for p in (cf, tf):
                try: os.remove(p)
                except: pass
            await cq.message.reply_text(f"❌ No results for `{kw}`"); return
        rt = "Unique" if unique else "All"
        await cq.message.reply_document(document=cf, caption=f"📊 **{rt} for `{kw}`** - {n:,} lines (CSV)")
        await cq.message.reply_document(document=tf, caption=f"📄 **{rt} for `{kw}`** - {n:,} lines (TXT)")
        os.remove(cf); os.remove(tf)
    except Exception as e: await cq.message.reply_text(f"❌ Error: {e}")

@app.on_callback_query(filters.regex("^(confirm|cancel)_import$"))
async def handle_import_confirmation(client, cq: CallbackQuery):
    uid = cq.from_user.id
    if not is_admin(uid): await cq.answer("🔒 Admin only!", show_alert=True); return
    if cq.data == "cancel_import":
        await cq.answer("❌ Cancelled"); await cq.message.edit_text("❌ Cancelled."); user_states[uid] = {"state": "waiting_file"}; return
    if uid not in user_states or user_states[uid].get("state") != "confirming_import": await cq.answer("Invalid!", show_alert=True); return
    idata = user_states[uid]["import_data"]
    await cq.answer("📥 Importing..."); await cq.message.edit_text("📥 **Importing...**\n⏳ Please wait...")
    try:
        kw_results = {}
        for item in idata:
            kw = item['keyword']
            if kw not in kw_results: kw_results[kw] = []
            kw_results[kw].append(item['result'])
        def _do_import():
            ts = {"imported": 0, "skipped": 0, "errors": 0}
            for kw, results in kw_results.items():
                s = database.import_results(kw, results, uid)
                ts["imported"] += s["imported"]; ts["skipped"] += s["skipped"]; ts["errors"] += s["errors"]
            return ts
        ts = await asyncio.to_thread(_do_import)  # batched DB writes off the event loop
        await cq.message.edit_text(f"✅ **Import Done!**\n• Imported: {ts['imported']:,}\n• Skipped: {ts['skipped']:,}\n• Errors: {ts['errors']:,}")
    except Exception as e: await cq.message.edit_text(f"❌ Failed: {e}")
    user_states[uid] = {"state": "waiting_file"}

@app.on_callback_query(filters.regex("^archive_pwd_(yes|no)$"))
async def handle_archive_password_check(client, cq: CallbackQuery):
    uid = cq.from_user.id
    if uid not in user_states or user_states[uid].get("state") != "confirming_archive_password": await cq.answer("Invalid!", show_alert=True); return
    if cq.data == "archive_pwd_yes":
        await cq.answer("📝 Send password"); await cq.message.edit_text("🔑 **Send the archive password:**")
        user_states[uid]["state"] = "waiting_archive_password"
    else:
        await cq.answer("✅ Processing...")
        fi = user_states[uid]
        status_msg = await cq.message.edit_text("🔍 **Processing...**\n⏳ Please wait...")
        try:
            of = await _run_archive_search(client, fi, status_msg, None)
            if of and os.path.exists(of):
                if os.path.getsize(of) == 0:
                    await status_msg.edit_text("❌ No matches found!")
                else:
                    await status_msg.edit_text("📤 Sending...")
                    await send_final_result(client, cq.message, of, f"✅ **Done!**\n🔍 Keywords: `{_fmt_keywords(fi['search_terms'])}`\n📄 Output: `{fi['output_filename']}`", cq.from_user, fi.get('file_name', 'Archive'))
                os.remove(of); await status_msg.delete()
            else:
                await status_msg.edit_text("❌ No matches found!")
            user_states[uid] = {"state": "waiting_file"}
        except Exception as e:
            es = str(e).lower()
            if "password" in es or "encrypted" in es:
                await status_msg.edit_text("🔒 **Archive needs a password!**\n🔑 Send the password:")
                user_states[uid]["state"] = "waiting_archive_password"
            else:
                await status_msg.edit_text(f"❌ Error: {e}"); user_states[uid] = {"state": "waiting_file"}

@app.on_callback_query(filters.regex("^urltype_(txt|zip|rar)$"))
async def handle_url_type_choice(client, cq: CallbackQuery):
    uid = cq.from_user.id
    st = user_states.get(uid)
    if not st or st.get("state") != "choosing_url_type" or not st.get("url"):
        await cq.answer("Invalid! Send /url again.", show_alert=True); return
    url = st["url"]
    base = st.get("url_base") or "remote"
    hdrs = st.get("req_headers")  # auth headers from a resolved share link (if any)
    if cq.data == "urltype_txt":
        fn = base if base.lower().endswith((".txt", ".csv")) else (base + ".txt" if base else "remote.txt")
        await cq.answer("📄 Text selected")
        await cq.message.edit_text(
            f"🌐 **Text URL accepted:** `{fn}`\n\n"
            "💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`"
        )
        # Plain text: no archive/password step, searched line by line while downloading.
        user_states[uid] = {"state": "waiting_keywords", "url": url, "file_name": fn,
            "source": f"URL: {fn}", "is_remote_text": True, "req_headers": hdrs}
    elif cq.data == "urltype_rar":
        fn = base if base.lower().endswith(".rar") else (base + ".rar" if base else "remote.rar")
        await cq.answer("📦 RAR selected")
        await cq.message.edit_text(
            f"🌐 **RAR URL accepted:** `{fn}`\n\n"
            "💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`"
        )
        # RAR must be downloaded fully (unrar needs a real file on disk).
        user_states[uid] = {"state": "waiting_keywords", "url": url, "file_name": fn,
            "source": f"URL: {fn}", "is_archive": True, "is_remote_archive": True, "archive_password": None, "req_headers": hdrs}
    else:
        fn = base if base.lower().endswith(".zip") else (base + ".zip" if base else "remote.zip")
        await cq.answer("📦 ZIP selected")
        await cq.message.edit_text(
            f"🌐 **ZIP URL accepted:** `{fn}`\n\n"
            "💬 Now send keywords (comma-separated)\nExample: `gmail.com, yahoo.com`"
        )
        # is_archive=True reuses the existing password-check flow; is_remote_zip picks the engine.
        user_states[uid] = {"state": "waiting_keywords", "url": url, "file_name": fn,
            "source": f"URL: {fn}", "is_archive": True, "is_remote_zip": True, "archive_password": None, "req_headers": hdrs}

# ==================== HEALTH CHECK (optional) ====================
def _start_healthcheck():
    """Tiny stdlib HTTP server returning 200 OK, so a PaaS that insists on an HTTP
    health check (e.g. Coolify when deployed as a normal app) sees the bot as healthy.
    Only runs if HEALTHCHECK_PORT (or PORT) is set — otherwise the bot is a pure worker."""
    port = os.getenv("HEALTHCHECK_PORT") or os.getenv("PORT")
    if not port:
        return
    try:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Type", "text/plain")
                self.end_headers(); self.wfile.write(b"OK")
            def log_message(self, *a): pass  # silence access logs
        srv = HTTPServer(("0.0.0.0", int(port)), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"🩺 Health check server listening on :{port}")
    except Exception as e:
        print(f"⚠️ Health check server failed to start: {e}")

# ==================== MAIN ====================
if __name__ == "__main__":
    print("🤖 Starting Search Bot...")
    _start_healthcheck()
    print("✅ Bot is running! Press Ctrl+C to stop.\n")
    from pyrogram.errors import Unauthorized, AuthKeyDuplicated
    try:
        app.run()
    except (Unauthorized, AuthKeyDuplicated) as e:
        # Saved session was revoked (e.g. token regenerated in @BotFather). Drop it so the
        # next restart logs in fresh with BOT_TOKEN instead of crash-looping forever.
        print(f"❌ Telegram rejected the saved session ({e}). Deleting it; restart will re-login.")
        for f in (_SESSION_FILE, _SESSION_FILE + "-journal"):
            try: os.remove(f)
            except OSError: pass
        raise SystemExit(1)
