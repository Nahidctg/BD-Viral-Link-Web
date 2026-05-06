import os
import asyncio
import datetime
import uvicorn
import time
import aiohttp
import hmac
import hashlib
import urllib.parse
import secrets
import json
import html
from PIL import Image, ImageFilter

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel
from pyrogram import Client as PyroClient

# ==========================================
# 1. Configuration & Global Variables
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "") 
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
BOT_USERNAME = "BDViralLinkProBot"

_db_ch = os.getenv("DB_CHANNEL_ID", "")
DB_CHANNEL_ID = int(_db_ch) if _db_ch.lstrip('-').isdigit() else None

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
security = HTTPBasic()

if SESSION_STRING:
    pyro_app = PyroClient("user_session", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True)
else:
    pyro_app = PyroClient("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN, in_memory=True)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID]) 
banned_cache = set() 
video_queue = asyncio.Queue()
is_processing = False

class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_category = State()
    waiting_for_series_search = State()
    waiting_for_episode_quality = State()
    waiting_for_gallery_photos = State() # NEW: Added for gallery system

# ==========================================
# 2. Image Processing (Wide Thumbnails)
# ==========================================
def make_wide_thumbnail(input_path, output_path):
    try:
        img = Image.open(input_path).convert('RGB')
        w, h = img.size
        target_w = int(h * 1.777)
        canvas = Image.new('RGB', (target_w, h))
        bg = img.resize((target_w, h))
        bg = bg.filter(ImageFilter.GaussianBlur(15))
        canvas.paste(bg, (0, 0))
        offset_x = (target_w - w) // 2
        canvas.paste(img, (offset_x, 0))
        canvas.save(output_path, quality=90)
        return True
    except Exception: return False

async def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{file_path}"'
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        return float(stdout.decode().strip())
    except: return 10.0 

async def generate_collage(video_path, output_path):
    duration = await get_video_duration(video_path)
    timestamps = [max(1, duration * 0.2), duration * 0.5, duration * 0.8]
    images = []
    for i, t in enumerate(timestamps):
        img_name = f"temp_frame_{i}_{int(time.time())}.jpg"
        cmd = f'ffmpeg -y -ss {t} -i "{video_path}" -vframes 1 -q:v 2 "{img_name}"'
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        if os.path.exists(img_name):
            try:
                img = Image.open(img_name)
                h_percent = (360 / float(img.size[1]))
                w_size = int((float(img.size[0]) * float(h_percent)))
                img = img.resize((w_size, 360), Image.Resampling.LANCZOS)
                images.append(img)
            except Exception: pass
            finally:
                if os.path.exists(img_name): os.remove(img_name)
    
    if not images: return False
    while len(images) < 3: images.append(images[-1].copy())
        
    img_w, img_h = images[0].size
    padding = 8
    poster_w = (img_w * 3) + (padding * 4)
    poster_h = img_h + (padding * 2)
    collage = Image.new('RGB', (poster_w, poster_h), color=(15, 23, 42))
    positions = [(padding, padding), (img_w + padding * 2, padding), (img_w * 2 + padding * 3, padding)]
    
    for idx, img in enumerate(images[:3]):
        if img.size != (img_w, img_h): img = img.resize((img_w, img_h), Image.Resampling.LANCZOS)
        collage.paste(img, positions[idx])
        
    collage.save(output_path, quality=90)
    return True

async def video_queue_worker():
    global is_processing
    while True:
        chat_id, message_id, aiogram_file_id, file_type = await video_queue.get()
        is_processing = True
        downloaded_file = None
        collage_path = None
        try:
            admin_id = chat_id
            status_msg = await bot.send_message(admin_id, "⏳ <b>Processing Video...</b> (Downloading)")
            pyro_msg = await pyro_app.get_messages(chat_id, message_id)
            
            total_vids = await db.movies.count_documents({})
            serial_no = total_vids + 1
            auto_title = f"New Viral Video {serial_no:04d}"
            
            video_name = f"temp_video_{serial_no}_{int(time.time())}.mp4"
            collage_path = os.path.abspath(f"collage_{serial_no}_{int(time.time())}.jpg")
            
            downloaded_file = await pyro_app.download_media(pyro_msg, file_name=video_name)
            if not downloaded_file:
                await bot.edit_message_text("❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে।", chat_id=admin_id, message_id=status_msg.message_id)
                continue
                
            await bot.edit_message_text("📸 <b>Generating Screenshots...</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
            success = await generate_collage(downloaded_file, collage_path)
            
            if not success:
                await bot.edit_message_text("❌ <b>Screenshot তৈরি করতে সমস্যা হয়েছে!</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
                continue
                
            db_file_id = None
            db_photo_id = None
            photo_id = None
            
            if DB_CHANNEL_ID:
                try:
                    copied_vid = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=chat_id, message_id=message_id)
                    db_file_id = copied_vid.message_id
                    
                    copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(collage_path))
                    db_photo_id = copied_photo.message_id
                    photo_id = copied_photo.photo[-1].file_id
                except Exception: pass
            
            photo_msg = await bot.send_photo(admin_id, photo=FSInputFile(collage_path), caption=f"✅ <b>{auto_title}</b> Successfully Uploaded!")
            if not photo_id: photo_id = photo_msg.photo[-1].file_id
            
            await db.movies.insert_one({
                "title": auto_title, "quality": "HD", "photo_id": photo_id, 
                "file_id": aiogram_file_id, "file_type": file_type,
                "db_file_id": db_file_id, "db_photo_id": db_photo_id,
                "categories": ["Auto Upload"], 
                "gallery": [], # Support for future gallery edits
                "clicks": 0, "created_at": datetime.datetime.utcnow()
            })
            await bot.delete_message(chat_id=admin_id, message_id=status_msg.message_id)

            if CHANNEL_ID:
                try:
                    bot_info = await bot.get_me()
                    kb = [[types.InlineKeyboardButton(text="📥 ভিডিওটি দেখতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
                    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                    caption = (f"🔥 <b>নতুন এক্সক্লুসিভ ভাইরাল ভিডিও!</b>\n\n📌 <b>টাইটেল:</b> {auto_title}\n🏷 <b>কোয়ালিটি:</b> HD (Original)\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
                except Exception: pass
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Error: {str(e)}")
        finally:
            if downloaded_file and os.path.exists(downloaded_file): os.remove(downloaded_file)
            if collage_path and os.path.exists(collage_path): os.remove(collage_path)
            video_queue.task_done()
            is_processing = False

# ==========================================
# 3. DB & Auth Functions
# ==========================================
async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    async for admin in db.admins.find(): admin_cache.add(admin["user_id"])

async def load_banned_users():
    banned_cache.clear()
    async for b_user in db.banned.find(): banned_cache.add(b_user["user_id"])

async def init_db():
    await db.movies.create_index([("title", "text")])
    await db.movies.create_index("created_at")
    await db.auto_delete.create_index("delete_at")
    await db.payments.create_index("trx_id", unique=True)

def validate_tg_data(init_data: str) -> bool:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop('hash', None)
        auth_date = int(parsed_data.get('auth_date', 0))
        if not hash_val or time.time() - auth_date > 86400: return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except Exception: return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect Info", headers={"WWW-Authenticate": "Basic"})
    return True

async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            expired_msgs = db.auto_delete.find({"delete_at": {"$lte": now}})
            async for msg in expired_msgs:
                try: await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except Exception: pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
        except Exception: pass
        await asyncio.sleep(60)

# ==========================================
# 4. FULL ADMIN COMMANDS
# ==========================================
def format_views(n):
    if n >= 1000000: return f"{n/1000000:.1f}M".replace(".0M", "M")
    if n >= 1000: return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache: return await message.answer("🚫 <b>আপনাকে ব্যান করা হয়েছে।</b>", parse_mode="HTML")
        
    await state.clear()
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    
    if not user:
        args = message.text.split(" ")
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                if referrer_id != uid:
                    await db.users.update_one({"user_id": referrer_id}, {"$inc": {"refer_count": 1, "coins": 10}})
                    try: await bot.send_message(referrer_id, "🎉 <b>অভিনন্দন!</b> নতুন রেফারের জন্য আপনি <b>১০ কয়েন</b> পেয়েছেন!", parse_mode="HTML")
                    except: pass
            except Exception: pass

        await db.users.insert_one({
            "user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "refer_count": 0, "coins": 0, "vip_until": now - datetime.timedelta(days=1)
        })
    
    kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন!</b>\n\n"
            "⚙️ <b>কমান্ড:</b>\n"
            "🔸 অটো আপলোড: <code>/autoupload on/off</code>\n"
            "🔸 অ্যাডমিন প্যানেল: <code>/addadmin ID</code> | <code>/deladmin ID</code> | <code>/adminlist</code>\n"
            "🔸 ডাইরেক্ট লিংক: <code>/addlink লিংক</code> | <code>/dellink লিংক</code> | <code>/seelinks</code>\n"
            "🔸 টেলিগ্রাম: <code>/settg লিংক</code> | 18+: <code>/set18 লিংক</code>\n"
            "🔸 সাপোর্ট লিংক: <code>/setsupport লিংক</code>\n"
            "🔸 পেমেন্ট নাম্বার: <code>/setbkash নাম্বার</code> | <code>/setnagad নাম্বার</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on/off</code> | অটো-ডিলিট: <code>/settime [মিনিট]</code>\n"
            "🔸 অ্যাড টাইম: <code>/setadtime [সেকেন্ড]</code>\n" 
            "🔸 স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code> | <code>/delallmovies</code>\n"
            "🔸 ব্যান: <code>/ban ID</code> | আনব্যান: <code>/unban ID</code>\n"
            "🔸 VIP দিন: <code>/addvip ID দিন</code> | VIP বাতিল: <code>/removevip ID</code>\n\n"
            f"🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n"
            "<i>লগিন: admin / admin123</i>\n\n"
            "📥 <b>মুভি অ্যাড করতে প্রথমে ভিডিও বা ডকুমেন্ট ফাইল পাঠান।</b>"
        )
    else: text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\nমুভি পেতে নিচের বাটনে ক্লিক করুন।"
        
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setadtime"))
async def set_ad_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        secs = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "ad_time"}, {"$set": {"seconds": secs}}, upsert=True)
        await m.answer(f"✅ অ্যাড ওয়েটিং টাইম <b>{secs} সেকেন্ড</b> সেট করা হয়েছে।", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/setadtime ১৫</code>", parse_mode="HTML")

@dp.message(Command("autoupload"))
async def toggle_auto_upload(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        await db.settings.update_one({"id": "auto_upload_mode"}, {"$set": {"status": state == "on"}}, upsert=True)
        await m.answer(f"✅ Auto Upload {'চালু' if state=='on' else 'বন্ধ'} করা হয়েছে।")
    except: pass

@dp.message(Command("addlink"))
async def add_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer(f"✅ লিংক অ্যাড করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("dellink"))
async def del_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}})
        await m.answer(f"❌ লিংকটি ডিলিট করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("seelinks"))
async def see_direct_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    links = dl_cfg.get("links", []) if dl_cfg else []
    if not links: return await m.answer("⚠️ কোনো ডাইরেক্ট লিংক নেই।")
    text = "🔗 <b>বর্তমান ডাইরেক্ট লিংক সমূহ:</b>\n\n"
    for i, link in enumerate(links, 1): text += f"{i}. <code>{link}</code>\n"
    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setbkash"))
async def set_bkash(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "bkash_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ বিকাশ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("setnagad"))
async def set_nagad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "nagad_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ নগদ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_tg"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("setsupport"))
async def set_support_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_support"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ সাপোর্ট লিংক আপডেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("set18"))
async def set_18_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_18"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ 18+ লিংক আপডেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": state == "on"}}, upsert=True)
        await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন {'চালু' if state=='on' else 'বন্ধ'} করা হয়েছে।")
    except Exception: pass

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        mins = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": mins}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট টাইম {mins} মিনিট সেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0:
            await m.answer(f"✅ '<b>{title}</b>' নামের {result.deleted_count} টি ফাইল ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ এই নামের কোনো মুভি পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/delmovie মুভির নাম</code>", parse_mode="HTML")

