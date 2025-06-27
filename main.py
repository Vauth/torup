import asyncio
import os
import time
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename
import aiotorrent

# --- Configuration ---
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DOWNLOAD_PATH = './downloads'

# --- Globals ---
client = TelegramClient('tornet', API_ID, API_HASH)
active_torrents = {}  # {chat_id: (torrent_task, message)}


# --- Helper Functions ---
def human_readable_size(size, decimal_places=2):
    """Converts bytes to a human-readable format."""
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"


async def progress_bar(current, total, bar_length=20):
    """Creates a textual progress bar."""
    fraction = current / total
    arrow = int(fraction * bar_length - 1) * '=' + '>'
    padding = (bar_length - len(arrow)) * ' '
    return f"[{arrow}{padding}] {fraction:.2%}"


# --- Torrent Management ---
async def download_torrent(magnet_link, event, message):
    """Handles the torrent download process."""
    chat_id = event.chat_id
    try:
        torrent = await aiotorrent.Torrent.from_magnet(magnet_link, download_path=DOWNLOAD_PATH)
        active_torrents[chat_id] = (asyncio.current_task(), message)

        while not torrent.is_completed():
            status = torrent.status()
            progress = status['progress']
            download_speed = human_readable_size(status['download_rate']) + '/s'
            upload_speed = human_readable_size(status['upload_rate']) + '/s'
            total_size = human_readable_size(status['total_size'])
            downloaded = human_readable_size(status['total_downloaded'])

            bar = await progress_bar(status['total_downloaded'], status['total_size'])

            progress_text = (
                f"**Downloading:** `{torrent.name}`\n"
                f"{bar}\n"
                f"**Progress:** {progress:.2f}%\n"
                f"**Downloaded:** {downloaded} / {total_size}\n"
                f"**Speed:** ⬇️ {download_speed} / ⬆️ {upload_speed}\n"
                f"**Peers:** {status['num_peers']}\n\n"
                f"To cancel, send /cancel"
            )

            await message.edit(progress_text)
            await asyncio.sleep(5)  # Update interval

        await message.edit(f"**Download complete!**\n\n`{torrent.name}`\n\nNow uploading to Telegram...")

        # Upload the downloaded file(s)
        for file in torrent.files:
            file_path = os.path.join(DOWNLOAD_PATH, file.path)

            # Check if it's a file and not a directory
            if os.path.isfile(file_path):
                await client.send_file(
                    chat_id,
                    file_path,
                    caption=file.path,
                    attributes=[DocumentAttributeFilename(file.path)],
                    progress_callback=lambda current, total: upload_progress(current, total, message, file.path)
                )

        await message.edit("**All files uploaded successfully!**")

    except aiotorrent.TorrentError as e:
        await message.edit(f"**Error:** {e}")
    except asyncio.CancelledError:
        await message.edit("**Download cancelled.**")
    finally:
        if chat_id in active_torrents:
            del active_torrents[chat_id]
        if 'torrent' in locals() and torrent:
            await torrent.stop()


async def upload_progress(current, total, message, filename):
    """Updates the message with upload progress."""
    bar = await progress_bar(current, total)
    await message.edit(f"**Uploading:** `{filename}`\n{bar}")


# --- Telegram Bot Event Handlers ---
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    """Handler for the /start command."""
    await event.respond(
        'Hello! I am a lightweight torrent downloader bot.\n\nSend me a magnet link to start downloading.')


@client.on(events.NewMessage(pattern='magnet:.*'))
async def handle_magnet(event):
    """Handler for magnet links."""
    chat_id = event.chat_id
    if chat_id in active_torrents:
        await event.respond(
            "A download is already in progress in this chat. Please wait for it to finish or cancel it.")
        return

    magnet_link = event.text
    message = await event.respond('Starting download...')

    # Start the download in a background task
    asyncio.create_task(download_torrent(magnet_link, event, message))


@client.on(events.NewMessage(pattern='/cancel'))
async def cancel_download(event):
    """Handler for the /cancel command."""
    chat_id = event.chat_id
    if chat_id in active_torrents:
        torrent_task, message = active_torrents[chat_id]
        torrent_task.cancel()
    else:
        await event.respond("No active download to cancel.")


# --- Main Function ---
async def main():
    """Main function to start the bot."""
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)

    print("Bot is starting...")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot has started successfully.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
