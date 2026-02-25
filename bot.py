import asyncio
import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

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
MAX_ZIP_SIZE = 45 * 1024 * 1024  # 45MB (Telegram limit is 50MB, keep margin)
PROGRESS_EVERY = 50  # Report progress every N pages


def render_pages_to_files(
    pdf_path: Path,
    output_dir: Path,
    dpi: int = OUTPUT_DPI,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """Render each PDF page to a separate PNG file. Returns list of PNG paths."""
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    png_files: list[Path] = []

    with fitz.open(pdf_path) as pdf:
        total_pages = len(pdf)
        for page_number, page in enumerate(pdf, start=1):
            image_path = output_dir / f"page_{page_number:04d}.png"
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(str(image_path))
            del pixmap
            png_files.append(image_path)

            if progress_callback and (
                page_number % PROGRESS_EVERY == 0 or page_number == total_pages
            ):
                progress_callback(page_number, total_pages)

    if not png_files:
        raise ValueError("The PDF file has no pages")

    return png_files


def pack_into_zips(
    png_files: list[Path],
    output_dir: Path,
    base_name: str,
    max_size: int = MAX_ZIP_SIZE,
) -> list[Path]:
    """Pack PNG files into one or more ZIP archives respecting max_size."""
    zip_paths: list[Path] = []
    current_zip_path: Path | None = None
    current_zip: zipfile.ZipFile | None = None
    current_size = 0

    def start_new_zip() -> None:
        nonlocal current_zip_path, current_zip, current_size
        part = len(zip_paths) + 1
        if current_zip:
            current_zip.close()
        current_zip_path = output_dir / f"{base_name}_part{part}.zip"
        current_zip = zipfile.ZipFile(
            current_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED
        )
        current_size = 0
        zip_paths.append(current_zip_path)

    start_new_zip()

    for png_path in png_files:
        file_size = png_path.stat().st_size

        # If adding this file would exceed limit and zip already has files, start new zip
        if current_size + file_size > max_size and current_size > 0:
            start_new_zip()

        assert current_zip is not None
        current_zip.write(png_path, arcname=png_path.name)
        current_size += file_size

    if current_zip:
        current_zip.close()

    # If only one part, rename to remove _part1 suffix
    if len(zip_paths) == 1:
        single_path = zip_paths[0]
        final_path = output_dir / f"{base_name}.zip"
        single_path.rename(final_path)
        zip_paths = [final_path]

    return zip_paths


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
    base_name = Path(pdf_name).stem + "_images"

    await update.message.reply_text("PDF received. Converting pages to PNG images...")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        pdf_path = temp_dir_path / pdf_name
        png_dir = temp_dir_path / "pages"
        png_dir.mkdir()
        zip_dir = temp_dir_path / "zips"
        zip_dir.mkdir()

        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(pdf_path))

        # Track progress messages to avoid spamming
        last_reported = [0]

        def on_progress(current: int, total: int) -> None:
            last_reported[0] = current

        try:
            loop = asyncio.get_event_loop()

            # Send progress updates while conversion runs
            async def run_with_progress() -> list[Path]:
                progress_state = {"current": 0, "total": 0}

                def progress_cb(current: int, total: int) -> None:
                    progress_state["current"] = current
                    progress_state["total"] = total

                conversion_task = asyncio.ensure_future(
                    asyncio.to_thread(
                        render_pages_to_files, pdf_path, png_dir, OUTPUT_DPI, progress_cb
                    )
                )

                last_sent = 0
                while not conversion_task.done():
                    await asyncio.sleep(3)
                    current = progress_state["current"]
                    total = progress_state["total"]
                    if total > 0 and current > last_sent and current < total:
                        if current - last_sent >= PROGRESS_EVERY or current == total:
                            assert update.message is not None
                            await update.message.reply_text(
                                f"Converting... {current}/{total} pages done."
                            )
                            last_sent = current

                return await conversion_task

            png_files = await run_with_progress()
        except Exception:
            logger.exception("Failed to convert PDF: %s", pdf_name)
            await update.message.reply_text(
                "I couldn't process this PDF. Please try another file."
            )
            return

        page_count = len(png_files)

        try:
            zip_paths = await asyncio.to_thread(
                pack_into_zips, png_files, zip_dir, base_name
            )
        except Exception:
            logger.exception("Failed to pack ZIP archives: %s", pdf_name)
            await update.message.reply_text(
                "I couldn't create the archive. Please try another file."
            )
            return

        total_parts = len(zip_paths)

        for i, zip_path in enumerate(zip_paths, start=1):
            if total_parts == 1:
                caption = (
                    f"Done. Converted {page_count} page(s) to PNG "
                    f"({OUTPUT_DPI} DPI) and packed them into this ZIP."
                )
            else:
                caption = (
                    f"Part {i} of {total_parts}. "
                    f"Total: {page_count} page(s) at {OUTPUT_DPI} DPI."
                )

            with zip_path.open("rb") as archive_file:
                await update.message.reply_document(
                    document=archive_file,
                    filename=zip_path.name,
                    caption=caption,
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