@dp.message(Command("delallmovies"))
async def del_all_movies_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    result = await db.movies.delete_many({})
    await m.answer(f"🗑 <b>সতর্কতা:</b> ডাটাবেস থেকে সর্বমোট <b>{result.deleted_count}</b> টি মুভি ডিলিট করা হয়েছে!", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users_today = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    
    text = (f"📊 <b>অ্যাডভান্সড স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন ইউজার: <code>{new_users_today}</code>\n"
            f"🎬 মোট ফাইল আপলোড: <code>{mc}</code>")
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        if target_uid in admin_cache: return await m.answer("⚠️ অ্যাডমিনকে ব্যান করা যাবেবিধা নেই!")
        await db.banned.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        banned_cache.add(target_uid)
        await m.answer(f"🚫 ইউজার <code>{target_uid}</code> কে ব্যান করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("unban"))
async def unban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": target_uid})
        banned_cache.discard(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> আনব্যান হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন অ্যাড করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        await db.admins.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        admin_cache.add(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে অ্যাডমিন বানানো হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র Owner অ্যাডমিন রিমুভ করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        if target_uid == OWNER_ID: return await m.answer("⚠️ Main Owner কে ডিলিট করা সম্ভব নয়!")
        await db.admins.delete_one({"user_id": target_uid})
        admin_cache.discard(target_uid)
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> রিমুভ করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("adminlist"))
async def list_admin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    text = f"👑 <b>Owner:</b> <code>{OWNER_ID}</code>\n\n👮‍♂️ <b>Admins:</b>\n"
    async for a in db.admins.find(): text += f"▪️ <code>{a['user_id']}</code>\n"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার ডাটাবেসে নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("removevip"))
async def remove_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        now = datetime.datetime.utcnow()
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": now - datetime.timedelta(days=1)}})
        await m.answer(f"❌ VIP বাতিল করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\nবাতিল করতে /start দিন।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন মুভি অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    success = 0
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
            success += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✅ সম্পন্ন! সর্বমোট <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache and (m.text is None or not m.text.startswith("/")))
async def forward_to_admin(m: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ রিপ্লাই দিন", callback_data=f"reply_{m.from_user.id}")
    markup = builder.as_markup()
    
    all_admins = set([OWNER_ID])
    async for a in db.admins.find(): 
        all_admins.add(a["user_id"])
        
    for admin_id in all_admins:
        try:
            await bot.send_message(
                admin_id, 
                f"📩 <b>Message from <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a></b>:\n\n{m.text or '[Media File/Sticker]'}", 
                parse_mode="HTML", 
                reply_markup=markup
            )
        except Exception: pass

# ==========================================
# 5. Movie Upload & Gallery Logic 
# ==========================================
@dp.message(F.content_type.in_({'video', 'document', 'photo'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    # যদি গ্যালারির ছবি পাঠানোর স্টেটে থাকে, তাহলে মেনু দেখাবে না
    if current_state == AdminStates.waiting_for_gallery_photos.state:
        return
        
    config = await db.settings.find_one({"id": "auto_upload_mode"})
    is_auto = config["status"] if config else False
    
    # অটো আপলোড শুধু ভিডিও বা ডকুমেন্টের জন্য
    if is_auto and not m.photo:
        aiogram_fid = m.video.file_id if m.video else m.document.file_id
        file_type = "video" if m.video else "document"
        await video_queue.put((m.chat.id, m.message_id, aiogram_fid, file_type))
        return await m.answer(f"✅ ভিডিও অটো-প্রসেস কিউতে যুক্ত হয়েছে! সিরিয়াল: <b>{video_queue.qsize()}</b>", parse_mode="HTML")
        
    fid = m.video.file_id if m.video else (m.photo[-1].file_id if m.photo else m.document.file_id)
    ftype = "video" if m.video else ("photo" if m.photo else "document")
    
    db_file_id = None
    if DB_CHANNEL_ID:
        try:
            copied = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=m.chat.id, message_id=m.message_id)
            db_file_id = copied.message_id
        except Exception: pass
        
    await state.update_data(file_id=fid, file_type=ftype, db_file_id=db_file_id)
    
    kb = [
        [types.InlineKeyboardButton(text="🎬 নতুন মুভি/সিরিজ যুক্ত করুন", callback_data="upload_new")],
        [types.InlineKeyboardButton(text="➕ আগের সিরিজের নতুন এপিসোড", callback_data="upload_episode")],
        [types.InlineKeyboardButton(text="🖼 গ্যালারি স্ক্রিনশট অ্যাড করুন", callback_data="add_gallery")] # NEW BUTTON
    ]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    await m.answer("✅ ফাইল পেয়েছি! এটি কীভাবে যুক্ত করতে চান?", reply_markup=markup)

# --- Gallery Menu Trigger ---
@dp.callback_query(F.data == "add_gallery")
async def add_gallery_cb(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("file_type") != "photo":
        return await c.answer("⚠️ গ্যালারি তৈরি করতে প্রথমে একটি Photo (স্ক্রিনশট) সেন্ড করে এই বাটনে ক্লিক করুন!", show_alert=True)
        
    await state.set_state(AdminStates.waiting_for_series_search)
    await state.update_data(is_gallery=True) 
    await c.message.edit_text("🖼 <b>গ্যালারি স্ক্রিনশট!</b>\n\nযে ভিডিওর নিচে এই স্ক্রিনশটগুলো রাখতে চান, সেই <b>ভিডিওর নামের কয়েক অক্ষর</b> লিখে রিপ্লাই দিন।", parse_mode="HTML")

@dp.callback_query(F.data == "upload_new")
async def upload_new_cb(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_photo)
    await c.message.edit_text("✅ <b>নতুন মুভি/সিরিজ!</b>\nএবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।", parse_mode="HTML")

@dp.callback_query(F.data == "upload_episode")
async def upload_episode_cb(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_series_search)
    await c.message.edit_text("✅ <b>নতুন এপিসোড!</b>\n\nযে সিরিজে এড করতে চান, সেই <b>সিরিজের নামের কয়েক অক্ষর</b> লিখে রিপ্লাই দিন (যেমন: Farzi)।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_series_search, F.text)
async def search_series_for_episode(m: types.Message, state: FSMContext):
    query = m.text.strip()
    pipeline = [
        {"$match": {"title": {"$regex": query, "$options": "i"}}},
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}, "categories": {"$first": "$categories"}}},
        {"$limit": 10}
    ]
    results = await db.movies.aggregate(pipeline).to_list(10)

    if not results:
        return await m.answer("⚠️ এই নামে কোনো সিরিজ/মুভি পাওয়া যায়নি! আবার সঠিক নাম লিখে পাঠান।")

    await state.update_data(search_results=results)
    
    builder = InlineKeyboardBuilder()
    for idx, res in enumerate(results):
        builder.button(text=f"📺 {res['_id']}", callback_data=f"sel_series_{idx}")
    builder.adjust(1)
    
    await m.answer("👇 নিচে থেকে আপনার কাঙ্ক্ষিত সিরিজটি সিলেক্ট করুন:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("sel_series_"))
async def selected_series_cb(c: types.CallbackQuery, state: FSMContext):
    idx = int(c.data.split("_")[2])
    data = await state.get_data()
    selected = data["search_results"][idx]

    # যদি গ্যালারির জন্য মুভি সিলেক্ট করে থাকে
    if data.get("is_gallery"):
        await state.update_data(target_movie_title=selected["_id"], gallery_photos=[data["file_id"]])
        await state.set_state(AdminStates.waiting_for_gallery_photos)
        await c.message.edit_text(f"✅ <b>{selected['_id']}</b> সিলেক্ট হয়েছে!\n\n📸 <b>প্রথম স্ক্রিনশট অ্যাড হয়েছে!</b>\nএবার বাকি স্ক্রিনশটগুলো এক এক করে সেন্ড করুন। সব পাঠানো শেষ হলে <b>/done</b> লিখে পাঠান।", parse_mode="HTML")
    else:
        await state.update_data(title=selected["_id"], photo_id=selected["photo_id"], db_photo_id=selected.get("db_photo_id"), categories=selected.get("categories", []))
        await state.set_state(AdminStates.waiting_for_episode_quality)
        await c.message.edit_text(f"✅ <b>{selected['_id']}</b> সিলেক্ট হয়েছে!\n\nএবার এই নতুন ফাইলের <b>এপিসোড নাম্বার বা কোয়ালিটি</b> লিখে পাঠান।", parse_mode="HTML")

# --- Gallery Photo Collector ---
@dp.message(AdminStates.waiting_for_gallery_photos, F.photo)
async def collect_gallery_photos(m: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("gallery_photos", [])
    photos.append(m.photo[-1].file_id)
    await state.update_data(gallery_photos=photos)
    await m.reply(f"✅ {len(photos)} টি স্ক্রিনশট রিসিভ হয়েছে। আরও পাঠান অথবা /done দিন।")

# --- Save Gallery Trigger ---
@dp.message(AdminStates.waiting_for_gallery_photos, Command("done"))
async def save_gallery(m: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("target_movie_title")
    photos = data.get("gallery_photos", [])
    
    await state.clear()
    
    if not photos:
        return await m.answer("⚠️ কোনো স্ক্রিনশট পাওয়া যায়নি!")

    # $addToSet with $each allows appending without duplicates, but let's just use $push with $each or overwrite if we want to add
    await db.movies.update_many(
        {"title": title},
        {"$push": {"gallery": {"$each": photos}}}
    )
    
    await m.answer(f"🎉 <b>{title}</b> এর নিচে <b>{len(photos)} টি স্ক্রিনশট</b> সফলভাবে যুক্ত হয়েছে!", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_episode_quality, F.text)
async def finalize_new_episode(m: types.Message, state: FSMContext):
    quality = m.text.strip()
    data = await state.get_data()
    title = data["title"]
    photo_id = data["photo_id"]
    categories = data.get("categories", [])
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), "db_photo_id": data.get("db_photo_id"),
        "categories": categories, "gallery": [],
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    
    await state.clear()
    await m.answer(f"🎉 <b>{title} [{quality}]</b> সফলভাবে সিরিজে এড করা হয়েছে!", parse_mode="HTML")

    if CHANNEL_ID:
        try:
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="📥 এপিসোডটি দেখতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            cat_display = ", ".join(categories) if categories else "N/A"
            caption = (f"🔥 <b>নতুন এপিসোড যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>এপিসোড/কোয়ালিটি:</b> {quality}\n🎭 <b>ক্যাটাগরি:</b> {cat_display}\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    status_msg = await m.answer("⏳ <b>ছবিটি চ্যাপ্টা (16:9) করা হচ্ছে...</b>", parse_mode="HTML")
    photo_id = m.photo[-1].file_id
    file_info = await bot.get_file(photo_id)
    
    temp_in = f"temp_in_{photo_id}.jpg"
    temp_out = f"temp_out_{photo_id}.jpg"
    await bot.download_file(file_info.file_path, temp_in)
    
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, make_wide_thumbnail, temp_in, temp_out)
    
    db_photo_id = None
    target_file = temp_out if success else temp_in
    
    if DB_CHANNEL_ID:
        try:
            copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(target_file))
            db_photo_id = copied_photo.message_id
            photo_id = copied_photo.photo[-1].file_id
        except Exception: pass
    
    if success:
        sent_photo = await m.answer_photo(FSInputFile(temp_out), caption="✅ <b>পোস্টার রেডি!</b>\nএবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        if not DB_CHANNEL_ID: photo_id = sent_photo.photo[-1].file_id
    else:
        await m.answer("✅ পোস্টার পেয়েছি! এবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        
    await state.update_data(photo_id=photo_id, db_photo_id=db_photo_id)
    await state.set_state(AdminStates.waiting_for_title)
    await bot.delete_message(m.chat.id, status_msg.message_id)
    
    if os.path.exists(temp_in): os.remove(temp_in)
    if os.path.exists(temp_out): os.remove(temp_out)

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ নাম সেভ হয়েছে! এবার ফাইলের <b>কোয়ালিটি বা এপিসোড নাম্বার</b> দিন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_category)
    await m.answer("✅ কোয়ালিটি সেভ হয়েছে!\n\nএবার মুভির <b>ক্যাটাগরি</b> লিখে পাঠান।\n<i>(একাধিক হলে কমা দিয়ে লিখুন। যেমন: Bangla Dub, Action, 18+)</i>\n\n<i>(ক্যাটাগরি না দিতে চাইলে 'Skip' লিখুন)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_category, F.text)
async def receive_movie_category(m: types.Message, state: FSMContext):
    cat_text = m.text.strip()
    if cat_text.lower() in ['skip', 'none', 'no']: categories = []
    else: categories = [cat.strip() for cat in cat_text.split(",") if cat.strip()]
    
    data = await state.get_data()
    await state.clear()
    
    title = data["title"]
    photo_id = data["photo_id"]
    quality = data["quality"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), "db_photo_id": data.get("db_photo_id"),
        "categories": categories, "gallery": [],
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    
    cat_display = ", ".join(categories) if categories else "N/A"
    await m.answer(f"🎉 <b>{title} [{quality}]</b> অ্যাপে যুক্ত করা হয়েছে!\n🏷 ক্যাটাগরি: <b>{cat_display}</b>", parse_mode="HTML")

    if CHANNEL_ID:
        try:
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="📥 ভিডিওটি দেখতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = (f"🔥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>কোয়ালিটি:</b> {quality}\n🎭 <b>ক্যাটাগরি:</b> {cat_display}\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

# ==========================================
# 6. Callbacks & Approvals
# ==========================================
@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action, _, pay_id = c.data.split("_")
    
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("ইতিমধ্যে প্রসেস করা হয়েছে!", show_alert=True)
        
    user_id = payment["user_id"]
    days = payment["days"]
    
    if action == "approve":
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + f"\n\n✅ <b>Approve করা হয়েছে!</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"🎉 <b>পেমেন্ট সফল!</b> আপনার পেমেন্ট অ্যাপ্রুভ হয়েছে এবং VIP চালু হয়েছে!", parse_mode="HTML")
        except: pass
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>Reject করা হয়েছে!</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(target_uid=user_id)
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখে পাঠান:</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_reply)
async def send_reply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    await state.clear()
    try:
        if m.text: await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
        else: await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
        await m.answer("✅ ইউজারকে রিপ্লাই পাঠানো হয়েছে!")
    except Exception: await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি!")

# ==========================================
# 7. Web Admin Panel HTML & API
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Panel - MovieZone BD</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    </head>
    <body class="bg-gray-900 text-white p-5 font-sans">
        <div class="max-w-6xl mx-auto">
            <h1 class="text-3xl font-bold text-red-500 mb-6 border-b border-gray-700 pb-3"><i class="fa-solid fa-screwdriver-wrench"></i> Admin Dashboard</h1>
            
            <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8" id="statsBoard">
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow flex items-center gap-4">
                    <div class="bg-blue-600 p-4 rounded-full text-2xl"><i class="fa-solid fa-users"></i></div>
                    <div><p class="text-gray-400 text-sm font-bold uppercase">Total Users</p><h3 class="text-2xl font-black" id="stUsers">...</h3></div>
                </div>
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow flex items-center gap-4">
                    <div class="bg-green-600 p-4 rounded-full text-2xl"><i class="fa-solid fa-film"></i></div>
                    <div><p class="text-gray-400 text-sm font-bold uppercase">Total Uploads</p><h3 class="text-2xl font-black" id="stMovies">...</h3></div>
                </div>
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow flex items-center gap-4">
                    <div class="bg-yellow-600 p-4 rounded-full text-2xl"><i class="fa-solid fa-eye"></i></div>
                    <div><p class="text-gray-400 text-sm font-bold uppercase">Total Views</p><h3 class="text-2xl font-black" id="stViews">...</h3></div>
                </div>
            </div>

            <div class="bg-gray-800 rounded-xl shadow-lg border border-gray-700 p-6">
                <div class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
                    <h2 class="text-xl font-bold text-gray-200"><i class="fa-solid fa-list-ul"></i> Manage Movies</h2>
                    <input type="text" id="adminSearch" placeholder="🔍 Search Movies..." class="bg-gray-700 text-white px-4 py-2 rounded-lg border border-gray-600 focus:outline-none w-full md:w-1/3">
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm whitespace-nowrap">
                        <thead class="bg-gray-700 text-gray-300">
                            <tr><th class="p-4">Title</th><th class="p-4">Category</th><th class="p-4">Views</th><th class="p-4">Files</th><th class="p-4">Action</th></tr>
                        </thead>
                        <tbody id="movieTableBody"><tr><td colspan="5" class="text-center p-8 text-gray-400">Loading...</td></tr></tbody>
                    </table>
                </div>
                <div class="flex justify-center items-center gap-3 mt-6" id="adminPagination"></div>
            </div>
        </div>
        <script>
            let currentPage = 1;
            let searchQuery = "";
            let searchTimeout = null;

            async function loadStats() {
                try {
                    const res = await fetch('/api/admin/stats');
                    const data = await res.json();
                    document.getElementById('stUsers').innerText = data.users;
                    document.getElementById('stMovies').innerText = data.movies;
                    document.getElementById('stViews').innerText = data.views;
                } catch(e) {}
            }

            document.getElementById('adminSearch').addEventListener('input', function(e) {
                clearTimeout(searchTimeout);
                searchQuery = e.target.value.trim();
                searchTimeout = setTimeout(() => loadAdminData(1), 500);
            });

            async function loadAdminData(page = 1) {
                currentPage = page;
                document.getElementById('movieTableBody').innerHTML = '<tr><td colspan="5" class="text-center p-8 text-gray-400">Loading...</td></tr>';
                const res = await fetch(`/api/admin/data?page=${currentPage}&q=${encodeURIComponent(searchQuery)}`); 
                const data = await res.json();
                
                let html = '';
                if(data.movies.length === 0) {
                    html = '<tr><td colspan="5" class="text-center p-8 text-gray-400">No movies found.</td></tr>';
                } else {
                    data.movies.forEach(m => {
                        let catHtml = m.categories && m.categories.length > 0 
                            ? m.categories.map(c => `<span class="bg-gray-700 px-2 py-1 rounded text-xs border border-gray-600">${c}</span>`).join(' ') 
                            : '<span class="text-gray-500">None</span>';
                        
                        html += `<tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="p-4 font-medium">${m._id}</td>
                            <td class="p-4">${catHtml}</td>
                            <td class="p-4 text-gray-400">${m.clicks} Views</td>
                            <td class="p-4 text-green-400 font-bold">${m.file_count}</td>
                            <td class="p-4 flex gap-2">
                                <button onclick="editCategory('${encodeURIComponent(m._id)}', '${encodeURIComponent(JSON.stringify(m.categories || []))}')" class="text-blue-400 bg-blue-900 px-3 py-1 rounded transition hover:bg-blue-800">Edit Cat.</button>
                                <button onclick="addViews('${encodeURIComponent(m._id)}')" class="text-yellow-400 bg-yellow-900 px-3 py-1 rounded transition hover:bg-yellow-800">Boost</button>
                                <button onclick="deleteMovie('${encodeURIComponent(m._id)}')" class="text-red-400 bg-red-900 px-3 py-1 rounded transition hover:bg-red-800">Delete</button>
                            </td>
                        </tr>`;
                    });
                }
                document.getElementById('movieTableBody').innerHTML = html;

                let pageHtml = "";
                if(data.total_pages > 1) {
                    pageHtml += `<button ${currentPage === 1 ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage - 1) + ')"'}>Prev</button>`;
                    pageHtml += `<span class="px-4 py-2 font-bold">Page ${currentPage} of ${data.total_pages}</span>`;
                    pageHtml += `<button ${currentPage === data.total_pages ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage + 1) + ')"'}>Next</button>`;
                }
                document.getElementById('adminPagination').innerHTML = pageHtml;
            }

            async function editCategory(title, currentCatsJson) {
                let currentCats = [];
                try { currentCats = JSON.parse(decodeURIComponent(currentCatsJson)); } catch(e) {}
                let currentCatStr = currentCats.join(", ");
                
                let newCatStr = prompt("Edit Categories (comma separated):", currentCatStr);
                if(newCatStr !== null) {
                    let newCategories = newCatStr.split(",").map(c => c.trim()).filter(c => c !== "");
                    await fetch('/api/admin/movie/' + title, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({new_categories: newCategories}) });
                    loadAdminData(currentPage);
                }
            }

            async function deleteMovie(title) {
                if(!confirm('Are you sure you want to delete ALL files for this movie?')) return;
                await fetch('/api/admin/movie/' + title, {method: 'DELETE'}); 
                loadAdminData(currentPage); loadStats();
            }

            async function addViews(title) {
                let amount = prompt("How many views to add?", "1000");
                if(amount && !isNaN(amount)) {
                    await fetch('/api/admin/movie/' + title, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({add_clicks: parseInt(amount)}) });
                    loadAdminData(currentPage); loadStats();
                }
            }
            
            loadStats(); loadAdminData(1);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/admin/stats")
async def admin_stats_api(auth: bool = Depends(verify_admin)):
    user_count = await db.users.count_documents({})
    movie_count = await db.movies.count_documents({})
    total_views = 0
    views_agg = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1)
    if views_agg: total_views = views_agg[0]["total"]
    return {"users": user_count, "movies": movie_count, "views": total_views}

@app.get("/api/admin/data")
async def get_admin_data(page: int = 1, q: str = "", auth: bool = Depends(verify_admin)):
    limit = 20
    skip = (page - 1) * limit
    match_stage = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}, "file_count": {"$sum": 1}, "created_at": {"$max": "$created_at"}, "categories": {"$first": "$categories"}}}, 
        {"$sort": {"created_at": -1}}, 
        {"$skip": skip}, 
        {"$limit": limit}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    
    total_groups = await db.movies.aggregate([{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]).to_list(1)
    total_pages = (total_groups[0]["total"] + limit - 1) // limit if total_groups else 0
    
    return {"movies": movies, "total_pages": total_pages}

@app.delete("/api/admin/movie/{title}")
async def delete_movie_api(title: str, auth: bool = Depends(verify_admin)):
    await db.movies.delete_many({"title": title})
    return {"ok": True}

@app.put("/api/admin/movie/{title}")
async def edit_movie_api(title: str, data: dict = Body(...), auth: bool = Depends(verify_admin)):
    if add_clicks := data.get("add_clicks"):
        await db.movies.update_many({"title": title}, {"$inc": {"clicks": int(add_clicks)}})
    if "new_categories" in data:
        await db.movies.update_many({"title": title}, {"$set": {"categories": data["new_categories"]}})
    return {"ok": True}

# ==========================================
# 8. Web UI (Perfect, Netflix Bottom Nav & Coin System)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    tg_cfg = await db.settings.find_one({"id": "link_tg"})
    support_cfg = await db.settings.find_one({"id": "link_support"})
    b18_cfg = await db.settings.find_one({"id": "link_18"})
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    
    ad_time_cfg = await db.settings.find_one({"id": "ad_time"})
    ad_wait_seconds = ad_time_cfg['seconds'] if ad_time_cfg else 10
    
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    support_link = support_cfg['url'] if support_cfg else "https://t.me/YourSupportUsername"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)

    html_code = r"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>MovieZone BD</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
            body { background: #0f172a; font-family: sans-serif; color: #fff; overflow-x: hidden; width: 100%; -webkit-overflow-scrolling: touch; padding-bottom: 80px; } 
            
            /* Clean Center Header */
            header { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 12px 10px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; width: 100%; transform: translateZ(0); will-change: transform; gap: 8px; }
            .logo { font-size: 22px; font-weight: 900; white-space: nowrap; letter-spacing: 1px; }
            .logo span { background: #ef4444; color: #fff; padding: 2px 6px; border-radius: 4px; margin-left: 3px; font-size: 14px; }
            
            .home-btn { background: rgba(59, 130, 246, 0.1); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.5); padding: 4px 12px; border-radius: 20px; font-weight: bold; font-size: 11px; cursor: pointer; display: flex; align-items: center; gap: 4px; transition: 0.2s; white-space: nowrap; }
            .home-btn:active { transform: scale(0.95); background: rgba(59, 130, 246, 0.2); }

            /* Bottom Navigation (Netflix Style) */
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(15px); border-top: 1px solid #334155; display: flex; justify-content: space-around; align-items: center; padding: 10px 0; z-index: 2000; padding-bottom: calc(10px + env(safe-area-inset-bottom)); }
            .nav-item { display: flex; flex-direction: column; align-items: center; justify-content: center; color: #94a3b8; font-size: 11px; font-weight: bold; cursor: pointer; transition: 0.2s; width: 25%; gap: 4px; }
            .nav-item i { font-size: 20px; transition: transform 0.2s; }
            .nav-item.active { color: #38bdf8; }
            .nav-item.active i { transform: scale(1.15); }
            .nav-item:active { transform: scale(0.9); }
            
            /* Profile Dropdown Menu fixed to appear above bottom nav */
            .dropdown-menu { display: none; position: fixed; bottom: 85px; right: 15px; background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(10px); border: 1px solid #334155; border-radius: 12px; overflow: hidden; box-shadow: 0 -5px 25px rgba(0,0,0,0.5); z-index: 2000; width: 250px; animation: slideUp 0.2s ease-out forwards; }
            @keyframes slideUp { 0% { opacity: 0; transform: translateY(15px); } 100% { opacity: 1; transform: translateY(0); } }
            
            .dropdown-menu a { display: flex; align-items: center; gap: 10px; padding: 12px 15px; color: white; text-decoration: none; font-weight: 600; font-size: 14px; cursor: pointer; transition: background 0.2s ease; border-bottom: 1px solid #334155; }
            .dropdown-menu a:hover, .dropdown-menu a:active { background: rgba(51, 65, 85, 0.5); }
            .dropdown-menu a i { font-size: 16px; width: 20px; text-align: center; }
            
            .coin-tag { background: #f59e0b; color: black; font-weight: 900; padding: 2px 6px; border-radius: 10px; margin-left: 2px; font-size: 12px; }
            .vip-tag { background: linear-gradient(45deg, #fbbf24, #f59e0b); color: #000; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; display: none; margin-left:5px; }

            .search-box { padding: 15px; }
            .search-input { width: 100%; padding: 16px; border-radius: 25px; border: none; outline: none; text-align: center; background: #1e293b; color: #fff; font-size: 18px; font-weight: bold; }
            
            .category-container { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 15px 15px; justify-content: center; }
            .cat-btn { background: rgba(30, 41, 59, 0.8); color: #cbd5e1; border: 1px solid #334155; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: bold; cursor: pointer; transition: all 0.2s ease; backdrop-filter: blur(5px); white-space: nowrap; }
            .cat-btn:active { transform: scale(0.95); }
            .cat-btn.active { background: linear-gradient(45deg, #ef4444, #f97316); color: white; border-color: transparent; box-shadow: 0 2px 8px rgba(239, 68, 68, 0.4); }

            .section-title { padding: 5px 15px 15px; font-size: 20px; font-weight: 900; display: flex; align-items: center; gap: 8px; color:#ff416c; }
            
            .trending-container { display: flex; overflow-x: auto; gap: 15px; padding: 0 15px 20px; scroll-behavior: smooth; }
            .trending-container::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 280px; max-width: 280px; background: transparent; overflow: hidden; cursor: pointer; flex-shrink: 0; position: relative; transition: transform 0.2s; transform: translateZ(0); will-change: transform; }
            .trending-card:active { transform: scale(0.98); }

            .grid { padding: 0 15px 20px; display: flex; flex-direction: column; gap: 20px; }
            .card { background: transparent; overflow: hidden; cursor: pointer; transition: transform 0.2s; border-radius: 0; transform: translateZ(0); will-change: transform; }
            .card:active { transform: scale(0.98); }
            
            .post-content { position: relative; padding: 3px; border-radius: 12px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; }
            .post-content img { width: 100%; aspect-ratio: 16/9; height: auto; object-fit: cover; display: block; border-radius: 10px; }
            
            .card-footer { padding: 12px 5px 0; display: flex; align-items: flex-start; gap: 12px; text-align: left; }
            .channel-logo { width: 40px; height: 40px; border-radius: 50%; background: white; color: #ef4444; border: 1px solid #e5e7eb; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 16px; flex-shrink: 0; }
            .title-text { color: #f8fafc; font-size: 16px; font-weight: bold; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; margin-top: 2px; }

            .top-badge, .ep-badge, .view-badge { position: absolute; font-weight: bold; padding: 4px 8px; border-radius: 6px; font-size: 11px; z-index: 10; color: white;}
            .top-badge { top: 10px; left: 10px; background: linear-gradient(45deg, #ff0000, #cc0000); }
            .view-badge { bottom: 10px; left: 10px; background: rgba(0,0,0,0.75); }
            .ep-badge { top: 10px; right: 10px; background: #10b981; }

            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 30px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; outline: none; transition: 0.2s;}
            .page-btn:hover { background: #334155; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; }

            .developer-credit { margin: 10px 15px 130px; padding: 22px 15px; background: linear-gradient(135deg, rgba(30, 41, 59, 0.8), rgba(15, 23, 42, 0.95)); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 16px; text-align: center; box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4), 0 0 15px rgba(56, 189, 248, 0.1); backdrop-filter: blur(10px); position: relative; overflow: hidden; }
            .developer-credit::before { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent); animation: shine 3s infinite; }
            @keyframes shine { 100% { left: 200%; } }
            .dev-title { font-size: 12px; color: #94a3b8; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 5px; }
            .dev-name { font-size: 22px; font-weight: 900; background: linear-gradient(45deg, #00f2fe, #4facfe); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }
            .dev-desc { font-size: 13.5px; color: #cbd5e1; margin-bottom: 18px; line-height: 1.5; }
            .dev-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: linear-gradient(45deg, #0ea5e9, #2563eb); color: white; padding: 12px 24px; border-radius: 30px; font-size: 15px; font-weight: bold; border: none; cursor: pointer; box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4); transition: 0.2s; position: relative; z-index: 10; }
            .dev-btn:active { transform: scale(0.95); }

            /* Floating Buttons Moved Up due to Bottom Nav */
            .floating-btn { position: fixed; right: 15px; color: white; width: 48px; height: 48px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
            .btn-18 { bottom: 205px; background: linear-gradient(45deg, #ff0000, #990000); font-weight: bold; font-size: 16px; border: 2px solid white; }
            .btn-tg { bottom: 145px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-req { bottom: 85px; background: linear-gradient(45deg, #10b981, #059669); }

            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: #1e293b; width: 92%; max-width: 400px; padding: 25px; border-radius: 20px; text-align: center; border: 1px solid #334155; max-height: 85vh; overflow-y: auto; position: relative; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: #334155; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; }
            
            .rgb-border { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; padding: 4px; border-radius: 14px; margin-bottom: 12px; cursor: pointer; width: 100%; }
            .rgb-inner { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 20px 18px; border-radius: 12px; color: white; font-weight: 900; font-size: 18px; }

            .btn-submit { background: linear-gradient(45deg, #10b981, #059669); color: white; border: none; padding: 15px 20px; border-radius: 12px; font-weight: bold; width: 100%; font-size: 18px; cursor: pointer; }

            .dl-rgb-wrap { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; padding: 4px; border-radius: 16px; width: 100%; max-width: 350px; margin: auto; }
            .dl-inner-box { background: rgba(15, 23, 42, 0.98); border-radius: 12px; padding: 30px 20px; display: flex; flex-direction: column; align-items: center; gap: 15px; }
            
            .spinner-new { width: 65px; height: 65px; border: 5px solid rgba(255,255,255,0.1); border-left-color: #10b981; border-radius: 50%; animation: spin-fast 1s linear infinite; margin: 0 auto 15px; }
            @keyframes spin-fast { 100% { transform: rotate(360deg); } }
            .big-processing-text { font-size: 26px; font-weight: 900; color: #4ade80; animation: pulse 1.5s infinite; }
            @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }

            /* In-App Gallery Viewer CSS */
            #galleryViewer { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #000; z-index: 9999; flex-direction: column; align-items: center; justify-content: center; }
            .gallery-header { position: absolute; top: 0; left: 0; width: 100%; padding: 15px; display: flex; justify-content: space-between; align-items: center; background: linear-gradient(to bottom, rgba(0,0,0,0.8), transparent); z-index: 10; }
            .gallery-counter { color: white; font-weight: bold; font-size: 16px; background: rgba(255,255,255,0.2); padding: 4px 10px; border-radius: 12px; backdrop-filter: blur(5px); }
            .gallery-close { color: white; font-size: 24px; cursor: pointer; width: 40px; height: 40px; background: rgba(255,255,255,0.2); border-radius: 50%; display: flex; align-items: center; justify-content: center; backdrop-filter: blur(5px); }
            .gallery-img { max-width: 100%; max-height: 80vh; object-fit: contain; border-radius: 8px; transition: opacity 0.3s ease; }
            .gallery-controls { position: absolute; bottom: 30px; left: 0; width: 100%; display: flex; justify-content: center; gap: 40px; z-index: 10; }
            .g-btn { background: rgba(255,255,255,0.2); color: white; border: none; width: 50px; height: 50px; border-radius: 50%; font-size: 20px; cursor: pointer; display: flex; align-items: center; justify-content: center; backdrop-filter: blur(5px); transition: 0.2s; }
            .g-btn:active { transform: scale(0.9); background: #ec4899; }
        </style>
    </head>
    <body onclick="closeMenu(event)">

        <!-- In-App Image Gallery Viewer -->
        <div id="galleryViewer">
            <div class="gallery-header">
                <div class="gallery-counter"><span id="gCurrent">1</span> / <span id="gTotal">10</span></div>
                <div class="gallery-close" onclick="closeGallery()"><i class="fa-solid fa-xmark"></i></div>
            </div>
            <img id="gImage" class="gallery-img" src="" alt="Gallery Image">
            <div class="gallery-controls">
                <button class="g-btn" onclick="prevGalleryImage()"><i class="fa-solid fa-chevron-left"></i></button>
                <button class="g-btn" onclick="nextGalleryImage()"><i class="fa-solid fa-chevron-right"></i></button>
            </div>
        </div>

        <!-- Beautiful Centered Header -->
        <header>
            <div class="logo">MovieZone<span>BD</span></div>
            <button onclick="goHome()" class="home-btn"><i class="fa-solid fa-house"></i> Home Page</button>
        </header>
        
        <!-- Dropdown Menu now opens from bottom -->
        <div id="dropdownMenu" class="dropdown-menu">
            <div style="padding: 12px 15px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 12px;">
                <div style="width: 40px; height: 40px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 18px; flex-shrink: 0;">
                    <i class="fa-solid fa-user"></i>
                </div>
                <div style="flex-grow: 1; text-align: left;">
                    <div style="font-size: 15px; font-weight: bold; color: white; line-height: 1.2;" id="menuUname">Guest</div>
                    <div style="font-size: 12px; color: #94a3b8; margin-top: 2px;" id="menuStatus">Free User</div>
                </div>
                <div style="text-align: right;">
                    <div id="coinDisplay" class="coin-tag" style="display:inline-block; margin-bottom:4px;">🪙 0</div>
                    <div id="vipBadge" class="vip-tag" style="display:inline-block;">VIP</div>
                </div>
            </div>
            
            <a onclick="openReferModal()"><i class="fa-solid fa-share-nodes text-blue-400"></i> রেফার ও ইনকাম</a>
            <a onclick="openReqModal()"><i class="fa-solid fa-code-pull-request text-green-400"></i> রিকোয়েস্ট মুভি</a>
            <div style="height: 1px; background: #334155; margin: 4px 0;"></div>
            <a onclick="tg.showAlert(`ডাউনলোডের নিয়ম:\n১. ডাউনলোড বাটনে ক্লিক করুন।\n২. লিংকে গিয়ে ${AD_WAIT_TIME} সেকেন্ড অপেক্ষা করুন।\n৩. মিনি অ্যাপে ব্যাক করলেই ভিডিও অটোমেটিক বটের ইনবক্সে চলে যাবে!`)"><i class="fa-solid fa-circle-question text-red-400"></i> ডাউনলোডের নিয়ম</a>
            <a onclick="window.open('{{TG_LINK}}')"><i class="fa-solid fa-bullhorn text-green-400"></i> আমাদের চ্যানেল</a>
            <a onclick="window.open('{{SUPPORT_LINK}}')"><i class="fa-brands fa-telegram text-blue-400"></i> সাপোর্ট / কন্টাক্ট</a>
            
            <a onclick="window.open(window.location.origin + '/admin', '_blank')" id="adminMenuBtn" style="display: none; color: #ef4444;"><i class="fa-solid fa-screwdriver-wrench"></i> অ্যাডমিন প্যানেল</a>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 মুভি বা ওয়েব সিরিজ খুঁজুন...">
        </div>

        <div id="categoryBox" class="category-container"></div>

        <div id="trendingWrapper">
            <div class="section-title"><i class="fa-solid fa-bolt text-yellow-400"></i>Trending now</div>
            <div class="trending-container" id="trendingGrid"></div>
        </div>

        <div class="section-title" id="recentTitle"><i class="fa-solid fa-clock-rotate-left text-blue-400"></i> সর্বশেষ আপলোড</div>
        <div class="grid" id="movieGrid"></div>
        <div class="pagination" id="paginationBox"></div>
        
        <div class="developer-credit">
            <div class="dev-title"><i class="fa-solid fa-laptop-code"></i> Developed & Deployed By</div>
            <div class="dev-name">Bot Developer</div>
            <div class="dev-desc">আপনিও কি আপনার চ্যানেল বা গ্রুপের জন্য এমন হাই-কোয়ালিটি এবং প্রিমিয়াম মুভি বট বানাতে চান? আজইমাদের সাথে যোগাযোগ করুন।</div>
            <button class="dev-btn" onclick="window.open('https://t.me/ProBotDeveloperBot', '_blank')">
                <i class="fa-brands fa-telegram"></i> Contact Developer
            </button>
        </div>

        <!-- Floating Buttons (Moved up) -->
        <div class="floating-btn btn-18" onclick="window.open('{{LINK_18}}')">18+</div>
        <div class="floating-btn btn-tg" onclick="window.open('{{TG_LINK}}')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openReqModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <!-- NEW: Netflix Style Bottom Navigation -->
        <div class="bottom-nav">
            <div class="nav-item active" id="navHome" onclick="goHome()">
                <i class="fa-solid fa-house"></i>
                <span>Home</span>
            </div>
            <div class="nav-item" id="navSearch" onclick="focusSearch()">
                <i class="fa-solid fa-magnifying-glass"></i>
                <span>Search</span>
            </div>
            <div class="nav-item" id="navVip" onclick="openVipModal()">
                <i class="fa-solid fa-coins"></i>
                <span id="navCoinText">0 🪙</span>
            </div>
            <div class="nav-item" id="navProfile" onclick="toggleMenu(event)">
                <i class="fa-solid fa-user"></i>
                <span>Profile</span>
            </div>
        </div>

        <!-- Download Modal -->
        <div id="qualityModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('qualityModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 id="modalTitle" style="color:#38bdf8; margin-bottom: 15px; font-size: 22px; font-weight:900;">Movie Title</h2>
                
                <div style="background: rgba(15, 23, 42, 0.9); border-left: 4px solid #f59e0b; padding: 12px; border-radius: 8px; text-align: left; margin-bottom: 20px;">
                    <p style="color:#fbbf24; font-weight:bold; font-size: 15px; margin-bottom: 8px;"><i class="fa-solid fa-circle-info"></i> কীভাবে ডাউনলোড করবেন?</p>
                    <p style="color:#cbd5e1; font-size: 13.5px; line-height: 1.6;">১. নিচের ডাউনলোড বাটনে ক্লিক করুন।<br>২. একটি নতুন পেইজ ওপেন হবে, সেখানে <b>{{AD_TIME}} সেকেন্ড</b> অপেক্ষা করুন।<br>৩. এরপর শুধু ব্যাক করে মিনি অ্যাপে আসলেই অটোমেটিক ভিডিওটি আপনার বটের ইনবক্সে চলে যাবে!</p>
                </div>

                <div id="qualityList" style="display: flex; flex-direction: column; gap: 8px;"></div>
            </div>
        </div>

        <!-- Direct Link Ad Modal -->
        <div id="directLinkModal" class="modal">
            <div class="modal-content" style="background: transparent; border: none; padding: 0;">
                <div class="close-icon" onclick="document.getElementById('directLinkModal').style.display='none'" style="top: -15px; right: 5px; z-index: 1000;"><i class="fa-solid fa-xmark"></i></div>
                <div class="dl-rgb-wrap">
                    <div class="dl-inner-box">
                        <h2 style="color: #4ade80; font-size: 24px; font-weight: 900;"><i class="fa-solid fa-unlock-keyhole"></i> আনলক করুন</h2>
                        <p id="dlDescText" style="color: #cbd5e1; font-size: 15px; font-weight: 600; text-align:center;">
                            ফাইল আনলক করতে নিচের লিংকে গিয়ে <b>{{AD_TIME}} সেকেন্ড</b> অপেক্ষা করুন।
                        </p>
                        <button id="dlClickBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); margin-top: 10px;" onclick="executeDirectLink()">🔗 Click Here (Open Link)</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- VIP & Coin Modal (Coin System) -->
        <div id="vipModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('vipModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fbbf24; font-size: 24px; margin-bottom:15px;"><i class="fa-solid fa-coins"></i> VIP & Coins</h2>
                
                <div style="background: rgba(15, 23, 42, 0.9); border: 1px solid #3b82f6; padding: 15px; border-radius: 12px; margin-bottom: 20px;">
                    <p style="color:#94a3b8; font-size: 14px; font-weight:bold;">আপনার বর্তমান কয়েন:</p>
                    <h1 style="color:#f59e0b; font-size: 36px; font-weight:900; margin: 5px 0;"><span id="modalCoinText">0</span> 🪙</h1>
                    <p style="color:#cbd5e1; font-size: 12px;">(১ দিন VIP = ৩০ কয়েন)</p>
                </div>
                
                <button id="coinAdBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); margin-bottom: 12px;" onclick="executeCoinAd()">
                    <i class="fa-solid fa-play"></i> অ্যাড দেখে ৫ কয়েন নিন
                </button>
                
                <button class="btn-submit" style="background: linear-gradient(45deg, #10b981, #059669);" onclick="buyVipWithCoins()">
                    <i class="fa-solid fa-crown"></i> ৩০ কয়েনে ১ দিন VIP নিন
                </button>
            </div>
        </div>

        <!-- Refer Modal -->
        <div id="referModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('referModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <i class="fa-solid fa-share-nodes" style="font-size:60px; color:#38bdf8;"></i>
                <h2 style="margin:15px 0; color:white; font-size: 24px;">রেফার ও ইনকাম</h2>
                <p style="color:#cbd5e1; font-size:15px; margin-bottom:15px;">প্রতিটি সফল রেফারের জন্য পাবেন <b>১০ কয়েন!</b></p>
                <div style="background:#0f172a; padding:15px; border:1px dashed #3b82f6; margin-bottom:15px; word-break:break-all;" id="refLinkText">...</div>
                <button class="btn-submit" onclick="copyReferLink()">লিংক কপি করুন</button>
            </div>
        </div>
        
        <!-- Request Modal -->
        <div id="reqModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('reqModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:white; font-size: 22px; margin-bottom:15px;">মুভি রিকোয়েস্ট 🗳️</h2>
                <input type="text" id="reqText" class="search-input" placeholder="মুভির নাম...">
                <button class="btn-submit" style="margin-top:10px;" onclick="sendReq()">রিকোয়েস্ট পাঠান</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            const AD_WAIT_TIME = {{AD_TIME}}; 
            
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let isUserVip = false;
            let userCoins = 0;
            let loadedMovies = {}; 
            let currentPage = 1; 
            let searchQuery = "";
            let activeCategory = "";
            let autoScrollInterval;

            function setNavActive(index) {
                const items = document.querySelectorAll('.nav-item');
                items.forEach((item, i) => {
                    if(i === index) item.classList.add('active');
                    else item.classList.remove('active');
                });
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                    userCoins = data.coins || 0;
                    
                    let firstName = tg.initDataUnsafe?.user?.first_name || 'Guest';
                    document.getElementById('menuUname').innerText = firstName;
                    
                    document.getElementById('coinDisplay').innerText = `🪙 ${userCoins}`;
                    document.getElementById('navCoinText').innerText = `${userCoins} 🪙`;
                    document.getElementById('modalCoinText').innerText = userCoins;
                    
                    if(isUserVip) {
                        document.getElementById('vipBadge').style.display = 'inline-block';
                        document.getElementById('menuStatus').innerText = '👑 VIP User';
                        document.getElementById('menuStatus').style.color = '#fbbf24';
                    } else {
                        document.getElementById('vipBadge').style.display = 'none';
                        document.getElementById('menuStatus').innerText = 'Free User';
                        document.getElementById('menuStatus').style.color = '#94a3b8';
                    }
                    
                    if(data.admin) {
                        document.getElementById('adminMenuBtn').style.display = 'flex';
                    }

                    document.getElementById('refLinkText').innerText = `https://t.me/${BOT_UNAME}?start=ref_${uid}`;
                } catch(e) {}
            }

            function toggleMenu(e) { 
                e.stopPropagation(); 
                setNavActive(3);
                const m = document.getElementById('dropdownMenu'); 
                m.style.display = m.style.display === 'block' ? 'none' : 'block'; 
            }
            
            function closeMenu() { 
                document.getElementById('dropdownMenu').style.display = 'none'; 
            }
            
            function goHome() { 
                setNavActive(0);
                document.getElementById('searchInput').value = ""; 
                searchQuery = ""; 
                activeCategory = "";
                document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
                let firstCatBtn = document.querySelector('.cat-btn');
                if(firstCatBtn) firstCatBtn.classList.add('active');
                
                document.getElementById('trendingWrapper').style.display = 'block';
                loadTrending();
                loadMovies(1); 
                closeMenu(); 
                window.scrollTo({ top: 0, behavior: 'smooth' }); 
            }
            
            function focusSearch() {
                setNavActive(1);
                closeMenu();
                window.scrollTo({ top: 0, behavior: 'smooth' });
                setTimeout(() => document.getElementById('searchInput').focus(), 300);
            }
            
            function openVipModal() { 
                setNavActive(2);
                document.getElementById('vipModal').style.display = 'flex'; 
                closeMenu(); 
            }

            function openReferModal() { document.getElementById('referModal').style.display = 'flex'; closeMenu(); }
            function copyReferLink() { navigator.clipboard.writeText(document.getElementById('refLinkText').innerText); tg.showAlert("✅ কপি হয়েছে!"); }
            function openReqModal() { document.getElementById('reqModal').style.display = 'flex'; closeMenu(); }
            
            async function sendReq() {
                const text = document.getElementById('reqText').value;
                if(!text) return;
                await fetch('/api/request', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, uname: tg.initDataUnsafe.user?.first_name || 'Guest', movie: text, initData: INIT_DATA}) });
                document.getElementById('reqText').value = ''; tg.showAlert('রিকোয়েস্ট পাঠানো হয়েছে!'); document.getElementById('reqModal').style.display='none';
            }

            function formatViews(n) { if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'; if (n >= 1000) return (n / 1000).toFixed(1) + 'K'; return n; }

            async function loadCategories() {
                try {
                    const res = await fetch('/api/categories');
                    const cats = await res.json();
                    if(cats.length === 0) return;
                    let html = `<button class="cat-btn active" onclick="setCategory('', this)">All</button>`;
                    cats.forEach(c => { html += `<button class="cat-btn" onclick="setCategory('${c.replace(/'/g, "\\'")}', this)">${c}</button>`; });
                    document.getElementById('categoryBox').innerHTML = html;
                } catch(e) {}
            }

            function setCategory(cat, btnElement) {
                activeCategory = cat;
                document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
                btnElement.classList.add('active');
                searchQuery = ""; 
                document.getElementById('searchInput').value = "";
                document.getElementById('trendingWrapper').style.display = cat === "" ? 'block' : 'none';
                loadMovies(1);
            }

            function startAutoScroll() {
                if(autoScrollInterval) clearInterval(autoScrollInterval);
                autoScrollInterval = setInterval(() => {
                    let grid = document.getElementById('trendingGrid');
                    if(grid) {
                        if (grid.scrollLeft >= (grid.scrollWidth - grid.clientWidth - 10)) grid.scrollTo({ left: 0, behavior: 'smooth' });
                        else grid.scrollBy({ left: 295, behavior: 'smooth' });
                    }
                }, 3000);
            }

            async function loadTrending() {
                try {
                    const r = await fetch(`/api/trending?uid=${uid}`);
                    const data = await r.json();
                    const grid = document.getElementById('trendingGrid');
                    if(data.length === 0) return document.getElementById('trendingWrapper').style.display = 'none';
                    grid.innerHTML = data.map(m => {
                        loadedMovies[m._id] = m;
                        return `<div class="trending-card" onclick="openQualityModal('${m._id.replace(/'/g, "\\'")}')">
                            <div class="post-content">
                                <div class="top-badge">🔥 TOP</div>
                                <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">
                                <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                <div class="view-badge"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                            </div>
                            <div class="card-footer">
                                <div class="channel-logo">MB</div>
                                <div class="title-text">${m._id}</div>
                            </div>
                        </div>`;
                    }).join('');
                    setTimeout(startAutoScroll, 1000);
                } catch(e) {}
            }

            async function loadMovies(page = 1) {
                currentPage = page;
                const grid = document.getElementById('movieGrid');
                grid.innerHTML = "<p style='color:white; text-align:center;'>Loading...</p>";
                try {
                    const r = await fetch(`/api/list?page=${currentPage}&q=${encodeURIComponent(searchQuery)}&uid=${uid}&cat=${encodeURIComponent(activeCategory)}`);
                    const data = await r.json();
                    if(data.movies.length === 0) return grid.innerHTML = `<p style='text-align:center; color:#fbbf24;'>কোনো মুভি পাওয়া যায়নি!</p>`;
                    
                    grid.innerHTML = data.movies.map(m => {
                        loadedMovies[m._id] = m; 
                        return `<div class="card" onclick="openQualityModal('${m._id.replace(/'/g, "\\'")}')">
                            <div class="post-content">
                                <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">
                                <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                <div class="view-badge"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                            </div>
                            <div class="card-footer">
                                <div class="channel-logo">MB</div>
                                <div class="title-text">${m._id}</div>
                            </div>
                        </div>`;
                    }).join('');
                    
                    let html = "";
                    if(data.total_pages > 1) {
                        html += `<button class="page-btn" ${currentPage === 1 ? 'disabled style="opacity:0.5;"' : ''} onclick="loadMovies(${currentPage - 1}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });"><i class="fa-solid fa-angle-left"></i></button>`;
                        
                        let startP = Math.max(1, currentPage - 1);
                        let endP = Math.min(data.total_pages, currentPage + 1);
                        
                        for(let i=startP; i<=endP; i++) { 
                            html += `<button class="page-btn ${i===currentPage?'active':''}" onclick="loadMovies(${i}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });">${i}</button>`; 
                        }
                        
                        html += `<button class="page-btn" ${currentPage === data.total_pages ? 'disabled style="opacity:0.5;"' : ''} onclick="loadMovies(${currentPage + 1}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });"><i class="fa-solid fa-angle-right"></i></button>`;
                    }
                    document.getElementById('paginationBox').innerHTML = html;
                } catch(e) {}
            }

            let timeout = null;
            document.getElementById('searchInput').addEventListener('input', function(e) {
                clearTimeout(timeout); searchQuery = e.target.value.trim();
                if(searchQuery !== "") { document.getElementById('trendingWrapper').style.display = 'none'; activeCategory = ""; document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active')); } 
                else { document.getElementById('trendingWrapper').style.display = 'block'; }
                timeout = setTimeout(() => loadMovies(1), 500); 
            });

            // --- Gallery Viewer Logic ---
            let currentGalleryImages = [];
            let currentGalleryIndex = 0;

            function openQualityModal(title) {
                const movie = loadedMovies[title];
                document.getElementById('modalTitle').innerText = title;
                
                let html = '';

                // Video files renderer
                movie.files.forEach(f => {
                    let isFree = f.is_unlocked || isUserVip;
                    let icon = isFree ? '<i class="fa-solid fa-paper-plane text-green-400"></i>' : '<i class="fa-solid fa-lock text-red-400"></i>';
                    let cls = isFree ? 'border-left: 5px solid #10b981;' : 'border-left: 5px solid #ef4444;';
                    
                    html += `<div class="rgb-border" onclick="handleQualityClick('${f.id}', ${f.is_unlocked})">
                                <div class="rgb-inner" style="${cls}"><span><i class="fa-solid fa-download"></i> ${f.quality}</span> ${icon}</div>
                             </div>`;
                });

                // Gallery Button renderer
                if(movie.gallery && movie.gallery.length > 0) {
                    let galIdsStr = movie.gallery.join(',');
                    let isGalUnlocked = isUserVip || movie.files.some(f => f.is_unlocked); 
                    let gIcon = isGalUnlocked ? '<i class="fa-solid fa-images text-pink-400"></i>' : '<i class="fa-solid fa-lock text-red-400"></i>';
                    let gCls = isGalUnlocked ? 'border-left: 5px solid #ec4899;' : 'border-left: 5px solid #ef4444;';
                    
                    html += `<h3 style="color:#f9a8d4; font-size:15px; margin: 20px 0 8px; text-align:left; border-bottom:1px solid #831843; padding-bottom:5px;"><i class="fa-solid fa-fire text-pink-500"></i> ইন-অ্যাপ গ্যালারি:</h3>`;
                    
                    html += `<div class="rgb-border" style="background: linear-gradient(45deg, #ec4899, #f43f5e, #fbbf24, #ec4899);" onclick="handleGalleryClick('${galIdsStr}', ${isGalUnlocked}, '${movie.files[0].id}')">
                                <div class="rgb-inner" style="${gCls}"><span><i class="fa-regular fa-image"></i> ${movie.gallery.length} টি পিকচার দেখুন</span> ${gIcon}</div>
                             </div>`;
                }

                document.getElementById('qualityList').innerHTML = html;
                document.getElementById('qualityModal').style.display = 'flex';
            }

            let pendingGalleryIds = "";
            function handleGalleryClick(idsStr, isUnlocked, firstFileId) {
                document.getElementById('qualityModal').style.display = 'none';
                if(isUnlocked || isUserVip) {
                    openGalleryViewer(idsStr.split(','));
                } else {
                    pendingGalleryIds = idsStr;
                    currentFileId = "GALLERY_UNLOCK_" + firstFileId;
                    document.getElementById('directLinkModal').style.display = 'flex';
                    resetDlButton();
                }
            }

            function openGalleryViewer(imgArray) {
                if(!imgArray || imgArray.length === 0) return;
                currentGalleryImages = imgArray;
                currentGalleryIndex = 0;
                document.getElementById('gTotal').innerText = currentGalleryImages.length;
                updateGalleryImage();
                document.getElementById('galleryViewer').style.display = 'flex';
            }

            function updateGalleryImage() {
                document.getElementById('gCurrent').innerText = currentGalleryIndex + 1;
                document.getElementById('gImage').src = `/api/image/${currentGalleryImages[currentGalleryIndex]}`;
            }

            function nextGalleryImage() {
                if(currentGalleryIndex < currentGalleryImages.length - 1) {
                    currentGalleryIndex++;
                    updateGalleryImage();
                }
            }

            function prevGalleryImage() {
                if(currentGalleryIndex > 0) {
                    currentGalleryIndex--;
                    updateGalleryImage();
                }
            }

            function closeGallery() {
                document.getElementById('galleryViewer').style.display = 'none';
                document.getElementById('gImage').src = "";
            }
            // -----------------------------

            let currentFileId = null; 

            function handleQualityClick(fileId, isUnlocked) {
                document.getElementById('qualityModal').style.display = 'none';
                if(isUnlocked || isUserVip) { 
                    sendFileAndClose(fileId); 
                } else { 
                    currentFileId = fileId; 
                    document.getElementById('directLinkModal').style.display = 'flex';
                    resetDlButton();
                }
            }

            let linkOpenedAt = 0;
            let isWaitingForReturn = false;
            let dlTimerInterval = null;

            function resetDlButton() {
                const btn = document.getElementById('dlClickBtn');
                btn.onclick = executeDirectLink;
                btn.innerText = "🔗 Click Here (Open Link)";
                btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
                btn.disabled = false;
            }

            function executeDirectLink() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) { 
                    document.getElementById('directLinkModal').style.display = 'none'; 
                    if (currentFileId && currentFileId.startsWith("GALLERY_UNLOCK_")) {
                        openGalleryViewer(pendingGalleryIds.split(','));
                    } else if (currentFileId) {
                        sendFileAndClose(currentFileId); 
                    }
                    return; 
                }
                
                tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]);
                linkOpenedAt = Date.now(); 
                isWaitingForReturn = true;
                
                const btn = document.getElementById('dlClickBtn');
                btn.disabled = true; 
                let timeLeft = AD_WAIT_TIME; 
                btn.style.background = "#475569";
                
                dlTimerInterval = setInterval(() => {
                    timeLeft--; 
                    if(timeLeft > 0) {
                        btn.innerText = `⏳ অপেক্ষা করুন... (${timeLeft}s)`;
                    } else {
                        clearInterval(dlTimerInterval);
                        if(isWaitingForReturn) {
                            isWaitingForReturn = false;
                            document.getElementById('directLinkModal').style.display = 'none';
                            if (currentFileId && currentFileId.startsWith("GALLERY_UNLOCK_")) {
                                let realId = currentFileId.replace("GALLERY_UNLOCK_", "");
                                fetch('/api/unlock_gallery', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: realId, initData: INIT_DATA}) });
                                openGalleryViewer(pendingGalleryIds.split(','));
                            } else if (currentFileId) {
                                sendFileAndClose(currentFileId);
                            }
                        }
                    }
                }, 1000);
            }

            let coinLinkOpenedAt = 0; 
            let isWaitingForCoinReturn = false; 
            let coinTimerInterval = null;

            function resetCoinButton() {
                const btn = document.getElementById('coinAdBtn');
                btn.disabled = false;
                btn.onclick = executeCoinAd;
                btn.innerHTML = '<i class="fa-solid fa-play"></i> অ্যাড দেখে ৫ কয়েন নিন';
                btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
            }

            function executeCoinAd() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) { tg.showAlert("⚠️ কোনো অ্যাড পাওয়া যায়নি!"); return; }
                tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]);
                
                coinLinkOpenedAt = Date.now(); 
                isWaitingForCoinReturn = true;
                
                const btn = document.getElementById('coinAdBtn');
                btn.disabled = true; 
                let timeLeft = AD_WAIT_TIME; 
                btn.style.background = "#475569";
                
                coinTimerInterval = setInterval(() => {
                    timeLeft--; 
                    if(timeLeft > 0) {
                        btn.innerHTML = `<i class="fa-solid fa-play"></i> অপেক্ষা করুন... (${timeLeft}s)`;
                    } else {
                        clearInterval(coinTimerInterval);
                        if(isWaitingForCoinReturn) {
                            isWaitingForCoinReturn = false;
                            claimAdCoin();
                            resetCoinButton();
                        }
                    }
                }, 1000);
            }

            document.addEventListener("visibilitychange", function() {
                if (document.visibilityState === 'visible') {
                    let now = Date.now();
                    
                    if (isWaitingForReturn) {
                        isWaitingForReturn = false; 
                        clearInterval(dlTimerInterval);
                        
                        let elapsedSeconds = (now - linkOpenedAt) / 1000;
                        if (elapsedSeconds < AD_WAIT_TIME - 1) { 
                            tg.showAlert(`⚠️ আপনাকে অবশ্যই পুরো ${AD_WAIT_TIME} সেকেন্ড লিংকে অপেক্ষা করতে হবে।`);
                            resetDlButton();
                        } else { 
                            document.getElementById('directLinkModal').style.display = 'none'; 
                            if (currentFileId && currentFileId.startsWith("GALLERY_UNLOCK_")) {
                                let realId = currentFileId.replace("GALLERY_UNLOCK_", "");
                                fetch('/api/unlock_gallery', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: realId, initData: INIT_DATA}) });
                                openGalleryViewer(pendingGalleryIds.split(','));
                            } else if (currentFileId) {
                                sendFileAndClose(currentFileId);
                            }
                        }
                    }
                    
                    if (isWaitingForCoinReturn) {
                        isWaitingForCoinReturn = false; 
                        clearInterval(coinTimerInterval);
                        
                        let elapsedSeconds = (now - coinLinkOpenedAt) / 1000;
                        if (elapsedSeconds < AD_WAIT_TIME - 1) {
                            tg.showAlert(`⚠️ আপনাকে অবশ্যই পুরো ${AD_WAIT_TIME} সেকেন্ড লিংকে অপেক্ষা করতে হবে।`);
                            resetCoinButton();
                        } else { 
                            claimAdCoin(); 
                            resetCoinButton();
                        }
                    }
                }
            });

            async function claimAdCoin() {
                try {
                    const res = await fetch('/api/add_coin', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, initData: INIT_DATA}) });
                    const data = await res.json();
                    if(data.ok) { 
                        tg.showAlert("🎉 অভিনন্দন! আপনি ৫টি কয়েন পেয়েছেন।");
                        fetchUserInfo(); 
                    } else { tg.showAlert("⚠️ কোনো সমস্যা হয়েছে।"); }
                } catch (e) {}
            }

            async function buyVipWithCoins() {
                if(userCoins < 30) {
                    tg.showAlert("⚠️ আপনার কাছে পর্যাপ্ত কয়েন নেই! অ্যাড দেখে অথবা রেফার করে কয়েন জমান।");
                    return;
                }
                if(confirm("আপনি কি ৩০ কয়েন দিয়ে ১ দিনের VIP নিতে চান?")) {
                    try {
                        const res = await fetch('/api/buy_vip', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, initData: INIT_DATA}) });
                        const data = await res.json();
                        if(data.ok) { 
                            document.getElementById('vipModal').style.display = 'none';
                            tg.showAlert("🎉 সফল! আপনার ২৪ ঘণ্টার VIP চালু হয়েছে।");
                            fetchUserInfo(); 
                        } else { tg.showAlert(data.msg); }
                    } catch (e) {}
                }
            }

            function showProcessingUI() {
                let procModal = document.getElementById('processingModalCustom');
                if(!procModal) {
                    procModal = document.createElement('div');
                    procModal.id = 'processingModalCustom';
                    procModal.style.cssText = 'position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.95); z-index:9999; display:flex; align-items:center; justify-content:center; flex-direction:column; backdrop-filter: blur(5px);';
                    procModal.innerHTML = `
                        <div class="spinner-new"></div>
                        <div class="big-processing-text">ফাইল পাঠানো হচ্ছে...</div>
                        <div style="color:#cbd5e1; margin-top:15px; font-size:16px; font-weight:bold;">অপেক্ষা করুন, বক্সে ফাইল যাচ্ছে!</div>
                    `;
                    document.body.appendChild(procModal);
                }
                procModal.style.display = 'flex';
            }

            function hideProcessingUI() {
                let procModal = document.getElementById('processingModalCustom');
                if(procModal) procModal.style.display = 'none';
            }

            async function sendFileAndClose(id) {
                showProcessingUI(); 
                try {
                    const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: id, initData: INIT_DATA}) });
                    const data = await res.json();
                    
                    if(data.ok) { 
                        setTimeout(() => {
                            tg.close();
                        }, 500);
                    } else {
                        hideProcessingUI();
                        tg.showAlert("⚠️ সেশন এক্সপায়ার হয়েছে! দয়া করে মিনি অ্যাপটি কেটে আবার ওপেন করুন।");
                    }
                } catch (e) {
                    hideProcessingUI();
                    tg.showAlert("⚠️ ইন্টারনেট সংযোগ সমস্যা! আবার চেষ্টা করুন।");
                }
            }

            fetchUserInfo(); loadCategories(); loadTrending(); loadMovies(1); 
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{TG_LINK}}", tg_url).replace("{{SUPPORT_LINK}}", support_link).replace("{{LINK_18}}", link_18).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{AD_TIME}}", str(ad_wait_seconds))
    return html_code

# ==========================================
# 8. Optimized APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    is_admin = uid in admin_cache
    if not user: return {"vip": False, "admin": is_admin, "coins": 0}
    return {
        "vip": user.get("vip_until", datetime.datetime.utcnow()) > datetime.datetime.utcnow(), 
        "admin": is_admin,
        "coins": user.get("coins", 0)
    }

class UserActionModel(BaseModel):
    uid: int
    initData: str

@app.post("/api/add_coin")
async def add_coin_api(d: UserActionModel):
    if d.uid == 0 or not validate_tg_data(d.initData): return {"ok": False}
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": 5}})
    return {"ok": True}

@app.post("/api/buy_vip")
async def buy_vip_api(d: UserActionModel):
    if d.uid == 0 or not validate_tg_data(d.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": d.uid})
    coins = user.get("coins", 0)
    
    if coins < 30: return {"ok": False, "msg": "পর্যাপ্ত কয়েন নেই!"}
    
    now = datetime.datetime.utcnow()
    current_vip = user.get("vip_until", now) if user.get("vip_until") else now
    if current_vip < now: current_vip = now
    new_vip = current_vip + datetime.timedelta(days=1)
    
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": -30}, "$set": {"vip_until": new_vip}})
    return {"ok": True}

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    pipeline = [
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}, "clicks": {"$sum": "$clicks"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}, "gallery": {"$first": "$gallery"}}},
        {"$sort": {"clicks": -1}}, {"$limit": 10}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(10)
    for m in movies:
        m["photo_id"] = f"db_{m['db_photo_id']}" if m.get("db_photo_id") else m.get("photo_id")
        m["gallery"] = m.get("gallery", [])
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return movies

@app.get("/api/categories")
async def get_categories():
    categories = await db.movies.distinct("categories")
    return [c for c in categories if c]

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = ""):
    limit = 20  
    skip = (page - 1) * limit
    unlocked_ids = []
    
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat: match_stage["categories"] = cat

    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}, "gallery": {"$first": "$gallery"}}},
        {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
    ]
    total_groups = (await db.movies.aggregate([{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]).to_list(1))
    total_pages = (total_groups[0]["total"] + limit - 1) // limit if total_groups else 0

    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        m["photo_id"] = f"db_{m['db_photo_id']}" if m.get("db_photo_id") else m.get("photo_id")
        m["gallery"] = m.get("gallery", [])
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        file_path = None
        
        if cache and cache.get("expires_at", now) > now: 
            file_path = cache["file_path"]
        else:
            if photo_id.startswith("db_"):
                msg_id = int(photo_id.split("_")[1])
                if DB_CHANNEL_ID:
                    pyro_msg = await pyro_app.get_messages(DB_CHANNEL_ID, msg_id)
                    if pyro_msg.photo:
                        actual_file_id = pyro_msg.photo.file_id
                        file_path = (await bot.get_file(actual_file_id)).file_path
            else:
                file_path = (await bot.get_file(photo_id)).file_path
            
            if file_path:
                await db.file_cache.update_one(
                    {"photo_id": photo_id}, 
                    {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(minutes=50)}}, 
                    upsert=True
                )
        
        if not file_path: return {"error": "not found"}
            
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except Exception as e: return {"error": str(e)}

