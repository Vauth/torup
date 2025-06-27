import os
import time
import uuid
import asyncio
import libtorrent as lt
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import MessageNotModified, FloodWait

# --- Configuration ---
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE") # It's recommended to use environment variables
OWNER_ID = 5052959324

DOWNLOAD_PATH = './downloads/'

# --- Bot & libtorrent Setup ---
app = Client("pyro_tornet", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# State management dictionaries
pending_downloads = {}  # {unique_id: magnet_link}
active_torrents = {}    # {chat_id: (torrent_handle, asyncio.Task)}

print("Configuring libtorrent session...")
settings = lt.default_settings()
settings['user_agent'] = 'Pyro-TorrentBot/3.0 libtorrent/2.0'
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
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def progress_bar_str(progress, length=10):
    filled_len = int(length * progress)
    return '▰' * filled_len + '▱' * (length - filled_len)

# --- Upload Progress Class ---
class UploadProgressReporter:
    """A stateful class to report upload progress by editing the main status message."""
    def __init__(self, message: Message, file_name: str):
        self._message = message
        self._file_name = file_name
        self._last_update_time = time.time()
        self._last_uploaded_bytes = 0

    async def __call__(self, current_bytes, total_bytes):
        current_time = time.time()
        if current_time - self._last_update_time < 5 and current_bytes != total_bytes:
            return

        elapsed_time = current_time - self._last_update_time
        bytes_since_last_update = current_bytes - self._last_uploaded_bytes
        speed = bytes_since_last_update / elapsed_time if elapsed_time > 0 else 0
        
        progress = current_bytes / total_bytes
        status_text = (
            f"**📤 Uploading: ** `{self._file_name}`\n\n"
            f"{progress_bar_str(progress)} **{progress*100:.2f}%**\n\n"
            f"**⬆️ Speed:** `{human_readable_size(speed)}/s`\n"
            f"**📦 Done:** `{human_readable_size(current_bytes)} / {human_readable_size(total_bytes)}`"
        )

        try:
            await self._message.edit_text(status_text)
        except (MessageNotModified, FloodWait):
            pass
        except Exception as e:
            print(f"Error editing progress message: {e}")

        self._last_update_time = current_time
        self._last_uploaded_bytes = current_bytes

# --- Core Torrenting Logic ---
async def get_torrent_info_task(magnet_link: str, message: Message):
    """Fetches torrent metadata and presents it to the user."""
    unique_id = str(uuid.uuid4())[:8]
    try:
        params = await asyncio.to_thread(lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        temp_handle = await asyncio.to_thread(ses.add_torrent, params)

        await message.edit('**🔎 Fetching torrent details...**')

        for _ in range(60):
            if await asyncio.to_thread(temp_handle.has_metadata):
                break
            await asyncio.sleep(1)
        else:
            await message.edit("❌ **Error:** Timed out fetching metadata. The torrent is likely dead or has no seeds.")
            await asyncio.to_thread(ses.remove_torrent, temp_handle)
            return

        ti = await asyncio.to_thread(temp_handle.get_torrent_info)
        await asyncio.to_thread(ses.remove_torrent, temp_handle)

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
        await message.edit(details_text, reply_markup=buttons)

    except Exception as e:
        await message.edit(f"❌ **An unexpected error occurred:**\n`{e}`")


async def download_task(chat_id: int, magnet_link: str, message: Message):
    """Main task to handle download, progress, and upload logic together."""
    handle = None
    try:
        params = await asyncio.to_thread(lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await asyncio.to_thread(ses.add_torrent, params)
        active_torrents[chat_id] = (handle, asyncio.current_task())
        
        while not await asyncio.to_thread(handle.has_metadata):
            await asyncio.sleep(0.5)
        ti = await asyncio.to_thread(handle.get_torrent_info)

        while handle.is_valid() and handle.status().state != lt.torrent_status.seeding:
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
                await message.edit(status_text, reply_markup=buttons)
            except (MessageNotModified, FloodWait):
                pass
            except Exception:
                break
            await asyncio.sleep(5)

        if not (handle.is_valid() and handle.status().state == lt.torrent_status.seeding):
            if not asyncio.current_task().cancelled():
                 await message.edit("❌ **Download Stalled or Failed.**")
            return

        # --- UPLOAD LOGIC STARTS HERE ---
        await message.edit(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 Preparing to upload...", reply_markup=None)

        files = sorted([ti.file_at(i) for i in range(ti.num_files())], key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                # Instantiate the reporter for each file, telling it to edit the status message
                reporter = UploadProgressReporter(message, os.path.basename(file_path))

                await app.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=os.path.basename(file_path),
                    force_document=True,
                    progress=reporter
                )
                
                try:
                    os.remove(file_path)
                    parent_dir = os.path.dirname(file_path)
                    if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                        os.removedirs(parent_dir)
                except OSError as e:
                    print(f"Error removing file/dir: {e}")

        await message.edit(f"🏁 **Finished!**\n\nAll files from `{ti.name()}` have been successfully uploaded.")

    except asyncio.CancelledError:
        await message.edit("❌ **Download Cancelled.**")
    except Exception as e:
        await message.edit(f"❌ **An unexpected error occurred during download:**\n`{e}`")
    finally:
        if chat_id in active_torrents: del active_torrents[chat_id]
        if handle and handle.is_valid():
            await asyncio.to_thread(ses.remove_torrent, handle)

# --- Telegram Event Handlers ---
owner_filter = filters.user(OWNER_ID)

@app.on_message(filters.command("start") & owner_filter)
async def start_command(client, message):
    await message.reply('**Welcome to your Ultimate Torrent Downloader!**\n\nSend me a magnet link to begin.')

@app.on_message(filters.regex(r'magnet:.*') & owner_filter)
async def handle_magnet(client, message):
    if message.chat.id in active_torrents:
        await message.reply("**⚠️ A download is already active in this chat. Please wait or cancel it first.**")
        return
    bot_message = await message.reply('⏳ **Validating magnet link...**', quote=True)
    asyncio.create_task(get_torrent_info_task(message.text, bot_message))

@app.on_callback_query(owner_filter)
async def handle_callback(client, cb):
    data, payload = cb.data.split('_', 1) if '_' in cb.data else (cb.data, None)
    chat_id = cb.message.chat.id

    if data == "start":
        if chat_id in active_torrents:
            await cb.answer("Another download is already active!", show_alert=True)
            return
        
        magnet_link = pending_downloads.pop(payload, None)
        if not magnet_link:
            await cb.message.edit("**❌ This download link has expired. Please send the magnet link again.**")
            return

        await cb.answer("🚀 Download initiated...")
        await cb.message.edit("**⏳ Initializing download...**", reply_markup=None)
        asyncio.create_task(download_task(chat_id, magnet_link, cb.message))

    elif data == "cancel":
        if chat_id in active_torrents:
            _, task = active_torrents.pop(chat_id)
            task.cancel()
            await cb.answer("❌ Download will be cancelled.", show_alert=True)
        else:
            await cb.answer("⚠️ This download is not active.", show_alert=True)

# --- Main Execution ---
async def alert_handler():
    while True:
        try:
            alerts = await asyncio.to_thread(ses.pop_alerts)
            for alert in alerts:
                if alert.what() and alert.message(): print(f"[{alert.what()}] {alert.message()}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Alert handler error: {e}")

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("FATAL: BOT_TOKEN environment variable not set.")
        return
        
    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)
        
    print("Bot is starting...")
    await app.start()
    asyncio.create_task(alert_handler())
    print("Bot has started successfully. Listening for magnet links...")
    await asyncio.Event().wait() # Keep the bot running

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
