import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from collections import defaultdict

from dotenv import load_dotenv
from vkbottle import API, Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message

# ================== ТИШИНА В КОНСОЛИ ==================
for _name in list(logging.Logger.manager.loggerDict):
    if _name == "vkbottle" or _name.startswith("vkbottle."):
        _lg = logging.getLogger(_name)
        _lg.setLevel(logging.WARNING)
        _lg.propagate = False
try:
    from loguru import logger as _loguru_logger
    _loguru_logger.remove()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
    force=True,
)
log = logging.getLogger("bot")

# ================== ЗАГРУЗКА НАСТРОЕК ==================
load_dotenv()

BOT_TOKEN = os.getenv("VK_BOT_TOKEN")
SERVICE_TOKEN = os.getenv("VK_SERVICE_TOKEN")
NEWS_GROUP_IDS_RAW = os.getenv("VK_GROUP_IDS", "")
ANIME_GROUP_IDS_RAW = os.getenv("VK_ANIME_GROUP_IDS", "")
PHOTO_GROUP_IDS_RAW = os.getenv("VK_PHOTO_GROUP_IDS", "")
COOLDOWN_SECONDS = float(os.getenv("VK_COOLDOWN_SECONDS", "15"))
POSTS_PER_GROUP = int(os.getenv("VK_POSTS_PER_GROUP", "10"))

if not BOT_TOKEN:
    raise SystemExit("[ОШИБКА] VK_BOT_TOKEN не задан")
if not SERVICE_TOKEN:
    raise SystemExit("[ОШИБКА] VK_SERVICE_TOKEN не задан")


def parse_ids(raw: str, var_name: str) -> list[int]:
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise SystemExit(f"[ОШИБКА] {var_name} должен быть списком чисел через запятую")


NEWS_GROUP_IDS = parse_ids(NEWS_GROUP_IDS_RAW, "VK_GROUP_IDS")
ANIME_GROUP_IDS = parse_ids(ANIME_GROUP_IDS_RAW, "VK_ANIME_GROUP_IDS")
PHOTO_GROUP_IDS = parse_ids(PHOTO_GROUP_IDS_RAW, "VK_PHOTO_GROUP_IDS")

if not NEWS_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_GROUP_IDS пуст")
if not ANIME_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_ANIME_GROUP_IDS пуст")
if not PHOTO_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_PHOTO_GROUP_IDS пуст")

# ================== НОРМАЛИЗАЦИЯ ТЕКСТА ==================
_MENTION_RE = re.compile(r"\[(?:club|id|public)\d+\|[^\]]+\]")

def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _MENTION_RE.sub("", text)
    return " ".join(cleaned.split())

# ================== КЛЮЧЕВЫЕ СЛОВА ДЛЯ «РАНДОМА» ==================
RANDOM_SEARCH_KEYWORDS = [
    "новости", "юмор", "наука", "кино", "спорт", "технологии",
    "музыка", "искусство", "фотография", "путешествия", "еда",
    "игры", "книги", "авто", "мода", "здоровье", "образование",
    "история", "психология", "дизайн", "аниме", "мемы",
]

RANDOM_MAX_ATTEMPTS = 5
RANDOM_POSTS_TO_FETCH = 50  # сколько постов запрашивать за один поиск

# ================== КАТЕГОРИИ ==================
CATEGORIES = {
    "news": {"label": "Новости", "triggers": ["Новости", "новости", "/новости", "НОВОСТИ"], "group_ids": NEWS_GROUP_IDS},
    "anime": {"label": "Аниме", "triggers": ["Аниме", "аниме", "/anime", "АНИМЕ"], "group_ids": ANIME_GROUP_IDS},
    "photo": {"label": "Фото", "triggers": ["Фото", "фото", "/photo", "ФОТО"], "group_ids": PHOTO_GROUP_IDS},
    "random": {"label": "Рандом", "triggers": ["Рандом", "рандом", "/random", "РАНДОМ"], "group_ids": [], "mode": "random_search"},
}

ALL_TRIGGERS: list[str] = []
for _cat in CATEGORIES.values():
    ALL_TRIGGERS.extend(_cat["triggers"])

RESET_TRIGGERS = ["Сброс", "сброс", "/reset", "/сброс", "СБРОС"]
HELP_TRIGGERS = ["Помощь", "помощь", "/help", "/помощь", "Help", "help", "Начать", "начать", "/start", "Старт", "старт", "Меню", "меню"]

def detect_category(text: str | None) -> str | None:
    if not text: return None
    for name, cat in CATEGORIES.items():
        if text in cat["triggers"]: return name
    return None

# ================== СОСТОЯНИЕ ==================
STATE_FILE = os.path.join("data", "state.json")
state_lock = asyncio.Lock()
peer_locks = defaultdict(asyncio.Lock)
last_request: dict[tuple[str, int], float] = {}

