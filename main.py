import os
import time
import uuid
import shutil
import asyncio
import threading
import http.server
import socketserver
import libtorrent as lt
from urllib.parse import quote
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# --- Configuration ---
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID"))
SERVER_URL = os.environ.get("SERVER_URL")
DOWNLOAD_PATH = './downloads/'

# --- File Server Configuration ---
FILE_SERVER_HOST = "0.0.0.0"
FILE_SERVER_PORT = 8080
BASE_URL = f"https://{SERVER_URL}"

# --- Bot Globals & Session Setup ---
app = Client(
    "tornet",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# State management dictionaries
pending_downloads = {}  # {unique_id: magnet_link}
active_torrents = {}    # {chat_id: (torrent_handle, asyncio.Task)}
completed_torrents = {} # {info_hash: {'name': str, 'files': list, 'path': str}}


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


# --- File Server ---
def start_file_server():
    """Starts a simple HTTP server in a separate thread that disallows directory listing."""

    class NoListHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if os.path.isdir(self.translate_path(self.path)):
                self.send_error(403, "I can imagine how smart you are")
                return
            super().do_GET()
    
    class MyHandler(NoListHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=DOWNLOAD_PATH, **kwargs)

    with socketserver.TCPServer((FILE_SERVER_HOST, FILE_SERVER_PORT), MyHandler) as httpd:
        print(f"File server started at http://{FILE_SERVER_HOST}:{FILE_SERVER_PORT}")
        print(f"Serving files from: {os.path.abspath(DOWNLOAD_PATH)}")
        httpd.serve_forever()


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

        for _ in range(60):
            if await loop.run_in_executor(None, temp_handle.has_metadata):
                break
            await asyncio.sleep(1)
        else:
            await message.edit_text(
                "❌ **Error:** Timed out fetching metadata. The torrent is likely dead or has no seeds.")
            await loop.run_in_executor(None, ses.remove_torrent, temp_handle)
            return

        ti = await loop.run_in_executor(None, temp_handle.get_torrent_info)
        await loop.run_in_executor(None, ses.remove_torrent, temp_handle)

        files = ti.files()
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

        while handle.is_valid() and not handle.status().is_seeding:
            s = handle.status()
            state_str = ['Queued', 'Checking', 'DL Metadata', 'Downloading', 'Finished', 'Seeding', 'Allocating',
                         'Checking resume'][s.state]

            status_text = (
                f"**🚀 Downloading: ** `{ti.name()}`\n\n"
                f"{progress_bar_str(s.progress)} **{s.progress * 100:.2f}%**\n\n"
                f"**⬇️ Speed:** `{human_readable_size(s.download_rate)}/s`\n"
                f"**⬆️ Speed:** `{human_readable_size(s.upload_rate)}/s`\n"
                f"**📦 Done:** `{human_readable_size(s.total_done)} / {human_readable_size(s.total_wanted)}`\n"
                f"**👤 Peers:** `{s.num_peers}` | **🚦 Status:** `{state_str}`"
            )
            buttons = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{chat_id}")]])

            try:
                await message.edit_text(status_text, reply_markup=buttons)
            except (FloodWait, MessageNotModified):
                pass
            except Exception:
                break
            await asyncio.sleep(5)

        if not handle.is_valid() or handle.status().state != lt.torrent_status.seeding:
            if not asyncio.current_task().cancelled():
                await message.edit_text("❌ **Download Stalled or Failed.**", reply_markup=None)
            return

        # --- DOWNLOAD COMPLETE ---
        await message.edit_text(f"✅ **Download complete!**\n`{ti.name()}`\n\nPreparing direct links...", reply_markup=None)

        info_hash = str(ti.info_hashes().v1)
        save_path = handle.status().save_path
        
        # Store details for later use by buttons
        completed_torrents[info_hash] = {
            'name': ti.name(),
            'files': [f for f in ti.files()],
            'path': save_path
        }

        # Generate direct download links
        download_links = []
        for f in ti.files():
            file_rel_path = f.path
            safe_rel_path = quote(file_rel_path)
            link = f"{BASE_URL}/{safe_rel_path}"
            file_name_display = os.path.basename(f.path)
            download_links.append(f"📄 [{file_name_display}]({link})")
        
        links_text = "\n".join(download_links)
        final_message_text = (
            f"🏁 **Finished!**\n\n"
            f"Files from `{ti.name()}` are ready.\n\n"
            f"**🔗 Direct Download Links:**\n{links_text}"
        )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ Upload Files", callback_data=f"upload_{info_hash}")],
            [InlineKeyboardButton("🗑️ Delete Files", callback_data=f"delete_{info_hash}")]
        ])

        await message.edit_text(final_message_text, reply_markup=buttons, disable_web_page_preview=True)

    except asyncio.CancelledError:
        await message.edit_text("❌ **Download Cancelled.**", reply_markup=None)
    except Exception as e:
        error_message = f"❌ **An unexpected error occurred during download:**\n`{e}`"
        await message.edit_text(error_message, reply_markup=None)
        print(error_message) # For debugging
    finally:
        if chat_id in active_torrents: del active_torrents[chat_id]
        if handle and handle.is_valid():
            pass


