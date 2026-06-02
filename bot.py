import asyncio, logging, os, sqlite3, subprocess, re, httpx, json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, CallbackQuery, PreCheckoutQuery, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

BOT_TOKEN = os.getenv("BOT_TOKEN", "8544244730:AAHj81ZKN2m2NlFbPrkTE6LAsyPsRhJIJwg")
VSEGPT_API_KEY = os.getenv("VSEGPT_API_KEY", "sk-or-vv-a623a8dffd32949c885c0ed9149e5550a4b7bc6fd27007af13f481536bc48b04")
FREE_LIMIT = 2
MAX_VIDEO_SIZE_MB = 20
SUB_PRICE = 111

PROXY_URL = os.getenv("PROXY_URL", None)
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

class DescState(StatesGroup):
    waiting_topic = State()

def init_db():
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, file_id TEXT, score REAL, report TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS usage (user_id BIGINT, week TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, week))")
    cur.execute("CREATE TABLE IF NOT EXISTS pro (user_id BIGINT PRIMARY KEY, until TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS last_report (user_id BIGINT PRIMARY KEY, report TEXT, score REAL)")
    conn.commit()
    conn.close()

init_db()

def get_week():
    now = datetime.now()
    iso = now.isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"

def is_pro(uid):
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("SELECT until FROM pro WHERE user_id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    return row and row[0] and datetime.now() < datetime.fromisoformat(row[0])

def get_usage(uid):
    wk = get_week()
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("SELECT count FROM usage WHERE user_id=? AND week=?", (uid, wk))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def inc_usage(uid):
    wk = get_week()
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("SELECT count FROM usage WHERE user_id=? AND week=?", (uid, wk))
    if cur.fetchone():
        cur.execute("UPDATE usage SET count=count+1 WHERE user_id=? AND week=?", (uid, wk))
    else:
        cur.execute("INSERT INTO usage VALUES (?,?,1)", (uid, wk))
    conn.commit()
    conn.close()

def can_analyze(uid):
    return is_pro(uid) or get_usage(uid) < FREE_LIMIT

def left_analyses(uid):
    return "∞ (Premium)" if is_pro(uid) else str(FREE_LIMIT - get_usage(uid))

def add_pro(uid, days=30):
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    until = (datetime.now() + timedelta(days=days)).isoformat()
    cur.execute("INSERT OR REPLACE INTO pro (user_id, until) VALUES (?,?)", (uid, until))
    conn.commit()
    conn.close()

def save_last_report(uid, report, score):
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO last_report (user_id, report, score) VALUES (?,?,?)", (uid, report, score))
    conn.commit()
    conn.close()

def get_last_report(uid):
    conn = sqlite3.connect("ttmoder.db")
    cur = conn.cursor()
    cur.execute("SELECT report, score FROM last_report WHERE user_id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    return row

def main_menu():
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="📤 Загрузить видео", callback_data="upload"))
    b.add(InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats"))
    b.add(InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction"))
    b.add(InlineKeyboardButton(text="⭐ Premium", callback_data="premium_info"))
    b.add(InlineKeyboardButton(text="ℹ️ О сервисе", callback_data="about"))
    b.adjust(1)
    return b.as_markup()

def after_analysis_menu():
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="🎯 Сгенерировать CTA", callback_data="gen_cta"))
    b.add(InlineKeyboardButton(text="📝 Сгенерировать описание", callback_data="gen_desc"))
    b.add(InlineKeyboardButton(text="🎤 Голосовой разбор", callback_data="gen_voice"))
    b.add(InlineKeyboardButton(text="📤 Ещё анализ", callback_data="upload"))
    b.add(InlineKeyboardButton(text="🏠 В меню", callback_data="menu"))
    b.adjust(1)
    return b.as_markup()

