import asyncio
import os
import time
import libtorrent as lt
from telethon import TelegramClient, events, Button

# --- Configuration ---
# Your API credentials and bot token
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN") # Best practice: get from an environment variable

# Path where downloaded files will be stored
DOWNLOAD_PATH = './downloads/'

# --- Bot Globals & Session Setup ---
client = TelegramClient('tornet', API_ID, API_HASH)

# In-memory dictionaries for state management
# { message_id: asyncio.Task }
cancellable_tasks = {}
# { chat_id: (torrent_handle, asyncio.Task) }
active_torrents = {}

# --- Libtorrent Session Optimization for Speed ---
print("Configuring libtorrent session for maximum speed...")
settings = lt.default_settings()
settings['user_agent'] = 'Telethon-TorrentBot/1.0 libtorrent/2.0'
settings['cache_size'] = 32768  # 512 MB cache
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
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if size < 1024.0: break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def progress_bar_str(progress, length=10):
    filled_len = int(length * progress)
    return '▓' * filled_len + '░' * (length - filled_len)

# --- Core Logic ---

async def get_torrent_info_task(magnet_link, event):
    """Cancellable task to fetch torrent metadata and present options."""
    message = event.message
    try:
        loop = asyncio.get_event_loop()
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        temp_handle = await loop.run_in_executor(None, ses.add_torrent, params)

        while not await loop.run_in_executor(None, temp_handle.has_metadata):
            await asyncio.sleep(1)

        ti = await loop.run_in_executor(None, temp_handle.get_torrent_info)
        await loop.run_in_executor(None, ses.remove_torrent, temp_handle)

        # **DEPRECATION FIX**: Use ti.num_files() and ti.file_at(i)
        files = [ti.file_at(i) for i in range(ti.num_files())]
        file_list = "\n".join(
            [f"📄 `{f.path}` ({human_readable_size(f.size)})" for f in files]
        )
        if len(file_list) > 2048: # Truncate if too long
            file_list = file_list[:2048] + "\n..."

        details_text = (
            f"✅ **Torrent Details Ready**\n\n"
            f"**🏷️ Name:** `{ti.name()}`\n"
            f"**🗂️ Size:** {human_readable_size(ti.total_size())}\n\n"
            f"**📦 Files:**\n{file_list}"
        )
        
        # Pass magnet link in the button data to avoid global dicts
        buttons = Button.inline("🚀 Download", data=f"start_{magnet_link}")
        await message.edit(details_text, buttons=buttons)

    except asyncio.CancelledError:
        await message.edit("❌ **Fetch Cancelled.**")
    except RuntimeError as e:
        await message.edit(f"❌ **Error:** Invalid magnet link or metadata fetch failed.\n\n`{e}`")
    finally:
        if message.id in cancellable_tasks:
            del cancellable_tasks[message.id]

