import asyncio
import os
import time
import uuid
import libtorrent as lt
from telethon import TelegramClient, events, Button

# --- Configuration ---
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN")

DOWNLOAD_PATH = './downloads/'

# --- Bot Globals & Session Setup ---
client = TelegramClient('tornet', API_ID, API_HASH)

# State management dictionaries
pending_downloads = {} # {unique_id: magnet_link}
active_torrents = {}   # {chat_id: (torrent_handle, asyncio.Task)}

# --- Libtorrent Session Optimization ---
print("Configuring libtorrent session...")
settings = lt.default_settings()
settings['user_agent'] = 'Telethon-TorrentBot/2.0 libtorrent/2.0'
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

        await message.edit('**🔎 Fetching torrent details...**')

        for _ in range(60): # Timeout after ~60 seconds
            if await loop.run_in_executor(None, temp_handle.has_metadata):
                break
            await asyncio.sleep(1)
        else:
            await message.edit("❌ **Error:** Timed out fetching metadata. The torrent is likely dead or has no seeds.")
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
        buttons = Button.inline("🚀 Download", data=f"start_{unique_id}")
        await message.edit(details_text, buttons=buttons)

    except RuntimeError as e:
        await message.edit(f"❌ **Error:** Invalid magnet link or metadata fetch failed.\n\n`{e}`")
    except Exception as e:
        await message.edit(f"❌ **An unexpected critical error occurred:**\n`{e}`")


async def download_task(chat_id, magnet_link, message):
    """The main task that handles the download and progress updates."""
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

        while handle.is_valid() and handle.status().state != lt.torrent_status.seeding:
            s = handle.status()
            state_str = ['Queued', 'Checking', 'DL Metadata', 'Downloading', 'Finished', 'Seeding', 'Allocating'][s.state]
            
            status_text = (
                f"**🚀 Downloading: ** `{ti.name()}`\n\n"
                f"{progress_bar_str(s.progress)} **{s.progress*100:.2f}%**\n\n"
                f"**⬇️ Speed:** `{human_readable_size(s.download_rate)}/s`\n"
                f"**⬆️ Speed:** `{human_readable_size(s.upload_rate)}/s`\n"
                f"**📦 Done:** `{human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}`\n"
                f"**👤 Peers:** `{s.num_peers}` | **🚦 Status:** `{state_str}`"
            )
            buttons = Button.inline("❌ Cancel Download", data=f"cancel_{chat_id}")
            
            try:
                await message.edit(status_text, buttons=buttons)
            except Exception: break
            await asyncio.sleep(5)

        if not (handle.is_valid() and handle.status().state == lt.torrent_status.seeding):
            if not asyncio.current_task().cancelled():
                 await message.edit("❌ **Download Stalled or Failed.**", buttons=None)
            return

        await message.edit(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 Preparing to upload...", buttons=None)

        files = sorted([ti.file_at(i) for i in range(ti.num_files())], key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                await upload_file(chat_id, message, file_path)

        await message.edit(f"🏁 **Finished!**\n\nAll files from `{ti.name()}` have been successfully uploaded.", buttons=None)

    except asyncio.CancelledError:
        await message.edit("❌ **Download Cancelled.**", buttons=None)
    except Exception as e:
        await message.edit(f"❌ **An unexpected error occurred during download:**\n`{e}`", buttons=None)
    finally:
        if chat_id in active_torrents: del active_torrents[chat_id]
        if handle and handle.is_valid():
            await loop.run_in_executor(None, ses.remove_torrent, handle)

class UploadProgressReporter:
    """A stateful class to report upload progress with speed calculation."""
    def __init__(self, message, file_name, loop):
        self._message = message
        self._file_name = file_name
        self._loop = loop
        self._last_update_time = time.time()
        self._last_uploaded_bytes = 0

    async def __call__(self, current_bytes, total_bytes):
        current_time = time.time()
        # Update every 3 seconds to avoid hitting flood limits
        if current_time - self._last_update_time < 3 and current_bytes != total_bytes:
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
            # **BUG FIX**: Explicitly set buttons=None to prevent old buttons reappearing
            await self._message.edit(status_text, buttons=None)
        except Exception:
            pass

        self._last_update_time = current_time
        self._last_uploaded_bytes = current_bytes

async def upload_file(chat_id, message, file_path):
    """Handles uploading a single file with a detailed progress reporter."""
    file_name = os.path.basename(file_path)
    loop = asyncio.get_event_loop()
    reporter = UploadProgressReporter(message, file_name, loop)
    
    await client.send_file(
        chat_id,
        file_path,
        caption=file_name,
        progress_callback=reporter
    )
    try:
        os.remove(file_path)
        # Clean up empty parent directories
        if os.path.isdir(os.path.dirname(file_path)):
            os.removedirs(os.path.dirname(file_path))
    except OSError:
        pass # Ignore errors if dir is not empty or doesn't exist

# --- Telegram Event Handlers ---

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond('**Welcome to your Ultimate Torrent Downloader!**\n\nSend me a magnet link to begin.')

@client.on(events.NewMessage(pattern='magnet:.*'))
async def handle_magnet(event):
    if event.chat_id in active_torrents:
        await event.respond("**⚠️ A download is already active in this chat. Please wait or cancel it first.**")
        return
    bot_message = await event.respond('⏳ **Validating magnet link...**')
    asyncio.create_task(get_torrent_info_task(event.text, bot_message))

@client.on(events.CallbackQuery)
async def handle_callback(event):
    """Handles all button clicks."""
    data_parts = event.data.decode('utf-8').split('_', 1)
    action, payload = data_parts[0], data_parts[1] if len(data_parts) > 1 else None
    
    chat_id = event.chat_id

    if action == "start":
        if chat_id in active_torrents:
            await event.answer("Another download is already active!", alert=True)
            return
        
        unique_id = payload
        magnet_link = pending_downloads.pop(unique_id, None)
        
        if not magnet_link:
            await event.edit("**❌ This download link has expired. Please send the magnet link again.**", buttons=None)
            return

        message = await event.get_message()
        if not message: return

        await event.answer("**🚀 Download initiated...**")
        # **BUG FIX**: This edit call reliably removes the "Download" button
        await message.edit("**⏳ Initializing download...**", buttons=None)
        asyncio.create_task(download_task(chat_id, magnet_link, message))

    elif action == "cancel":
        if chat_id in active_torrents:
            handle, task = active_torrents.pop(chat_id)
            task.cancel()
            await event.answer("**❌ Download will be cancelled.**", alert=True)
        else:
            await event.answer("**⚠️ This download is not active.**", alert=True)

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

async def main():
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN environment variable not set.")
        return
        
    if not os.path.exists(DOWNLOAD_PATH): os.makedirs(DOWNLOAD_PATH)
        
    print("Bot is starting...")
    await client.start(bot_token=BOT_TOKEN)
    
    asyncio.create_task(alert_handler())
    
    print("Bot has started successfully. Listening for magnet links...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"A critical error occurred in main: {e}")