async def ai_ask(prompt: str, max_tokens: int = 800) -> str:
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(
                "https://api.vsegpt.ru/v1/chat/completions",
                headers={"Authorization": f"Bearer {VSEGPT_API_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"] if "choices" in data else "⚠️ Пустой ответ AI"
            return f"⚠️ Ошибка AI: {r.status_code}"
    except Exception as e:
        return f"⚠️ Ошибка: {str(e)[:100]}"

async def analyze_video(video_path: str):
    try:
        result = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", video_path], capture_output=True, text=True, timeout=30)
        if result.returncode != 0: return None
        return json.loads(result.stdout)
    except: return None

def parse_tech_info(data: dict):
    if not data or "streams" not in data:
        return {"resolution": "не определено", "codec": "не определено", "fps": "не определено", "bitrate": "не определено", "duration": 0, "has_audio": False}
    video_stream = audio_stream = None
    for s in data["streams"]:
        if s["codec_type"] == "video" and not video_stream: video_stream = s
        elif s["codec_type"] == "audio" and not audio_stream: audio_stream = s
    info = {"resolution": "не определено", "codec": "не определено", "fps": "не определено", "bitrate": "не определено", "duration": 0, "has_audio": audio_stream is not None}
    if video_stream:
        w, h = video_stream.get("width", 0), video_stream.get("height", 0)
        if w and h: info["resolution"] = f"{w}x{h}"
        info["codec"] = video_stream.get("codec_name", "не определено")
        fps_str = video_stream.get("r_frame_rate", "0/1")
        if "/" in fps_str:
            parts = fps_str.split("/")
            if parts[1] != "0": info["fps"] = f"{round(int(parts[0])/int(parts[1]), 1)} fps"
    if "format" in data:
        info["bitrate"] = f"{round(int(data['format'].get('bit_rate', 0))/1000)} kbps" if data["format"].get("bit_rate") else "не определено"
        info["duration"] = round(float(data["format"].get("duration", 0)))
    return info

def calculate_score(info: dict, file_size: int) -> tuple:
    w, h = 0, 0
    if "x" in info['resolution']:
        parts = info['resolution'].split("x")
        w, h = int(parts[0]), int(parts[1])
    pixels = w * h
    if pixels >= 1920*1080: res_score, res_text = 30, f"✅ Full HD {info['resolution']}"
    elif pixels >= 1280*720: res_score, res_text = 22, f"⚠️ HD {info['resolution']}"
    elif pixels >= 1024*576: res_score, res_text = 12, f"⚠️ {info['resolution']} — низкое"
    elif pixels >= 640*360: res_score, res_text = 5, f"❌ {info['resolution']} — очень низкое"
    else: res_score, res_text = 0, f"❌ {info['resolution']} — критическое"
    ratio = h / w if w > 0 else 0
    if 1.7 <= ratio <= 1.8: ratio_score, ratio_text = 10, "✅ Соотношение 9:16"
    elif 1.3 <= ratio <= 2.2: ratio_score, ratio_text = 5, f"⚠️ Соотношение {ratio:.1f}:1"
    else: ratio_score, ratio_text = 0, f"❌ Соотношение {ratio:.1f}:1"
    br = int(info['bitrate'].replace(" kbps", "")) if "kbps" in info['bitrate'] else 0
    if br >= 8000: br_score, br_text = 20, f"✅ {info['bitrate']}"
    elif br >= 4000: br_score, br_text = 17, f"✅ {info['bitrate']}"
    elif br >= 2000: br_score, br_text = 13, f"✅ {info['bitrate']}"
    elif br >= 1000: br_score, br_text = 8, f"⚠️ {info['bitrate']}"
    elif br >= 500: br_score, br_text = 4, f"⚠️ {info['bitrate']}"
    elif br > 0: br_score, br_text = 1, f"❌ {info['bitrate']}"
    else: br_score, br_text = 0, "❌ Битрейт не определён"
    fps_val = float(info['fps'].replace(" fps", "")) if "fps" in info['fps'] else 0
    if 29 <= fps_val <= 31: fps_score, fps_text = 10, f"✅ {info['fps']}"
    elif 24 <= fps_val <= 30: fps_score, fps_text = 9, f"✅ {info['fps']}"
    elif 30 < fps_val <= 60: fps_score, fps_text = 7, f"⚠️ {info['fps']}"
    elif 15 <= fps_val < 24: fps_score, fps_text = 5, f"⚠️ {info['fps']}"
    elif fps_val > 60: fps_score, fps_text = 3, f"⚠️ {info['fps']}"
    elif fps_val > 0: fps_score, fps_text = 2, f"❌ {info['fps']}"
    else: fps_score, fps_text = 0, "❌ FPS не определён"
    codec = info['codec']
    if codec in ['h264', 'hevc', 'h265']: codec_score, codec_text = 10, f"✅ {codec}"
    else: codec_score, codec_text = 4, f"⚠️ {codec}"
    dur = info['duration']
    if 15 <= dur <= 30: dur_score, dur_text = 10, f"✅ {dur} сек"
    elif 10 <= dur <= 60: dur_score, dur_text = 9, f"✅ {dur} сек"
    elif 5 <= dur <= 180: dur_score, dur_text = 6, f"⚠️ {dur} сек"
    elif dur < 5: dur_score, dur_text = 2, f"❌ {dur} сек"
    elif dur > 180: dur_score, dur_text = 3, f"❌ {dur} сек"
    else: dur_score, dur_text = 5, f"⚠️ {dur} сек"
    if info['has_audio']: audio_score, audio_text = 10, "✅ Аудио есть"
    else: audio_score, audio_text = 0, "❌ Аудио отсутствует"
    size_text = f"✅ Размер: {round(file_size/1024/1024, 1) if file_size else '?'} МБ"
    total = res_score + ratio_score + br_score + fps_score + codec_score + dur_score + audio_score
    total = max(1, min(100, total))
    return total, [res_text, ratio_text, br_text, fps_text, codec_text, dur_text, audio_text, size_text]

def generate_report_file(report: str, uid: int) -> str:
    path = f"./TikAudit_{uid}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path

@dp.message(Command("start"))
async def start(msg: Message):
    left = left_analyses(msg.from_user.id)
    await msg.answer(
        f"🎬 TikAudit v1.1\n\nПривет, креатор! 👋\n\n"
        f"Я — твой AI-помощник для TikTok.\n"
        f"Загрузи видео, и я покажу:\n"
        f"🔍 Где ты теряешь просмотры\n💡 Как улучшить качество\n"
        f"🎯 Какие CTA и хештеги сработают\n📈 Лучшее время для публикации\n\n"
        f"Всё за 30 секунд. Без воды, только польза.\n\n"
        f"🎁 Бесплатно: {left} анализов\n💎 Premium: безлимит за {SUB_PRICE} Stars/мес (~199₽)",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data=="instruction")
async def instruction(cb: CallbackQuery):
    await cb.message.edit_text("📖 <b>ИНСТРУКЦИЯ</b>\n\n1. Нажми 📤 Загрузить видео\n2. Отправь видео файлом\n3. Получи отчёт\n4. Используй генераторы\n\nБесплатно: 2/нед | Premium: безлимит", parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data=="about")
async def about(cb: CallbackQuery):
    await cb.message.edit_text("🎬 TikAudit v1.1\n\n• Технический анализ (ffprobe)\n• AI-рекомендации (DeepSeek)\n• Реальная система оценки\n• CTA, описание, голос\n\nБесплатно: 2/нед | Premium: 111 Stars/мес", reply_markup=main_menu())

@dp.callback_query(F.data=="premium_info")
async def premium_info(cb: CallbackQuery):
    if is_pro(cb.from_user.id):
        conn = sqlite3.connect("ttmoder.db"); cur = conn.cursor()
        cur.execute("SELECT until FROM pro WHERE user_id=?", (cb.from_user.id,))
        row = cur.fetchone(); conn.close()
        d = datetime.fromisoformat(row[0]).strftime("%d.%m.%Y") if row and row[0] else ""
        await cb.message.edit_text(f"⭐ PREMIUM АКТИВЕН\n\nПодписка до: {d}\nБезлимитный анализ.", reply_markup=main_menu())
        return
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text=f"💎 Купить Premium — {SUB_PRICE} ⭐", callback_data="buy_premium"))
    b.add(InlineKeyboardButton(text="📖 Как купить Stars?", callback_data="stars_guide"))
    b.add(InlineKeyboardButton(text="🏠 В меню", callback_data="menu"))
    b.adjust(1)
    await cb.message.edit_text(f"⭐ PREMIUM\n\n• Безлимитный анализ\n• Все AI-генераторы\n\nЦена: {SUB_PRICE} Stars/мес (~199₽)", reply_markup=b.as_markup())

