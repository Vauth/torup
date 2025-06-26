import os
import re
import mimetypes
import uuid
import time
import asyncio
from urllib.parse import urlparse, unquote
from typing import Dict, Any

# Async/HTTP Libraries
import aiohttp
import aiofiles

# Pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import RPCError, FloodWait
from pyrogram import enums

# --- Bot Configuration ---
API_ID = 8138160  # Replace with your API ID
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"  # Replace with your API Hash
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # It's recommended to use environment variables

# Initialize the bot
app = Client("uploader_no_ffmpeg", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory storage for user states and download data
user_states: Dict[int, Dict[str, Any]] = {}
download_requests: Dict[str, Dict[str, str]] = {}

# --- Helper Functions ---

def get_filename_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.path:
            return os.path.basename(unquote(parsed.path))
    except Exception as e:
        print(f"Error parsing filename: {e}")
    return "downloaded_file"

def get_file_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()

def get_mime_type(file_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or "application/octet-stream"

def clean_filename(filename: str) -> str:
    return re.sub(r'[^\w\-. ]', '', filename)

def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def human_readable_speed(speed_bytes_per_sec, decimal_places=2):
    speed = speed_bytes_per_sec
    for unit in ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']:
        if speed < 1024.0:
            break
        speed /= 1024.0
    return f"{speed:.{decimal_places}f} {unit}"

async def download_file(url: str, file_path: str, progress_message: Message) -> bool:
    """Asynchronously downloads a file with progress updates."""
    filename = os.path.basename(file_path)
    last_update_time = time.time()
    downloaded_since_last_update = 0

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('content-length', 0))
                
                async with aiofiles.open(file_path, 'wb') as f:
                    downloaded_size = 0
                    async for data in response.content.iter_chunked(131072): # 128KB chunks
                        await f.write(data)
                        downloaded_size += len(data)
                        downloaded_since_last_update += len(data)
                        current_time = time.time()

                        if current_time - last_update_time > 2:
                            elapsed = current_time - last_update_time
                            speed = downloaded_since_last_update / elapsed
                            progress_percent = int((downloaded_size / total_size) * 100) if total_size else 0
                            
                            try:
                                text = (f"⬇️ **Downloading...**\n\n"
                                        f"📄 `__{filename}__`\n\n"
                                        f"✅ `{human_readable_size(downloaded_size)}` of `{human_readable_size(total_size)}`\n"
                                        f"🚀 **Progress:** {progress_percent}%\n"
                                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                                await progress_message.edit_text(text, parse_mode=enums.ParseMode.MARKDOWN)
                            except FloodWait as e:
                                await asyncio.sleep(e.value)
                            except Exception: pass
                            
                            last_update_time = current_time
                            downloaded_since_last_update = 0
        return True
    except Exception as e:
        await progress_message.edit_text(f"❌ **Download failed:**\n`{e}`")
        return False

# --- Bot Handlers ---

@app.on_message(filters.command(["start", "help"]))
async def start_handler(_, message: Message):
    """Handles /start and /help commands."""
    await message.reply_text(
        "**Welcome to the Advanced URL Uploader Bot!**\n\n"
        "Send me a direct download link and I'll handle the rest.\n\n"
        "**Features:**\n"
        "- Blazing fast async downloads\n"
        "- Live progress and speed indicators\n"
        "- Intelligent media handling (videos are uploaded as videos)\n"
        "- Option to rename files before uploading",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def url_handler(_, message: Message):
    """Handles URL messages."""
    url = message.text.strip()
    filename = get_filename_from_url(url)
    if not filename:
        await message.reply_text("Could not determine a filename from the URL.")
        return

    request_id = str(uuid.uuid4())
    download_requests[request_id] = {"url": url, "filename": clean_filename(filename)}

    await message.reply_text(
        f"🔗 **URL Received**\n📄 Detected filename: `{clean_filename(filename)}`\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬆️ Upload with this name", callback_data=f"upload|{request_id}")],
            [InlineKeyboardButton("✏️ Rename before upload", callback_data=f"rename|{request_id}")]
        ]),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex(r'^rename\|'))
