import aiohttp
import logging
import os
import re
import pytz
import random
import asyncio

from datetime import datetime
from rapidfuzz import fuzz

# ==========================================================
# 🛑 LOGGING
# ==========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================================
# 🔑 API CONFIG
# ==========================================================
keys_env = os.getenv(
    "OPENROUTER_API_KEYS",
    os.getenv("OPENROUTER_API_KEY", "")
)

API_KEYS = [k.strip() for k in keys_env.split(",") if k.strip()]

MODEL_NAME = "openai/gpt-4o-mini"

# ==========================================================
# 🌐 SESSION
# ==========================================================
session_instance = None

async def get_session():
    global session_instance

    if session_instance is None or session_instance.closed:
        timeout = aiohttp.ClientTimeout(total=40)
        session_instance = aiohttp.ClientSession(timeout=timeout)

    return session_instance

# ==========================================================
# 🌍 BANGLA NORMALIZER
# ==========================================================
BN_MAP = {
    "কেজিএফ": "kgf",
    "অ্যাভেঞ্জার": "avengers",
    "এভেঞ্জার": "avengers",
    "স্পাইডারম্যান": "spiderman",
    "স্পাইডার ম্যান": "spiderman",
    "মানি হেইস্ট": "money heist",
    "স্কুইড গেম": "squid game",
    "পুষ্পা": "pushpa",
    "জওয়ান": "jawan",
    "পাঠান": "pathaan",
    "ডন": "don",
    "টাইগার": "tiger",
}

REMOVE_WORDS = [
    "movie",
    "download",
    "series",
    "full movie",
    "full",
    "hd",
    "hindi",
    "bangla",
    "english",
    "season",
    "episode",
    "part",
    "watch",
    "dekhbo",
    "dao",
    "den",
    "please",
]

def normalize_query(text):
    text = text.lower().strip()

    for bn, en in BN_MAP.items():
        text = text.replace(bn.lower(), en)

    for word in REMOVE_WORDS:
        text = text.replace(word, "")

    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()

# ==========================================================
# 🔍 SUPER SMART SEARCH
# ==========================================================
async def smart_search(db, text):

    try:
        query = normalize_query(text)

        if not query or len(query) < 2:
            return None

        # ====================================
        # 1. EXACT MATCH
        # ====================================
        exact = await db.movies.find_one({
            "title": {
                "$regex": f"^{re.escape(query)}$",
                "$options": "i"
            }
        })

        if exact:
            logger.info(f"Exact Match: {exact['title']}")
            return exact

        # ====================================
        # 2. PARTIAL MATCH
        # ====================================
        partial = await db.movies.find_one({
            "title": {
                "$regex": re.escape(query),
                "$options": "i"
            }
        })

        if partial:
            logger.info(f"Partial Match: {partial['title']}")
            return partial

        # ====================================
        # 3. TEXT SEARCH
        # ====================================
        try:
            text_res = await db.movies.find_one({
                "$text": {
                    "$search": query
                }
            })

            if text_res:
                logger.info(f"Text Match: {text_res['title']}")
                return text_res

        except:
            pass

        # ====================================
        # 4. FUZZY MATCH
        # ====================================
        all_movies = await db.movies.find(
            {},
            {
                "title": 1
            }
        ).to_list(length=5000)

        best_match = None
        best_score = 0

        for movie in all_movies:

            movie_title = normalize_query(
                movie.get("title", "")
            )

            score = fuzz.token_sort_ratio(
                query,
                movie_title
            )

            if score > best_score:
                best_score = score
                best_match = movie

        if best_match and best_score >= 72:
            logger.info(
                f"Fuzzy Match: {best_match['title']} ({best_score}%)"
            )
            return best_match

        logger.info("No Match Found")
        return None

    except Exception as e:
        logger.error(f"Search Error: {e}")
        return None

# ==========================================================
# 👤 USER CONTEXT
# ==========================================================
async def get_bot_context(db, user_id):

    try:
        user = await db.users.find_one({
            "user_id": user_id
        })

        total_movies = await db.movies.count_documents({})
        total_users = await db.users.count_documents({})

        latest_cursor = db.movies.find(
            {},
            {
                "title": 1
            }
        ).sort("created_at", -1).limit(10)

        latest_movies = await latest_cursor.to_list(length=10)

        user_info = {
            "is_vip": (
                "Premium"
                if user and user.get(
                    "vip_until",
                    datetime.utcnow()
                ) > datetime.utcnow()
                else "Free"
            ),

            "coins": (
                user.get("coins", 0)
                if user else 0
            ),

            "total_movies": total_movies,
            "total_users": total_users,

            "latest_list": ", ".join([
                m["title"]
                for m in latest_movies
            ])
        }

        return user_info

    except Exception as e:
        logger.error(f"Context Error: {e}")

        return {
            "is_vip": "Free",
            "coins": 0,
            "total_movies": 0,
            "total_users": 0,
            "latest_list": "No Data"
        }

