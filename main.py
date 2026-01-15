# Deskform App v6.15 WebApp Handler
import asyncio
import logging
import json
import os
from datetime import datetime
from dataclasses import asdict

# Проверка библиотек
try:
    from dotenv import load_dotenv
    from pymongo import UpdateOne
    from aiohttp import web
except ImportError:
    print("❌ ОШИБКА: Выполните pip install python-dotenv pymongo motor aiogram aiohttp")
    exit()

# Импорт бизнес-логики и моделей
try:
    from business_logic import WebAppOrderData, OrderItem, PricingEngine
except ImportError:
    print("⚠️ ПРЕДУПРЕЖДЕНИЕ: Файл business_logic.py не найден. Расчеты и модели не будут работать.")
    WebAppOrderData = OrderItem = PricingEngine = None

# Импорт сервера админки
try:
    from admin_server import start_admin_server
except ImportError:
    print("⚠️ ПРЕДУПРЕЖДЕНИЕ: Файл admin_server.py не найден. Веб-интерфейс не запустится.")
    start_admin_server = None

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode, ContentType
from aiogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    WebAppInfo, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    Message
)

from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================

load_dotenv() 

API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
WEBAPP_URL = os.getenv("WEBAPP_URL")
MONGO_URL = os.getenv("MONGO_URL")

DB_NAME = "deskform_db"
TRUCK_COST = 1000000        
TRANSIT_DELAY_SEC = 3600    

CITIES = [
    "Ташкент", "Андижан", "Наманган", "Фергана", "Коканд", 
    "Гулистан", "Джизак", "Самарканд", "Карши", "Термез", 
    "Бухара", "Навои", "Ургенч", "Нукус"
]

STATUS_MAP = {
    'new': "⏳ Обработка",
    'approved': "✅ Подтвержден (Сборка)",
    'shipped': "🚚 Отгружен (В пути)",
    'rejected': "❌ Отменен"
}

logging.basicConfig(level=logging.INFO)

if not API_TOKEN:
    logging.error("Не найден BOT_TOKEN в .env")
    exit()

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ==========================================
# 2. БАЗА ДАННЫХ
# ==========================================

cluster = AsyncIOMotorClient(MONGO_URL)
db = cluster[DB_NAME]

users_collection = db['users']
orders_collection = db['orders']
consolidations_collection = db['consolidations'] 
routes_collection = db['routes']                 
products_collection = db['products']
chat_history_collection = db['chat_history'] 

# ==========================================
# 3. ЛОГИРОВАНИЕ (Middleware & Helpers)
# ==========================================

async def log_chat_event(user_id, sender, text, message_id=None):
    """Базовая функция записи в БД"""
    if not text: return
    try:
        await chat_history_collection.insert_one({
            "user_id": user_id,
            "sender": sender, # 'user' | 'bot'
            "text": text,
            "message_id": message_id,
            "timestamp": datetime.now()
        })
    except Exception as e:
        logging.error(f"Chat Log Error: {e}")

# 🔥 НОВИНКА: Middleware для перехвата ВСЕХ сообщений юзера
class AllMessagesMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            user_id = event.from_user.id
            content = "Неизвестный тип"
            
            # Определяем тип контента для красивого отображения
            if event.text:
                content = event.text
            elif event.photo:
                content = f"🖼 [ФОТО] {event.caption if event.caption else ''}"
            elif event.document:
                content = f"📄 [ДОКУМЕНТ] {event.document.file_name}"
            elif event.voice:
                content = "🎤 [ГОЛОСОВОЕ]"
            elif event.video:
                content = f"🎥 [ВИДЕО] {event.caption if event.caption else ''}"
            elif event.sticker:
                content = f"😊 [СТИКЕР] {event.sticker.emoji}"
            elif event.contact:
                content = f"📱 [КОНТАКТ] {event.contact.phone_number}"
            elif event.location:
                content = "📍 [ГЕОПОЗИЦИЯ]"
            elif event.web_app_data:
                content = "📦 [ACTION] Отправил данные из WebApp"

            # Логируем (только входящие от юзера, т.к. исходящие мы ловим в send_and_track)
            # Фильтруем команды /history чтобы не захламлять лог запросами
            if not (event.text and event.text.startswith('/history')):
                await log_chat_event(user_id, "user", content, event.message_id)
        
        return await handler(event, data)

