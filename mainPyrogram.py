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
app = Client(
    "pyro_tornet_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# State management dictionaries
pending_downloads = {}  # {unique_id: magnet_link}
active_torrents = {}    # {chat_id: (torrent_handle, asyncio.Task)}

# --- Libtorrent Session Optimization ---
print("Configuring libtorrent session...")
settings = lt.default_settings()
settings['user_agent'] = 'Pyro-TorrentBot/2.0 libtorrent/2.0'
settings['cache_size'] = 32768
settings['aio_threads'] = 8
settings['connections_limit'] = 1000
settings['alert_mask'] = (
    lt.alert.category_t.error_notification |
    lt.alert.category_t.storage_notification |
    lt.alert.category_t.status_notification
)
ses = lt.session(settings)
ses.listen_on(6881, 6891)
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
    """Fetches torrent metadata and presents it to the user."""
    unique_id = str(uuid.uuid4())[:8]
    try:
        loop = asyncio.get_event_loop()
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
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Download", callback_data=f"start_{unique_id}")]])
        await message.edit_text(details_text, reply_markup=buttons)

    except RuntimeError as e:
        await message.edit_text(f"❌ **Error:** Invalid magnet link or metadata fetch failed.\n\n`{e}`")
    except Exception as e:
        await message.edit_text(f"❌ **An unexpected critical error occurred:**\n`{e}`")


async def download_task(chat_id, magnet_link, message):
    """The main task that handles download, progress, and sequential uploads."""
    loop = asyncio.get_event_loop()
    handle = None
    try:
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await loop.run_in_executor(None, ses.add_torrent, params)
        active_torrents[chat_id] = (handle, asyncio.current_task())

        # --- Download Loop (same as your original code) ---
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
            except Exception:
                break
            await asyncio.sleep(5)

        if not (handle.is_valid() and handle.status().is_seeding):
            if not asyncio.current_task().cancelled():
                await message.edit_text("❌ **Download Stalled or Failed.**", reply_markup=None)
            return
        # --- End of Download Loop ---

        await message.edit_text(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 **Preparing to upload...**", reply_markup=None)

        # --- NEW: Improved Upload Logic ---
        files = sorted([ti.file_at(i) for i in range(ti.num_files())], key=lambda f: f.path)
        for i, f in enumerate(files):
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                file_name = os.path.basename(file_path)

                # This message edit is crucial. It persists while the file uploads.
                await message.edit_text(
                    f"**📤 Uploading file {i+1} of {len(files)}...**\n"
                    f"`{file_name}`"
                )

                # The reporter will now edit the message with detailed progress.
                reporter = UploadProgressReporter(message, file_name)

                await app.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    caption=f"`{file_name}`",
                    force_document=True,
                    progress=reporter
                )

                # Clean up the file after successful upload
                try:
                    os.remove(file_path)
                    # Clean up empty parent directories
                    if os.path.isdir(os.path.dirname(file_path)) and not os.listdir(os.path.dirname(file_path)):
                        os.removedirs(os.path.dirname(file_path))
                except (OSError, Exception) as e:
                    print(f"Error cleaning up file {file_path}: {e}")

        await message.edit_text(f"🏁 **Finished!**\n\nAll files from `{ti.name()}` have been successfully uploaded.", reply_markup=None)

    except asyncio.CancelledError:
        await message.edit_text("❌ **Download Cancelled.**", reply_markup=None)
    except Exception as e:
        await message.edit_text(f"❌ **An unexpected error occurred during download/upload:**\n`{e}`", reply_markup=None)
    finally:
        if chat_id in active_torrents: del active_torrents[chat_id]
        if handle and handle.is_valid():
            await loop.run_in_executor(None, ses.remove_torrent, handle)


class UploadProgressReporter:
    """A stateful class to report upload progress by editing a message."""
    def __init__(self, message, file_name):
        self._message = message
        self._file_name = file_name
        self._last_update_time = time.time()
        self._last_uploaded_bytes = 0

    async def __call__(self, current_bytes, total_bytes):
        current_time = time.time()
        # Update every 4 seconds to avoid hitting flood limits
        if current_time - self._last_update_time < 4 and current_bytes != total_bytes:
            return

        elapsed_time = current_time - self._last_update_time
        if elapsed_time == 0: elapsed_time = 1 # Avoid division by zero
        
        bytes_since_last_update = current_bytes - self._last_uploaded_bytes
        speed = bytes_since_last_update / elapsed_time
        progress = current_bytes / total_bytes
        
        status_text = (
            f"**📤 Uploading: ** `{self._file_name}`\n\n"
            f"{progress_bar_str(progress)} **{progress*100:.2f}%**\n\n"
            f"**⬆️ Speed:** `{human_readable_size(speed)}/s`\n"
            f"**📦 Done:** `{human_readable_size(current_bytes)} / {human_readable_size(total_bytes)}`"
        )

        try:
            # Edit the message with the upload progress
            await self._message.edit_text(status_text, reply_markup=None)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except Exception:
            pass # Ignore other errors like message not modified

        self._last_update_time = current_time
        self._last_uploaded_bytes = current_bytes


async def upload_file(message, file_path):
    """Handles uploading a single file with a detailed progress reporter."""
    file_name = os.path.basename(file_path)
    # The reporter will edit the 'message' object it's given.
    reporter = UploadProgressReporter(message, file_name)

    await app.send_document(
        chat_id=message.chat.id,
        document=file_path,
        caption=f"`{file_name}`",
        force_document=True,
        progress=reporter
    )
    try:
        os.remove(file_path)
        # Clean up empty parent directories
        if os.path.isdir(os.path.dirname(file_path)) and not os.listdir(os.path.dirname(file_path)):
             os.removedirs(os.path.dirname(file_path))
    except (OSError, Exception):
        pass


# --- Telegram Event Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply_text('**Welcome to your Ultimate Torrent Downloader!**\n\nSend me a magnet link to begin.')

@app.on_message(filters.regex(r"^magnet:.*") & filters.private)
async def handle_magnet(client, message):
    if message.from_user.id != OWNER_ID: return
    if message.chat.id in active_torrents:
        await message.reply_text("**⚠️ A download is already active in this chat. Please wait or cancel it first.**")
        return
    bot_message = await message.reply_text('⏳ **Validating magnet link...**')
    asyncio.create_task(get_torrent_info_task(message.text, bot_message))


@app.on_callback_query()
async def handle_callback(client, callback_query):
    """Handles all button clicks."""
    data_parts = callback_query.data.split('_', 1)
    action = data_parts[0]
    payload = data_parts[1] if len(data_parts) > 1 else None

    chat_id = callback_query.message.chat.id

    if action == "start":
        if chat_id in active_torrents:
            await callback_query.answer("Another download is already active!", show_alert=True)
            return

        unique_id = payload
        magnet_link = pending_downloads.pop(unique_id, None)

        if not magnet_link:
            await callback_query.message.edit_text("**❌ This download link has expired. Please send the magnet link again.**", reply_markup=None)
            return

        message = callback_query.message
        if not message: return

        await callback_query.answer("🚀 Download initiated...", show_alert=False)
        await message.edit_text("**⏳ Initializing download...**", reply_markup=None)
        asyncio.create_task(download_task(chat_id, magnet_link, message))

    elif action == "cancel":
        if chat_id in active_torrents:
            handle, task = active_torrents.pop(chat_id)
            task.cancel()
            await callback_query.answer("❌ Download will be cancelled.", show_alert=True)
        else:
            await callback_query.answer("⚠️ This download is not active.", show_alert=True)


# --- Alert Handler & Main Function ---
async def alert_handler():
    """A background task that prints session alerts for debugging."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            alerts = await loop.run_in_executor(None, ses.pop_alerts)
            for alert in alerts:
                if alert.what() and alert.message():
                    print(f"[{alert.what()}] {alert.message()}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Alert handler error: {e}")


def main():
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN environment variable not set.")
        return

    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)

    # Start the alert handler as a background asyncio task
    asyncio.get_event_loop().create_task(alert_handler())
    
    print("Bot has started successfully. Listening for magnet links...")
    app.run() # This will block and run the bot
    print("Bot stopped.")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"A critical error occurred in main: {e}")
