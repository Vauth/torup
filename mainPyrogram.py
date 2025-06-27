import os
import time
import uuid
import asyncio
import libtorrent as lt
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

# --- Configuration ---
API_ID = 8138160
OWNER_ID = 5052959324
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN")

DOWNLOAD_PATH = './downloads/'

# --- Bot Globals & Session Setup ---
# **FIX 1: Main Event Loop Holder**
# We need to capture the main event loop to use it from other threads.
main_loop = None

app = Client(
    "pyro_tornet_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# State management dictionaries
pending_downloads = {}
active_torrents = {}

# --- Libtorrent Session Optimization ---
print("Configuring libtorrent session...")
settings = lt.default_settings()
settings['user_agent'] = 'Pyro-TorrentBot/2.2 libtorrent/2.0'
settings['cache_size'] = 32768
settings['aio_threads'] = 8
settings['connections_limit'] = 1000
# **FIX 2: DeprecationWarning for listen_on()**
# The modern way to set the listen port.
settings["listen_interfaces"] = "0.0.0.0:6881,0.0.0.0:6891"
settings['alert_mask'] = (
    lt.alert.category_t.error_notification |
    lt.alert.category_t.storage_notification |
    lt.alert.category_t.status_notification
)
ses = lt.session(settings)
print("Session configured.")

# --- Helper Functions ---
def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0: break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def progress_bar_str(progress, length=10):
    filled_len = int(length * progress)
    return '▰' * filled_len + '▱' * (length - filled_len)

# --- Core Logic ---
async def get_torrent_info_task(magnet_link, message):
    unique_id = str(uuid.uuid4())[:8]
    try:
        loop = asyncio.get_event_loop()
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        temp_handle = await loop.run_in_executor(None, ses.add_torrent, params)
        await message.edit_text('**🔎 Fetching torrent details...**')
        for _ in range(60):
            if await loop.run_in_executor(None, temp_handle.has_metadata):
                break
            await asyncio.sleep(1)
        else:
            await message.edit_text("❌ **Error:** Timed out fetching metadata...")
            await loop.run_in_executor(None, ses.remove_torrent, temp_handle)
            return
        ti = await loop.run_in_executor(None, temp_handle.get_torrent_info)
        await loop.run_in_executor(None, ses.remove_torrent, temp_handle)
        
        # **FIX 3: DeprecationWarning for file iteration**
        # Convert to a list before iterating.
        files = list(ti.files())
        file_list = "\n".join([f"📄 `{f.path}` ({human_readable_size(f.size)})" for f in files])
        if len(file_list) > 2048:
            file_list = file_list[:2048] + "\n..."
        details_text = (
            f"✅ **Torrent Details**\n\n"
            f"**🏷️ Name:** `{ti.name()}`\n"
            f"**🗂️ Size:** {human_readable_size(ti.total_size())}\n\n"
            f"**📦 Files:**\n{file_list}"
        )
        pending_downloads[unique_id] = magnet_link
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Download", callback_data=f"start_{unique_id}")]])
        await message.edit_text(details_text, reply_markup=buttons)
    except Exception as e:
        await message.edit_text(f"❌ **An unexpected critical error occurred:**\n`{e}`")

async def download_task(chat_id, magnet_link, message):
    loop = asyncio.get_event_loop()
    handle = None
    try:
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await loop.run_in_executor(None, ses.add_torrent, params)
        active_torrents[chat_id] = (handle, asyncio.current_task())
        while not await loop.run_in_executor(None, handle.has_metadata):
            await asyncio.sleep(0.5)
        ti = await loop.run_in_executor(None, handle.get_torrent_info)
        while handle.is_valid() and not handle.status().is_seeding:
            s = handle.status()
            state_str = ['Queued', 'Checking', 'DL Metadata', 'Downloading', 'Finished', 'Seeding', 'Allocating', 'Checking resume'][s.state]
            status_text = (
                f"**🚀 Downloading: ** `{ti.name()}`\n\n"
                f"{progress_bar_str(s.progress)} **{s.progress*100:.2f}%**\n\n"
                f"**⬇️ Speed:** `{human_readable_size(s.download_rate)}/s`\n"
                f"**⬆️ Speed:** `{human_readable_size(s.upload_rate)}/s`\n"
                f"**📦 Done:** `{human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}`\n"
                f"**👤 Peers:** `{s.num_peers}` | **🚦 Status:** `{state_str}`"
            )
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{chat_id}")]])
            try:
                await message.edit_text(status_text, reply_markup=buttons)
            except FloodWait as fw:
                await asyncio.sleep(fw.value)
            except Exception: break
            await asyncio.sleep(5)
        if not (handle.is_valid() and handle.status().is_seeding):
            if not asyncio.current_task().cancelled():
                 await message.edit_text("❌ **Download Stalled or Failed.**", reply_markup=None)
            return
        await message.edit_text(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 **Preparing to upload...**", reply_markup=None)
        
        # **FIX 3: DeprecationWarning for file iteration**
        files = sorted(list(ti.files()), key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                await upload_file(message, file_path)
        await message.edit_text(f"🏁 **Finished!**\n\nAll files from `{ti.name()}` have been successfully uploaded.", reply_markup=None)
    except asyncio.CancelledError:
        await message.edit_text("❌ **Download Cancelled.**", reply_markup=None)
    except Exception as e:
        await message.edit_text(f"❌ **An unexpected error occurred during download:**\n`{e}`", reply_markup=None)
    finally:
        if chat_id in active_torrents: del active_torrents[chat_id]
        if handle and handle.is_valid():
            await loop.run_in_executor(None, ses.remove_torrent, handle)

# **FIX 1: Main 'RuntimeError' Fix**
class UploadProgressReporter:
    def __init__(self, message, file_name, loop):
        self._message = message
        self._file_name = file_name
        self._loop = loop  # Store the main event loop
        self._last_update_time = time.time()
        self._last_uploaded_bytes = 0

    def __call__(self, current_bytes, total_bytes):
        current_time = time.time()
        if current_time - self._last_update_time < 4 and current_bytes != total_bytes:
            return
        elapsed_time = current_time - self._last_update_time or 1
        bytes_since_last_update = current_bytes - self._last_uploaded_bytes
        speed = bytes_since_last_update / elapsed_time
        progress = current_bytes / total_bytes
        status_text = (
            f"**📤 Uploading: ** `{self._file_name}`\n\n"
            f"{progress_bar_str(progress)} **{progress*100:.2f}%**\n\n"
            f"**⬆️ Speed:** `{human_readable_size(speed)}/s`\n"
            f"**📦 Done:** `{human_readable_size(current_bytes)} / {human_readable_size(total_bytes)}`"
        )
        
        # This is the key change: schedule the coroutine on the main loop
        # from this background thread.
        asyncio.run_coroutine_threadsafe(self._edit_message(status_text), self._loop)
        
        self._last_update_time = current_time
        self._last_uploaded_bytes = current_bytes

    async def _edit_message(self, text):
        try:
            await self._message.edit_text(text, reply_markup=None)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except Exception:
            pass

async def upload_file(message, file_path):
    file_name = os.path.basename(file_path)
    # Pass the main_loop to the reporter instance
    reporter = UploadProgressReporter(message, file_name, main_loop)
    await app.send_document(
        chat_id=message.chat.id,
        document=file_path,
        caption=f"`{file_name}`",
        force_document=True,
        progress=reporter
    )
    try:
        os.remove(file_path)
        if os.path.isdir(os.path.dirname(file_path)) and not os.listdir(os.path.dirname(file_path)):
             os.removedirs(os.path.dirname(file_path))
    except (OSError, Exception):
        pass

# --- Telegram Event Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start(_, message):
    await message.reply_text('**Welcome to your Ultimate Torrent Downloader!**\n\nSend me a magnet link to begin.')

@app.on_message(filters.regex(r"^magnet:.*") & filters.private)
async def handle_magnet(_, message):
    if message.from_user.id != OWNER_ID: return
    if message.chat.id in active_torrents:
        await message.reply_text("**⚠️ A download is already active...**")
        return
    bot_message = await message.reply_text('⏳ **Validating magnet link...**')
    asyncio.create_task(get_torrent_info_task(message.text, bot_message))

@app.on_callback_query()
async def handle_callback(_, callback_query):
    data_parts = callback_query.data.split('_', 1)
    action, payload = data_parts[0], data_parts[1] if len(data_parts) > 1 else None
    chat_id = callback_query.message.chat.id

    if action == "start":
        if chat_id in active_torrents:
            await callback_query.answer("Another download is already active!", show_alert=True)
            return
        magnet_link = pending_downloads.pop(payload, None)
        if not magnet_link:
            await callback_query.message.edit_text("**❌ This download link has expired...**")
            return
        message = callback_query.message
        if not message: return
        await callback_query.answer("🚀 Download initiated...", show_alert=False)
        await message.edit_text("**⏳ Initializing download...**", reply_markup=None)
        asyncio.create_task(download_task(chat_id, magnet_link, message))

    elif action == "cancel":
        if chat_id in active_torrents:
            _, task = active_torrents.pop(chat_id)
            task.cancel()
            await callback_query.answer("❌ Download will be cancelled.", show_alert=True)
        else:
            await callback_query.answer("⚠️ This download is not active.", show_alert=True)

# --- Main Application Runner ---
async def run_bot():
    global main_loop
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN environment variable not set.")
        return
    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)

    # **FIX 1: Capture the main event loop**
    main_loop = asyncio.get_running_loop()
    
    # Start the alert handler as a background task
    main_loop.create_task(alert_handler())
    
    print("Bot starting...")
    await app.start()
    print("Bot has started successfully. Listening for magnet links...")
    await asyncio.Event().wait() # Keep running until interrupted
    print("Bot stopping...")
    await app.stop()

async def alert_handler():
    while True:
        try:
            alerts = await main_loop.run_in_executor(None, ses.pop_alerts)
            for alert in alerts:
                if alert.what() and alert.message():
                    print(f"[{alert.what()}] {alert.message()}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Alert handler error: {e}")

if __name__ == '__main__':
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        print("Shutdown signal received.")