async def rename_callback(_, callback_query):
    """Handles rename request."""
    _, request_id = callback_query.data.split('|', 1)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    filename = download_requests[request_id]["filename"]
    await callback_query.message.edit_text(
        f"✏️ **Rename File**\n\nSend me the new name for the file (extension is optional).\n\n"
        f"Current filename: `{filename}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )
    user_states[callback_query.from_user.id] = {"request_id": request_id}
    await callback_query.answer()

@app.on_message(filters.private & ~filters.command(["start", "help"]) & ~filters.regex(r'https?://[^\s]+'))
async def filename_handler(_, message: Message):
    """Handles filename input after a rename request."""
    user_id = message.from_user.id
    if user_id not in user_states or not message.text:
        await message.reply_text("Please send a URL first to start the process.")
        return

    request_id = user_states[user_id]["request_id"]
    if request_id not in download_requests:
        await message.reply_text("Your previous request has expired. Please send the URL again.")
        del user_states[user_id]
        return

    original_ext = get_file_extension(download_requests[request_id]["filename"])
    new_name_base = os.path.splitext(message.text.strip())[0] # remove extension if user provides it
    new_filename = f"{clean_filename(new_name_base)}{original_ext}"
    
    download_requests[request_id]["filename"] = new_filename

    await message.reply_text(
        f"✅ **Filename Updated!**\n\nNew filename: `{new_filename}`\n\nClick the button to start the upload.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⬆️ Upload as {new_filename}", callback_data=f"upload|{request_id}")]]),
        parse_mode=enums.ParseMode.MARKDOWN
    )
    del user_states[user_id]


@app.on_callback_query(filters.regex(r'^upload\|'))
async def upload_callback(client: Client, callback_query):
    """Handles the final file upload process."""
    _, request_id = callback_query.data.split('|', 1)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return
    
    await callback_query.answer("Starting process...")
    request_data = download_requests[request_id]
    url, filename = request_data["url"], request_data["filename"]
    
    msg = await callback_query.message.edit_text(f"⏳ **Preparing to download...**\n`{filename}`")
    
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = os.path.join(temp_dir, f"{request_id}_{filename}")

    if not await download_file(url, temp_file, msg):
        if request_id in download_requests: del download_requests[request_id]
        return

    await msg.edit_text("Processing file for upload...")
    mime_type = get_mime_type(temp_file)
    file_size = os.path.getsize(temp_file)
    caption = f"**File:** `{filename}`\n**Size:** `{human_readable_size(file_size)}`"
    
    last_update_time = time.time()
    last_uploaded_bytes = 0

    async def progress(current, total):
        nonlocal last_update_time, last_uploaded_bytes
        current_time = time.time()
        if current_time - last_update_time > 2:
            elapsed = current_time - last_update_time
            speed = (current - last_uploaded_bytes) / elapsed
            try:
                text = (f"⬆️ **Uploading...**\n\n"
                        f"✅ `{human_readable_size(current)}` of `{human_readable_size(total)}`\n"
                        f"🚀 **Progress:** {int((current/total)*100)}%\n"
                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                await msg.edit_text(text, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception: pass
            last_update_time = current_time
            last_uploaded_bytes = current

    try:
        if mime_type.startswith("video/"):
            await client.send_video(
                chat_id=callback_query.message.chat.id,
                video=temp_file,
                caption=caption,
                file_name=filename,
                progress=progress,
                supports_streaming=True # Good practice for videos
            )
        # Add elif for audio/images here if desired
        # elif mime_type.startswith("audio/"):
        #     await client.send_audio(...)
        else: # Default to document
            await client.send_document(
                chat_id=callback_query.message.chat.id,
                document=temp_file,
                caption=caption,
                file_name=filename,
                progress=progress
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ **Upload Failed:**\n`{e}`")
    finally:
        if os.path.exists(temp_file): os.remove(temp_file)
        if request_id in download_requests: del download_requests[request_id]

if __name__ == "__main__":
    print("Advanced Uploader Bot (No-FFmpeg Version) is running...")
    app.run()