def load_state() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("seen", {})
            data.setdefault("known", {})
            return data
        except Exception as e:
            log.warning(f"Не удалось прочитать {STATE_FILE}: {e}")
    return {"seen": {}, "known": {}}

def save_state() -> None:
    os.makedirs("data", exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Не удалось сохранить {STATE_FILE}: {e}")

state = load_state()

# ================== КЛАВИАТУРА ==================
def make_keyboard() -> Keyboard:
    return (
        Keyboard(one_time=True, inline=False)
        .add(Text("Новости"), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Аниме"), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Фото"), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text("Рандом"), color=KeyboardButtonColor.POSITIVE)
        .add(Text("Сброс"), color=KeyboardButtonColor.SECONDARY)
        .add(Text("Помощь"), color=KeyboardButtonColor.SECONDARY)
    )

HELP_TEXT = (
    "👋 Привет! Я показываю лучшие посты из заданных сообществ.\n\n"
    "📰 «Новости» — по одному лучшему посту из каждой новостной группы.\n"
    "🎌 «Аниме» — по одному лучшему посту из каждой аниме-группы.\n"
    "📷 «Фото» — по одному лучшему посту из каждой фотогруппы.\n"
    "🎲 «Рандом» — ищу посты по случайным ключевым словам (новости, юмор, наука и т.д.), "
    "собираю их и показываю самый популярный, который вы ещё не видели. "
    "Если всё найденное уже показано — ищу заново по другому слову.\n\n"
    "Во всех режимах:\n"
    "    • Сначала показываю новые посты (которых раньше не было).\n"
    "    • Если новых нет — следующий по популярности из старых.\n"
    "    • Если все доступные посты уже показаны — пишу «Пусто».\n\n"
    "♻️ «Сброс» — очищаю память по всем разделам.\n\n"
    "⏱ Внутри одного раздела между запросами есть пауза — защита от спама."
)

# ================== ЛОГИКА ==================
api = API(token=SERVICE_TOKEN)
bot = Bot(token=BOT_TOKEN)

async def get_candidate_for_group(group_id: int, peer_key: str):
    try:
        response = await api.wall.get(owner_id=group_id, count=POSTS_PER_GROUP)
    except Exception as e:
        log.error(f"Чтение группы {group_id}: {e}")
        return None, "error"
    posts = response.items
    if not posts: return None, "no_posts"
    current_ids = {p.id for p in posts}
    posts_by_id = {p.id: p for p in posts}
    group_key = str(group_id)
    async with state_lock:
        seen = set(state["seen"].get(peer_key, []))
        known = set(state["known"].get(peer_key, {}).get(group_key, []))
        new_ids = current_ids - known
        old_ids = current_ids & known
        state["known"].setdefault(peer_key, {})[group_key] = list(current_ids)
        new_posts = [posts_by_id[i] for i in new_ids if f"{group_id}_{i}" not in seen]
        old_posts = [posts_by_id[i] for i in old_ids if f"{group_id}_{i}" not in seen]
        new_posts.sort(key=lambda p: p.likes.count if p.likes else 0, reverse=True)
        old_posts.sort(key=lambda p: p.likes.count if p.likes else 0, reverse=True)
        candidates = new_posts + old_posts
        if not candidates:
            save_state()
            return None, "empty"
        chosen = candidates[0]
        state["seen"].setdefault(peer_key, []).append(f"{group_id}_{chosen.id}")
        save_state()
        return chosen, "ok"

async def get_random_post_from_search(peer_key: str):
    last_error = False
    for attempt in range(1, RANDOM_MAX_ATTEMPTS + 1):
        keyword = random.choice(RANDOM_SEARCH_KEYWORDS)
        log.info(f"🎲 Рандом, попытка {attempt}/{RANDOM_MAX_ATTEMPTS}: слово «{keyword}»")
        try:
            search_response = await api.newsfeed.search(
                q=keyword,
                count=RANDOM_POSTS_TO_FETCH,
                extended=0,
            )
        except Exception as e:
            log.error(f"newsfeed.search «{keyword}»: {e}")
            last_error = True
            continue
        items = search_response.items or []
        if not items:
            continue
        # Сортируем по лайкам (по убыванию)
        items.sort(key=lambda p: p.likes.count if p.likes else 0, reverse=True)
        async with state_lock:
            seen = set(state["seen"].get(peer_key, []))
        for post in items:
            key = f"random_{post.owner_id}_{post.id}"
            if key not in seen:
                async with state_lock:
                    state["seen"].setdefault(peer_key, []).append(key)
                    save_state()
                log.info(f"🎲 Найден новый пост: {post.owner_id}_{post.id}")
                return post, "ok"
    if last_error: return None, "error"
    return None, "empty"