class UploadProgressReporter:
    """A stateful class to report upload progress by editing a message."""
    def __init__(self, message, file_name):
        self._message = message
        self._file_name = file_name
        self._loop = asyncio.get_event_loop()
        self._last_update_time = time.time()
        self._last_uploaded_bytes = 0

    def __call__(self, current_bytes, total_bytes):
        current_time = time.time()
        if current_time - self._last_update_time > 4 or current_bytes == total_bytes:
            elapsed_time = current_time - self._last_update_time
            if elapsed_time == 0: elapsed_time = 1
            bytes_since_last = current_bytes - self._last_uploaded_bytes
            speed = bytes_since_last / elapsed_time
            progress = current_bytes / total_bytes
            status_text = (
                f"**📤 Uploading: ** `{self._file_name}`\n\n"
                f"{progress_bar_str(progress)} **{progress * 100:.2f}%**\n\n"
                f"**⬆️ Speed:** `{human_readable_size(speed)}/s`\n"
                f"**📦 Done:** `{human_readable_size(current_bytes)} / {human_readable_size(total_bytes)}`"
            )
            self._loop.create_task(self.edit_message(status_text))
            self._last_update_time = current_time
            self._last_uploaded_bytes = current_bytes

    async def edit_message(self, text):
        """Asynchronously edits the message to avoid blocking."""
        try:
            await self._message.edit_text(text)
        except (FloodWait, MessageNotModified):
            pass
        except Exception as e:
            print(f"Error while editing upload progress: {e}")


def delete_torrent_files(torrent_info):
    """Deletes all files and the containing folder for a torrent."""
    if not torrent_info: return False
    try:
        torrent_base_path = os.path.join(torrent_info['path'], torrent_info['name'])
        
        if os.path.isdir(torrent_base_path):
            shutil.rmtree(torrent_base_path)
            print(f"Deleted directory: {torrent_base_path}")
        elif os.path.isfile(torrent_base_path):
            os.remove(torrent_base_path)
            print(f"Deleted file: {torrent_base_path}")
        else:
            for f in torrent_info['files']:
                file_to_delete = os.path.join(torrent_info['path'], f.path)
                if os.path.exists(file_to_delete):
                    os.remove(file_to_delete)
                    print(f"Deleted file: {file_to_delete}")
        return True
    except Exception as e:
        print(f"Error deleting files for {torrent_info['name']}: {e}")
        return False


# --- Telegram Event Handlers ---
@app.on_message(filters.command("start"))
async def start(client, message):
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Dev",url="https://t.me/feelded")],
            [InlineKeyboardButton("Updates", url="https://t.me/execal")]
        ]
    )
    await message.reply_text('**[⁪⁬⁮⁮⁮⁮](https://i.ibb.co/bR8tDYC5/e838f480e29a.jpg)Welcome to Torrent Uploader!**\n\nSend me a magnet link to begin.', reply_markup=keyboard)