@dp.callback_query(F.data=="stars_guide")
async def stars_guide(cb: CallbackQuery):
    await cb.message.edit_text("📖 <b>Как купить Telegram Stars</b>\n\n<b>@PremiumBot</b>\n1. Открой @PremiumBot\n2. /stars → Купить\n3. Оплати картой/СберПэй\n💰 ~179₽ за 100 ⭐", parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data=="buy_premium")
async def buy_premium(cb: CallbackQuery):
    await bot.send_invoice(cb.from_user.id, "TikAudit Premium", "Безлимитный анализ на 30 дней.", "premium_month", "XTR", [LabeledPrice(label="Premium (30 дней)", amount=SUB_PRICE)], provider_token="")
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def paid(msg: Message):
    add_pro(msg.from_user.id, 30)
    await msg.answer("✅ Premium активирован на 30 дней!")

@dp.callback_query(F.data=="menu")
async def menu(cb: CallbackQuery):
    await cb.message.edit_text(f"🎬 TikAudit v1.1\n\nОсталось: {left_analyses(cb.from_user.id)} анализов", reply_markup=main_menu())

@dp.callback_query(F.data=="upload")
async def upload(cb: CallbackQuery):
    if not can_analyze(cb.from_user.id):
        b = InlineKeyboardBuilder()
        b.add(InlineKeyboardButton(text=f"💎 Premium — {SUB_PRICE} ⭐", callback_data="buy_premium"))
        b.add(InlineKeyboardButton(text="🏠 В меню", callback_data="menu"))
        b.adjust(1)
        await cb.message.edit_text("🔒 Лимит исчерпан.", reply_markup=b.as_markup())
        return
    await cb.message.answer("📤 Пришли видео файлом.\nМакс. 20 МБ. MP4, MOV, AVI.")

