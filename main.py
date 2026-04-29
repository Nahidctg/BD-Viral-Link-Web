import os, asyncio, datetime, uvicorn
import aiohttp
from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson import ObjectId
from pydantic import BaseModel

# --- ржХржиржлрж┐ржЧрж╛рж░рзЗрж╢ржи ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")

# ржЖржкржирж╛рж░ ржЪрзНржпрж╛ржирзЗрж▓рзЗрж░ ржЖржЗржбрж┐ ржирж┐ржЪрзЗ ржжрж┐ржи ржЕржержмрж╛ Environment Variable (CHANNEL_ID) ржерзЗржХрзЗ рж╕рзЗржЯ ржХрж░рзБржи
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003655443965") 

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_temp = {}
admin_cache = set([OWNER_ID]) 

async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    async for admin in db.admins.find():
        admin_cache.add(admin["user_id"])

# --- ржмрзНржпрж╛ржХржЧрзНрж░рж╛ржЙржирзНржб ржЕржЯрзЛ-ржбрж┐рж▓рж┐ржЯ ржУрзЯрж╛рж░рзНржХрж╛рж░ ---
async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            expired_msgs = db.auto_delete.find({"delete_at": {"$lte": now}})
            async for msg in expired_msgs:
                try:
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except Exception: pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
        except Exception: pass
        await asyncio.sleep(60)

# ==========================================
# рзз. ржорзЗржЗржи ржУржирж╛рж░ (Owner) рж╕рзНржкрзЗрж╢рж╛рж▓ ржХржорж╛ржирзНржб
# ==========================================

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    try:
        new_admin = int(m.text.split()[1])
        if new_admin in admin_cache:
            return await m.answer("тЪая╕П ржПржЗ ржЗржЙржЬрж╛рж░ржЯрж┐ ржЖржЧрзЗ ржерзЗржХрзЗржЗ ржЕрзНржпрж╛ржбржорж┐ржи!")
        await db.admins.insert_one({"user_id": new_admin})
        admin_cache.add(new_admin)
        await m.answer(f"тЬЕ ржирждрзБржи ржЕрзНржпрж╛ржбржорж┐ржи ржпрзБржХрзНржд ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗ: <code>{new_admin}</code>", parse_mode="HTML")
        try: await bot.send_message(new_admin, "ЁЯОЙ <b>ржЕржнрж┐ржиржирзНржжржи!</b> ржЖржкржирж╛ржХрзЗ ржПржЗ ржмржЯрзЗрж░ ржЕрзНржпрж╛ржбржорж┐ржи ржмрж╛ржирж╛ржирзЛ рж╣рзЯрзЗржЫрзЗред ржЖржкржирж┐ ржПржЦржи ржорзБржнрж┐ ржЖржкрж▓рзЛржб ржХрж░рждрзЗ ржкрж╛рж░ржмрзЗржиред", parse_mode="HTML")
        except: pass
    except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/addadmin ржЗржЙржЬрж╛рж░_ржЖржЗржбрж┐</code>", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    try:
        del_admin = int(m.text.split()[1])
        if del_admin == OWNER_ID: return await m.answer("тЪая╕П ржЖржкржирж┐ ржирж┐ржЬрзЗржХрзЗ (Owner) ржбрж┐рж▓рж┐ржЯ ржХрж░рждрзЗ ржкрж╛рж░ржмрзЗржи ржЕрзНржпрж╛ржХрж╛ржЙржирзНржЯрзЗрж░!")
        await db.admins.delete_one({"user_id": del_admin})
        admin_cache.discard(del_admin)
        await m.answer(f"тЬЕ ржЕрзНржпрж╛ржбржорж┐ржи рж░рж┐ржорзБржн ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗ: <code>{del_admin}</code>", parse_mode="HTML")
    except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/deladmin ржЗржЙржЬрж╛рж░_ржЖржЗржбрж┐</code>", parse_mode="HTML")

@dp.message(Command("adminlist"))
async def list_admins_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    text = "ЁЯСе <b>ржмрж░рзНрждржорж╛ржи ржЕрзНржпрж╛ржбржорж┐ржи рж▓рж┐рж╕рзНржЯ:</b>\n"
    text += f"ЁЯСС Owner: <code>{OWNER_ID}</code>\n"
    for ad in admin_cache:
        if ad != OWNER_ID: text += f"ЁЯСо Admin: <code>{ad}</code>\n"
    await m.answer(text, parse_mode="HTML")

