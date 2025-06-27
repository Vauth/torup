import asyncio
import os
import time
import libtorrent as lt
from telethon import TelegramClient, events, Button

# --- Configuration ---
# Your API credentials and bot token
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN") # It's best practice to get this from an environment variable

# Path where downloaded files will be stored
DOWNLOAD_PATH = './downloads/'

# --- Bot Globals & Session Setup ---
client = TelegramClient('tornet', API_ID, API_HASH)

# In-memory dictionary to hold magnet links pending user confirmation
# Format: {message_id: magnet_link}
pending_torrents = {}

# Dictionary to keep track of active torrents for each chat
# Format: {chat_id: (torrent_handle, task)}
active_torrents = {}

# --- Libtorrent Session Optimization for Speed ---
print("Configuring libtorrent session for maximum speed...")
settings = lt.default_settings()
settings['user_agent'] = 'libtorrent/2.0'
settings['cache_size'] = 32768  # 512 MB cache size (32768 * 16KiB blocks)
settings['peer_connect_timeout'] = 10
settings['request_timeout'] = 20
settings['stop_tracker_timeout'] = 5
settings['aio_threads'] = 8 # More threads for disk I/O
settings['checking_mem_usage'] = 2048 # Use more RAM for checking files
settings['connections_limit'] = 1000 # Increase connection limit

ses = lt.session(settings)
ses.listen_on(6881, 6891)
print("Session configured.")

# --- Helper Functions ---
def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def progress_bar_str(progress, length=10):
    filled_len = int(length * progress)
    return '▓' * filled_len + '░' * (length - filled_len)


# --- Core Logic ---

async def get_torrent_info_from_magnet(magnet_link, message):
    """Fetches metadata from a magnet link without starting the download."""
    loop = asyncio.get_event_loop()
    try:
        params = await loop.run_in_executor(
            None, lambda: lt.parse_magnet_uri(magnet_link)
        )
        # We need to add the torrent to the session to fetch metadata
        temp_handle = await loop.run_in_executor(
            None, lambda: ses.add_torrent(params)
        )

        # Wait for metadata
        while not await loop.run_in_executor(None, temp_handle.has_metadata):
            await asyncio.sleep(0.5)

        ti = await loop.run_in_executor(None, temp_handle.get_torrent_info)
        
        # Now that we have metadata, remove the temporary handle.
        # It will be re-added when the user clicks "Download".
        await loop.run_in_executor(None, lambda: ses.remove_torrent(temp_handle))
        
        return ti
    except (lt.libtorrent_error, RuntimeError) as e:
        await message.edit(f"**Error fetching metadata:**\n`{e}`\n\nPlease check if the magnet link is valid.")
        return None


async def download_task(chat_id, magnet_link, message):
    """The main task that handles the download and progress updates."""
    loop = asyncio.get_event_loop()
    try:
        params = await loop.run_in_executor(None, lt.parse_magnet_uri, magnet_link)
        params.save_path = DOWNLOAD_PATH
        handle = await loop.run_in_executor(None, ses.add_torrent, params)
        
        # Store handle and the current task to allow cancellation
        active_torrents[chat_id] = (handle, asyncio.current_task())

        ti = await loop.run_in_executor(None, handle.get_torrent_info)

        # --- Download Monitoring Loop ---
        while not await loop.run_in_executor(None, handle.status):
            s = await loop.run_in_executor(None, handle.status)
            if s.state == lt.torrent_status.seeding:
                break
            
            state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating'][s.state]
            progress = s.progress * 100
            bar = progress_bar_str(s.progress)
            
            status_text = (
                f"**Downloading:** `{ti.name()}`\n"
                f"**[{bar}]** {progress:.2f}%\n\n"
                f"**Speed:** ⬇️ {human_readable_size(s.download_rate)}/s | ⬆️ {human_readable_size(s.upload_rate)}/s\n"
                f"**Size:** {human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}\n"
                f"**Peers:** {s.num_peers} | **State:** {state_str}"
            )
            
            # The cancel button data includes the chat_id for identification
            buttons = Button.inline("Cancel", data=f"cancel_{chat_id}")
            
            try:
                await message.edit(status_text, buttons=buttons)
            except Exception: # Message might be deleted
                break
            await asyncio.sleep(4)

        await message.edit(f"✅ **Download complete!**\n`{ti.name()}`\n\nNow uploading files...", buttons=None)

        # --- Uploading Files ---
        files = sorted(await loop.run_in_executor(None, ti.files), key=lambda f: f.path)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.isfile(file_path):
                await upload_file(chat_id, message, file_path)

        await message.edit("**All files uploaded successfully!**", buttons=None)

    except asyncio.CancelledError:
        await message.edit("**Download cancelled by user.**", buttons=None)
        # The torrent handle is removed in the callback handler
    except Exception as e:
        await message.edit(f"**An unexpected error occurred during download:**\n`{e}`", buttons=None)
    finally:
        if chat_id in active_torrents:
            del active_torrents[chat_id]