@dp.message(F.video | F.document)
async def handle_video(msg: Message):
    uid = msg.from_user.id
    if not can_analyze(uid):
        await msg.answer("🔒 Лимит исчерпан.")
        return
    file_size = msg.video.file_size if msg.video else msg.document.file_size
    if file_size and file_size > MAX_VIDEO_SIZE_MB * 1024 * 1024:
        await msg.answer(f"⚠️ Слишком большой. Макс {MAX_VIDEO_SIZE_MB} МБ.")
        return
    
    status_msg = await msg.answer("🔍 Анализирую...")
    video_path = f"./tt_{uid}.mp4"
    try:
        file_id = msg.video.file_id if msg.video else msg.document.file_id
        await bot.download_file((await bot.get_file(file_id)).file_path, video_path)
        probe = await analyze_video(video_path)
        if not probe:
            await status_msg.edit_text("❌ Не удалось.")
            return
        info = parse_tech_info(probe)
        score, checks = calculate_score(info, file_size)
        
        await status_msg.edit_text("🤖 AI анализирует...")
        prompt = f"""Ты — эксперт по TikTok. Проанализируй параметры видео и дай ПОЛНЫЙ разбор:
- Разрешение: {info['resolution']}, Длительность: {info['duration']} сек, Кодек: {info['codec']}, Аудио: {'есть' if info['has_audio'] else 'нет'}, Битрейт: {info['bitrate']}, FPS: {info['fps']}
Ответь по пунктам: 1. 🔧 ОПТИМИЗАЦИЯ 2. 💡 УЛУЧШЕНИЯ 3. ⏰ ВРЕМЯ ПУБЛИКАЦИИ 4. #️⃣ ХЕШТЕГИ (5-7) 5. 🎯 CTA (3 варианта) 6. ✅ ЧЕК-ЛИСТ"""
        ai_result = await ai_ask(prompt, 8000)
        
        emoji = '🟢 Отлично' if score >= 80 else '🟡 Можно лучше' if score >= 50 else '🔴 Доработка'
        tech_block = "\n".join(checks)
        
        full_report = (
            f"{'='*50}\n"
            f"  📊 TikAudit v1.1 — ПОЛНЫЙ ОТЧЁТ\n"
            f"{'='*50}\n\n"
            f"  🏆 ОЦЕНКА: {score}/100 {emoji}\n"
            f"{'='*50}\n\n"
            f"🔧 ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ\n{'-'*30}\n{tech_block}\n\n"
            f"🤖 AI-АНАЛИЗ И РЕКОМЕНДАЦИИ\n{'-'*30}\n{ai_result}\n\n"
            f"{'='*50}\n"
            f"  TikAudit — профессиональный анализ для TikTok\n"
            f"  @tikaudit_bot\n"
            f"{'='*50}\n"
        )
        
        tg_report = full_report[:3900] + "\n\n⚠️ Обрезано. Файл содержит полную версию." if len(full_report) > 4000 else full_report
        
        save_last_report(uid, full_report, score)
        conn = sqlite3.connect("ttmoder.db"); cur = conn.cursor()
        cur.execute("INSERT INTO analyses (user_id, file_id, score, report) VALUES (?,?,?,?)", (uid, file_id, score, full_report))
        conn.commit(); conn.close()
        inc_usage(uid)
        
        # Отправляем отчёт и прикрепляем к нему файл
        report_msg = await msg.answer(tg_report, reply_markup=after_analysis_menu())
        file_path = generate_report_file(full_report, uid)
        await report_msg.reply_document(FSInputFile(file_path), caption="📄 TikAudit — полный отчёт")
        os.remove(file_path)
        await status_msg.delete()
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")
    finally:
        if os.path.exists(video_path): os.remove(video_path)

