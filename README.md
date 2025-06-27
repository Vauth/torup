# Torup - Torrent Downloader
A high-performance, lightweight, asynchronous Telegram bot built with Python, `pyrogram`, and `libtorrent` to download torrents via magnet links to a server, with subsequent options for file management through direct download links or uploading to Telegram.

<br>

## Core Technologies

- [Python 3.8+](https://www.python.org/)
- [Pyrogram](https://docs.pyrogram.org/)
- [libtorrent](https://www.libtorrent.org/)
- [Asyncio](https://docs.python.org/3/library/asyncio.html)
- [http.server](https://docs.python.org/3/library/http.server.html)

<br>

## Features

- Asynchronous from the Ground Up
- Secure and Private
- Interactive Pre-download Information
- Real-time Progress Updates
- Multiple Post-Download Actions
- Optimized `libtorrent` Session
- Built-in File Server

<br>

## System Architecture
- **Main Bot Thread:** An `asyncio` event loop manages the Pyrogram client, listens for user commands and callbacks, and orchestrates the torrenting process.
- **File Server Thread:** A separate `threading.Thread` runs a simple `http.server` instance. This allows for non-blocking file serving without interfering with the bot's asynchronous operations.

<br>

## Installation and Configuration

### Prerequisites
- Python 3.8 or newer.
- `pip` for package installation.
- A server with a public-facing IP address or a configurable domain (`SERVER_URL`).
- Your own Telegram API credentials (`API_ID`, `API_HASH`) from [my.telegram.org](https://my.telegram.org) and a `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).

### Setup Instructions
- **Clone the repository:**
```bash
git clone https://github.com/vauth/torup
cd torup
```

- **Install dependencies:**
```bash
pip3 install -r requirements.txt
```

- **Start the project:**
```bash
python3 main.py
```

### Environment Variables
For security and portability, all configuration is handled via environment variables.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `BOT_TOKEN` | Your Telegram bot token from @BotFather. | `"123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"` |
| `OWNER_ID` | Your unique Telegram user ID. | `"123456789"` |
| `SERVER_URL` | The public URL or IP of the server hosting the bot. | `"your-vps.com"` |
| `API_ID` | Your Telegram App API ID. | `"8138160"` (use your own) |
| `API_HASH` | Your Telegram App API Hash. | `"1ad2dae5b9fddc7fe7bfee2db9d54ff2"` (use your own) |

<br>

## Contributing
Contributions are welcome! Feel free to submit a pull request or report an issue.

<br>

## License
```
MIT License

Copyright (c) 2025 Vauth

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