async def download_task(chat_id, magnet_link, message):
    """The main task that handles the download and progress updates."""
    loop = asyncio.get_event_loop()
    try:
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await loop.run_in_executor(None, ses.add_torrent, params)
        
        active_torrents[chat_id] = (handle, asyncio.current_task())
        
        # Wait for metadata to be ready before starting the main loop
        while not await loop.run_in_executor(None, handle.has_metadata):
            await asyncio.sleep(0.5)
        ti = await loop.run_in_executor(None, handle.get_torrent_info)

        while handle.is_valid() and not await loop.run_in_executor(None, lambda: handle.status().state == lt.torrent_status.seeding):
            s = await loop.run_in_executor(None, handle.status)
            state_str = ['Queued', 'Checking', 'DL Metadata', 'Downloading', 'Finished', 'Seeding', 'Allocating'][s.state]
            
            status_text = (
                f"**🚀 Downloading: ** `{ti.name()}`\n\n"
                f"`{progress_bar_str(s.progress)}` **{s.progress*100:.2f}%**\n\n"
                f"**⬇️ Speed:** {human_readable_size(s.download_rate)}/s\n"
                f"**⬆️ Speed:** {human_readable_size(s.upload_rate)}/s\n"
                f"**📦 Done:** {human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}\n"
                f"**👤 Peers:** {s.num_peers} | **🚦 Status:** {state_str}"
            )
            buttons = Button.inline("❌ Cancel Download", data=f"cancel_{chat_id}")
            
            try:
                await message.edit(status_text, buttons=buttons)
            except Exception: break
            await asyncio.sleep(5)

        if not handle.is_valid():
             await message.edit("❌ **Download Cancelled or Invalidated.**", buttons=None)
             return

        await message.edit(f"✅ **Download complete!**\n`{ti.name()}`\n\n📤 Preparing to upload...", buttons=None)

        # **DEPRECATION FIX**: Use modern file iteration
        files = sorted([ti.file_at(i) for i in range(ti.num_files())], key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                await upload_file(chat_id, message, file_path)
            # Clean up empty directories after upload
            try:
                if os.path.isdir(os.path.dirname(file_path)):
                    os.removedirs(os.path.dirname(file_path))
            except OSError:
                pass # Directory not empty, which is fine

        await message.edit("✅ **All files uploaded successfully!**", buttons=None)

    except asyncio.CancelledError:
        await message.edit("❌ **Download Cancelled.**", buttons=None)
    except Exception as e:
        await message.edit(f"❌ **An unexpected error occurred:**\n`{e}`", buttons=None)
    finally:
        if chat_id in active_torrents:
            del active_torrents[chat_id]


async def upload_file(chat_id, message, file_path):
    """Handles uploading a single file with progress callback."""
    file_name = os.path.basename(file_path)
    
    async def progress_callback(current, total):
        try:
            await message.edit(
                f"**📤 Uploading:** `{file_name}`\n"
                f"`{progress_bar_str(current/total)}`"
            )
        except Exception: pass

    await client.send_file(chat_id, file_path, caption=file_name, progress_callback=progress_callback)
    try:
        os.remove(file_path)
    except OSError as e:
        print(f"Error removing file {file_path}: {e}")

# --- Telegram Event Handlers ---

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond('**Welcome to your Torrent Downloader Bot!**\n\nSend me a magnet link to begin.')

@client.on(events.NewMessage(pattern='magnet:.*'))
async def handle_magnet(event):
    if event.chat_id in active_torrents:
        await event.respond("A download is already active in this chat. Please wait or cancel it first.", parse_mode='md')
        return

    message = await event.respond(
        '🔎 **Fetching torrent details...**',
        buttons=Button.inline("❌ Cancel", data=f"cancel_fetch_{event.message.id}")
    )
    
    task = asyncio.create_task(get_torrent_info_task(event.text, event))
    cancellable_tasks[message.id] = task

@client.on(events.CallbackQuery)
async def handle_callback(event):
    """Handles all button clicks."""
    data_parts = event.data.decode('utf-8').split('_', 1)
    action = data_parts[0]
    payload = data_parts[1] if len(data_parts) > 1 else None
    
    chat_id = event.chat_id
    message_id = event.message_id

    if action == "start":
        if chat_id in active_torrents:
            await event.answer("Another download is already active!", alert=True)
            return
        
        magnet_link = payload
        # **ATTRIBUTEERROR FIX**: Use `await event.get_message()`
        message = await event.get_message()
        if not message: return

        await event.answer("🚀 Starting download...")
        await message.edit("⏳ Initializing download...", buttons=None)
        asyncio.create_task(download_task(chat_id, magnet_link, message))

    elif action == "cancel":
        if chat_id in active_torrents:
            handle, task = active_torrents.pop(chat_id)
            task.cancel()
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: ses.remove_torrent(handle, lt.session.delete_files) if handle.is_valid() else None)
            
            await event.answer("❌ Download cancelled.")
        else:
            await event.answer("This download is not active.", alert=True)
            await event.edit(buttons=None)

    elif action == "cancel_fetch":
        task = cancellable_tasks.pop(int(payload), None)
        if task:
            task.cancel()
            await event.answer("❌ Fetch operation cancelled.")
        else:
            await event.answer("This operation is already complete or cancelled.", alert=True)

# --- ADVANCED ERROR HANDLING ---
async def alert_handler():
    """A background task that prints session alerts for debugging and monitoring."""
    loop = asyncio.get_event_loop()
    while True:
        alerts = await loop.run_in_executor(None, ses.pop_alerts)
        for alert in alerts:
            if alert.what() and alert.message():
                 print(f"[{alert.what()}] {alert.message()}")
        await asyncio.sleep(2) # Check every 2 seconds

# --- Main Function ---
async def main():
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN environment variable not set.")
        return
        
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
        
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
