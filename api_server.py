# Deskform App v6.20 API Server
import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from dataclasses import asdict

# --- Импорт бизнес-логики ---
from business_logic import WebAppOrderData, OrderItem, PricingEngine

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================

# --- Базовая конфигурация логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Загружаем переменные окружения (предполагается, что .env файл существует)
from dotenv import load_dotenv
load_dotenv() 

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = "deskform_db"

# --- Инициализация Flask и CORS ---
# Указываем static_folder, чтобы Flask знал, где искать файлы типа img, css и т.д.
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app) # Разрешаем кросс-доменные запросы для фронтенда

# --- Инициализация Базы Данных ---
try:
    if not MONGO_URL:
        raise ValueError("Переменная окружения MONGO_URL не установлена.")
    cluster = MongoClient(MONGO_URL)
    db = cluster[DB_NAME]
    users_collection = db['users']
    orders_collection = db['orders']
    products_collection = db['products']
    logging.info("✅ База данных MongoDB подключена.")
except Exception as e:
    logging.error(f"❌ Не удалось подключиться к MongoDB: {e}")
    db = None

# ==========================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def format_currency(value):
    try:
        return "{:,.0f}".format(float(str(value).replace(' ', ''))).replace(",", " ")
    except:
        return str(value)

# ==========================================
# 3. ЭНДПОИНТЫ API
# ==========================================

@app.route('/')
def serve_index():
    """Отдает главный файл фронтенда."""
    return send_from_directory('.', 'index.html')

@app.route('/status')
def status():
    return "Deskform API Server is running.", 200

@app.route('/calculate', methods=['POST'])
def calculate_price():
    """
    Эндпоинт для динамического расчета скидок.
    Принимает { base_sum, client_type, is_prepay, is_promo }
    """
    data = request.json
    base_sum = data.get('base_sum', 0)
    client_type = data.get('client_type', 'new_partner')
    is_prepay = data.get('is_prepay', False)
    is_promo = data.get('is_promo', False)
    
    if not PricingEngine:
        return jsonify({"error": "Pricing Engine не инициализирован"}), 500

    # Используем движок из business_logic.py
    result = PricingEngine.calculate_order(base_sum, client_type, is_prepay, is_promo)
    
    return jsonify(result)


@app.route('/submit_order', methods=['POST'])
def submit_order():
    """
    Основной эндпоинт для сохранения заказа в базу данных.
    """
    if db is None:
        return jsonify({"error": "Подключение к базе данных отсутствует"}), 500
        
    raw_data = request.json
    logging.info(f"Получены сырые данные для заказа: {raw_data}")
    
    try:
        # 1. Валидация и структурирование данных через дата-класс
        order_items = [OrderItem(**item) for item in raw_data.pop('items', [])]
        
        # ВАЖНО: Удаляем ключ, который приходит от фронтенда, но отсутствует в модели
        raw_data.pop('recipient_info', None)
        
        order_model = WebAppOrderData(items=order_items, **raw_data)

        # 2. Конвертация в словарь для MongoDB и обогащение
        db_data = asdict(order_model)
        
        user_id = int(order_model.userId) # Безопасное приведение к int
        user_doc = users_collection.find_one({"_id": user_id}) or {}
        
        db_data.update({
            'created_at': datetime.now(),
            'status': 'new', # Новый заказ всегда в статусе "new"
            # Добавляем денормализованные данные о пользователе для удобства
            'user_id': user_id,
            'user_city': user_doc.get('city', 'Unknown'), 
            'user_phone': user_doc.get('phone'),
            'company_name': user_doc.get('company_name', 'Не указана'),
            # ВАЖНО: Telegram информацию мы здесь получить не можем, она останется в боте
            'telegram_username': user_doc.get('username', 'N/A'),
            'telegram_user_name': user_doc.get('full_name', 'N/A'),
        })
        
        # 3. Сохранение в БД
        res = orders_collection.insert_one(db_data)
        
        # 4. Пост-обработка (только обновление статистики, без уведомлений)
        # Уведомления остаются прерогативой Telegram-бота
        # update_product_stats(db_data['items'], db_data['created_at'])
        
        return jsonify({
            "status": "success",
            "order_id": str(res.inserted_id),
            "message": "Заказ успешно создан"
        }), 201

    except TypeError as e:
        logging.error(f"TypeError при обработке заказа: {e}", exc_info=True)
        return jsonify({"error": "Ошибка в структуре данных", "details": str(e)}), 400
    except Exception as e:
        logging.error(f"Exception при обработке заказа: {e}", exc_info=True)
        return jsonify({"error": "Внутренняя ошибка сервера", "details": str(e)}), 500


# ==========================================
# 4. ЗАПУСК СЕРВЕРА
# ==========================================
if __name__ == '__main__':
    # Используйте host='0.0.0.0' чтобы сделать сервер доступным в локальной сети
    app.run(host='0.0.0.0', port=5001, debug=True)