# Подключаем Middleware к диспетчеру
dp.message.middleware(AllMessagesMiddleware())


async def send_and_track(chat_id, text, **kwargs):
    """
    Обертка для исходящих сообщений БОТА.
    """
    try:
        msg = await bot.send_message(chat_id, text, **kwargs)
        # Логируем исходящее от бота
        await log_chat_event(chat_id, "bot", text, msg.message_id)
        return msg
    except Exception as e:
        logging.error(f"Send & Track Error: {e}")
        return None

# ==========================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

async def update_product_stats(order_items, order_date):
    if not order_items: return
    operations = []
    for item in order_items:
        # Теперь order_items - это список словарей из asdict
        pid = item.get('product_id') or item.get('id')
        if not pid: continue
        try:
            qty = int(item.get('qty', 0))
            total = float(item.get('total', 0))
        except ValueError: continue

        op = UpdateOne(
            {"_id": str(pid)}, 
            {
                "$inc": {"total_qty_sold": qty, "total_revenue": total, "orders_count": 1},
                "$set": {"last_known_name": item.get('name', 'Unknown'), "last_sold_at": order_date},
                "$setOnInsert": {"first_sold_at": order_date}
            },
            upsert=True
        )
        operations.append(op)
    if operations:
        try: await products_collection.bulk_write(operations)
        except Exception as e: logging.error(f"Stats Update Error: {e}")

