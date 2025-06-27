import os
import time
import uuid
import asyncio
import libtorrent as lt
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

# --- Configuration ---
API_ID = 8138160  # Replace with your API ID
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"  # Replace with your API HASH
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Use environment variable or hardcode
OWNER_ID = 5052959324  # Your Telegram user ID

DOWNLOAD_PATH = './downloads/'

# --- Bot Setup ---
app = Client(
    "tornet",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.MARKDOWN
)

# State management dictionaries
pending_downloads = {}  # {unique_id: magnet_link}
active_torrents = {}  # {chat_id: (torrent_handle, asyncio.Task)}

# --- Libtorrent Session Optimization ---
print("Configuring libtorrent session...")
settings = {
    'user_agent': 'Pyrogram-TorrentBot/2.0 libtorrent/2.0',
    'cache_size': 32768,
    'aio_threads': 8,
    'connections_limit': 1000,
    'alert_mask': (
        lt.alert.category_t.error_notification |
        lt.alert.category_t.storage_notification |
        lt.alert.category_t.status_notification
    ),
}
ses = lt.session(settings)
ses.listen_on(6881, 6891)
print("Session configured.")

# --- Helper Functions ---
def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def progress_bar_str(progress, length=10):
    filled_len = int(length * progress)
    return '▰' * filled_len + '▱' * (length - filled_len)

# --- Core Logic ---
async def get_torrent_info_task(magnet_link: str, message: Message):
    """Fetches torrent metadata and presents it to the user."""
    unique_id = str(uuid.uuid4())[:8]
    try:
        loop = asyncio.get_running_loop()
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        temp_handle = await loop.run_in_executor(None, ses.add_torrent, params)

        await message.edit_text('**🔎 Fetching torrent details...**')

        for _ in range(60):  # Timeout after ~60 seconds
            if await loop.run_in_executor(None, temp_handle.has_metadata):
                break
            await asyncio.sleep(1)
        else:
            await message.edit_text("❌ **Error:** Timed out fetching metadata. The torrent is likely dead or has no seeds.")
            await loop.run_in_executor(None, ses.remove_torrent, temp_handle)
            return

        ti = await loop.run_in_executor(None, temp_handle.get_torrent_info)
        await loop.run_in_executor(None, ses.remove_torrent, temp_handle)

        files = [ti.file_at(i) for i in range(ti.num_files())]
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
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Download", callback_data=f"start_{unique_id}")]
        ])
        await message.edit_text(details_text, reply_markup=buttons)

    except Exception as e:
        await message.edit_text(f"❌ **Error:** {str(e)}")