# ==========================================
# рзи. ржмржЯрзЗрж░ рж╕рж╛ржзрж╛рж░ржг ржЕрзНржпрж╛ржбржорж┐ржи ржХржорж╛ржирзНржб
# ==========================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await db.users.update_one({"user_id": message.from_user.id}, {"$set": {"first_name": message.from_user.first_name}}, upsert=True)
    kb = [[types.InlineKeyboardButton(text="ЁЯОм BD Viral Link", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    uid = message.from_user.id
    if uid in admin_cache:
        text = (
            "ЁЯСЛ <b>рж╣рзНржпрж╛рж▓рзЛ ржЕрзНржпрж╛ржбржорж┐ржи!</b>\n\n"
            "тЪЩя╕П <b>ржХржорж╛ржирзНржб:</b>\n"
            "ЁЯФ╕ ржЕрзНржпрж╛ржб ржЬрзЛржи: <code>/setad ID</code> | ржЕрзНржпрж╛ржб рж╕ржВржЦрзНржпрж╛: <code>/setadcount рж╕ржВржЦрзНржпрж╛</code>\n"
            "ЁЯФ╕ ржЯрзЗрж▓рж┐ржЧрзНрж░рж╛ржо: <code>/settg рж▓рж┐ржВржХ</code> | 18+: <code>/set18 рж▓рж┐ржВржХ</code>\n"
            "ЁЯФ╕ ржкрзНрж░рзЛржЯрзЗржХрж╢ржи: <code>/protect on</code> ржмрж╛ <code>/protect off</code>\n"
            "ЁЯФ╕ ржЕржЯрзЛ-ржбрж┐рж▓рж┐ржЯ ржЯрж╛ржЗржо: <code>/settime [ржорж┐ржирж┐ржЯ]</code>\n"
            "ЁЯФ╕ ржбрж┐рж▓рж┐ржЯ: <code>/del</code> | рж╕рзНржЯрзНржпрж╛ржЯрж╛рж╕: <code>/stats</code> | ржмрзНрж░ржбржХрж╛рж╕рзНржЯ: <code>/cast</code>\n"
        )
        if uid == OWNER_ID:
            text += "\nЁЯСС <b>ржУржирж╛рж░ ржХржорж╛ржирзНржб:</b>\nЁЯФ╕ ржЕрзНржпрж╛ржб ржЕрзНржпрж╛ржбржорж┐ржи: <code>/addadmin ID</code>\nЁЯФ╕ ржбрж┐рж▓рж┐ржЯ ржЕрзНржпрж╛ржбржорж┐ржи: <code>/deladmin ID</code>\nЁЯФ╕ ржЕрзНржпрж╛ржбржорж┐ржи рж▓рж┐рж╕рзНржЯ: <code>/adminlist</code>\n"
            
        text += "\nЁЯУе <b>ржорзБржнрж┐ ржЕрзНржпрж╛ржб ржХрж░рждрзЗ ржкрзНрж░ржержорзЗ ржнрж┐ржбрж┐ржУ ржмрж╛ ржбржХрзБржорзЗржирзНржЯ ржлрж╛ржЗрж▓ ржкрж╛ржарж╛ржиред</b>"
    else:
        text = f"ЁЯСЛ <b>рж╕рзНржмрж╛ржЧрждржо {message.from_user.first_name}!</b>\n\n[ржЖржкржирж╛рж░ ржЯрзЗрж▓рж┐ржЧрзНрж░рж╛ржо ржЖржЗржбрж┐: <code>{uid}</code>]\n\nржорзБржнрж┐ ржжрзЗржЦрждрзЗ ржирж┐ржЪрзЗрж░ ржмрж╛ржЯржирзЗ ржХрзНрж▓рж┐ржХ ржХрж░рзБржиред"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split(" ")[1])
        if count < 1: count = 1
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"тЬЕ ржкрзНрж░рждрж┐ ржорзБржнрж┐рждрзЗ ржЕрзНржпрж╛ржб ржжрзЗржЦрж╛рж░ рж╕ржВржЦрзНржпрж╛ рж╕рзЗржЯ ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗ: <b>{count} ржЯрж┐</b>ред", parse_mode="HTML")
    except:
        await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/setadcount 3</code> (рзйржЯрж┐ ржЕрзНржпрж╛ржб ржжрзЗржЦрждрзЗ рж╣ржмрзЗ)", parse_mode="HTML")

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        if state == "on":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": True}}, upsert=True)
            await m.answer("тЬЕ ржлрж░рзЛрзЯрж╛рж░рзНржб ржкрзНрж░рзЛржЯрзЗржХрж╢ржи <b>ржЪрж╛рж▓рзБ (ON)</b> ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗред ржПржЦржи ржХрзЗржЙ ржорзБржнрж┐ ржлрж░рзЛрзЯрж╛рж░рзНржб ржмрж╛ рж╕рзЗржн ржХрж░рждрзЗ ржкрж╛рж░ржмрзЗ ржирж╛ред", parse_mode="HTML")
        elif state == "off":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": False}}, upsert=True)
            await m.answer("тЬЕ ржлрж░рзЛрзЯрж╛рж░рзНржб ржкрзНрж░рзЛржЯрзЗржХрж╢ржи <b>ржмржирзНржз (OFF)</b> ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗред ржПржЦржи рж╕ржмрж╛ржЗ ржорзБржнрж┐ ржлрж░рзЛрзЯрж╛рж░рзНржб ржХрж░рждрзЗ ржкрж╛рж░ржмрзЗред", parse_mode="HTML")
        else: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/protect on</code> ржЕржержмрж╛ <code>/protect off</code>", parse_mode="HTML")
    except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/protect on</code> ржЕржержмрж╛ <code>/protect off</code>", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_m = time_cfg['minutes'] if time_cfg else 60
    protect_cfg = await db.settings.find_one({"id": "protect_content"})
    prot_status = "ON ЁЯФТ" if protect_cfg and protect_cfg.get('status', True) else "OFF ЁЯФУ"
    ad_count_cfg = await db.settings.find_one({"id": "ad_count"})
    ads_req = ad_count_cfg['count'] if ad_count_cfg else 1
    
    await m.answer(f"ЁЯУК <b>рж╕рзНржЯрзНржпрж╛ржЯрж╛рж╕:</b>\nЁЯСе ржорзЛржЯ ржЗржЙржЬрж╛рж░: <code>{uc}</code>\nЁЯОм ржорзЛржЯ ржорзБржнрж┐: <code>{mc}</code>\nтП│ ржЕржЯрзЛ-ржбрж┐рж▓рж┐ржЯ: <code>{del_m} ржорж┐ржирж┐ржЯ</code>\nЁЯЫбя╕П ржкрзНрж░рзЛржЯрзЗржХрж╢ржи: <b>{prot_status}</b>\nЁЯТ░ ржЕрзНржпрж╛ржб ржЯрж╛рж░рзНржЧрзЗржЯ: <b>{ads_req} ржЯрж┐</b>", parse_mode="HTML")

@dp.message(Command("del"))
async def del_movie_list(m: types.Message):
    if m.from_user.id not in admin_cache: return
    movies = await db.movies.find().sort("created_at", -1).limit(20).to_list(length=20)
    if not movies: return await m.answer("ржХрзЛржирзЛ ржорзБржнрж┐ ржирзЗржЗред")
    builder = InlineKeyboardBuilder()
    for mv in movies: builder.button(text=f"тЭМ {mv['title']}", callback_data=f"del_{str(mv['_id'])}")
    builder.adjust(1)
    await m.answer("тЪая╕П ржбрж┐рж▓рж┐ржЯ ржХрж░рждрзЗ ржХрзНрж▓рж┐ржХ ржХрж░рзБржи:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("del_"))
async def del_movie_callback(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    try:
        await db.movies.delete_one({"_id": ObjectId(c.data.split("_")[1])})
        await c.answer("тЬЕ ржбрж┐рж▓рж┐ржЯ рж╣рзЯрзЗржЫрзЗ!", show_alert=True)
        await c.message.edit_text("тЬЕ ржорзБржнрж┐ржЯрж┐ ржбрж╛ржЯрж╛ржмрзЗрж╕ ржерзЗржХрзЗ ржорзБржЫрзЗ ржлрзЗрж▓рж╛ рж╣ржпрж╝рзЗржЫрзЗред", reply_markup=None)
    except: pass

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": int(m.text.split(" ")[1])}}, upsert=True)
            await m.answer(f"тЬЕ ржЕржЯрзЛ-ржбрж┐рж▓рж┐ржЯ ржЯрж╛ржЗржо рж╕рзЗржЯ ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗред")
        except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/settime 60</code>", parse_mode="HTML")

@dp.message(Command("setad"))
async def set_ad(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "ad_config"}, {"$set": {"zone_id": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("тЬЕ ржЬрзЛржи ржЖржкржбрзЗржЯ рж╣рзЯрзЗржЫрзЗред")
        except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/setad 1234567</code>", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "link_tg"}, {"$set": {"url": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("тЬЕ ржЯрзЗрж▓рж┐ржЧрзНрж░рж╛ржо рж▓рж┐ржВржХ ржЖржкржбрзЗржЯ рж╣рзЯрзЗржЫрзЗред")
        except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/settg https://t.me/...</code>", parse_mode="HTML")

@dp.message(Command("set18"))
async def set_18(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "link_18"}, {"$set": {"url": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("тЬЕ 18+ рж▓рж┐ржВржХ ржЖржкржбрзЗржЯ рж╣рзЯрзЗржЫрзЗред")
        except: await m.answer("тЪая╕П рж╕ржарж┐ржХ ржирж┐рзЯржо: <code>/set18 https://t.me/...</code>", parse_mode="HTML")

# ==========================================
# рзй. ржЗржиржкрзБржЯ ржкрзНрж░рж╕рзЗрж╕рж┐ржВ (ржЖржкрж▓рзЛржб, ржмрзНрж░ржбржХрж╛рж╕рзНржЯ, рж░рж┐ржкрзНрж▓рж╛ржЗ)
# ==========================================

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "bcast_wait"}
    await m.answer("ЁЯУв <b>ржЕрзНржпрж╛ржбржнрж╛ржирзНрж╕ржб ржмрзНрж░ржбржХрж╛рж╕рзНржЯ:</b>\nржпрзЗ ржорзЗрж╕рзЗржЬржЯрж┐ ржмрзНрж░ржбржХрж╛рж╕рзНржЯ ржХрж░рждрзЗ ржЪрж╛ржи рж╕рзЗржЯрж┐ ржкрж╛ржарж╛ржиред\n<i>ржирзЛржЯ: ржмржЯ ржЕржЯрзЛржорзЗржЯрж┐ржХ ржорзЗрж╕рзЗржЬрзЗрж░ ржирж┐ржЪрзЗ 'ЁЯОм ржУржкрзЗржи ржорзБржнрж┐ ржЕрзНржпрж╛ржк' ржмрж╛ржЯржи рж▓рж╛ржЧрж┐рзЯрзЗ ржжрж┐ржмрзЗред</i>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    admin_temp[c.from_user.id] = {"step": "reply_user", "target_uid": user_id}
    await c.message.reply("тЬНя╕П <b>ржЗржЙржЬрж╛рж░ржХрзЗ ржХрзА рж░рж┐ржкрзНрж▓рж╛ржЗ ржжрж┐рждрзЗ ржЪрж╛ржи рждрж╛ рж▓рж┐ржЦрзЗ ржкрж╛ржарж╛ржи:</b>\n(ржЯрзЗржХрзНрж╕ржЯ, ржЫржмрж┐ ржмрж╛ ржнрзЯрзЗрж╕ ржорзЗрж╕рзЗржЬ ржкрж╛ржарж╛рждрзЗ ржкрж╛рж░рзЗржи)", parse_mode="HTML")
    await c.answer()

@dp.message(F.content_type.in_({'text', 'photo', 'video', 'document', 'voice'}))
async def catch_all_inputs(m: types.Message):
    uid = m.from_user.id
    
    if uid in admin_cache and admin_temp.get(uid, {}).get("step") == "reply_user":
        target_uid = admin_temp[uid]["target_uid"]
        del admin_temp[uid]
        try:
            if m.text: await bot.send_message(target_uid, f"ЁЯУй <b>ржЕрзНржпрж╛ржбржорж┐ржи рж░рж┐ржкрзНрж▓рж╛ржЗ:</b>\n\n{m.text}", parse_mode="HTML")
            else: await m.copy_to(target_uid, caption=f"ЁЯУй <b>ржЕрзНржпрж╛ржбржорж┐ржи рж░рж┐ржкрзНрж▓рж╛ржЗ:</b>\n\n{m.caption or ''}", parse_mode="HTML")
            await m.answer("тЬЕ ржЗржЙржЬрж╛рж░ржХрзЗ рж╕ржлрж▓ржнрж╛ржмрзЗ рж░рж┐ржкрзНрж▓рж╛ржЗ ржкрж╛ржарж╛ржирзЛ рж╣рзЯрзЗржЫрзЗ!")
        except Exception: await m.answer("тЪая╕П рж░рж┐ржкрзНрж▓рж╛ржЗ ржкрж╛ржарж╛ржирзЛ ржпрж╛рзЯржирж┐! ржЗржЙржЬрж╛рж░ рж╣рзЯрждрзЛ ржмржЯ ржмрзНрж▓ржХ ржХрж░рзЗржЫрзЗред")
        return

    if uid in admin_cache and admin_temp.get(uid, {}).get("step") == "bcast_wait":
        del admin_temp[uid]
        await m.answer("тП│ ржмрзНрж░ржбржХрж╛рж╕рзНржЯ рж╢рзБрж░рзБ рж╣рзЯрзЗржЫрзЗ...")
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="ЁЯОм ржУржкрзЗржи ржорзБржнрж┐ ржЕрзНржпрж╛ржк", web_app=types.WebAppInfo(url=APP_URL))]])
        success = 0
        async for u in db.users.find():
            try:
                await m.copy_to(chat_id=u['user_id'], reply_markup=kb)
                success += 1
                await asyncio.sleep(0.05)
            except: pass
        await m.answer(f"тЬЕ рж╕ржорзНржкржирзНржи! рж╕рж░рзНржмржорзЛржЯ <b>{success}</b> ржЬржиржХрзЗ ржорзЗрж╕рзЗржЬ ржкрж╛ржарж╛ржирзЛ рж╣рзЯрзЗржЫрзЗред", parse_mode="HTML")
        return

    if uid in admin_cache and (m.document or m.video):
        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"
        admin_temp[uid] = {"step": "photo", "file_id": fid, "type": ftype}
        await m.answer("тЬЕ ржлрж╛ржЗрж▓ ржкрзЗрзЯрзЗржЫрж┐! ржПржмрж╛рж░ ржорзБржнрж┐рж░ <b>ржкрзЛрж╕рзНржЯрж╛рж░ (Photo)</b> рж╕рзЗржирзНржб ржХрж░рзБржиред", parse_mode="HTML")
        return

    if uid in admin_cache and m.photo and admin_temp.get(uid, {}).get("step") == "photo":
        admin_temp[uid]["photo_id"] = m.photo[-1].file_id
        admin_temp[uid]["step"] = "title"
        await m.answer("тЬЕ ржкрзЛрж╕рзНржЯрж╛рж░ ржкрзЗрзЯрзЗржЫрж┐! ржПржмрж╛рж░ ржорзБржнрж┐рж░ <b>ржирж╛ржо</b> рж▓рж┐ржЦрзЗ ржкрж╛ржарж╛ржиред", parse_mode="HTML")
        return

    # === ржорзБржнрж┐ ржбрж╛ржЯрж╛ржмрзЗрж╕рзЗ рж╕рзЗржн ржПржмржВ ржЕржЯрзЛ-ржирзЛржЯрж┐ржлрж┐ржХрзЗрж╢ржи рж╕рзЗржХрж╢ржи ===
    if uid in admin_cache and m.text and not str(m.text).startswith("/"):
        if admin_temp.get(uid, {}).get("step") == "title":
            title = m.text.strip()
            photo_id = admin_temp[uid]["photo_id"]
            file_id = admin_temp[uid]["file_id"]
            file_type = admin_temp[uid]["type"]
            
            # ржбрж╛ржЯрж╛ржмрзЗрж╕рзЗ рж╕рзЗржн ржХрж░рж╛
            await db.movies.insert_one({
                "title": title, 
                "photo_id": photo_id, 
                "file_id": file_id, 
                "file_type": file_type, 
                "clicks": 0, 
                "created_at": datetime.datetime.utcnow()
            })
            del admin_temp[uid]
            await m.answer(f"ЁЯОЙ <b>{title}</b> ржЕрзНржпрж╛ржкрзЗ рж╕ржлрж▓ржнрж╛ржмрзЗ ржпрзБржХрзНржд ржХрж░рж╛ рж╣рзЯрзЗржЫрзЗ!", parse_mode="HTML")
            
            # ржЪрзНржпрж╛ржирзЗрж▓рзЗ ржирзЛржЯрж┐ржлрж┐ржХрзЗрж╢ржи ржкрж╛ржарж╛ржирзЛ
            if CHANNEL_ID and CHANNEL_ID != "-100XXXXXXXXXX":
                try:
                    # ржЪрзНржпрж╛ржирзЗрж▓ ржЖржЗржбрж┐ржЯрж┐ String ржерзЗржХрзЗ Integer ржП ржХржиржнрж╛рж░рзНржЯ ржХрж░рж╛ рж╣рж▓рзЛ ржпрж╛рждрзЗ ржПрж░рж░ ржирж╛ ржЖрж╕рзЗ
                    try:
                        target_channel = int(CHANNEL_ID)
                    except ValueError:
                        target_channel = CHANNEL_ID

                    # ржмржЯрзЗрж░ ржЗржЙржЬрж╛рж░ржирзЗржо ржмрзЗрж░ ржХрж░рж╛ рж╣ржЪрзНржЫрзЗ
                    bot_info = await bot.get_me()
                    bot_username = bot_info.username
                    
                    # ржЪрзНржпрж╛ржирзЗрж▓рзЗ web_app ржмрж╛ржЯржирзЗрж░ ржмржжрж▓рзЗ url ржмрж╛ржЯржи ржмрзНржпржмрж╣рж╛рж░ ржХрж░рж╛ рж╣рж▓рзЛ
                    kb = [[types.InlineKeyboardButton(text="ЁЯОм ржнрж┐ржбрж┐ржУржЯрж┐ ржжрзЗржЦрждрзЗ ржПржЦрж╛ржирзЗ ржХрзНрж▓рж┐ржХ ржХрж░рзБржи", url=f"https://t.me/{bot_username}?start=new")]]
                    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                    
                    caption = (
                        f"ЁЯОм <b>ржирждрзБржи ржнрж┐ржбрж┐ржУ ржпрзБржХрзНржд рж╣рзЯрзЗржЫрзЗ!</b>\n\n"
                        f"ЁЯУМ <b>ржирж╛ржо:</b> {title}\n\n"
                        f"ЁЯСЗ <i>ржнрж┐ржбрж┐ржУржЯрж┐ ржжрзЗржЦрждрзЗ ржирж┐ржЪрзЗрж░ ржмрж╛ржЯржирзЗ ржХрзНрж▓рж┐ржХ ржХрж░рзЗ ржмржЯрзЗ ржпрж╛ржи ржПржмржВ ржЕрзНржпрж╛ржкржЯрж┐ ржУржкрзЗржи ржХрж░рзБржиред</i>"
                    )
                    
                    await bot.send_photo(
                        chat_id=target_channel, 
                        photo=photo_id, 
                        caption=caption, 
                        parse_mode="HTML", 
                        reply_markup=markup
                    )
                except Exception as e:
                    # ржПржмрж╛рж░ ржЪрзНржпрж╛ржирзЗрж▓рзЗ ржорзЗрж╕рзЗржЬ ржирж╛ ржЧрзЗрж▓рзЗ ржХрзА ржХрж╛рж░ржгрзЗ ржпрж╛рзЯржирж┐ рж╕рзЗржЯрж╛ ржЖржкржирж╛ржХрзЗ рж░рж┐ржкрзНрж▓рж╛ржЗрждрзЗ рж╕рзНржкрж╖рзНржЯ ржмрж▓рзЗ ржжрж┐ржмрзЗ
                    await m.answer(f"тЪая╕П ржорзБржнрж┐ ржбрж╛ржЯрж╛ржмрзЗрж╕рзЗ ржпрзБржХрзНржд рж╣рзЯрзЗржЫрзЗ, ржХрж┐ржирзНрждрзБ ржЪрзНржпрж╛ржирзЗрж▓рзЗ ржпрж╛рзЯржирж┐!\n<b>ржХрж╛рж░ржг:</b> <code>{str(e)}</code>", parse_mode="HTML")

# ==========================================
# рзк. ржУрзЯрзЗржм ржЕрзНржпрж╛ржк UI ржПржмржВ APIs
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    ad_cfg = await db.settings.find_one({"id": "ad_config"})
    tg_cfg = await db.settings.find_one({"id": "link_tg"})
    b18_cfg = await db.settings.find_one({"id": "link_18"})
    ad_count_cfg = await db.settings.find_one({"id": "ad_count"})
    
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    required_ads = ad_count_cfg['count'] if ad_count_cfg else 1

    html_code = r"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Moviee BD</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            * { margin:0; padding:0; box-sizing:border-box; }
            body { background:#0f172a; font-family: sans-serif; color:#fff; } 
            header { display:flex; justify-content:space-between; align-items:center; padding:15px; border-bottom:1px solid #1e293b; position:sticky; top:0; background:#0f172a; z-index:1000; }
            .logo { font-size:24px; font-weight:bold; }
            .logo span { background:red; color:#fff; padding:2px 5px; border-radius:5px; margin-left:5px; font-size:16px; }
            .user-info { display:flex; align-items:center; gap:8px; background:#1e293b; padding:5px 12px; border-radius:20px; font-weight:bold; font-size:14px; }
            .user-info img { width:26px; height:26px; border-radius:50%; object-fit:cover; }
            
            .search-box { padding:15px; }
            .search-input { width:100%; padding:14px; border-radius:25px; border:none; outline:none; text-align:center; background:#1e293b; color:#fff; font-size:16px; transition: 0.3s; }
            .search-input:focus { box-shadow: 0 0 10px rgba(248,113,113,0.5); }
            
            .section-title { padding: 5px 15px 10px; font-size: 18px; font-weight: bold; color: #f87171; display:flex; align-items:center; gap:8px;}
            
            .trending-container { display: flex; overflow-x: auto; gap: 12px; padding: 0 15px 20px; scroll-behavior: smooth; }
            .trending-container::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 130px; max-width: 130px; background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; flex-shrink: 0; position:relative;}
            .trending-card img { height: 170px; object-fit:cover; width:100%; border-radius:10px; display:block; }
            .trending-card .post-content { padding:3px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00); border-radius: 12px; }
            
            .grid { padding:0 15px 20px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; }
            .card { background:#1e293b; border-radius:12px; overflow:hidden; cursor:pointer; transition: transform 0.2s; }
            .card:active { transform: scale(0.95); }
            
            .post-content { 
                position:relative; padding: 3px; border-radius: 12px;
                background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000);
                background-size: 400%; animation: glowing 8s linear infinite;
            }
            @keyframes glowing { 0% { background-position: 0 0; } 50% { background-position: 400% 0; } 100% { background-position: 0 0; } }

            .post-content img { width:100%; height:180px; object-fit:cover; display:block; border-radius: 10px; }
            
            .tag { position:absolute; top:8px; right:8px; padding:4px 6px; border-radius:6px; font-weight:bold; font-size:10px; display:flex; align-items:center; gap:4px; box-shadow: 0 2px 5px rgba(0,0,0,0.5); }
            .tag-locked { background:rgba(0,0,0,0.85); color:#f87171; border: 1px solid #f87171; }
            .tag-unlocked { background:rgba(0,0,0,0.85); color:#10b981; border: 1px solid #10b981; }
            
            .top-badge { position:absolute; top:8px; left:8px; background:red; color:white; padding:3px 6px; border-radius:6px; font-size:10px; font-weight:bold; box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index:10;}
            .view-badge { position:absolute; bottom:8px; left:8px; background:rgba(0,0,0,0.7); color:#fff; padding:3px 6px; border-radius:6px; font-size:11px; font-weight:bold; display:flex; align-items:center; gap:4px; box-shadow: 0 2px 5px rgba(0,0,0,0.5); }

            .card-footer { padding:10px; font-size:13px; font-weight:bold; text-align:center; word-wrap: break-word; color:#e2e8f0; line-height:1.4; }
            
            .skeleton { background: #1e293b; border-radius: 12px; height: 215px; overflow: hidden; position: relative; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 120px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 10px 15px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.2s; outline: none; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; }
            .page-btn:disabled { opacity: 0.4; cursor: not-allowed; }

            .floating-btn { position:fixed; right:20px; color:white; width:50px; height:50px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:20px; font-weight:bold; z-index:500; cursor:pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
            .btn-18 { bottom:155px; background:red; border:2px solid #fff; }
            .btn-tg { bottom:95px; background:#24A1DE; }
            .btn-req { bottom:35px; background:#10b981; }

            /* Ad Screen Updated Styles */
            .ad-screen { position:fixed; top:0; left:0; width:100%; height:100%; background:#0f172a; display:none; flex-direction:column; align-items:center; justify-content:center; z-index:2000; }
            .timer-ui { display:flex; flex-direction:column; align-items:center; }
            .timer { width:100px; height:100px; border-radius:50%; border:5px solid red; display:flex; align-items:center; justify-content:center; font-size:40px; margin-bottom:15px; color:red; font-weight:bold; }
            .ad-step-text { font-size:18px; font-weight:bold; color:#fff; margin-bottom: 20px; background:#1e293b; padding:8px 15px; border-radius:20px;}
            .btn-next-ad { display:none; background:#f87171; color:white; border:none; padding:15px 30px; border-radius:30px; font-size:18px; font-weight:bold; cursor:pointer; box-shadow: 0 4px 15px rgba(248,113,113,0.5); transition: 0.3s;}
            .btn-next-ad:active { transform: scale(0.95); }
            
            .modal { position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); display:none; align-items:center; justify-content:center; z-index:3000; }
            .modal-content { background:#1e293b; width:90%; padding:30px; border-radius:15px; text-align:center; }
            .req-input { width: 100%; padding: 12px; margin: 15px 0; border-radius: 8px; border: none; background: #0f172a; color: white; outline:none; }
            .btn-submit { background: #10b981; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-weight: bold; width:100%; font-size:16px;}
        </style>
    </head>
    <body>
        <header>
            <div class="logo">BD Viral <span>Link</span></div>
            <div class="user-info"><span id="uName">Guest</span><img id="uPic" src="https://cdn-icons-png.flaticon.com/512/3135/3135715.png"></div>
        </header>

        <div class="search-box">
            <input type="text" id="searchInput" class="search-input" placeholder="ржорзБржнрж┐ ржмрж╛ ржУрзЯрзЗржм рж╕рж┐рж░рж┐ржЬ ржЦрзБржБржЬрзБржи...">
        </div>

        <div id="trendingWrapper">
            <div class="section-title"><i class="fa-solid fa-fire"></i> ржЯрзНрж░рзЗржирзНржбрж┐ржВ ржнрж╛ржЗрж░рж╛рж▓ ржнрж┐ржбрж┐ржУ</div>
            <div class="trending-container" id="trendingGrid">
                <div class="skeleton" style="min-width:130px; height:180px;"></div>
                <div class="skeleton" style="min-width:130px; height:180px;"></div>
                <div class="skeleton" style="min-width:130px; height:180px;"></div>
            </div>
        </div>

        <div class="section-title"><i class="fa-solid fa-film"></i> ржирждрзБржи рж╕ржм ржнрж╛ржЗрж░рж╛рж▓ ржнрж┐ржбрж┐ржУ</div>
        <div class="grid" id="movieGrid"></div>
        <div class="pagination" id="paginationBox"></div>

        <div class="floating-btn btn-18" onclick="window.open('{{LINK_18}}')">18+</div>
        <div class="floating-btn btn-tg" onclick="window.open('{{TG_LINK}}')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openReqModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <!-- Ad Screen with Multi-Ad Logic -->
        <div id="adScreen" class="ad-screen">
            <div class="ad-step-text" id="adStepText">ржЕрзНржпрж╛ржб: 1/1</div>
            
            <div class="timer-ui" id="timerUI">
                <div class="timer" id="timer">15</div>
                <p>рж╕рж╛рж░рзНржнрж╛рж░рзЗрж░ рж╕рж╛ржерзЗ ржХрж╛ржирзЗржХрзНржЯ рж╣ржЪрзНржЫрзЗ...</p>
            </div>
            
            <button class="btn-next-ad" id="nextAdBtn" onclick="nextAdStep()">ржкрж░ржмрж░рзНрждрзА ржЕрзНржпрж╛ржб ржжрзЗржЦрзБржи <i class="fa-solid fa-arrow-right"></i></button>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content">
                <i class="fa-solid fa-circle-check" style="font-size:60px; color:#10b981;"></i>
                <h2 style="margin:15px 0;">рж╕ржорзНржкржирзНржи рж╣рзЯрзЗржЫрзЗ!</h2>
                <p style="margin-bottom: 20px; color:gray; font-size:14px;">ржмржЯрзЗрж░ ржЗржиржмржХрзНрж╕ ржЪрзЗржХ ржХрж░рзБржиред <br><span style="color:#f87171;">рж╕рждрж░рзНржХрждрж╛: ржХржкрж┐рж░рж╛ржЗржЯ ржПрзЬрж╛рждрзЗ ржорзБржнрж┐ржЯрж┐ ржХрж┐ржЫрзБржХрзНрж╖ржг ржкрж░ ржЕржЯрзЛржорзЗржЯрж┐ржХ ржбрж┐рж▓рж┐ржЯ рж╣рзЯрзЗ ржпрж╛ржмрзЗред</span></p>
                <button class="btn-submit" onclick="tg.close()">ржмржЯрзЗ ржлрж┐рж░рзЗ ржпрж╛ржи</button>
            </div>
        </div>

        <div id="reqModal" class="modal">
            <div class="modal-content">
                <h2>ржорзБржнрж┐ рж░рж┐ржХрзЛрзЯрзЗрж╕рзНржЯ ржХрж░рзБржи</h2>
                <input type="text" id="reqText" class="req-input" placeholder="ржорзБржнрж┐рж░ ржирж╛ржо ржУ рж░рж┐рж▓рж┐ржЬ рж╕рж╛рж▓ рж▓рж┐ржЦрзБржи...">
                <button class="btn-submit" onclick="sendReq()">рж╕рж╛ржмржорж┐ржЯ ржХрж░рзБржи</button>
                <p style="margin-top:15px; color:gray; cursor:pointer;" onclick="document.getElementById('reqModal').style.display='none'">ржмрж╛рждрж┐рж▓ ржХрж░рзБржи</p>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const ZONE_ID = "{{ZONE_ID}}";
            const REQUIRED_ADS = parseInt("{{AD_COUNT}}");
            
            let currentPage = 1; let isLoading = false; let searchQuery = "";
            let uid = tg.initDataUnsafe.user?.id || 0;
            
            let currentAdStep = 1;
            let activeMovieId = null;

            if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                document.getElementById('uName').innerText = tg.initDataUnsafe.user.first_name;
                if(tg.initDataUnsafe.user.photo_url) document.getElementById('uPic').src = tg.initDataUnsafe.user.photo_url;
            }

            const s = document.createElement('script');
            s.src = '//libtl.com/sdk.js'; s.setAttribute('data-zone', ZONE_ID); s.setAttribute('data-sdk', 'show_' + ZONE_ID);
            document.head.appendChild(s);

            function drawSkeletons(count) {
                let html = ""; for(let i=0; i<count; i++) html += `<div class="skeleton"></div>`; return html;
            }

            function startAutoScroll() {
                setInterval(() => {
                    let grid = document.getElementById('trendingGrid');
                    if(grid) {
                        let cardWidth = 142;
                        if (grid.scrollLeft >= (grid.scrollWidth - grid.clientWidth - 10)) {
                            grid.scrollTo({ left: 0, behavior: 'smooth' });
                        } else {
                            grid.scrollBy({ left: cardWidth, behavior: 'smooth' });
                        }
                    }
                }, 3000);
            }

            async function loadTrending() {
                try {
                    const r = await fetch(`/api/trending?uid=${uid}`);
                    const data = await r.json();
                    const grid = document.getElementById('trendingGrid');
                    if(data.length === 0) {
                        document.getElementById('trendingWrapper').style.display = 'none';
                        return;
                    }
                    grid.innerHTML = data.map(m => {
                        let tagHtml = m.is_unlocked ? `<div class="tag tag-unlocked"><i class="fa-solid fa-unlock"></i></div>` : `<div class="tag tag-locked"><i class="fa-solid fa-lock"></i></div>`;
                        return `
                        <div class="trending-card" onclick="handleMovieClick('${m._id}', ${m.is_unlocked})">
                            <div class="post-content">
                                <div class="top-badge">ЁЯФе TOP</div>
                                <img src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/400x200?text=No+Image'">
                                ${tagHtml}
                                <div class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks}</div>
                            </div>
                            <div class="card-footer" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${m.title}</div>
                        </div>`;
                    }).join('');
                    setTimeout(startAutoScroll, 2000);
                } catch(e) {}
            }

            async function loadMovies(page = 1) {
                if(isLoading) return;
                isLoading = true;
                currentPage = page;
                
                const grid = document.getElementById('movieGrid');
                const pBox = document.getElementById('paginationBox');
                grid.innerHTML = drawSkeletons(16); pBox.innerHTML = "";

                try {
                    const r = await fetch(`/api/list?page=${currentPage}&q=${searchQuery}&uid=${uid}`);
                    const data = await r.json();
                    
                    if(data.movies.length === 0) {
                        grid.innerHTML = "<p style='grid-column: span 2; text-align:center; color:gray; padding:20px;'>ржХрзЛржирзЛ ржорзБржнрж┐ ржкрж╛ржУрзЯрж╛ ржпрж╛рзЯржирж┐!</p>";
                    } else {
                        grid.innerHTML = data.movies.map(m => {
                            let tagHtml = m.is_unlocked ? `<div class="tag tag-unlocked"><i class="fa-solid fa-unlock"></i></div>` : `<div class="tag tag-locked"><i class="fa-solid fa-lock"></i></div>`;
                            return `
                            <div class="card" onclick="handleMovieClick('${m._id}', ${m.is_unlocked})">
                                <div class="post-content">
                                    <img src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/400x200?text=No+Image'">
                                    ${tagHtml}
                                    <div class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks}</div>
                                </div>
                                <div class="card-footer">${m.title}</div>
                            </div>`;
                        }).join('');
                        renderPagination(data.total_pages);
                    }
                } catch(e) {}
                isLoading = false;
            }

            function renderPagination(totalPages) {
                if (totalPages <= 1) return;
                let html = "";
                html += `<button class="page-btn" ${currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${currentPage - 1})"><i class="fa-solid fa-angle-left"></i></button>`;
                let start = Math.max(1, currentPage - 1); let end = Math.min(totalPages, currentPage + 1);
                if (start > 1) { html += `<button class="page-btn" onclick="goToPage(1)">1</button>`; if (start > 2) html += `<span style="color:gray;">...</span>`; }
                for (let i = start; i <= end; i++) { html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`; }
                if (end < totalPages) { if (end < totalPages - 1) html += `<span style="color:gray;">...</span>`; html += `<button class="page-btn" onclick="goToPage(${totalPages})">${totalPages}</button>`; }
                html += `<button class="page-btn" ${currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${currentPage + 1})"><i class="fa-solid fa-angle-right"></i></button>`;
                document.getElementById('paginationBox').innerHTML = html;
            }

            function goToPage(p) {
                if (p < 1) return; loadMovies(p);
                window.scrollTo({ top: document.getElementById('movieGrid').offsetTop - 100, behavior: 'smooth' });
            }

            let timeout = null;
            document.getElementById('searchInput').addEventListener('input', function(e) {
                clearTimeout(timeout); searchQuery = e.target.value.trim();
                if(searchQuery !== "") document.getElementById('trendingWrapper').style.display = 'none';
                else { document.getElementById('trendingWrapper').style.display = 'block'; loadTrending(); }
                timeout = setTimeout(() => { loadMovies(1); }, 500); 
            });

            // Multi-Ad Logic
            function handleMovieClick(id, isUnlocked) {
                if(isUnlocked) {
                    sendFile(id);
                } else {
                    activeMovieId = id;
                    currentAdStep = 1;
                    startAdTimer();
                }
            }

            function startAdTimer() {
                if (typeof window['show_' + ZONE_ID] === 'function') window['show_' + ZONE_ID]();
                
                document.getElementById('adScreen').style.display = 'flex';
                document.getElementById('timerUI').style.display = 'flex';
                document.getElementById('nextAdBtn').style.display = 'none';
                
                document.getElementById('adStepText').innerText = `ржЕрзНржпрж╛ржб: ${currentAdStep}/${REQUIRED_ADS}`;
                
                let t = 15;
                document.getElementById('timer').innerText = t;
                
                let iv = setInterval(() => {
                    t--; document.getElementById('timer').innerText = t;
                    if(t <= 0) { 
                        clearInterval(iv); 
                        if(currentAdStep < REQUIRED_ADS) {
                            document.getElementById('timerUI').style.display = 'none';
                            document.getElementById('nextAdBtn').style.display = 'block';
                            document.getElementById('nextAdBtn').innerHTML = `ржкрж░ржмрж░рзНрждрзА ржЕрзНржпрж╛ржб ржжрзЗржЦрзБржи (${currentAdStep + 1}/${REQUIRED_ADS}) <i class="fa-solid fa-arrow-right"></i>`;
                        } else {
                            sendFile(activeMovieId); 
                        }
                    }
                }, 1000);
            }

            function nextAdStep() {
                currentAdStep++;
                startAdTimer();
            }

            async function sendFile(id) {
                await fetch('/api/send', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({userId: uid, movieId: id})});
                document.getElementById('adScreen').style.display = 'none';
                document.getElementById('successModal').style.display = 'flex';
                setTimeout(() => { loadTrending(); loadMovies(currentPage); }, 1000); 
            }

            function openReqModal() { document.getElementById('reqModal').style.display = 'flex'; }
            async function sendReq() {
                const text = document.getElementById('reqText').value;
                if(!text) return alert('ржорзБржнрж┐рж░ ржирж╛ржо рж▓рж┐ржЦрзБржи!');
                await fetch('/api/request', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({uid: uid, uname: tg.initDataUnsafe.user?.first_name || 'Guest', movie: text})});
                document.getElementById('reqModal').style.display = 'none';
                document.getElementById('reqText').value = '';
                alert('рж░рж┐ржХрзЛрзЯрзЗрж╕рзНржЯ рж╕ржлрж▓ржнрж╛ржмрзЗ ржкрж╛ржарж╛ржирзЛ рж╣рзЯрзЗржЫрзЗ!');
            }

            loadTrending();
            loadMovies(1); 
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{ZONE_ID}}", zone_id).replace("{{TG_LINK}}", tg_url).replace("{{LINK_18}}", link_18).replace("{{AD_COUNT}}", str(required_ads))
    return html_code

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    unlocked_movie_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_movie_ids.append(u["movie_id"])

    movies = []
    async for m in db.movies.find().sort("clicks", -1).limit(10):
        m_id = str(m["_id"])
        m["_id"] = m_id
        m["clicks"] = m.get("clicks", 0)
        m["is_unlocked"] = m_id in unlocked_movie_ids 
        movies.append(m)
    return movies

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0):
    limit = 16
    skip = (page - 1) * limit
    query = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    total_movies = await db.movies.count_documents(query)
    total_pages = (total_movies + limit - 1) // limit

    unlocked_movie_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_movie_ids.append(u["movie_id"])

    movies = []
    async for m in db.movies.find(query).sort("created_at", -1).skip(skip).limit(limit):
        m_id = str(m["_id"])
        m["_id"] = m_id
        m["clicks"] = m.get("clicks", 0)
        m["created_at"] = str(m.get("created_at", ""))
        m["is_unlocked"] = m_id in unlocked_movie_ids 
        movies.append(m)
        
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        file_info = await bot.get_file(photo_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except: return {"error": "not found"}

@app.post("/api/send")
async def send_file(d: dict = Body(...)):
    uid = d['userId']
    mid = d['movieId']
    if uid == 0: return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(mid)})
        if m:
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True
            
            caption = f"ЁЯОе <b>{m['title']}</b>\n\nтП│ <b>рж╕рждрж░рзНржХрждрж╛:</b> ржХржкрж┐рж░рж╛ржЗржЯ ржПрзЬрж╛рждрзЗ ржорзБржнрж┐ржЯрж┐ <b>{del_minutes} ржорж┐ржирж┐ржЯ</b> ржкрж░ ржЕржЯрзЛ-ржбрж┐рж▓рж┐ржЯ рж╣рзЯрзЗ ржпрж╛ржмрзЗред ржжрзЯрж╛ ржХрж░рзЗ ржПржЦржиржЗ ржлрж░ржУрзЯрж╛рж░рзНржб ржмрж╛ рж╕рзЗржн ржХрж░рзЗ ржирж┐ржи!\n\nЁЯУе Join: https://t.me/+5ixoBj0Ay7oxOTM1"
            
            sent_msg = None
            if m.get("file_type") == "video": 
                sent_msg = await bot.send_video(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: 
                sent_msg = await bot.send_document(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(mid)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": uid, "movie_id": mid}, {"$set": {"unlocked_at": datetime.datetime.utcnow()}}, upsert=True)
            
            if sent_msg:
                delete_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": uid, "message_id": sent_msg.message_id, "delete_at": delete_at})
    except Exception as e: pass
    return {"ok": True}

class ReqModel(BaseModel):
    uid: int; uname: str; movie: str

@app.post("/api/request")
async def handle_request(data: ReqModel):
    try: 
        builder = InlineKeyboardBuilder()
        builder.button(text="тЬНя╕П рж░рж┐ржкрзНрж▓рж╛ржЗ ржжрж┐ржи", callback_data=f"reply_{data.uid}")
        await bot.send_message(OWNER_ID, f"ЁЯФФ <b>ржирждрзБржи ржорзБржнрж┐ рж░рж┐ржХрзЛрзЯрзЗрж╕рзНржЯ!</b>\n\nЁЯСд ржЗржЙржЬрж╛рж░: {data.uname} (<code>{data.uid}</code>)\nЁЯОм ржорзБржнрж┐рж░ ржирж╛ржо: <b>{data.movie}</b>", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass
    return {"ok": True}

async def start():
    await load_admins()
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    asyncio.create_task(auto_delete_worker())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": asyncio.run(start())