async def upload_file(chat_id, message, file_path):
    """Handles uploading a single file with progress callback."""
    file_name = os.path.basename(file_path)
    
    async def progress_callback(current, total):
        bar = progress_bar_str(current / total)
        try:
            await message.edit(f"**Uploading:** `{file_name}`\n`{bar}`")
        except Exception:
            pass

    await client.send_file(
        chat_id,
        file_path,
        caption=file_name,
        progress_callback=progress_callback
    )
    # Clean up the file after upload
    os.remove(file_path)

# --- Telegram Event Handlers ---

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond('**Welcome!**\nSend me a magnet link to begin.')

@client.on(events.NewMessage(pattern='magnet:.*'))
async def handle_magnet(event):
    chat_id = event.chat_id
    if chat_id in active_torrents:
        await event.respond("A download is already active in this chat. Please cancel it before starting a new one.", parse_mode='md')
        return

    message = await event.respond('🔎 Fetching torrent details, please wait...')
    magnet_link = event.text
    
    ti = await get_torrent_info_from_magnet(magnet_link, message)
    if ti is None:
        return # Error already handled in the function

    # Store magnet link for the callback
    pending_torrents[message.id] = magnet_link

    # Prepare details message
    file_list = "\n".join([f"• `{f.path}` ({human_readable_size(f.size)})" for f in ti.files()])
    details_text = (
        f"**Torrent Details:**\n\n"
        f"**Name:** `{ti.name()}`\n"
        f"**Size:** {human_readable_size(ti.total_size())}\n\n"
        f"**Files:**\n{file_list}"
    )

    buttons = Button.inline("🚀 Download", data=f"start_{message.id}")
    await message.edit(details_text, buttons=buttons)

@client.on(events.CallbackQuery)
async def handle_callback(event):
    """Handles all button clicks."""
    data = event.data.decode('utf-8')
    chat_id = event.chat_id
    message_id = event.message_id
    
    # --- DOWNLOAD BUTTON ---
    if data.startswith('start_'):
        if chat_id in active_torrents:
            await event.answer("Another download is already active in this chat!", alert=True)
            return

        magnet_link = pending_torrents.pop(message_id, None)
        if not magnet_link:
            await event.edit("This download link has expired or is invalid.")
            return

        await event.edit("Starting download...", buttons=None)
        # Start the download task in the background
        asyncio.create_task(download_task(chat_id, magnet_link, event.message))

    # --- CANCEL BUTTON ---
    elif data.startswith('cancel_'):
        if chat_id in active_torrents:
            handle, task = active_torrents.pop(chat_id)
            task.cancel() # Cancel the asyncio task
            
            # Also remove the torrent from the libtorrent session
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: ses.remove_torrent(handle) if handle.is_valid() else None)
            
            await event.answer("Download cancelled.")
            # The message will be updated by the cancelled task's exception handler
        else:
            await event.answer("This download is already completed or cancelled.", alert=True)
            await event.edit(buttons=None)


# --- Main Function ---
async def main():
    if BOT_TOKEN is None:
        print("Error: BOT_TOKEN environment variable not set.")
        return
        
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
        
    print("Bot is starting...")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot has started successfully. Listening for magnet links...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"An error occurred: {e}")
