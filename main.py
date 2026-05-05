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
BOT_USERNAME = "BDViralLinkProBot" # আপনার বটের ইউজারনেম

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
                
            photo_msg = await bot.send_photo(admin_id, photo=FSInputFile(collage_path), caption=f"✅ <b>{auto_title}</b> Successfully Uploaded!")
            photo_id = photo_msg.photo[-1].file_id
            
            await db.movies.insert_one({
                "title": auto_title, "quality": "HD", "photo_id": photo_id, 
                "file_id": aiogram_file_id, "file_type": file_type,
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
                    await db.users.update_one({"user_id": referrer_id}, {"$inc": {"refer_count": 1}})
                    ref_user = await db.users.find_one({"user_id": referrer_id})
                    if ref_user and ref_user.get("refer_count", 0) % 5 == 0:
                        current_vip = ref_user.get("vip_until", now)
                        if current_vip < now: current_vip = now
                        await db.users.update_one({"user_id": referrer_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=1)}})
                        try: await bot.send_message(referrer_id, "🎉 <b>অভিনন্দন!</b> ৫ জন রেফার পূর্ণ হওয়ায় আপনাকে ২৪ ঘণ্টার VIP দেওয়া হয়েছে!", parse_mode="HTML")
                        except: pass
            except Exception: pass

        await db.users.insert_one({
            "user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "refer_count": 0, "vip_until": now - datetime.timedelta(days=1)
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
        if target_uid in admin_cache: return await m.answer("⚠️ অ্যাডমিনকে ব্যান করা যাবে না!")
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

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache and not m.text.startswith("/"))
async def forward_to_admin(m: types.Message):
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই দিন", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <b>Message from <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a></b>:\n\n{m.text or 'Media file'}", parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception: pass


# ==========================================
# 5. Movie Upload Logic (With Manual Channel Post)
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    config = await db.settings.find_one({"id": "auto_upload_mode"})
    is_auto = config["status"] if config else False
    
    if is_auto:
        aiogram_fid = m.video.file_id if m.video else m.document.file_id
        file_type = "video" if m.video else "document"
        await video_queue.put((m.chat.id, m.message_id, aiogram_fid, file_type))
        await m.answer(f"✅ ভিডিও অটো-প্রসেস কিউতে যুক্ত হয়েছে! সিরিয়াল: <b>{video_queue.qsize()}</b>", parse_mode="HTML")
    else:
        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"
        await state.set_state(AdminStates.waiting_for_photo)
        await state.update_data(file_id=fid, file_type=ftype)
        await m.answer("✅ ফাইল পেয়েছি! এবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।", parse_mode="HTML")

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
    
    if success:
        sent_photo = await m.answer_photo(FSInputFile(temp_out), caption="✅ <b>পোস্টার রেডি!</b>\nএবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        await state.update_data(photo_id=sent_photo.photo[-1].file_id)
    else:
        await state.update_data(photo_id=photo_id)
        await m.answer("✅ পোস্টার পেয়েছি! এবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        
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
    quality = m.text.strip()
    data = await state.get_data()
    await state.clear()
    
    title = data["title"]
    photo_id = data["photo_id"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    
    await m.answer(f"🎉 <b>{title} [{quality}]</b> অ্যাপে যুক্ত করা হয়েছে!", parse_mode="HTML")

    if CHANNEL_ID:
        try:
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="📥 ভিডিওটি দেখতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = (f"🔥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>কোয়ালিটি:</b> {quality}\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
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
# 7. Web Admin Panel HTML & API (With Search & Pagination)
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Panel</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    </head>
    <body class="bg-gray-900 text-white p-5 font-sans">
        <div class="max-w-5xl mx-auto">
            <h1 class="text-3xl font-bold text-red-500 mb-6 border-b border-gray-700 pb-3"><i class="fa-solid fa-screwdriver-wrench"></i> Admin Dashboard</h1>
            <div class="bg-gray-800 rounded-xl shadow-lg border border-gray-700 p-6">
                
                <div class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
                    <h2 class="text-xl font-bold text-gray-200">Manage Movies</h2>
                    <input type="text" id="adminSearch" placeholder="🔍 Search Movies..." class="bg-gray-700 text-white px-4 py-2 rounded-lg border border-gray-600 focus:outline-none w-full md:w-1/3">
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm whitespace-nowrap">
                        <thead class="bg-gray-700 text-gray-300">
                            <tr><th class="p-4">Title</th><th class="p-4">Views</th><th class="p-4">Files</th><th class="p-4">Action</th></tr>
                        </thead>
                        <tbody id="movieTableBody"><tr><td colspan="4" class="text-center p-8 text-gray-400">Loading...</td></tr></tbody>
                    </table>
                </div>
                
                <!-- Pagination Controls -->
                <div class="flex justify-center items-center gap-3 mt-6" id="adminPagination"></div>

            </div>
        </div>
        <script>
            let currentPage = 1;
            let searchQuery = "";
            let searchTimeout = null;

            document.getElementById('adminSearch').addEventListener('input', function(e) {
                clearTimeout(searchTimeout);
                searchQuery = e.target.value.trim();
                searchTimeout = setTimeout(() => loadAdminData(1), 500);
            });

            async function loadAdminData(page = 1) {
                currentPage = page;
                document.getElementById('movieTableBody').innerHTML = '<tr><td colspan="4" class="text-center p-8 text-gray-400">Loading...</td></tr>';
                const res = await fetch(`/api/admin/data?page=${currentPage}&q=${encodeURIComponent(searchQuery)}`); 
                const data = await res.json();
                
                let html = '';
                if(data.movies.length === 0) {
                    html = '<tr><td colspan="4" class="text-center p-8 text-gray-400">No movies found.</td></tr>';
                } else {
                    data.movies.forEach(m => {
                        html += `<tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="p-4 font-medium">${m._id}</td>
                            <td class="p-4 text-gray-400">${m.clicks} Views</td>
                            <td class="p-4 text-green-400">${m.file_count}</td>
                            <td class="p-4 flex gap-2">
                                <button onclick="addViews('${encodeURIComponent(m._id)}')" class="text-yellow-400 bg-yellow-900 px-3 py-1 rounded transition hover:bg-yellow-800">Boost</button>
                                <button onclick="deleteMovie('${encodeURIComponent(m._id)}')" class="text-red-400 bg-red-900 px-3 py-1 rounded transition hover:bg-red-800">Delete</button>
                            </td>
                        </tr>`;
                    });
                }
                document.getElementById('movieTableBody').innerHTML = html;

                // Pagination Render
                let pageHtml = "";
                if(data.total_pages > 1) {
                    pageHtml += `<button ${currentPage === 1 ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage - 1) + ')"'}>Prev</button>`;
                    pageHtml += `<span class="px-4 py-2 font-bold">Page ${currentPage} of ${data.total_pages}</span>`;
                    pageHtml += `<button ${currentPage === data.total_pages ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage + 1) + ')"'}>Next</button>`;
                }
                document.getElementById('adminPagination').innerHTML = pageHtml;
            }

            async function deleteMovie(title) {
                if(!confirm('Are you sure you want to delete ALL files for this movie?')) return;
                await fetch('/api/admin/movie/' + title, {method: 'DELETE'}); 
                loadAdminData(currentPage);
            }

            async function addViews(title) {
                let amount = prompt("How many views to add?", "1000");
                if(amount && !isNaN(amount)) {
                    await fetch('/api/admin/movie/' + title, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({add_clicks: parseInt(amount)}) });
                    loadAdminData(currentPage);
                }
            }
            loadAdminData(1);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/admin/data")
async def get_admin_data(page: int = 1, q: str = "", auth: bool = Depends(verify_admin)):
    limit = 20
    skip = (page - 1) * limit
    match_stage = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}, "file_count": {"$sum": 1}, "created_at": {"$max": "$created_at"}}}, 
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
    return {"ok": True}

