import os
import re
import mimetypes
import uuid
from urllib.parse import urlparse
from typing import Dict, Any, Tuple, Optional
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import RPCError
from pyrogram import enums
import requests
from tqdm import tqdm

# Bot configuration
API_ID = 8138160
API_HASH = "1ad2dae5b9fddc7fe7bfee2db9d54ff2"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Initialize the bot
app = Client("uploader", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory storage for user states and download data
user_states: Dict[int, Dict[str, Any]] = {}
download_requests: Dict[str, Dict[str, str]] = {}


# Helper functions
def get_filename_from_url(url: str) -> str:
    """Extract filename from URL"""
    try:
        parsed = urlparse(url)
        if parsed.path:
            return os.path.basename(parsed.path)
    except Exception:
        pass
    return "downloaded_file"


def get_file_extension(filename: str) -> str:
    """Get file extension from filename"""
    return os.path.splitext(filename)[1].lower()


def get_mime_type(file_path: str) -> str:
    """Get MIME type of file"""
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or "application/octet-stream"


def download_file_with_progress(url: str, file_path: str, progress_message: Message) -> bool:
    """Download file from URL with progress bar"""
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024  # 1 Kibibyte

        with open(file_path, 'wb') as f, tqdm(
            total=total_size,
            unit='iB',
            unit_scale=True,
            desc=f"Downloading {os.path.basename(file_path)}",
            ncols=100
        ) as progress:
            for data in response.iter_content(block_size):
                progress.update(len(data))
                f.write(data)

        if total_size != 0 and os.path.getsize(file_path) != total_size:
            print("ERROR, something went wrong during download")
            return False
        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False


def clean_filename(filename: str) -> str:
    """Clean filename by removing special characters"""
    return re.sub(r'[^\w\-_. ]', '', filename)


# Bot handlers
@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    """Handle /start and /help commands"""
    help_text = """
**Welcome to URL File Uploader Bot!**

📤 Send me a direct download URL and I'll upload the file to Telegram.

🔹 **Features:**
- Progress bar for downloads/uploads
- Automatic file type detection
- Ability to rename files before upload
- Fast upload speeds

📝 **Usage:**
1. Send a direct download URL.
2. The bot will detect the filename.
3. You can choose to upload with the detected name or rename it first.
4. The bot will then download and upload the file to our chat.

⚠️ **Note:** This bot works best with direct download links.
"""
    await message.reply_text(help_text, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def url_handler(client: Client, message: Message):
    """Handle URL messages"""
    url = message.text.strip()
    original_filename = get_filename_from_url(url)
    clean_name = clean_filename(original_filename)
    request_id = str(uuid.uuid4())

    download_requests[request_id] = {"url": url, "filename": clean_name}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Upload with this name", callback_data=f"upload|{request_id}")],
        [InlineKeyboardButton("Rename before upload", callback_data=f"rename|{request_id}")]
    ])

    await message.reply_text(
        f"🔗 URL received\n\n"
        f"📄 Detected filename: `{clean_name}`\n\n"
        f"Choose an option:",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )


@app.on_callback_query(filters.regex(r'^rename\|'))
async def rename_callback(client: Client, callback_query):
    """Handle rename callback"""
    _, request_id = callback_query.data.split('|', 1)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    request_data = download_requests[request_id]
    filename = request_data["filename"]

    await callback_query.message.edit_text(
        f"✏️ Please send the new filename (without the extension).\n\n"
        f"Current filename: `{filename}`\n"
        f"Detected extension: `{get_file_extension(filename)}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    user_id = callback_query.from_user.id
    user_states[user_id] = {"request_id": request_id}


@app.on_message(filters.private & ~filters.command(["start", "help"]))
async def filename_handler(client: Client, message: Message):
    """Handle filename input after rename request"""
    user_id = message.from_user.id
    if user_id not in user_states:
        return

    state = user_states[user_id]
    request_id = state["request_id"]

    if request_id not in download_requests:
        await message.reply_text("Your previous request has expired. Please send the URL again.")
        del user_states[user_id]
        return

    original_filename = download_requests[request_id]["filename"]
    extension = get_file_extension(original_filename)
    new_filename_base = clean_filename(message.text.strip())
    new_filename = f"{new_filename_base}{extension}"

    download_requests[request_id]["filename"] = new_filename

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Upload as {new_filename}", callback_data=f"upload|{request_id}")]
    ])

    await message.reply_text(
        f"✅ Filename updated!\n\n"
        f"New filename: `{new_filename}`\n\n"
        f"Click the button below to start the upload:",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

    del user_states[user_id]


@app.on_callback_query(filters.regex(r'^upload\|'))
async def upload_callback(client: Client, callback_query):
    """Handle file upload"""
    _, request_id = callback_query.data.split('|', 1)
    if request_id not in download_requests:
        await callback_query.answer("Request expired or invalid.", show_alert=True)
        return

    request_data = download_requests[request_id]
    url = request_data["url"]
    filename = request_data["filename"]

    await callback_query.answer("Starting download...")

    msg = await callback_query.message.edit_text(
        f"⬇️ Downloading file...\n\n"
        f"📄 Filename: `{filename}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    os.makedirs("temp", exist_ok=True)
    temp_file = os.path.join("temp", filename)

    if not download_file_with_progress(url, temp_file, msg):
        await msg.edit_text("❌ Failed to download the file. Please check the URL and try again.")
        return

    file_size = os.path.getsize(temp_file)
    mime_type = get_mime_type(temp_file)

    await msg.edit_text(
        f"⬆️ Uploading file...\n\n"
        f"📄 Filename: `{filename}`\n"
        f"📦 Size: {file_size / 1024 / 1024:.2f} MB\n"
        f"📝 Type: `{mime_type}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    last_update_percentage = -1

    async def progress(current, total):
        nonlocal last_update_percentage
        progress_percent = int((current / total) * 100)
        if progress_percent > last_update_percentage:
            last_update_percentage = progress_percent
            if progress_percent % 5 == 0:
                try:
                    await msg.edit_text(
                        f"⬆️ Uploading file...\n\n"
                        f"📄 Filename: `{filename}`\n"
                        f"📦 Size: {file_size / 1024 / 1024:.2f} MB\n"
                        f"📝 Type: `{mime_type}`\n\n"
                        f"🚀 Progress: {progress_percent}%",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

    try:
        await client.send_document(
            chat_id=callback_query.message.chat.id,
            document=temp_file,
            file_name=filename,
            progress=progress,
            caption=f"📄 `{filename}`"
        )
        await msg.edit_text("✅ File uploaded successfully!")
    except RPCError as e:
        await msg.edit_text(f"❌ Upload failed: {e}")
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        if request_id in download_requests:
            del download_requests[request_id]


if __name__ == "__main__":
    print("Bot is running...")
    app.run()
