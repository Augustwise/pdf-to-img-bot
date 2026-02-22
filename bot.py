import asyncio
import logging
import os
import tempfile
import zipfile
from pathlib import Path

import fitz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
OUTPUT_DPI = 500


def convert_pdf_to_zip(pdf_path: Path, zip_path: Path, dpi: int = OUTPUT_DPI) -> int:
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    page_count = 0

    with fitz.open(pdf_path) as pdf, zipfile.ZipFile(
        zip_path, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as zip_file:
        for page_number, page in enumerate(pdf, start=1):
            image_name = f"page_{page_number:04d}.png"
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            zip_file.writestr(image_name, pixmap.tobytes("png"))
            page_count += 1

    if page_count == 0:
        raise ValueError("The PDF file has no pages")

    return page_count


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Send me a PDF file and I will return a ZIP archive with PNG images for each page."
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    document = update.message.document
    is_pdf = document.mime_type == "application/pdf" or (
        document.file_name and document.file_name.lower().endswith(".pdf")
    )
    if not is_pdf:
        await update.message.reply_text("Please send a PDF document.")
        return

    original_name = document.file_name or "document.pdf"
    pdf_name = Path(original_name).name
    archive_name = f"{Path(pdf_name).stem}_images.zip"

    await update.message.reply_text("PDF received. Converting pages to PNG images...")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        pdf_path = temp_dir_path / pdf_name
        zip_path = temp_dir_path / archive_name

        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(pdf_path))

        try:
            page_count = await asyncio.to_thread(convert_pdf_to_zip, pdf_path, zip_path)
        except Exception:
            logger.exception("Failed to convert PDF: %s", pdf_name)
            await update.message.reply_text(
                "I couldn't process this PDF. Please try another file."
            )
            return

        with zip_path.open("rb") as archive_file:
            await update.message.reply_document(
                document=archive_file,
                filename=archive_name,
                caption=(
                    f"Done. Converted {page_count} page(s) to PNG "
                    f"({OUTPUT_DPI} DPI) and packed them into this ZIP."
                ),
            )


async def handle_non_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("Please send a PDF document.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_non_pdf))
    app.run_polling()


if __name__ == "__main__":
    main()