class SendRequestModel(BaseModel):
    userId: int
    movieId: str
    initData: str

@app.post("/api/unlock_gallery")
async def unlock_gallery(d: SendRequestModel):
    if d.userId == 0 or not validate_tg_data(d.initData): return {"ok": False}
    now = datetime.datetime.utcnow()
    await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
    return {"ok": True}

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or not validate_tg_data(d.initData): return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
        if m:
            now = datetime.datetime.utcnow()
            user = await db.users.find_one({"user_id": d.userId})
            is_vip = user and user.get("vip_until", now) > now
            
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True

            caption = f"🎥 <b>{m['title']} [{m.get('quality', 'HD')}]</b>\n\n📥 Join: @TGLinkBase"
            if not is_vip:
                caption += f"\n\n⏳ <i>সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি <b>{del_minutes} মিনিট</b> পর অটোমেটিক ডিলিট হয়ে যাবে!</i>"

            db_file_id = m.get("db_file_id")
            sent_msg = None
            
            if db_file_id and DB_CHANNEL_ID:
                sent_msg = await bot.copy_message(
                    chat_id=d.userId, 
                    from_chat_id=DB_CHANNEL_ID, 
                    message_id=db_file_id, 
                    caption=caption, 
                    parse_mode="HTML", 
                    protect_content=is_protected
                )
            else:
                if m.get("file_type") == "video":
                    sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
                else:
                    sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            
            if sent_msg and not is_vip:
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": now + datetime.timedelta(minutes=del_minutes)})
    except Exception: pass
    return {"ok": True}

