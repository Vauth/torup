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
app = Client("stable_uploader", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_states: Dict[int, Dict[str, Any]] = {}
download_requests: Dict[str, Dict[str, Any]] = {}

# --- Helper Classes & Functions ---

class ProgressTracker:
    """A helper class to neatly track upload/download progress."""
    def __init__(self, message: Message, total_size: int, title: str):
        self.message = message
        self.total_size = total_size
        self.title = title
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.last_uploaded_bytes = 0

    async def aio_progress(self, downloaded_bytes: int):
        """Async progress callback for downloads."""
        now = time.time()
        if now - self.last_update_time > 2:
            elapsed = now - self.start_time
            speed = downloaded_bytes / elapsed if elapsed > 0 else 0
            progress_percent = int((downloaded_bytes / self.total_size) * 100) if self.total_size > 0 else 0
            
            try:
                text = (f"⬇️ **{self.title}**\n\n"
                        f"✅ `{human_readable_size(downloaded_bytes)}` of `{human_readable_size(self.total_size)}`\n"
                        f"🚀 **Progress:** {progress_percent}%\n"
                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                await self.message.edit_text(text)
                self.last_update_time = now
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass # Ignore other edit errors

    async def pyrogram_progress(self, current: int, total: int):
        """Pyrogram-compatible progress callback for uploads."""
        now = time.time()
        if now - self.last_update_time > 2:
            speed = (current - self.last_uploaded_bytes) / (now - self.last_update_time) if now > self.last_update_time else 0
            try:
                text = (f"⬆️ **{self.title}**\n\n"
                        f"✅ `{human_readable_size(current)}` of `{human_readable_size(total)}`\n"
                        f"🚀 **Progress:** {int((current/total)*100)}%\n"
                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                await self.message.edit_text(text)
                self.last_update_time = now
                self.last_uploaded_bytes = current
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

def clean_filename(filename: str) -> str:
    """Remove invalid characters from a filename."""
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
    """Fetches filename, content_type, and size from a URL by inspecting headers."""
    async with aiohttp.ClientSession() as session:
        # Use GET with a timeout to fetch headers, as some servers don't support HEAD
        async with session.get(url, allow_redirects=True, timeout=10) as response:
            response.raise_for_status()
            
            content_disposition = response.headers.get('Content-Disposition')
            filename = ""
            if content_disposition:
                match = re.search(r"filename\*=UTF-8''([\S'\"]+)|filename=\"([^\"]+)\"", content_disposition)
                if match:
                    raw_filename = match.group(1) or match.group(2)
                    filename = unquote(raw_filename)

            if not filename:
                path = urlparse(str(response.url)).path # Convert yarl.URL to string
                filename = unquote(os.path.basename(path)) if path and os.path.basename(path) else None

            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            if not filename:
                ext = mimetypes.guess_extension(content_type.split(';')[0])
                filename = f"download{ext or '.file'}"

            size = int(response.headers.get('Content-Length', 0))
            return clean_filename(filename), content_type, size

async def download_file(url: str, file_path: str, progress_message: Message) -> bool:
    """Asynchronously downloads a file with robust progress updates."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0))
                
                tracker = ProgressTracker(progress_message, total_size, "Downloading...")
                
                async with aiofiles.open(file_path, 'wb') as f:
                    downloaded_size = 0
                    async for data in response.content.iter_chunked(131072): # 128KB chunks
                        await f.write(data)
                        downloaded_size += len(data)
                        await tracker.aio_progress(downloaded_size)
        
        # Final progress update to show 100%
        await progress_message.edit_text("✅ **Download Complete!**\n\nPreparing to upload...")
        return True
    except asyncio.TimeoutError:
        await progress_message.edit_text("❌ **Download failed:** The server took too long to respond.")
        return False
    except Exception as e:
        await progress_message.edit_text(f"❌ **Download failed:**\n`{str(e)}`")
        return False

# --- Bot Handlers ---

@app.on_message(filters.command(["start", "help"]))
async def start_handler(_, message: Message):
    await message.reply_text(
        "**Welcome to the Stable URL Uploader!**\n\n"
        "Send me a direct download link, and I'll handle the rest. This bot is fast, reliable, and easy to use.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def url_handler(_, message: Message):
    url = message.text.strip()
    status_msg = await message.reply_text("🔎 Inspecting URL...", quote=True)
    try:
        filename, content_type, size = await get_file_details_from_url(url)
    except Exception as e:
        await status_msg.edit(f"❌ **Invalid URL or Unreachable Host.**\n`{str(e)}`")
        return

    request_id = str(uuid.uuid4())
    download_requests[request_id] = {"url": url, "filename": filename, "size": size, "content_type": content_type}

    buttons = []
    mime_group = content_type.split('/')[0] if content_type else ""
    if mime_group == 'video':
        buttons.append(InlineKeyboardButton("🎬 Upload as Video", callback_data=f"upload|{request_id}|video"))
    elif mime_group == 'audio':
        buttons.append(InlineKeyboardButton("🎵 Upload as Audio", callback_data=f"upload|{request_id}|audio"))
    
    buttons.append(InlineKeyboardButton("📎 Upload as Document", callback_data=f"upload|{request_id}|document"))
    keyboard = [buttons, [InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{request_id}")]]
    
    file_info = f"📄 **Filename:** `{filename}`\n"
    if size > 0:
        file_info += f"📦 **Size:** `{human_readable_size(size)}`\n"
    file_info += f"📝 **Type:** `{content_type}`"

    await status_msg.edit(f"✅ **URL Inspected!**\n\n{file_info}\n\nHow would you like to upload this file?", reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query(filters.regex(r'^(rename|cancel_rename)\|'))
async def rename_callback(_, callback_query):
    action, request_id = callback_query.data.split('|', 1)

    if action == "cancel_rename":
        # Restore the original message if cancel is hit
        original_details = download_requests.get(request_id)
        if not original_details:
            await callback_query.message.edit("This request has expired.")
            return
        
        # This part re-builds the original message to restore it
        filename, content_type, size = original_details['filename'], original_details['content_type'], original_details['size']
        buttons = []
        mime_group = content_type.split('/')[0] if content_type else ""
        if mime_group == 'video':
            buttons.append(InlineKeyboardButton("🎬 Upload as Video", callback_data=f"upload|{request_id}|video"))
        elif mime_group == 'audio':
            buttons.append(InlineKeyboardButton("🎵 Upload as Audio", callback_data=f"upload|{request_id}|audio"))
        buttons.append(InlineKeyboardButton("📎 Upload as Document", callback_data=f"upload|{request_id}|document"))
        keyboard = [buttons, [InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{request_id}")]]
        file_info = f"📄 **Filename:** `{filename}`\n📦 **Size:** `{human_readable_size(size)}`\n📝 **Type:** `{content_type}`"
        await callback_query.message.edit(f"✅ **URL Inspected!**\n\n{file_info}\n\nHow would you like to upload this file?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- If action is "rename" ---
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    await callback_query.message.edit(
        "✏️ Send me the new filename, including the extension.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rename|{request_id}")]]))
    user_states[callback_query.from_user.id] = {"request_id": request_id, "original_message_id": callback_query.message.id}
    await callback_query.answer()

@app.on_message(filters.private & ~filters.command() & ~filters.regex(r'https?://'))
async def filename_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states or not message.text:
        return
    
    state = user_states[user_id]
    request_id = state["request_id"]
    if request_id not in download_requests:
        await message.reply_text("Your previous request has expired. Please send the URL again.", quote=True)
        del user_states[user_id]
        return

    new_filename = clean_filename(message.text.strip())
    download_requests[request_id]['filename'] = new_filename
    
    # Get the original message to restore its keyboard
    try:
        original_message = await client.get_messages(user_id, state["original_message_id"])
        await message.delete()
        await original_message.edit_text(
            f"✅ **Filename updated to:** `{new_filename}`\n\nPlease choose an upload option.",
            reply_markup=original_message.reply_markup
        )
    except Exception: # If original message is deleted
        await message.reply_text(f"✅ **Filename updated to:** `{new_filename}`\n\nBut I couldn't edit the original message. Please send the URL again.")
    finally:
        del user_states[user_id]

@app.on_callback_query(filters.regex(r'^upload\|'))
async def upload_callback(client: Client, callback_query):
    _, request_id, upload_mode = callback_query.data.split('|', 2)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return
    
    msg = await callback_query.message.edit_text("⏳ Queued for download...")
    
    request_data = download_requests[request_id]
    url, filename = request_data["url"], request_data["filename"]
    
    temp_dir = os.path.join(os.getcwd(), "temp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = os.path.join(temp_dir, f"{request_id}_{filename}")
    
    try:
        if not await download_file(url, temp_file, msg):
            return # Error message is sent by download_file
        
        file_size = os.path.getsize(temp_file)
        caption = f"`{filename}`"
        
        upload_tracker = ProgressTracker(msg, file_size, "Uploading...")
        
        if upload_mode == 'video':
            await client.send_video(
                chat_id=callback_query.message.chat.id, video=temp_file, caption=caption, file_name=filename,
                supports_streaming=True, progress=upload_tracker.pyrogram_progress
            )
        elif upload_mode == 'audio':
            await client.send_audio(
                chat_id=callback_query.message.chat.id, audio=temp_file, caption=caption, file_name=filename,
                progress=upload_tracker.pyrogram_progress
            )
        else: # 'document'
            await client.send_document(
                chat_id=callback_query.message.chat.id, document=temp_file, caption=caption, file_name=filename,
                progress=upload_tracker.pyrogram_progress
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ **Upload Failed:**\n`{str(e)}`")
    finally:
        # Guaranteed cleanup
        if os.path.exists(temp_file):
            os.remove(temp_file)
        if request_id in download_requests:
            del download_requests[request_id]

if __name__ == "__main__":
    print("Stable Uploader Bot is running...")
    app.run()
