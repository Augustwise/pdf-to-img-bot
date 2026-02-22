# pdf-to-img-bot

Telegram bot that converts an uploaded PDF file into PNG images (one image per page), packs them into a `.zip` archive, and sends the archive back.

## Usage

1. Send a PDF file to the bot.
2. The bot converts all pages to high-quality PNG images (`300 DPI`).
3. The bot returns a ZIP archive where files are named by page number:
   - `page_0001.png`
   - `page_0002.png`
   - ...