# ==========================================
# 8. Web UI (Perfect & Optimized)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    tg_cfg = await db.settings.find_one({"id": "link_tg"})
    support_cfg = await db.settings.find_one({"id": "link_support"})
    b18_cfg = await db.settings.find_one({"id": "link_18"})
    bkash_cfg = await db.settings.find_one({"id": "bkash_no"})
    nagad_cfg = await db.settings.find_one({"id": "nagad_no"})
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    support_link = support_cfg['url'] if support_cfg else "https://t.me/YourSupportUsername"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    bkash_no = bkash_cfg['number'] if bkash_cfg else "Not Set"
    nagad_no = nagad_cfg['number'] if nagad_cfg else "Not Set"
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)

    html_code = r"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>BD Viral Link</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            html { scroll-behavior: smooth; }
            body { background: #0f172a; font-family: sans-serif; color: #fff; overscroll-behavior-y: none; } 
            
            header { display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; }
            .logo { font-size: 24px; font-weight: bold; }
            .logo span { background: red; color: #fff; padding: 2px 6px; border-radius: 5px; margin-left: 5px; font-size: 16px; }
            .header-right { display: flex; align-items: center; gap: 10px; }
            
            /* Home Button */
            .home-btn { background: linear-gradient(45deg, #3b82f6, #2563eb); color: white; border: none; padding: 8px 12px; border-radius: 8px; font-weight: bold; font-size: 14px; cursor: pointer; display: flex; align-items: center; gap: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.3); transition: 0.2s; }
            .home-btn:active { transform: scale(0.95); }

            .user-info { display: flex; align-items: center; gap: 8px; background: #1e293b; padding: 6px 14px; border-radius: 25px; font-weight: bold; font-size: 14px; border: 1px solid #334155; }
            .user-info img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; }
            
            .menu-btn { background: #1e293b; border: 1px solid #334155; padding: 8px 12px; border-radius: 8px; cursor: pointer; color: white; font-size: 18px; }
            
            .dropdown-menu { display: none; position: absolute; top: 65px; right: 15px; background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(10px); border: 1px solid #334155; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.5); z-index: 2000; width: 240px; transform-origin: top right; animation: menuFade 0.2s ease-out forwards; }
            @keyframes menuFade { 0% { opacity: 0; transform: scale(0.95) translateY(-10px); } 100% { opacity: 1; transform: scale(1) translateY(0); } }
            .dropdown-menu a { display: flex; align-items: center; gap: 10px; padding: 12px 15px; color: white; text-decoration: none; font-weight: 600; font-size: 14px; cursor: pointer; transition: background 0.2s ease; border-bottom: 1px solid #334155; }
            .dropdown-menu a:hover, .dropdown-menu a:active { background: rgba(51, 65, 85, 0.5); }
            .dropdown-menu a i { font-size: 16px; width: 20px; text-align: center; }
            
            .search-box { padding: 15px; }
            .search-input { width: 100%; padding: 16px; border-radius: 25px; border: none; outline: none; text-align: center; background: #1e293b; color: #fff; font-size: 18px; font-weight: bold; }
            .section-title { padding: 5px 15px 15px; font-size: 20px; font-weight: 900; display: flex; align-items: center; gap: 8px; color:#ff416c; }
            
            /* Trending Auto Scroll System */
            .trending-container { display: flex; overflow-x: auto; gap: 15px; padding: 0 15px 20px; scroll-behavior: smooth; }
            .trending-container::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 280px; max-width: 280px; background: transparent; overflow: hidden; cursor: pointer; flex-shrink: 0; position: relative; transition: transform 0.2s; }
            .trending-card:active { transform: scale(0.98); }

            /* Optimized Wide Grid System for Main Feed */
            .grid { padding: 0 15px 20px; display: flex; flex-direction: column; gap: 20px; }
            .card { background: transparent; overflow: hidden; cursor: pointer; transition: transform 0.2s; border-radius: 0; }
            .card:active { transform: scale(0.98); }
            
            .post-content { position: relative; padding: 3px; border-radius: 12px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; }
            @keyframes glowing { 0% { background-position: 0 0; } 50% { background-position: 400% 0; } 100% { background-position: 0 0; } }
            
            .post-content img { width: 100%; aspect-ratio: 16/9; height: auto; object-fit: cover; display: block; border-radius: 10px; }
            
            .card-footer { padding: 12px 5px 0; display: flex; align-items: flex-start; gap: 12px; text-align: left; }
            .channel-logo { width: 40px; height: 40px; border-radius: 50%; background: white; color: #ef4444; border: 1px solid #e5e7eb; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 16px; flex-shrink: 0; }
            .title-text { color: #f8fafc; font-size: 16px; font-weight: bold; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; margin-top: 2px; }

            .top-badge, .ep-badge, .view-badge { position: absolute; font-weight: bold; padding: 4px 8px; border-radius: 6px; font-size: 11px; z-index: 10; color: white;}
            .top-badge { top: 10px; left: 10px; background: linear-gradient(45deg, #ff0000, #cc0000); }
            .view-badge { bottom: 10px; left: 10px; background: rgba(0,0,0,0.75); }
            .ep-badge { top: 10px; right: 10px; background: #10b981; }

            /* Clean Pagination */
            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 30px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; outline: none; transition: 0.2s;}
            .page-btn:hover { background: #334155; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; }

            /* Premium Developer Credit Section */
            .developer-credit { margin: 10px 15px 110px; padding: 22px 15px; background: linear-gradient(135deg, rgba(30, 41, 59, 0.8), rgba(15, 23, 42, 0.95)); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 16px; text-align: center; box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4), 0 0 15px rgba(56, 189, 248, 0.1); backdrop-filter: blur(10px); position: relative; overflow: hidden; }
            .developer-credit::before { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent); animation: shine 3s infinite; }
            @keyframes shine { 100% { left: 200%; } }
            .dev-title { font-size: 12px; color: #94a3b8; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 5px; }
            .dev-name { font-size: 22px; font-weight: 900; background: linear-gradient(45deg, #00f2fe, #4facfe); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }
            .dev-desc { font-size: 13.5px; color: #cbd5e1; margin-bottom: 18px; line-height: 1.5; }
            .dev-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: linear-gradient(45deg, #0ea5e9, #2563eb); color: white; padding: 12px 24px; border-radius: 30px; font-size: 15px; font-weight: bold; border: none; cursor: pointer; box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4); transition: 0.2s; position: relative; z-index: 10; }
            .dev-btn:active { transform: scale(0.95); }

            .floating-btn { position: fixed; right: 20px; color: white; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 22px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
            .btn-18 { bottom: 155px; background: linear-gradient(45deg, #ff0000, #990000); font-weight: bold; font-size: 18px; border: 2px solid white; }
            .btn-tg { bottom: 95px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-req { bottom: 35px; background: linear-gradient(45deg, #10b981, #059669); }

            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: #1e293b; width: 92%; max-width: 400px; padding: 25px; border-radius: 20px; text-align: center; border: 1px solid #334155; max-height: 85vh; overflow-y: auto; position: relative; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: #334155; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; }
            
            /* Bigger Download Buttons inside Quality Modal */
            .rgb-border { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; padding: 4px; border-radius: 14px; margin-bottom: 12px; cursor: pointer; width: 100%; }
            .rgb-inner { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 20px 18px; border-radius: 12px; color: white; font-weight: 900; font-size: 18px; }

            .btn-submit { background: linear-gradient(45deg, #10b981, #059669); color: white; border: none; padding: 15px 20px; border-radius: 12px; font-weight: bold; width: 100%; font-size: 18px; cursor: pointer; }
            .vip-tag { background: linear-gradient(45deg, #fbbf24, #f59e0b); color: #000; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; display: none; margin-left:5px; }

            .dl-rgb-wrap { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; padding: 4px; border-radius: 16px; width: 100%; max-width: 350px; margin: auto; }
            .dl-inner-box { background: rgba(15, 23, 42, 0.98); border-radius: 12px; padding: 30px 20px; display: flex; flex-direction: column; align-items: center; gap: 15px; }
            
            .method-btn { padding: 12px; width: 48%; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; font-size: 16px; }
            .pay-box { background: #0f172a; border: 1px solid #334155; padding: 15px; border-radius: 10px; margin-top:15px; text-align: left; display:none; }
            .pkg-label { display: block; background: #1e293b; padding: 12px; border-radius: 8px; margin-bottom: 8px; cursor: pointer; border: 1px solid #334155; font-weight: bold; }
        </style>
    </head>
    <body onclick="closeMenu(event)">
        <header>
            <div class="logo">BD Viral <span>Link</span></div>
            <div class="header-right">
                <button onclick="goHome()" class="home-btn"><i class="fa-solid fa-house"></i> HOME</button>
                <div class="user-info">
                    <span id="uName">Guest</span>
                    <span id="vipBadge" class="vip-tag"><i class="fa-solid fa-crown"></i> VIP</span>
                </div>
                <div class="menu-btn" onclick="toggleMenu(event)"><i class="fa-solid fa-bars"></i></div>
            </div>
        </header>
        
        <!-- Enhanced Premium Dropdown Menu -->
        <div id="dropdownMenu" class="dropdown-menu">
            <div style="padding: 12px 15px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 10px;">
                <div style="width: 35px; height: 35px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 18px;">
                    <i class="fa-solid fa-user"></i>
                </div>
                <div>
                    <div style="font-size: 14px; font-weight: bold; color: white;" id="menuUname">Guest</div>
                    <div style="font-size: 11px; color: #94a3b8;" id="menuStatus">Free User</div>
                </div>
            </div>
            
            <a onclick="openVipModal()"><i class="fa-solid fa-crown text-yellow-400"></i> VIP প্যাকেজ কিনুন</a>
            <a onclick="openReferModal()"><i class="fa-solid fa-share-nodes text-blue-400"></i> রেফার ও ইনকাম</a>
            <a onclick="openReqModal()"><i class="fa-solid fa-code-pull-request text-green-400"></i> রিকোয়েস্ট মুভি</a>
            
            <div style="height: 1px; background: #334155; margin: 4px 0;"></div>
            
            <a onclick="tg.showAlert('ডাউনলোডের নিয়ম:\n১. ডাউনলোড বাটনে ক্লিক করুন।\n২. ১৫ সেকেন্ড অপেক্ষা করুন।\n৩. ভিডিওটি অটোমেটিক বটের ইনবক্সে চলে যাবে!')"><i class="fa-solid fa-circle-question text-red-400"></i> ডাউনলোডের নিয়ম</a>
            <a onclick="window.open('{{TG_LINK}}')"><i class="fa-solid fa-bullhorn text-green-400"></i> আমাদের চ্যানেল</a>
            <a onclick="window.open('{{SUPPORT_LINK}}')"><i class="fa-brands fa-telegram text-blue-400"></i> সাপোর্ট / কন্টাক্ট</a>
            
            <a onclick="window.open(window.location.origin + '/admin', '_blank')" id="adminMenuBtn" style="display: none; color: #ef4444;"><i class="fa-solid fa-screwdriver-wrench"></i> অ্যাডমিন প্যানেল</a>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 মুভি বা ওয়েব সিরিজ খুঁজুন...">
        </div>

        <!-- Trending Section -->
        <div id="trendingWrapper">
            <div class="section-title"><i class="fa-solid fa-bolt text-yellow-400"></i> ট্রেন্ডিং ভাইরাল</div>
            <div class="trending-container" id="trendingGrid"></div>
        </div>

        <div class="section-title" id="recentTitle"><i class="fa-solid fa-clock-rotate-left text-blue-400"></i> সর্বশেষ আপলোড</div>
        <div class="grid" id="movieGrid"></div>
        <div class="pagination" id="paginationBox"></div>
        
        <!-- Developer Credit Section (Fixed from Bot Admin) -->
        <div class="developer-credit">
            <div class="dev-title"><i class="fa-solid fa-laptop-code"></i> Developed & Deployed By</div>
            <div class="dev-name">Bot Developer</div>
            <div class="dev-desc">আপনিও কি আপনার চ্যানেল বা গ্রুপের জন্য এমন হাই-কোয়ালিটি এবং প্রিমিয়াম মুভি বট বানাতে চান? আজই আমাদের সাথে যোগাযোগ করুন।</div>
            <button class="dev-btn" onclick="window.open('https://t.me/ProBotDeveloperBot', '_blank')">
                <i class="fa-brands fa-telegram"></i> Contact Developer
            </button>
        </div>

        <div class="floating-btn btn-18" onclick="window.open('{{LINK_18}}')">18+</div>
        <div class="floating-btn btn-tg" onclick="window.open('{{TG_LINK}}')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openReqModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <!-- Download Modal -->
        <div id="qualityModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('qualityModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 id="modalTitle" style="color:#38bdf8; margin-bottom: 15px; font-size: 22px; font-weight:900;">Movie Title</h2>
                
                <div style="background: rgba(15, 23, 42, 0.9); border-left: 4px solid #f59e0b; padding: 12px; border-radius: 8px; text-align: left; margin-bottom: 20px;">
                    <p style="color:#fbbf24; font-weight:bold; font-size: 15px; margin-bottom: 8px;"><i class="fa-solid fa-circle-info"></i> কীভাবে ডাউনলোড করবেন?</p>
                    <p style="color:#cbd5e1; font-size: 13.5px; line-height: 1.6;">১. নিচের ডাউনলোড বাটনে ক্লিক করুন।<br>২. একটি নতুন পেইজ ওপেন হবে, সেখানে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন।<br>৩. এরপর অটোমেটিক ভিডিওটি আপনার টেলিগ্রাম বটের ইনবক্সে চলে যাবে!</p>
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
                            ফাইল আনলক করতে নিচের লিংকে গিয়ে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন।
                        </p>
                        <button id="dlClickBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); margin-top: 10px;" onclick="executeDirectLink()">🔗 Click Here (Open Link)</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- VIP Modal -->
        <div id="vipModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('vipModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fbbf24; font-size: 24px; margin-bottom:15px;"><i class="fa-solid fa-crown"></i> VIP কিনুন</h2>
                <div style="display:flex; justify-content:space-between; margin-bottom: 15px;">
                    <button class="method-btn" style="background:#e11471;" onclick="selectPayment('bkash')">bKash</button>
                    <button class="method-btn" style="background:#f97316;" onclick="selectPayment('nagad')">Nagad</button>
                </div>
                <div id="payBox" class="pay-box">
                    <div class="pkg-options">
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="7" data-price="10" checked> ৭ দিন - ১০ টাকা</label>
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="30" data-price="30"> ১ মাস - ৩০ টাকা</label>
                    </div>
                    <p style="color:white; margin-top:10px;">নাম্বারে Send Money করুন: <b id="payNumberText" style="color:#4ade80; font-size:18px;">...</b></p>
                    <input type="text" id="trxIdInput" class="search-input" style="margin-top:10px;" placeholder="TrxID দিন...">
                    <button class="btn-submit" style="margin-top:10px;" onclick="submitPayment()">ভেরিফাই করুন</button>
                </div>
            </div>
        </div>

        <!-- Refer Modal -->
        <div id="referModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('referModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <i class="fa-solid fa-share-nodes" style="font-size:60px; color:#38bdf8;"></i>
                <h2 style="margin:15px 0; color:white; font-size: 24px;">রেফার ও ইনকাম</h2>
                <p style="color:#cbd5e1; font-size:15px; margin-bottom:15px;">৫ জন রেফার করলেই ২৪ ঘণ্টার VIP ফ্রি!</p>
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

        <!-- Success Modal -->
        <div id="successModal" class="modal">
            <div class="modal-content">
                <i class="fa-solid fa-circle-check" style="font-size:80px; color:#4ade80;"></i>
                <h2 style="margin:20px 0 10px; color:white; font-size: 26px;">সম্পন্ন হয়েছে!</h2>
                <p style="color: #4ade80; font-size: 17px; font-weight: bold;">✅ ফাইলটি বটের ইনবক্সে পাঠানো হয়েছে।</p>
                <button class="btn-submit" style="margin-top:20px;" onclick="tg.close()">বটে ফিরে যান</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            const BKASH_NO = "{{BKASH_NO}}";
            const NAGAD_NO = "{{NAGAD_NO}}";
            
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let isUserVip = false;
            let loadedMovies = {}; 
            let currentPage = 1; 
            let searchQuery = "";
            let onAdCompleteCallback = null;
            let autoScrollInterval;

            if(tg.initDataUnsafe?.user) {
                document.getElementById('uName').innerText = tg.initDataUnsafe.user.first_name;
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                    
                    let firstName = tg.initDataUnsafe?.user?.first_name || 'Guest';
                    document.getElementById('menuUname').innerText = firstName;
                    
                    if(isUserVip) {
                        document.getElementById('vipBadge').style.display = 'inline-block';
                        document.getElementById('menuStatus').innerText = '👑 VIP User';
                        document.getElementById('menuStatus').style.color = '#fbbf24';
                    }
                    
                    if(data.admin) {
                        document.getElementById('adminMenuBtn').style.display = 'flex';
                    }

                    document.getElementById('refLinkText').innerText = `https://t.me/${BOT_UNAME}?start=ref_${uid}`;
                } catch(e) {}
            }

            function toggleMenu(e) { e.stopPropagation(); const m = document.getElementById('dropdownMenu'); m.style.display = m.style.display === 'block' ? 'none' : 'block'; }
            function closeMenu() { document.getElementById('dropdownMenu').style.display = 'none'; }
            
            // Home Button Function
            function goHome() { 
                document.getElementById('searchInput').value = ""; 
                searchQuery = ""; 
                document.getElementById('trendingWrapper').style.display = 'block';
                loadTrending();
                loadMovies(1); 
                closeMenu(); 
                window.scrollTo({ top: 0, behavior: 'smooth' }); 
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

            function openVipModal() { document.getElementById('vipModal').style.display = 'flex'; closeMenu(); }
            let selectedPayMethod = "";
            function selectPayment(method) { selectedPayMethod = method; document.getElementById('payBox').style.display = 'block'; document.getElementById('payNumberText').innerText = method === 'bkash' ? BKASH_NO : NAGAD_NO; }
            async function submitPayment() {
                const trxId = document.getElementById('trxIdInput').value.trim();
                if(trxId.length < 5) return tg.showAlert("সঠিক TrxID দিন!");
                let selectedRadio = document.querySelector('input[name="vip_pkg"]:checked');
                try {
                    const res = await fetch('/api/payment/submit', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, method: selectedPayMethod, trx_id: trxId, days: parseInt(selectedRadio.value), price: parseInt(selectedRadio.getAttribute('data-price')), initData: INIT_DATA}) });
                    const data = await res.json();
                    if(data.ok) { tg.showAlert("✅ পেমেন্ট রিকোয়েস্ট পাঠানো হয়েছে!"); document.getElementById('vipModal').style.display = 'none'; } 
                    else { tg.showAlert(data.msg); }
                } catch(e) {}
            }

            function formatViews(n) { if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'; if (n >= 1000) return (n / 1000).toFixed(1) + 'K'; return n; }

            // Trending Auto Scroll System
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
                    const r = await fetch(`/api/list?page=${currentPage}&q=${encodeURIComponent(searchQuery)}&uid=${uid}`);
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
                    
                    // Clean Pagination
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
                if(searchQuery !== "") { document.getElementById('trendingWrapper').style.display = 'none'; } 
                else { document.getElementById('trendingWrapper').style.display = 'block'; }
                timeout = setTimeout(() => loadMovies(1), 500); 
            });

            function openQualityModal(title) {
                const movie = loadedMovies[title];
                document.getElementById('modalTitle').innerText = title;
                document.getElementById('qualityList').innerHTML = movie.files.map(f => {
                    let isFree = f.is_unlocked || isUserVip;
                    let icon = isFree ? '<i class="fa-solid fa-paper-plane text-green-400"></i>' : '<i class="fa-solid fa-lock text-red-400"></i>';
                    let cls = isFree ? 'border-left: 5px solid #10b981;' : 'border-left: 5px solid #ef4444;';
                    return `<div class="rgb-border" onclick="handleQualityClick('${f.id}', ${f.is_unlocked})"><div class="rgb-inner" style="${cls}"><span><i class="fa-solid fa-download"></i> ${f.quality}</span> ${icon}</div></div>`;
                }).join('');
                document.getElementById('qualityModal').style.display = 'flex';
            }

            function handleQualityClick(fileId, isUnlocked) {
                document.getElementById('qualityModal').style.display = 'none';
                if(isUnlocked || isUserVip) { sendFile(fileId); } 
                else { 
                    onAdCompleteCallback = () => sendFile(fileId);
                    document.getElementById('directLinkModal').style.display = 'flex';
                }
            }

            let linkOpenedAt = 0; let isWaitingForReturn = false; let dlTimerInterval = null;
            function executeDirectLink() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) { document.getElementById('directLinkModal').style.display = 'none'; if (onAdCompleteCallback) onAdCompleteCallback(); return; }
                tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]);
                linkOpenedAt = Date.now(); isWaitingForReturn = true;
                const btn = document.getElementById('dlClickBtn');
                btn.disabled = true; let timeLeft = 15; btn.style.background = "#475569";
                dlTimerInterval = setInterval(() => {
                    timeLeft--; btn.innerText = `⏳ অপেক্ষা করুন... (${timeLeft}s)`;
                    if (timeLeft <= 0) { clearInterval(dlTimerInterval); btn.innerText = `✅ সম্পন্ন হয়েছে!`; }
                }, 1000);
            }

            document.addEventListener("visibilitychange", function() {
                if (document.visibilityState === 'visible' && isWaitingForReturn) {
                    isWaitingForReturn = false; clearInterval(dlTimerInterval);
                    if (Date.now() - linkOpenedAt < 14000) {
                        tg.showAlert("⚠️ আপনাকে অবশ্যই পুরো ১৫ সেকেন্ড অপেক্ষা করতে হবে।");
                        const btn = document.getElementById('dlClickBtn'); btn.disabled = false; btn.innerText = "🔗 Click Here (Open Link)"; btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
                    } else { document.getElementById('directLinkModal').style.display = 'none'; if (onAdCompleteCallback) onAdCompleteCallback(); }
                }
            });

            async function sendFile(id) {
                try {
                    const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: id, initData: INIT_DATA}) });
                    const data = await res.json();
                    if(data.ok) { document.getElementById('successModal').style.display = 'flex'; }
                } catch (e) {}
            }

            fetchUserInfo(); loadTrending(); loadMovies(1); 
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{TG_LINK}}", tg_url).replace("{{SUPPORT_LINK}}", support_link).replace("{{LINK_18}}", link_18).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{BKASH_NO}}", bkash_no).replace("{{NAGAD_NO}}", nagad_no)
    return html_code

# ==========================================
# 8. Optimized APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    is_admin = uid in admin_cache
    if not user: return {"vip": False, "admin": is_admin}
    return {"vip": user.get("vip_until", datetime.datetime.utcnow()) > datetime.datetime.utcnow(), "admin": is_admin}

class PaymentModel(BaseModel):
    uid: int
    method: str
    trx_id: str
    days: int
    price: int
    initData: str

@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    if await db.payments.find_one({"trx_id": data.trx_id}): return {"ok": False, "msg": "TrxID আগে ব্যবহার করা হয়েছে!"}
    
    res = await db.payments.insert_one({"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending"})
    try:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        b.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        await bot.send_message(OWNER_ID, f"💰 <b>নতুন পেমেন্ট!</b>\nUID: {data.uid}\nTrxID: {data.trx_id}\nAmount: {data.price} TK", reply_markup=b.as_markup(), parse_mode="HTML")
    except Exception: pass
    return {"ok": True}

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    pipeline = [
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}}},
        {"$sort": {"clicks": -1}}, {"$limit": 10}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(10)
    for m in movies:
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return movies

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0):
    limit = 10
    skip = (page - 1) * limit
    unlocked_ids = []
    
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    match_stage = {"title": {"$regex": q, "$options": "i"}} if q else {}
    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}}},
        {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
    ]
    total_groups = (await db.movies.aggregate([{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]).to_list(1))
    total_pages = (total_groups[0]["total"] + limit - 1) // limit if total_groups else 0

    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        if cache and cache.get("expires_at", now) > now: file_path = cache["file_path"]
        else:
            file_path = (await bot.get_file(photo_id)).file_path
            await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(minutes=50)}}, upsert=True)
            
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except Exception: return {"error": "not found"}

class SendRequestModel(BaseModel):
    userId: int
    movieId: str
    initData: str

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

            caption = (f"🎥 <b>{m['title']} [{m.get('quality', 'HD')}]</b>\n\n📥 Join: @TGLinkBase")
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
    try: await bot.send_message(OWNER_ID, f"🔔 <b>মুভি রিকোয়েস্ট!</b>\nইউজার: {data.uname}\n🎬 মুভি: <b>{data.movie}</b>", parse_mode="HTML")
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