@dp.callback_query(F.data=="gen_cta")
async def gen_cta(cb: CallbackQuery):
    row = get_last_report(cb.from_user.id)
    if not row: await cb.answer("⚠️ Сначала загрузи видео.", show_alert=True); return
    await cb.message.answer("🎯 Генерирую...")
    result = await ai_ask(f"3 ярких CTA для TikTok:\n{row[0][:500]}", 400)
    await cb.message.answer(f"🎯 CTA:\n\n{result}")

@dp.callback_query(F.data=="gen_desc")
async def gen_desc(cb: CallbackQuery, state: FSMContext):
    row = get_last_report(cb.from_user.id)
    if not row: await cb.answer("⚠️ Сначала загрузи видео.", show_alert=True); return
    await state.set_state(DescState.waiting_topic)
    await cb.message.answer("📝 Напиши тему видео:")

@dp.message(DescState.waiting_topic)
async def desc_generate(msg: Message, state: FSMContext):
    await state.clear()
    row = get_last_report(msg.from_user.id)
    if not row: await msg.answer("⚠️ Сначала загрузи видео."); return
    await msg.answer("📝 Генерирую...")
    result = await ai_ask(f"Описание для TikTok на тему: «{msg.text}». С эмодзи, хештегами.", 400)
    await msg.answer(f"📝 ОПИСАНИЕ:\n\n{result}")

@dp.callback_query(F.data=="gen_voice")
async def gen_voice(cb: CallbackQuery):
    row = get_last_report(cb.from_user.id)
    if not row: await cb.answer("⚠️ Сначала загрузи видео.", show_alert=True); return
    await cb.message.answer("🎤 Готовлю...")
    result = await ai_ask(f"Текст для озвучки (30 сек):\n{row[0][:500]}", 400)
    await cb.message.answer(f"🎤 ОЗВУЧКА:\n\n{result}")

@dp.callback_query(F.data=="stats")
async def stats(cb: CallbackQuery):
    conn = sqlite3.connect("ttmoder.db"); cur = conn.cursor()
    cur.execute("SELECT score, created_at FROM analyses WHERE user_id=? ORDER BY id DESC LIMIT 7", (cb.from_user.id,))
    rows = cur.fetchall(); conn.close()
    if not rows: await cb.message.edit_text("📊 Нет данных.", reply_markup=main_menu()); return
    text = "📊 АНАЛИЗЫ\n\n"
    for score, date in reversed(rows):
        bar = "▓" * (int(score) // 10)
        text += f"{date}: {int(score)}/100 {bar}\n"
    await cb.message.edit_text(text, reply_markup=main_menu())

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
