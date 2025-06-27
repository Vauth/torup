import asyncio
import os
import time
import libtorrent as lt
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename

# --- Configuration ---
# Get these from my.telegram.org and @BotFather
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Path where downloaded files will be stored
DOWNLOAD_PATH = './downloads/'

# --- Bot Globals ---
client = TelegramClient('tornet', API_ID, API_HASH)
# Dictionary to keep track of active torrents for each chat
# Format: {chat_id: (torrent_handle, message_to_edit)}
active_torrents = {}
# The libtorrent session object
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})


# --- Helper Functions ---
def human_readable_size(size, decimal_places=2):
    """Converts bytes to a human-readable format (e.g., KiB, MiB, GiB)."""
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']:
        if size < 1024.0 or unit == 'PiB':
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"


def progress_bar_str(progress, length=20):
    """Creates a textual progress bar string."""
    filled_len = int(length * progress)
    return '█' * filled_len + '░' * (length - filled_len)


# --- Core Torrent Management (Asynchronous) ---

async def download_manager(event, magnet_link):
    """Manages the lifecycle of a single torrent download asynchronously."""
    chat_id = event.chat_id
    loop = asyncio.get_event_loop()
    message = await event.respond('Processing magnet link...')

    try:
        # --- Add Torrent (Blocking operation, run in executor) ---
        params = await loop.run_in_executor(
            None, lt.parse_magnet_uri, magnet_link
        )
        params.save_path = os.path.join(DOWNLOAD_PATH, '')  # Ensure directory exists
        handle = await loop.run_in_executor(
            None, ses.add_torrent, params
        )
        active_torrents[chat_id] = (handle, message)

        # --- Metadata Fetch Loop ---
        await message.edit('Fetching torrent metadata...')
        while not await loop.run_in_executor(None, handle.has_metadata):
            await asyncio.sleep(1)

        ti = await loop.run_in_executor(None, handle.get_torrent_info)
        torrent_name = ti.name()

        # --- Download Loop ---
        while not await loop.run_in_executor(None, handle.status):
            s = await loop.run_in_executor(None, handle.status)
            if s.state == lt.torrent_status.seeding:
                break

            state_str = [
                'queued', 'checking', 'downloading metadata', 'downloading',
                'finished', 'seeding', 'allocating'
            ]

            progress = s.progress
            bar = progress_bar_str(progress)

            # Prepare status text
            status_text = (
                f"**Downloading:** `{torrent_name}`\n"
                f"`{bar}` {progress:.2%}\n\n"
                f"**Speed:** ⬇️ {human_readable_size(s.download_rate)}/s | "
                f"⬆️ {human_readable_size(s.upload_rate)}/s\n"
                f"**Progress:** {human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}\n"
                f"**Peers:** {s.num_peers} | **State:** {state_str[s.state]}\n\n"
                f"To cancel, send /cancel"
            )

            await message.edit(status_text)
            await asyncio.sleep(5)  # Update interval

        await message.edit(f"✅ **Download complete!**\n\n`{torrent_name}`\n\nNow preparing to upload...")

        # --- Uploading Files ---
        files = await loop.run_in_executor(None, ti.files)
        for f in files:
            file_path = os.path.join(DOWNLOAD_PATH, f.path)
            if os.path.exists(file_path):
                await upload_file(chat_id, message, file_path)

        await message.edit("**All files uploaded successfully!**")

    except lt.libtorrent_error as e:
        await message.edit(f"**Torrent Error:** {e}")
    except Exception as e:
        await message.edit(f"**An unexpected error occurred:** {e}")
    finally:
        # --- Cleanup ---
        if chat_id in active_torrents:
            handle_to_remove, _ = active_torrents.pop(chat_id)
            await loop.run_in_executor(
                None, lambda: ses.remove_torrent(handle_to_remove) if handle_to_remove.is_valid() else None
            )


async def upload_file(chat_id, message, file_path):
    """Handles uploading a single file with progress callback."""
    file_name = os.path.basename(file_path)

    async def progress_callback(current, total):
        percentage = current / total
        bar = progress_bar_str(percentage)
        await message.edit(
            f"**Uploading:** `{file_name}`\n"
            f"`{bar}` {percentage:.2%}"
        )

    await client.send_file(
        chat_id,
        file_path,
        caption=file_name,
        attributes=[DocumentAttributeFilename(file_name)],
        progress_callback=progress_callback
    )


# --- Telegram Bot Event Handlers ---
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond(
        '**Welcome to the Torrent Downloader Bot!**\n\n'
        'Send me a magnet link to start a download. '
        'Features:\n'
        '- Real-time progress updates\n'
        '- Cancellation support with /cancel\n'
        '- Automatic upload to Telegram'
    )


@client.on(events.NewMessage(pattern='magnet:.*'))
async def handle_magnet(event):
    chat_id = event.chat_id
    if chat_id in active_torrents:
        await event.respond("A download is already active in this chat. Please /cancel it or wait for it to complete.")
        return

    magnet_link = event.text
    # Start the download manager in the background
    asyncio.create_task(download_manager(event, magnet_link))


@client.on(events.NewMessage(pattern='/cancel'))
async def cancel_download(event):
    chat_id = event.chat_id
    loop = asyncio.get_event_loop()

    if chat_id in active_torrents:
        handle, message = active_torrents.pop(chat_id)
        # Run the blocking remove call in the executor
        await loop.run_in_executor(None, ses.remove_torrent, handle)
        await message.edit("**Download has been cancelled.**")
    else:
        await event.respond("No active download to cancel.")


# --- Main Function ---
async def main():
    """Main function to start the bot and libtorrent session."""
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)

    print("Bot is starting...")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot has started successfully.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    finally:
        # Cleanly shut down the libtorrent session if needed, though it's
        # often handled automatically on process exit.
        del ses