class ReqModel(BaseModel):
    uid: int
    uname: str
    movie: str
    initData: str

@app.post("/api/request")
async def handle_request(data: ReqModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    
    all_admins = set([OWNER_ID])
    async for a in db.admins.find(): 
        all_admins.add(a["user_id"])
        
    for admin_id in all_admins:
        try:
            await bot.send_message(
                admin_id, 
                f"🔔 <b>নতুন মুভি রিকোয়েস্ট!</b>\n👤 ইউজার: {data.uname} (<code>{data.uid}</code>)\n🎬 মুভি: <b>{data.movie}</b>", 
                parse_mode="HTML"
            )
        except Exception: pass
    return {"ok": True}

# ==========================================
# 9. Main Application Startup
# ==========================================
async def start():
    print("Initializing Database...")
    await init_db()
    await load_admins()
    await load_banned_users()
    
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), loop="asyncio")
    server = uvicorn.Server(config)
    
    print("Starting Background Workers...")
    await pyro_app.start()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(video_queue_worker()) 
    
    print("Connecting to Telegram Bot API...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("Server is Running!")
    asyncio.create_task(server.serve())
    await dp.start_polling(bot)

if __name__ == "__main__": 
    try: asyncio.run(start())
    except (KeyboardInterrupt, SystemExit): print("Bot Stopped!")
