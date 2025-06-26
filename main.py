import os
import re
import mimetypes
import uuid
import time
import asyncio
from urllib.parse import urlparse, unquote
from typing import Dict, Any, Tuple

# Async/HTTP Libraries
import aiohttp
import aiofiles

# Pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import RPCError, FloodWait
from pyrogram import enums

# --- Configuration ---
# It is highly recommended to use environment variables for security
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# --- Global State ---
app = Client("ultimate_stable_uploader", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_states: Dict[int, Dict[str, Any]] = {}
download_requests: Dict[str, Dict[str, Any]] = {}

# --- Helper Classes & Functions ---

class ProgressTracker:
    """A helper class to neatly track upload/download progress with clear state."""
    def __init__(self, message: Message):
        self._message = message
        self._start_time = time.time()
        self._last_update_time = self._start_time
        self._last_uploaded_bytes = 0

    async def update(self, current_bytes: int, total_bytes: int, title: str):
        now = time.time()
        if now - self._last_update_time > 2:  # Update every 2 seconds
            speed = (current_bytes - self._last_uploaded_bytes) / (now - self._last_update_time) if now > self._last_update_time else 0
            
            # Build the progress message
            if total_bytes > 0:
                percent = int((current_bytes / total_bytes) * 100)
                progress_bar = f"✅ `{human_readable_size(current_bytes)}` of `{human_readable_size(total_bytes)}`"
                progress_details = f"🚀 **Progress:** {percent}%"
            else:
                progress_bar = f"✅ `{human_readable_size(current_bytes)}` downloaded"
                progress_details = "Total size unknown."

            try:
                text = (f"{title}\n\n"
                        f"{progress_bar}\n"
                        f"{progress_details}\n"
                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                await self._message.edit_text(text)
                self._last_update_time = now
                self._last_uploaded_bytes = current_bytes
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

def clean_filename(filename: str) -> str:
    """Remove invalid filesystem characters from a filename."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename).strip()

def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0: break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def human_readable_speed(speed, decimal_places=2):
    for unit in ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']:
        if speed < 1024.0: break
        speed /= 1024.0
    return f"{speed:.{decimal_places}f} {unit}"

async def get_file_details_from_url(url: str) -> Tuple[str, str, int]:
    """Fetches filename, content_type, and size by inspecting URL headers."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=True, timeout=20) as response:
            response.raise_for_status()
            
            content_disposition = response.headers.get('Content-Disposition')
            filename = ""
            if content_disposition:
                match = re.search(r"filename\*=UTF-8''([\S'\"]+)|filename=\"([^\"]+)\"", content_disposition)
                if match:
                    filename = unquote(match.group(1) or match.group(2))

            if not filename:
                path = urlparse(str(response.url)).path
                if path and os.path.basename(path):
                    filename = unquote(os.path.basename(path))

            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            if not filename:
                ext = mimetypes.guess_extension(content_type.split(';')[0])
                filename = f"download{ext or '.file'}"

            size = int(response.headers.get('Content-Length', 0))
            return clean_filename(filename), content_type, size

async def download_file_with_realtime_progress(url: str, file_path: str, progress_message: Message) -> bool:
    """Downloads a file with robust, real-time progress updates."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0))
                tracker = ProgressTracker(progress_message)
                
                async with aiofiles.open(file_path, 'wb') as f:
                    downloaded_size = 0
                    await tracker.update(0, total_size, "⬇️ **Downloading...**")
                    async for data in response.content.iter_chunked(131072): # 128KB chunks
                        await f.write(data)
                        downloaded_size += len(data)
                        await tracker.update(downloaded_size, total_size, "⬇️ **Downloading...**")
        return True
    except asyncio.TimeoutError:
        await progress_message.edit_text("❌ **Download Failed:** The server took too long to respond.")
        return False
    except Exception as e:
        await progress_message.edit_text(f"❌ **Download Failed:**\n`{str(e)}`")
        return False

# --- Bot Handlers ---

@app.on_message(filters.command(["start", "help"]))
async def start_handler(_, message: Message):
    await message.reply_text(
        "**Welcome to the Ultimate Stable Uploader!**\n\n"
        "This bot is designed to be fast, stable, and easy to use. Send me a direct download link to begin.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def url_handler(_, message: Message):
    url = message.text.strip()
    status_msg = await message.reply_text("🔎 **Inspecting URL...**", quote=True)
    try:
        filename, content_type, size = await get_file_details_from_url(url)
    except Exception as e:
        await status_msg.edit(f"❌ **Invalid URL or Unreachable Host.**\n\n`{str(e)}`")
        return

    request_id = str(uuid.uuid4())
    download_requests[request_id] = {"url": url, "filename": filename, "size": size, "content_type": content_type}

    buttons = []
    # Use .startswith() for robust MIME type checking
    if content_type.startswith('video/'):
        buttons.append(InlineKeyboardButton("🎬 Upload as Video", callback_data=f"upload|{request_id}|video"))
    elif content_type.startswith('audio/'):
        buttons.append(InlineKeyboardButton("🎵 Upload as Audio", callback_data=f"upload|{request_id}|audio"))
    
    buttons.append(InlineKeyboardButton("📎 Upload as Document", callback_data=f"upload|{request_id}|document"))
    keyboard = [buttons, [InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{request_id}")]]
    
    file_info = f"📄 **Filename:** `{filename}`\n"
    if size > 0:
        file_info += f"📦 **Size:** `{human_readable_size(size)}`\n"
    file_info += f"📝 **Type:** `{content_type}`"

    await status_msg.edit(f"✅ **URL Inspected!**\n\n{file_info}\n\nHow would you like to upload this file?", reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query(filters.regex(r'^(rename|cancel_rename)\|'))
async def rename_or_cancel_handler(_, callback_query: Message):
    action, request_id = callback_query.data.split('|', 1)
    
    if action == "cancel_rename":
        # Logic to restore the original state if rename is cancelled
        original_details = download_requests.get(request_id)
        if not original_details:
            await callback_query.message.edit("This request has expired.")
            return
        
        filename, content_type, size = original_details['filename'], original_details['content_type'], original_details['size']
        buttons = []
        if content_type.startswith('video/'):
            buttons.append(InlineKeyboardButton("🎬 Upload as Video", callback_data=f"upload|{request_id}|video"))
        buttons.append(InlineKeyboardButton("📎 Upload as Document", callback_data=f"upload|{request_id}|document"))
        keyboard = [buttons, [InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{request_id}")]]
        file_info = f"📄 **Filename:** `{filename}`\n📦 **Size:** `{human_readable_size(size)}`\n📝 **Type:** `{content_type}`"
        await callback_query.message.edit(f"✅ **URL Inspected!**\n\n{file_info}\n\nHow would you like to upload this file?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    await callback_query.message.edit(
        "✏️ Send me the new filename, including the extension.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rename|{request_id}")]]))
    user_states[callback_query.from_user.id] = {"request_id": request_id, "original_message_id": callback_query.message.id}
    await callback_query.answer()

@app.on_message(filters.private & filters.text)
async def filename_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states: return
    
    state = user_states.pop(user_id) # Consume state after use
    request_id = state["request_id"]
    if request_id not in download_requests:
        await message.reply_text("Your previous request has expired. Please send the URL again.", quote=True)
        return

    new_filename = clean_filename(message.text.strip())
    download_requests[request_id]['filename'] = new_filename
    
    try:
        original_message = await client.get_messages(user_id, state["original_message_id"])
        await message.delete()
        await original_message.edit_text(
            f"✅ **Filename updated to:** `{new_filename}`\n\nPlease choose an upload option.",
            reply_markup=original_message.reply_markup
        )
    except Exception:
        await message.reply_text(f"✅ **Filename updated to:** `{new_filename}`\n\nCould not edit the original message. Please restart by sending the URL.")

@app.on_callback_query(filters.regex(r'^upload\|'))
async def upload_callback(client: Client, callback_query: Message):
    _, request_id, upload_mode = callback_query.data.split('|', 2)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return
    
    msg = await callback_query.message.edit_text("⏳ **Queued for download...**")
    
    request_data = download_requests[request_id]
    url, filename = request_data["url"], request_data["filename"]
    
    temp_dir = os.path.join(os.getcwd(), "temp")
    os.makedirs(temp_dir, exist_ok=True)
    # Use only the request_id for the temp file to prevent any naming conflicts
    temp_file_path = os.path.join(temp_dir, request_id)
    
    try:
        if not await download_file_with_realtime_progress(url, temp_file_path, msg):
            return
        
        await msg.edit_text("⬆️ **Preparing to upload...**")
        file_size = os.path.getsize(temp_file_path)
        caption = f"`{filename}`"
        
        upload_tracker = ProgressTracker(msg)
        
        # Define a simplified progress function for Pyrogram
        async def progress(current, total):
            await upload_tracker.update(current, total, "⬆️ **Uploading...**")
            
        if upload_mode == 'video':
            await client.send_video(
                chat_id=callback_query.message.chat.id, video=temp_file_path, caption=caption, file_name=filename,
                supports_streaming=True, progress=progress
            )
        elif upload_mode == 'audio':
            await client.send_audio(
                chat_id=callback_query.message.chat.id, audio=temp_file_path, caption=caption, file_name=filename,
                progress=progress
            )
        else: # 'document'
            await client.send_document(
                chat_id=callback_query.message.chat.id, document=temp_file_path, caption=caption, file_name=filename,
                progress=progress
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ **Upload Failed:**\n`{str(e)}`")
    finally:
        # Guaranteed cleanup of temporary files and session data
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if request_id in download_requests:
            del download_requests[request_id]

if __name__ == "__main__":
    print("Ultimate Stable Uploader Bot is running...")
    app.run()