@app.on_message(filters.regex(r"^magnet:.*"))
async def handle_magnet(client, message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("**🚫 Sorry, you are not authorized to use this bot.**")
        return
    if message.chat.id in active_torrents:
        await message.reply_text("**⚠️ A download is already active in this chat. Please wait or cancel it first.**")
        return
    bot_message = await message.reply_text('⏳ **Validating magnet link...**')
    asyncio.create_task(get_torrent_info_task(message.text, bot_message))


@app.on_callback_query()
async def handle_callback(client, callback_query):
    """Handles all button clicks."""
    if callback_query.from_user.id != OWNER_ID:
        await callback_query.answer("You are not authorized to perform this action.", show_alert=True)
        return
        
    data_parts = callback_query.data.split('_', 1)
    action = data_parts[0]
    payload = data_parts[1] if len(data_parts) > 1 else None
    chat_id = callback_query.message.chat.id
    message = callback_query.message

    if action == "start":
        if chat_id in active_torrents:
            await callback_query.answer("Another download is already active!", show_alert=True)
            return
        unique_id = payload
        magnet_link = pending_downloads.pop(unique_id, None)
        if not magnet_link:
            await message.edit_text(
                "**❌ This download link has expired. Please send the magnet link again.**", reply_markup=None)
            return
        await callback_query.answer("🚀 Download initiated...", show_alert=False)
        await message.edit_text("**⏳ Initializing download...**", reply_markup=None)
        asyncio.create_task(download_task(chat_id, magnet_link, message))

    elif action == "cancel":
        if chat_id in active_torrents:
            handle, task = active_torrents.pop(chat_id)
            task.cancel()
            if handle.is_valid():
                ses.remove_torrent(handle)
            await callback_query.answer("❌ Download will be cancelled.", show_alert=True)
        else:
            await callback_query.answer("⚠️ This download is not active.", show_alert=True)

    elif action == "upload":
        info_hash = payload
        torrent_info = completed_torrents.get(info_hash)
        if not torrent_info:
            await message.edit_text("❌ **Error:** Torrent data expired or already processed.", reply_markup=None)
            return

        await callback_query.answer("⬆️ Starting upload...", show_alert=False)
        files_to_upload = sorted(torrent_info['files'], key=lambda f: f.path)

        for f in files_to_upload:
            file_path = os.path.join(torrent_info['path'], f.path)
            file_name = os.path.basename(file_path)
            if os.path.isfile(file_path):
                reporter = UploadProgressReporter(message, file_name)
                await client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=f"`{file_name}`",
                    force_document=True,
                    progress=reporter
                )
        
        delete_torrent_files(torrent_info)
        completed_torrents.pop(info_hash, None)
        
        await message.edit_text(f"✅ **Upload complete!**\nFiles for `{torrent_info['name']}` have been sent and deleted from the server.", reply_markup=None)

    elif action == "delete":
        info_hash = payload
        torrent_info = completed_torrents.pop(info_hash, None)
        if not torrent_info:
            await message.edit_text("❌ **Error:** These files may have already been deleted.", reply_markup=None)
            return

        await callback_query.answer("🗑️ Deleting files...", show_alert=False)
        if delete_torrent_files(torrent_info):
            await message.edit_text(f"✅ **Files Deleted.**\nAll files for `{torrent_info['name']}` have been removed from the server.", reply_markup=None)
        else:
            await message.edit_text(f"❌ **Error:** Could not delete files for `{torrent_info['name']}`.", reply_markup=None)


# --- Alert Handler & Main Function ---
async def alert_handler():
    """A background task that prints session alerts for debugging."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            alerts = await loop.run_in_executor(None, ses.pop_alerts)
            for alert in alerts:
                if alert.what() and "outstanding" not in alert.message():
                    print(f"[Libtorrent Alert: {alert.what()}] {alert.message()}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Alert handler error: {e}")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("FATAL: BOT_TOKEN environment variable not set.")
        return
    if not SERVER_URL or SERVER_URL == "your-server-url.com":
        print("FATAL: SERVER_URL environment variable not set.")
        return

    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)

    server_thread = threading.Thread(target=start_file_server, daemon=True)
    server_thread.start()

    asyncio.get_event_loop().create_task(alert_handler())

    print("Bot has started successfully. Listening for magnet links...")
    app.run()
    print("Bot stopped.")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"A critical error occurred in main: {e}")
