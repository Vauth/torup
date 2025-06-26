import os
import re
import mimetypes
import uuid
import time
import asyncio
import shlex
from urllib.parse import urlparse, unquote
from typing import Dict, Any, Tuple

import aiohttp
import aiofiles
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import RPCError, FloodWait
from pyrogram import enums

# --- Configuration ---
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# --- Global State ---
app = Client("ultimate_uploader", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_states: Dict[int, Dict[str, Any]] = {}
download_requests: Dict[str, Dict[str, Any]] = {}
FFMPEG_AVAILABLE = False

# --- Helper Functions ---
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

async def check_ffmpeg():
    """Check if FFmpeg is installed and accessible."""
    global FFMPEG_AVAILABLE
    try:
        process = await asyncio.create_subprocess_shell(
            'ffmpeg -version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        FFMPEG_AVAILABLE = process.returncode == 0
    except FileNotFoundError:
        FFMPEG_AVAILABLE = False
    status = "ENABLED" if FFMPEG_AVAILABLE else "DISABLED (install FFmpeg for thumbnails and metadata)"
    print(f"FFmpeg support is {status}.")

async def get_file_details_from_url(url: str) -> Tuple[str, str, int]:
    """Fetches filename, content_type, and size from a URL by inspecting headers."""
    async with aiohttp.ClientSession() as session:
        async with session.head(url, allow_redirects=True) as response:
            response.raise_for_status()
            
            content_disposition = response.headers.get('Content-Disposition')
            filename = ""
            if content_disposition:
                # Use regex to find filename* or filename=
                match = re.search(r"filename\*=UTF-8''([\S'\"]+)|filename=\"([^\"]+)\"", content_disposition)
                if match:
                    # Prefer the UTF-8 version if it exists
                    raw_filename = match.group(1) or match.group(2)
                    filename = unquote(raw_filename)

            if not filename:
                path = urlparse(response.url).path
                filename = unquote(os.path.basename(path)) if path else None

            content_type = response.headers.get('Content-Type')
            if not filename:
                # Guess extension from MIME type
                ext = mimetypes.guess_extension(content_type.split(';')[0]) if content_type else ''
                filename = f"download{ext or '.file'}"

            size = int(response.headers.get('Content-Length', 0))
            return clean_filename(filename), content_type, size

async def get_video_metadata(file_path: str) -> Dict[str, Any]:
    """Extracts video metadata using FFmpeg."""
    if not FFMPEG_AVAILABLE: return {}
    try:
        cmd = f"ffprobe -v error -show_format -show_streams -of default=noprint_wrappers=1 {shlex.quote(file_path)}"
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode != 0: return {}
        
        output = stdout.decode('utf-8', errors='ignore')
        metadata = {'duration': 0, 'width': 0, 'height': 0}
        
        for line in output.split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                if key == 'duration' and value != 'N/A': metadata['duration'] = int(float(value))
                elif key == 'width': metadata['width'] = int(value)
                elif key == 'height': metadata['height'] = int(value)
        return metadata if all(metadata.values()) else {}
    except Exception as e:
        print(f"FFprobe error: {e}")
        return {}

async def generate_thumbnail(video_path: str, thumb_path: str) -> bool:
    """Generates a thumbnail from the video."""
    if not FFMPEG_AVAILABLE: return False
    try:
        cmd = f"ffmpeg -i {shlex.quote(video_path)} -ss 00:00:05 -vframes 1 -vf scale=320:-1 {shlex.quote(thumb_path)}"
        process = await asyncio.create_subprocess_shell(cmd, stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        await process.communicate()
        return os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception as e:
        print(f"Thumbnail generation error: {e}")
        return False

async def download_file(url: str, file_path: str, progress_message: Message) -> bool:
    """Asynchronously downloads a file with progress updates."""
    last_update_time = time.time()
    downloaded_since_last_update = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                total_size = int(response.headers.get('Content-Length', 0))
                
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
                                        f"📄 `__{os.path.basename(file_path)}__`\n\n"
                                        f"✅ `{human_readable_size(downloaded_size)}` of `{human_readable_size(total_size)}`\n"
                                        f"🚀 **Progress:** {progress_percent}%\n"
                                        f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
                                await progress_message.edit_text(text)
                            except Exception: pass
                            
                            last_update_time, downloaded_since_last_update = current_time, 0
        return True
    except Exception as e:
        await progress_message.edit_text(f"❌ **Download failed:**\n`{e}`")
        return False

# --- Bot Handlers ---

@app.on_message(filters.command(["start", "help"]))
async def start_handler(_, message: Message):
    await message.reply_text(
        "**Welcome to the Ultimate URL Uploader!**\n\n"
        "Send me a direct download link, and I'll handle the rest. This bot is fast, reliable, and packed with features.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def url_handler(_, message: Message):
    url = message.text.strip()
    status_msg = await message.reply_text("🔎 Inspecting URL...", quote=True)
    try:
        filename, content_type, size = await get_file_details_from_url(url)
    except Exception as e:
        await status_msg.edit(f"❌ **Invalid URL or Unreachable Host.**\n`{e}`")
        return

    request_id = str(uuid.uuid4())
    download_requests[request_id] = {"url": url, "filename": filename, "size": size}

    # Build dynamic keyboard based on content type
    buttons = []
    mime_group = content_type.split('/')[0] if content_type else ""
    if mime_group == 'video':
        buttons.append(InlineKeyboardButton("🎬 Upload as Video", callback_data=f"upload|{request_id}|video"))
    elif mime_group == 'audio':
        buttons.append(InlineKeyboardButton("🎵 Upload as Audio", callback_data=f"upload|{request_id}|audio"))
    
    buttons.append(InlineKeyboardButton("📎 Upload as Document", callback_data=f"upload|{request_id}|document"))
    keyboard = [buttons, [InlineKeyboardButton("✏️ Rename File", callback_data=f"rename|{request_id}")]]
    
    file_info = f"📄 **Filename:** `{filename}`\n"
    if size > 0:
        file_info += f"📦 **Size:** `{human_readable_size(size)}`\n"
    if content_type:
        file_info += f"📝 **Type:** `{content_type}`"

    await status_msg.edit(f"✅ **URL Inspected!**\n\n{file_info}\n\nHow would you like to upload this file?", reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query(filters.regex(r'^rename\|'))
async def rename_callback(_, callback_query):
    _, request_id = callback_query.data.split('|', 1)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    await callback_query.message.edit_text("✏️ Send me the new filename, including the extension if you wish to change it.")
    user_states[callback_query.from_user.id] = {"request_id": request_id, "original_message_id": callback_query.message.id}
    await callback_query.answer()

@app.on_message(filters.private & ~filters.command(["start", "help"]) & ~filters.regex(r'https?://[^\s]+'))
async def filename_handler(_, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states or not message.text:
        return
    
    state = user_states[user_id]
    request_id = state["request_id"]
    if request_id not in download_requests:
        await message.reply_text("Your previous request has expired. Please send the URL again.")
        del user_states[user_id]
        return

    new_filename = clean_filename(message.text.strip())
    download_requests[request_id]['filename'] = new_filename
    
    # Restore the original options with the new filename
    original_message = await app.get_messages(user_id, state["original_message_id"])
    original_keyboard = original_message.reply_markup
    
    await message.delete() # Clean up the user's message
    await original_message.edit_text(
        f"✅ **Filename updated to:** `{new_filename}`\n\nPlease choose an upload option.",
        reply_markup=original_keyboard
    )
    del user_states[user_id]

async def progress_func(current, total, msg, start_time, last_uploaded):
    now = time.time()
    if now - last_uploaded['time'] > 2:
        elapsed = now - start_time
        speed = current / elapsed
        text = (f"⬆️ **Uploading...**\n"
                f"✅ `{human_readable_size(current)}` of `{human_readable_size(total)}`\n"
                f"🚀 **Progress:** {int((current/total)*100)}%\n"
                f"⚡️ **Speed:** `{human_readable_speed(speed)}`")
        try:
            await msg.edit_text(text)
            last_uploaded['time'] = now
        except Exception:
            pass

@app.on_callback_query(filters.regex(r'^upload\|'))
async def upload_callback(client: Client, callback_query):
    _, request_id, upload_mode = callback_query.data.split('|', 2)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return
    
    await callback_query.answer("Starting download...")
    request_data = download_requests[request_id]
    url, filename = request_data["url"], request_data["filename"]
    
    msg = await callback_query.message.edit_text(f"⏳ **Preparing to download...**\n`{filename}`")
    
    temp_dir = os.path.join(os.getcwd(), "temp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = os.path.join(temp_dir, f"{request_id}_{filename}")

    if not await download_file(url, temp_file, msg):
        if request_id in download_requests: del download_requests[request_id]
        return

    await msg.edit_text("📥 Download complete. Preparing to upload...")
    
    file_size = os.path.getsize(temp_file)
    caption = f"`{filename}`"
    start_time = time.time()
    last_uploaded = {'time': start_time}

    try:
        if upload_mode == 'video':
            metadata = await get_video_metadata(temp_file)
            thumb_path = os.path.join(temp_dir, f"{request_id}.jpg")
            thumb = thumb_path if await generate_thumbnail(temp_file, thumb_path) else None
            await client.send_video(
                chat_id=callback_query.message.chat.id, video=temp_file, caption=caption, file_name=filename,
                duration=metadata.get('duration', 0), width=metadata.get('width', 0), height=metadata.get('height', 0),
                thumb=thumb, supports_streaming=True, progress=progress_func, progress_args=(msg, start_time, last_uploaded)
            )
            if thumb: os.remove(thumb)
        elif upload_mode == 'audio':
            await client.send_audio(
                chat_id=callback_query.message.chat.id, audio=temp_file, caption=caption, file_name=filename,
                progress=progress_func, progress_args=(msg, start_time, last_uploaded)
            )
        else: # 'document'
            await client.send_document(
                chat_id=callback_query.message.chat.id, document=temp_file, caption=caption, file_name=filename,
                progress=progress_func, progress_args=(msg, start_time, last_uploaded)
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ **Upload Failed:**\n`{e}`")
    finally:
        if os.path.exists(temp_file): os.remove(temp_file)
        if request_id in download_requests: del download_requests[request_id]

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(check_ffmpeg())
    print("Ultimate Uploader Bot is running...")
    app.run()