# ==========================================================
# 🤖 MAIN AI SYSTEM
# ==========================================================
async def get_smart_reply(
    user_text: str,
    user_name: str,
    db,
    user_id=None
):

    search_res = None

    identifier = str(user_id) if user_id else user_name

    try:

        now = datetime.now(
            pytz.timezone("Asia/Dhaka")
        )

        current_time = now.strftime("%I:%M %p")
        current_day = now.strftime("%A")

        clean_user_text = user_text.strip()

        # ====================================
        # USER CONTEXT
        # ====================================
        ctx = await get_bot_context(
            db,
            user_id
        )

        # ====================================
        # CHAT HISTORY
        # ====================================
        chat_history = []

        try:

            history_cursor = db.messages.find({
                "user_id": identifier
            }).sort("_id", -1).limit(4)

            history = await history_cursor.to_list(length=4)

            history.reverse()

            for item in history:

                chat_history.append({
                    "role": "user",
                    "content": item.get("text", "")
                })

                chat_history.append({
                    "role": "assistant",
                    "content": item.get("reply", "")
                })

        except:
            pass

        # ====================================
        # CASUAL DETECTION
        # ====================================
        casual_words = [
            "hi",
            "hello",
            "হাই",
            "হ্যালো",
            "কেমন আছো",
            "কি করো",
            "hey"
        ]

        is_casual = (
            len(clean_user_text) <= 2
            or clean_user_text.lower() in casual_words
        )

        # ====================================
        # SMART SEARCH
        # ====================================
        if not is_casual:
            search_res = await smart_search(
                db,
                clean_user_text
            )

        # ====================================
        # SEARCH GUIDE
        # ====================================
        if search_res:

            db_guide = f"""
Movie Found Successfully.

Movie Title:
{search_res['title']}

Tell the user happily that the movie
is available in database.
"""

        elif is_casual:

            db_guide = """
User is casually chatting.
Reply naturally and friendly.
"""

        else:

            db_guide = f"""
Movie not found in database.

Possible reasons:
- wrong spelling
- different language title
- incomplete title

Politely ask the user
to try another spelling.
Never roast the user.
"""

        # ====================================
        # SYSTEM PROMPT
        # ====================================
        system_prompt = f"""
You are Maya.

A smart Bengali movie assistant of MovieZone BD.

Current Time:
{current_time}
{current_day}

User Name:
{user_name}

VIP Status:
{ctx['is_vip']}

Coins:
{ctx['coins']}

Database Movies:
{ctx['total_movies']}

Total Users:
{ctx['total_users']}

LATEST MOVIES:
{ctx['latest_list']}

IMPORTANT RULES:

1. Always talk naturally in Bangladeshi Bengali.

2. Never say you are AI.

3. If movie found:
be excited and tell user movie exists.

4. If movie not found:
be polite and helpful.

5. Never insult users.

6. Never generate harmful content.

7. Keep replies short and natural.

8. Use emotional Bengali style.

9. Do not hallucinate fake movie links.

10. Behave like a smart Telegram assistant.

DATABASE STATUS:
{db_guide}
"""

        # ====================================
        # FALLBACK
        # ====================================
        if not API_KEYS:
            return fallback_reply(
                user_name,
                search_res
            )

        # ====================================
        # API REQUEST
        # ====================================
        current_api_key = random.choice(API_KEYS)

        headers = {
            "Authorization": f"Bearer {current_api_key}",
            "HTTP-Referer": "https://t.me/MovieZoneBot",
            "Content-Type": "application/json"
        }

        payload = {
            "model": MODEL_NAME,

            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },

                *chat_history,

                {
                    "role": "user",
                    "content": user_text
                }
            ],

            "temperature": 0.8,
            "max_tokens": 250
        }

        url = "https://openrouter.ai/api/v1/chat/completions"

        session = await get_session()

        final_reply = None

        async with session.post(
            url,
            headers=headers,
            json=payload
        ) as resp:

            if resp.status == 200:

                data = await resp.json()

                final_reply = data["choices"][0][
                    "message"
                ]["content"]

            else:

                logger.error(
                    f"OpenRouter Error: {resp.status}"
                )

        # ====================================
        # FALLBACK IF EMPTY
        # ====================================
        if not final_reply:

            return fallback_reply(
                user_name,
                search_res
            )

        # ====================================
        # CLEANUP
        # ====================================
        final_reply = (
            final_reply
            .replace("**", "")
            .replace("#", "")
            .strip()
        )

        # ====================================
        # SAVE MEMORY
        # ====================================
        try:

            await db.messages.insert_one({
                "user_id": identifier,
                "text": user_text,
                "reply": final_reply,
                "timestamp": now
            })

            msg_count = await db.messages.count_documents({
                "user_id": identifier
            })

            # Keep only last 20 messages
            if msg_count > 20:

                old_msgs = await db.messages.find({
                    "user_id": identifier
                }).sort("_id", 1).limit(
                    msg_count - 20
                ).to_list(None)

                await db.messages.delete_many({
                    "_id": {
                        "$in": [
                            m["_id"]
                            for m in old_msgs
                        ]
                    }
                })

        except Exception as e:
            logger.error(f"Memory Error: {e}")

        return final_reply

    except Exception as e:

        logger.error(f"Maya Error: {e}")

        return fallback_reply(
            user_name,
            search_res
        )

# ==========================================================
# 💬 FALLBACK
# ==========================================================
def fallback_reply(
    user_name,
    search_res
):

    if search_res:

        return (
            f"আরে {user_name}! 🍿\n\n"
            f"'{search_res['title']}' "
            f"মুভিটা পাওয়া গেছে 😎\n"
            f"নিচের বাটনে ক্লিক করে দেখে নাও!"
        )

    return (
        f"উফফ {user_name}! 🥺\n\n"
        f"একটু সমস্যা হচ্ছে এখন...\n"
        f"আরেকবার ট্রাই দাও প্লিজ!"
    )
