"""
DESKFORM BUSINESS LOGIC SUMMARY
Этот файл описывает логику работы Frontend (JS) на языке Python.
Используется как контекст для AI-агента, чтобы понимать структуру данных и расчеты.
"""

from dataclasses import dataclass
from typing import List, Dict, Optional

# ==========================================
# 1. ПРОФИЛИ СКИДОК (Копия из deskform-core.js)
# ==========================================
DISCOUNT_PROFILES = {
    'new_partner': {
        'name': "Новый партнер",
        'tiers': [5, 10, 25, 50, 100, 150],  # Млн сум
        'percents': [2, 3, 5, 7, 9, 11]     # Скидка %
    },
    'active': {
        'name': "Действующий партнер",
        'tiers': [0],
        'percents': [0] # Индивидуальные условия
    },
    'market_urikzar': {
        'name': "Рынок Урикзар",
        'tiers': [0],
        'percents': [0]
    }
}

# ==========================================
# 2. МОДЕЛИ ДАННЫХ (Что приходит от WebApp)
# ==========================================

@dataclass
class OrderItem:
    product_id: str      # Артикул (он же ID)
    name: str            # Наименование
    qty: int             # Количество (шт)
    price_per_unit: float # Цена товара (Оптовая/Дилерская)
    total: float         # price_per_unit * qty

@dataclass
class WebAppOrderData:
    userId: int
    clientType: str       # Ключ из DISCOUNT_PROFILES (например 'new_partner')
    items: List[OrderItem]
    total_base: float     # Сумма ДО всех скидок
    total_final: float    # Итоговая сумма К ОПЛАТЕ
    discount_tier_pct: int # Процент скидки за объем
    discount_pay_pct: int  # Процент скидки за оплату (Promo/Prepay)
    is_promo: bool
    is_prepay: bool
    delivery_type: str    # 'standard'
    shipping_method: str  # 'standard' (Фура) или 'express' (Пятак)

# ==========================================
# 3. ЛОГИКА РАСЧЕТА (Алгоритм Frontend)
# ==========================================

class PricingEngine:
    """
    Симуляция расчетов deskform-core.js.
    Можно использовать в main.py для проверки честности цены.
    """
    
    @staticmethod
    def calculate_order(base_sum: float, client_type: str, is_prepay: bool, is_promo: bool) -> dict:
        profile = DISCOUNT_PROFILES.get(client_type, DISCOUNT_PROFILES['new_partner'])
        tiers = profile['tiers']
        percents = profile['percents']
        
        # 1. Скидка за объем (Tier Discount)
        tier_pct = 0
        millions = base_sum / 1_000_000
        
        if not (len(tiers) == 1 and tiers[0] == 0):
            for i, limit in enumerate(tiers):
                if millions >= limit:
                    tier_pct = percents[i]
                else:
                    break
        
        sum_after_tier = base_sum - (base_sum * (tier_pct / 100))
        
        # 2. Скидка за условия оплаты (Payment Discount)
        pay_pct = 0
        if is_promo:
            pay_pct += 4
        if is_prepay:
            pay_pct += 2
            
        final_sum = sum_after_tier - (sum_after_tier * (pay_pct / 100))
        
        return {
            "base_sum": base_sum,
            "tier_pct": tier_pct,
            "pay_pct": pay_pct,
            "final_sum": int(final_sum) # JS округляет через Math.round
        }

# ==========================================
# 4. ИНСТРУКЦИЯ ПО EXCEL (Маппинг)
# ==========================================
"""
Парсинг price.xlsx в JS происходит динамическим поиском заголовков.
Ключевые слова для поиска колонок:
- ID/Артикул: 'артикул'
- Бренд: 'бренд', 'brand'
- Название: 'наименование', 'товар'
- Цена РРЦ: 'ташкент', 'розн'
- Цена Опт: 'дилер', 'партнер' (Базовая цена для расчетов!)
- Штрих-код: 'штрих', 'barcode' (Используется сканером)
- Упаковка: 'упак', 'кор'
"""