async def process_category(message: Message, category_name: str):
    peer_id = message.peer_id
    cat = CATEGORIES[category_name]
    peer_key = f"{category_name}:{peer_id}"
    lock_key = (category_name, peer_id)

    async with peer_locks[lock_key]:
        now = time.monotonic()
        last = last_request.get(lock_key, 0.0)
        elapsed = now - last
        if elapsed < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - elapsed
            log.info(f"{peer_id} / {category_name}: кулдаун, осталось {remaining:.1f} сек.")
            await message.answer(f"⏳ По разделу «{cat['label']}» подожди {remaining:.0f} сек.", keyboard=make_keyboard())
            return
        last_request[lock_key] = now
        log.info(f"→ {message.from_id}: «{cat['label']}» в чат {peer_id}")
        await message.answer(f"Ищу «{cat['label']}»...", keyboard=make_keyboard())

        if cat.get("mode") == "random_search":
            post, status = await get_random_post_from_search(peer_key)
            if post:
                attachment = f"wall{post.owner_id}_{post.id}"
                try:
                    await message.answer(attachment=attachment)
                    likes = post.likes.count if post.likes else 0
                    log.info(f"← {peer_id} / random: {attachment} (лайков {likes})")
                except Exception as e:
                    log.error(f"Отправка {attachment}: {e}")
                    await message.answer("Не удалось отправить найденный пост.", keyboard=make_keyboard())
            else:
                if status == "empty":
                    await message.answer("🎲 «Рандом»: Пусто (все найденные посты уже показаны). Сбрось память кнопкой «Сброс».", keyboard=make_keyboard())
                else:
                    await message.answer("🎲 «Рандом»: не удалось найти посты. Попробуй позже.", keyboard=make_keyboard())
            return

        # Для news/anime/photo — старый цикл
        for group_id in cat["group_ids"]:
            post, status = await get_candidate_for_group(group_id, peer_key)
            if post:
                attachment = f"wall{post.owner_id}_{post.id}"
                try:
                    await message.answer(attachment=attachment)
                    likes = post.likes.count if post.likes else 0
                    log.info(f"← {peer_id} / {category_name}: {attachment} (лайков {likes})")
                except Exception as e:
                    log.error(f"Отправка {attachment}: {e}")
                    await message.answer(f"Не удалось отправить пост из группы {group_id}", keyboard=make_keyboard())
            else:
                if status == "empty":
                    await message.answer(f"«{cat['label']}» / группа {group_id}: Пусто (все посты уже показаны). Сбрось память кнопкой «Сброс».", keyboard=make_keyboard())
                elif status == "no_posts":
                    await message.answer(f"«{cat['label']}» / группа {group_id}: Пусто (нет постов).", keyboard=make_keyboard())
                elif status == "error":
                    await message.answer(f"«{cat['label']}» / группа {group_id}: Ошибка чтения.", keyboard=make_keyboard())

# ================== ОБРАБОТЧИКИ ==================
@bot.on.message(func=lambda m: normalize_text(m.text) in ALL_TRIGGERS)
async def category_handler(message: Message):
    category_name = detect_category(normalize_text(message.text))
    if category_name:
        await process_category(message, category_name)

@bot.on.message(func=lambda m: normalize_text(m.text) in RESET_TRIGGERS)
async def reset_handler(message: Message):
    peer_id = message.peer_id
    async with state_lock:
        total_seen = 0
        total_groups = 0
        for category_name in CATEGORIES.keys():
            peer_key = f"{category_name}:{peer_id}"
            total_seen += len(state["seen"].pop(peer_key, []))
            total_groups += len(state["known"].pop(peer_key, {}))
        save_state()
    log.info(f"↺ {peer_id}: сброс памяти (удалено seen={total_seen}, known_groups={total_groups})")
    await message.answer("♻️ Память по всем разделам («Новости», «Аниме», «Фото», «Рандом») сброшена.", keyboard=make_keyboard())

@bot.on.message(func=lambda m: normalize_text(m.text) in HELP_TRIGGERS)
async def help_handler(message: Message):
    log.info(f"? {message.from_id} запросил помощь в чате {message.peer_id}")
    await message.answer(HELP_TEXT, keyboard=make_keyboard())

@bot.on.message()
async def fallback_handler(message: Message):
    log.info(f"? {message.from_id}: неизвестная команда «{message.text}»")
    await message.answer("Не знаю такую команду. Вот что я умею:\n\n" + HELP_TEXT, keyboard=make_keyboard())

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("Бот запускается...")
    log.info(f"Новости: {NEWS_GROUP_IDS}")
    log.info(f"Аниме:   {ANIME_GROUP_IDS}")
    log.info(f"Фото:    {PHOTO_GROUP_IDS}")
    log.info(f"Рандом:  поиск постов по {len(RANDOM_SEARCH_KEYWORDS)} ключевым словам")
    log.info(f"Кулдаун: {COOLDOWN_SECONDS:.0f} сек.")
    log.info("=" * 50)
    bot.run_forever()