async def notify_admin(text, reply_markup=None):
    try:
        await bot.send_message(ADMIN_ID, f"🔔 <b>Админ:</b>\n{text}", parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Admin notify error: {e}")

def format_currency(value):
    try:
        return "{:,.0f}".format(float(str(value).replace(' ', ''))).replace(",", " ")
    except:
        return str(value)

async def upsert_user(user_id, username, full_name, phone, city, company_name):
    await users_collection.update_one(
        {"_id": user_id},
        {
            "$set": {
                "username": username, "full_name": full_name,
                "phone": phone, "city": city, "company_name": company_name,
                "last_active": datetime.now()
            },
            "$setOnInsert": {"created_at": datetime.now(), "client_type": "new", "balance": 0}
        },
        upsert=True
    )

def generate_order_receipt(order_data, is_admin=False):
    items_str = ""
    for i, item in enumerate(order_data['items'], 1):
        items_str += (
            f"{i}. {item['name']}\n"
            f"   └ <b>{item['qty']} шт</b> x {format_currency(item['price_per_unit'])} = {format_currency(item['total'])}\n"
        )
    method = order_data.get('shipping_method', 'standard')
    delivery_icon = "🚕" if method == 'express' else "🚛"
    delivery_mode = "ЭКСПРЕСС" if method == 'express' else "СТАНДАРТ (Фура)"
    status_human = STATUS_MAP.get(order_data.get('status', 'new'), '?')

    text = f"{delivery_icon} <b>ЗАКАЗ #{str(order_data.get('_id', '...'))[-6:]}</b>\n"
    text += f"📅 <i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>\n"
    text += f"📊 Статус: <b>{status_human}</b>\n\n"
    text += f"<b>🛒 Товары:</b>\n{items_str}\n"
    text += f"{'─'*15}\n"
    text += f"💵 <b>ИТОГО: {format_currency(order_data['total_final'])} сум</b>\n"
    
    discount_tier = order_data.get('discount_tier_pct', 0)
    discount_pay = order_data.get('discount_pay_pct', 0)
    if discount_tier > 0 or discount_pay > 0:
        total_pct = discount_tier + discount_pay
        savings = order_data['total_base'] - order_data['total_final']
        text += f"📉 Скидка: {total_pct}% (Экономия: {format_currency(savings)})\n"
    
    text += f"\n<b>{delivery_icon} Доставка: {delivery_mode}</b>\n"
    text += f"📍 Регион: {order_data.get('user_city', 'Не указан')}\n"
        
    if is_admin:
        text += f"\n👤 <b>Дилер:</b> {order_data.get('telegram_user_name')}\n"
        text += f"🆔 <b>ID:</b> <code>{order_data.get('user_id')}</code>\n"
        text += f"🏢 <b>Организация:</b> {order_data.get('company_name', 'Не указана')}\n"
        text += f"📞 <b>Тел:</b> {order_data.get('user_phone', 'Нет')}\n"
        text += f"🏷 Тип: {order_data.get('clientType')}\n"
    return text

# ==========================================
# 5. ЛОГИСТИКА
# ==========================================

class RouteManager:
    STATIC_ROUTES = {
        "Самарканд": ["Гулистан", "Джизак"],
        "Бухара": ["Гулистан", "Джизак", "Самарканд", "Навои"],
        "Карши": ["Гулистан", "Джизак", "Самарканд"],
        "Термез": ["Гулистан", "Джизак", "Самарканд", "Карши"],
        "Ургенч": ["Самарканд", "Бухара"],
        "Нукус": ["Самарканд", "Бухара", "Ургенч"],
        "Фергана": ["Ангрен", "Коканд"],
        "Андижан": ["Ангрен", "Коканд", "Фергана"]
    }
    @staticmethod
    async def get_transit_cities(destination_city):
        try:
            route_doc = await routes_collection.find_one({"name": {"$regex": destination_city}})
            if route_doc:
                return [p['city'] for p in route_doc['points'] if p['city'] != destination_city]
        except: pass
        return RouteManager.STATIC_ROUTES.get(destination_city, [])

class ConsolidationManager:
    @staticmethod
    async def process_order_logistics(order_data, city):
        method = order_data.get('shipping_method', 'standard')
        if method == 'express':
            return "🚕 <b>Тип: ЭКСПРЕСС</b> (Без консолидации)"

        flight = await consolidations_collection.find_one({"destination": city, "status": "open"})
        order_sum = order_data['total_final']
        
        current_total = order_sum
        if flight: current_total += flight['total_sum']
        coverage_pct = int((current_total / TRUCK_COST) * 100)

        if not flight:
            new_flight_data = {
                "destination": city, "status": "open", "created_at": datetime.now(),
                "total_sum": order_sum, "orders_count": 1, "order_ids": [order_data['_id']]
            }
            await consolidations_collection.insert_one(new_flight_data)
            asyncio.create_task(ConsolidationManager.notify_neighbors(city, order_data['user_id']))
            asyncio.create_task(ConsolidationManager.schedule_transit_notification(city, order_data['user_id']))
            return f"🚛 <b>Новый рейс:</b> Набор на <b>{city}</b>.\n💰 Покрытие: <b>{coverage_pct}%</b>."
        else:
            await consolidations_collection.update_one(
                {"_id": flight['_id']},
                {"$inc": {"total_sum": order_sum, "orders_count": 1}, "$push": {"order_ids": order_data['_id']}}
            )
            return f"🚛 <b>Рейс {city}:</b> Добавлен заказ.\n💰 Покрытие: <b>{coverage_pct}%</b>."

    @staticmethod
    async def notify_neighbors(city, exclude_user_id):
        query = {"city": city, "_id": {"$ne": exclude_user_id}, "client_type": {"$ne": "new"}}
        users = await users_collection.find(query).to_list(length=100)
        for user in users:
            try:
                await send_and_track(user['_id'], f"🏙 <b>Ваш город активизировался!</b>\nМашина в <b>{city}</b>.\n📦 Добавьте груз!", parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.3)
            except: pass

    @staticmethod
    async def schedule_transit_notification(destination_city, exclude_user_id):
        await asyncio.sleep(TRANSIT_DELAY_SEC)
        flight = await consolidations_collection.find_one({"destination": destination_city, "status": "open"})
        if not flight: return
        transit_cities = await RouteManager.get_transit_cities(destination_city)
        if not transit_cities: return
        query = {"city": {"$in": transit_cities}, "_id": {"$ne": exclude_user_id}, "client_type": {"$ne": "new"}}
        users = await users_collection.find(query).to_list(length=200)
        for user in users:
            try:
                await send_and_track(user['_id'], f"🔔 <b>Транзит!</b>\nМашина в <b>{destination_city}</b> едет через вас!", parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.3)
            except: pass

# ==========================================
# 6. КЛАВИАТУРЫ И СОСТОЯНИЯ
# ==========================================

class RegState(StatesGroup):
    waiting_for_company_name = State()
    waiting_for_city = State()

def get_auth_kb(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Войти по номеру телефона", request_contact=True)]], resize_keyboard=True)
def get_cities_kb():
    builder = []; row = []
    for city in CITIES:
        row.append(KeyboardButton(text=city))
        if len(row) == 2: builder.append(row); row = []
    if row: builder.append(row)
    return ReplyKeyboardMarkup(keyboard=builder, resize_keyboard=True)
def get_pending_kb(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Проверить статус")], [KeyboardButton(text="ℹ️ О нас"), KeyboardButton(text="📞 Поддержка")]], resize_keyboard=True)
def get_active_kb(user_id, client_type):
    shop_url = f"{WEBAPP_URL}?userId={user_id}&clientType={client_type}"
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=shop_url))], [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О нас")]], resize_keyboard=True)
def get_admin_order_kb(order_oid):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ord_ok_{order_oid}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ord_no_{order_oid}")], [InlineKeyboardButton(text="🚚 Отгружено / В пути", callback_data=f"ord_go_{order_oid}")]])

# ==========================================
# 7. ХЭНДЛЕРЫ
# ==========================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = await users_collection.find_one({"_id": message.from_user.id})
    if not user or not user.get('phone'):
        await message.answer("👋 Добро пожаловать! Подтвердите номер телефона.", reply_markup=get_auth_kb())
        return
    client_type = user.get('client_type', 'new')
    kb = get_pending_kb() if client_type == 'new' else get_active_kb(message.from_user.id, client_type)
    txt = "⏳ На проверке" if client_type == 'new' else f"✅ Доступ открыт! Статус: {client_type}"
    # Используем трекер для приветствия
    await send_and_track(message.from_user.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)

# --- РЕГИСТРАЦИЯ ---
@dp.message(F.contact)
async def process_contact(message: types.Message, state: FSMContext):
    # Логирование контакта делает Middleware, здесь только бизнес-логика
    await state.update_data(phone=message.contact.phone_number, full_name=message.from_user.full_name, username=message.from_user.username)
    await message.answer("🏢 <b>Введите название вашей организации (или магазина):</b>", parse_mode=ParseMode.HTML, reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(RegState.waiting_for_company_name)

@dp.message(RegState.waiting_for_company_name)
async def process_company_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, введите название текстом.")
        return
    await state.update_data(company_name=message.text) 
    await message.answer("📍 Теперь выберите ваш город:", reply_markup=get_cities_kb())
    await state.set_state(RegState.waiting_for_city)

@dp.message(RegState.waiting_for_city, F.text.in_(CITIES))
async def process_city(message: types.Message, state: FSMContext):
    data = await state.get_data()
    city = message.text
    company = data.get('company_name', 'Не указано')
    await upsert_user(message.from_user.id, data['username'], data['full_name'], data['phone'], city, company)
    await state.clear()
    await notify_admin(f"👤 <b>Новый дилер!</b>\n🏢 Фирма: <b>{company}</b>\nИмя: {data['full_name']}\nГород: <b>{city}</b>\nТел: {data['phone']}")
    await send_and_track(message.from_user.id, f"✅ Город <b>{city}</b> сохранен!", reply_markup=get_pending_kb(), parse_mode=ParseMode.HTML)

# --- ИСТОРИЯ И УДАЛЕНИЕ СООБЩЕНИЙ ---
@dp.message(Command("history"))
async def cmd_history_link(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("⚠️ Использование: `/history ID_ПОЛЬЗОВАТЕЛЯ`")
        return
    target_id = args[1]
    link = f"http://localhost:8080/history?user_id={target_id}"
    
    # Отправляем ссылку как код
    text = (
        f"📂 <b>Диалог с {target_id}</b>\n\n"
        f"Скопируйте ссылку и вставьте в браузер:\n"
        f"<code>{link}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

# --- ЗАКАЗЫ ---
@dp.message(F.text == "📦 Мои заказы")
async def my_orders_handler(message: types.Message):
    cursor = orders_collection.find({"user_id": message.from_user.id}).sort("created_at", -1).limit(5)
    orders = await cursor.to_list(length=5)
    if not orders: return await message.answer("📂 У вас пока нет заказов.")
    text = "📋 <b>История:</b>\n\n"
    for order in orders:
        text += f"🔹 <b>#{str(order['_id'])[-6:]}</b> ({order['created_at'].strftime('%d.%m')}) - {STATUS_MAP.get(order.get('status'), '?')}\nSum: {format_currency(order['total_final'])}\n\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

@dp.message(F.content_type == ContentType.WEB_APP_DATA)
async def process_order(message: types.Message):
    # Логирование WebApp данных делает Middleware
    try:
        raw_data = json.loads(message.web_app_data.data)
        
        # 1. Валидация и структурирование данных через дата-класс
        order_items = [OrderItem(**item) for item in raw_data.pop('items', [])]
        order_model = WebAppOrderData(items=order_items, **raw_data)

        # 2. Конвертация в словарь для MongoDB и обогащение
        db_data = asdict(order_model)
        
        user_id = message.from_user.id
        user_doc = await users_collection.find_one({"_id": user_id})
        
        db_data.update({
            'created_at': datetime.now(),
            'user_id': user_id,
            'telegram_username': message.from_user.username,
            'telegram_user_name': message.from_user.full_name,
            'user_city': user_doc.get('city', 'Unknown'), 
            'user_phone': user_doc.get('phone'),
            'company_name': user_doc.get('company_name', 'Не указана'),
            'status': 'new'
        })
        
        # 3. Сохранение в БД
        res = await orders_collection.insert_one(db_data)
        db_data['_id'] = res.inserted_id
        
        # 4. Пост-обработка (статистика, уведомления)
        asyncio.create_task(update_product_stats(db_data['items'], db_data['created_at']))

        await send_and_track(user_id, generate_order_receipt(db_data, False) + "\n⏳ <i>Передано на проверку.</i>", parse_mode=ParseMode.HTML)
        
        admin_receipt = generate_order_receipt(db_data, True)
        logistics_msg = await ConsolidationManager.process_order_logistics(db_data, user_doc.get('city'))
        if logistics_msg: admin_receipt += f"\n{logistics_msg}"
        await bot.send_message(ADMIN_ID, admin_receipt, parse_mode=ParseMode.HTML, reply_markup=get_admin_order_kb(str(res.inserted_id)))

    except Exception as e:
        logging.error(f"Order processing error: {e}", exc_info=True)
        await message.answer("❌ Ошибка при оформлении заказа. Сообщите администратору.")

@dp.callback_query(F.data.startswith("ord_"))
async def admin_order_action(callback: CallbackQuery):
    try:
        _, action, oid = callback.data.split("_")
        
        # Логируем действие админа
        action_name = {"ok": "✅ Подтвердить", "no": "❌ Отклонить", "go": "🚚 В пути"}.get(action, action)
        await log_chat_event(ADMIN_ID, "user", f"🔘 [ADMIN CLICK] Нажал кнопку: {action_name}")

        order = await orders_collection.find_one({"_id": ObjectId(oid)})
        if not order: return await callback.answer("Заказ не найден", show_alert=True)
        
        new_status = {"ok": "approved", "no": "rejected", "go": "shipped"}.get(action)
        btn_text = {"ok": "✅ Подтверждено", "no": "❌ Отклонено", "go": "🚚 В пути"}.get(action)
        
        await orders_collection.update_one({"_id": ObjectId(oid)}, {"$set": {"status": new_status}})
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Статус заказа #{str(oid)[-6:]}: {btn_text}")
        
        msg = {"approved": "✅ Заказ подтвержден!", "rejected": "❌ Заказ отклонен.", "shipped": "🚚 Заказ отгружен."}.get(new_status)
        if msg and order.get('user_id'): 
            await send_and_track(order['user_id'], msg)
    except: await callback.answer("Ошибка обработки", show_alert=True)

@dp.message(F.text == "🔄 Проверить статус")
async def check_status_btn(message: types.Message):
    user = await users_collection.find_one({"_id": message.from_user.id})
    if user and user.get('client_type', 'new') != 'new':
        await message.answer("🎉 Аккаунт подтвержден!", reply_markup=get_active_kb(message.from_user.id, user['client_type']))
    else: await message.answer("⏳ Все еще на проверке.", reply_markup=get_pending_kb())

@dp.message(F.text == "ℹ️ О нас")
async def about_handler(message: types.Message):
    await message.answer("🏢 <b>Deskform Dealer Bot</b>\nОптовые поставки Deli.", parse_mode=ParseMode.HTML)

# ==========================================
# 8. ЗАПУСК
# ==========================================

async def start_db_watcher():
    logging.info("👀 User Watcher Started...")
    try:
        async with users_collection.watch([{"$match": {"operationType": "update"}}]) as stream:
            async for change in stream:
                fields = change.get('updateDescription', {}).get('updatedFields', {})
                if 'client_type' in fields:
                    uid = change['documentKey']['_id']; ntype = fields['client_type']
                    try: await send_and_track(uid, f"🎉 Ваш статус: {ntype}", reply_markup=get_active_kb(uid, ntype))
                    except: pass
    except Exception as e:
        logging.error(f"User Watcher Error: {e}")

async def start_orders_watcher():
    """
    Следит за коллекцией заказов и отправляет уведомления при создании нового.
    """
    logging.info("📦 Order Watcher Started...")
    try:
        pipeline = [{"$match": {"operationType": "insert"}}]
        async with orders_collection.watch(pipeline) as stream:
            async for change in stream:
                order_doc = change['fullDocument']
                user_id = order_doc.get('user_id')
                user_city = order_doc.get('user_city', 'Unknown')
                
                if not user_id: continue

                # --- Логика, перенесенная из старого хендлера ---
                
                # 1. Отправляем чек пользователю
                user_receipt = generate_order_receipt(order_doc, is_admin=False)
                await send_and_track(user_id, user_receipt + "\n⏳ <i>Передано на проверку.</i>", parse_mode=ParseMode.HTML)
                
                # 2. Отправляем расширенный чек и инфо по логистике админу
                admin_receipt = generate_order_receipt(order_doc, is_admin=True)
                logistics_msg = await ConsolidationManager.process_order_logistics(order_doc, user_city)
                if logistics_msg:
                    admin_receipt += f"\n\n{logistics_msg}"
                
                await bot.send_message(
                    ADMIN_ID, 
                    admin_receipt, 
                    parse_mode=ParseMode.HTML, 
                    reply_markup=get_admin_order_kb(str(order_doc['_id']))
                )
                
                # 3. Обновляем статистику по товарам
                asyncio.create_task(update_product_stats(order_doc.get('items', []), order_doc.get('created_at')))

    except Exception as e:
        logging.error(f"Order Watcher Error: {e}", exc_info=True)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await users_collection.create_index("city")
    await consolidations_collection.create_index("destination")
    
    if start_admin_server:
        await start_admin_server(bot, db)
    
    # Запускаем оба наблюдателя как фоновые задачи
    asyncio.create_task(start_db_watcher())
    asyncio.create_task(start_orders_watcher())

    logging.info("🚀 Bot started successfully.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Bot stopped.")