async def download_task(chat_id: int, magnet_link: str, message: Message):
    """The main task that handles the download and progress updates."""
    loop = asyncio.get_running_loop()
    handle = None
    try:
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await loop.run_in_executor(None, ses.add_torrent, params)

        active_torrents[chat_id] = (handle, asyncio.current_task())

        while not await loop.run_in_executor(None, handle.has_metadata):
            await asyncio.sleep(0.5)
        ti = await loop.run_in_executor(None, handle.get_torrent_info)

        last_update_time = time.time()
        while handle.is_valid() and handle.status().state != lt.torrent_status.seeding:
            s = handle.status()
            state_str = ['Queued', 'Checking', 'DL Metadata', 'Downloading', 'Finished', 'Seeding', 'Allocating'][s.state]

            # Only update every 5 seconds to avoid flooding
            if time.time() - last_update_time >= 5:
                status_text = (
                    f"**🚀 Downloading: ** `{ti.name()}`\n\n"
                    f"{progress_bar_str(s.progress)} **{s.progress * 100:.2f}%**\n\n"
                    f"**⬇️ Speed:** `{human_readable_size(s.download_rate)}/s`\n"
                    f"**⬆️ Speed:** `{human_readable_size(s.upload_rate)}/s`\n"
                    f"**📦 Done:** `{human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}`\n"
                    f"**👤 Peers:** `{s.num_peers}` | **🚦 Status:** `{state_str}`"
                )
                buttons = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{chat_id}")]
                ])

                try:
                    await message.edit_text(status_text, reply_markup=buttons)
                    last_update_time = time.time()
                except Exception as e:
                    print(f"Error updating status: {e}")

            await asyncio.sleep(1)

        if not (handle.is_valid() and handle.status().state == lt.torrent_status.seeding):
            if not asyncio.current_task().cancelled():
                await message.edit_text("❌ **Download Stalled or Failed.**", reply_markup=None)
            return

        await message.edit_text(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 Preparing to upload...", reply_markup=None)

        files = sorted([ti.file_at(i) for i in range(ti.num_files())], key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                await upload_file(chat_id, message, file_path)

        await message.edit_text(
            f"🏁 **Finished!**\n\nAll files from `{ti.name()}` have been successfully uploaded.",
            reply_markup=None
        )

    except asyncio.CancelledError:
        await message.edit_text("❌ **Download Cancelled.**", reply_markup=None)
    except Exception as e:
        await message.edit_text(f"❌ **Error during download:** {str(e)}", reply_markup=None)
    finally:
        if chat_id in active_torrents:
            del active_torrents[chat_id]
        if handle and handle.is_valid():
            await loop.run_in_executor(None, ses.remove_torrent, handle)

async def upload_file(chat_id: int, message: Message, file_path: str):
    """Handles uploading a single file with progress updates."""
    file_name = os.path.basename(file_path)
    last_update_time = 0
    
    async def progress(current, total):
        nonlocal last_update_time
        now = time.time()
        if now - last_update_time < 5 and current != total:
            return
        
        progress_percent = (current / total) * 100
        speed = (current / (now - last_update_time)) if now > last_update_time else 0
        
        status_text = (
            f"**📤 Uploading: ** `{file_name}`\n\n"
            f"{progress_bar_str(current/total)} **{progress_percent:.2f}%**\n\n"
            f"**⬆️ Speed:** `{human_readable_size(speed)}/s`\n"
            f"**📦 Done:** `{human_readable_size(current)} / {human_readable_size(total)}`"
        )
        
        try:
            await message.edit_text(status_text)
            last_update_time = now
        except Exception:
            pass

    try:
        await app.send_document(
            chat_id=chat_id,
            document=file_path,
            caption=file_name,
            force_document=True,
            progress=progress
        )
    except Exception as e:
        await message.edit_text(f"❌ **Upload failed:** {str(e)}")
    finally:
        try:
            os.remove(file_path)
            dir_path = os.path.dirname(file_path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError:
            pass

# --- Telegram Event Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    await message.reply_text(
        '**Welcome to your Ultimate Torrent Downloader!**\n\n'
        'Send me a magnet link to begin.'
    )

@app.on_message(filters.regex(r'^magnet:\?') & filters.user(OWNER_ID))
async def handle_magnet(client, message: Message):
    if message.chat.id in active_torrents:
        await message.reply_text("**⚠️ A download is already active in this chat. Please wait or cancel it first.**")
        return
    
    bot_message = await message.reply_text('⏳ **Validating magnet link...**')
    asyncio.create_task(get_torrent_info_task(message.text, bot_message))

@app.on_callback_query(filters.regex(r'^start_') | filters.regex(r'^cancel_'))
async def handle_callback(client, callback_query):
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    
    if data.startswith("start_"):
        unique_id = data.split('_')[1]
        magnet_link = pending_downloads.pop(unique_id, None)
        
        if not magnet_link:
            await callback_query.answer("This download link has expired!", show_alert=True)
            return await callback_query.message.edit_text(
                "**❌ This download link has expired. Please send the magnet link again.**",
                reply_markup=None
            )
        
        await callback_query.answer("Download starting...")
        await callback_query.message.edit_text("**⏳ Initializing download...**", reply_markup=None)
        asyncio.create_task(download_task(chat_id, magnet_link, callback_query.message))
        
    elif data.startswith("cancel_"):
        target_chat_id = int(data.split('_')[1])
        if target_chat_id in active_torrents:
            handle, task = active_torrents.pop(target_chat_id)
            task.cancel()
            await callback_query.answer("Download cancelled!", show_alert=True)
        else:
            await callback_query.answer("No active download to cancel!", show_alert=True)

# --- Alert Handler & Main Function ---
async def alert_handler():
    """A background task that prints session alerts for debugging."""
    while True:
        try:
            alerts = ses.pop_alerts()
            for alert in alerts:
                if alert.what() and alert.message():
                    print(f"[{alert.what()}] {alert.message()}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Alert handler error: {e}")

async def run_bot():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not set!")
        return
    
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    
    print("Starting bot...")
    async with app:
        # Start background tasks
        asyncio.create_task(alert_handler())
        
        print("Bot is running!")
        await asyncio.Event().wait()  # Run forever

if __name__ == '__main__':
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
