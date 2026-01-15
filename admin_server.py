from aiohttp import web
import logging
from datetime import datetime
import json

# Глобальные переменные, которые мы получим из main.py
bot_instance = None
db_instance = None

async def handle_history(request):
    """Отображает HTML страницу с историей"""
    user_id = request.query.get('user_id')
    if not user_id:
        return web.Response(text="No user_id provided", status=400)

    try:
        uid = int(user_id)
        # Получаем историю из базы
        # ВАЖНО: коллекция должна называться так же, как в main.py (chat_history)
        cursor = db_instance['chat_history'].find({"user_id": uid}).sort("timestamp", 1)
        messages = await cursor.to_list(length=500)
        
        # Получаем имя юзера
        user_doc = await db_instance['users'].find_one({"_id": uid})
        user_name = user_doc.get('full_name', 'Unknown') if user_doc else str(uid)

        # Генерируем HTML
        html = generate_html(uid, user_name, messages)
        return web.Response(text=html, content_type='text/html')
    except Exception as e:
        logging.error(f"Web History Error: {e}")
        return web.Response(text=f"Error: {e}", status=500)

async def handle_delete(request):
    """API для удаление сообщения по клику"""
    try:
        data = await request.json()
        message_id = int(data.get('message_id'))
        chat_id = int(data.get('chat_id'))
        mongo_id = data.get('mongo_id') # ID записи в базе

        # 1. Удаляем из Telegram (если это сообщение бота)
        # Сообщения пользователя удалить бот не может (ограничение API), только свои.
        try:
            await bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logging.warning(f"Telegram delete failed (maybe too old): {e}")

        # 2. Удаляем из MongoDB (всегда)
        from bson.objectid import ObjectId
        await db_instance['chat_history'].delete_one({"_id": ObjectId(mongo_id)})

        return web.json_response({"status": "ok", "deleted_id": message_id})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

def generate_html(user_id, user_name, messages):
    items_html = ""
    for msg in messages:
        sender_cls = "user" if msg['sender'] == 'user' else "bot"
        time_str = msg['timestamp'].strftime('%H:%M %d.%m')
        text = msg.get('text', '').replace("\n", "<br>")
        msg_id = msg.get('message_id')
        mongo_id = str(msg['_id'])
        
        # Кнопка удаления (появляется только у сообщений бота, или просто удаляет из истории)
        del_btn = f"""
        <button onclick="deleteMsg({msg_id}, '{mongo_id}', this)" class="del-btn" title="Удалить">🗑</button>
        """
        
        items_html += f"""
        <div class="msg-row {sender_cls}">
            {del_btn if msg['sender'] == 'bot' else ''}
            <div class="msg-bubble">
                <div class="text">{text}</div>
                <div class="meta">{time_str}</div>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Чат: {user_name}</title>
        <style>
            body {{ background: #e5ddd5; font-family: sans-serif; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #efe7dd; min-height: 90vh; padding: 20px; border-radius: 10px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }}
            .header {{ position: sticky; top: 0; background: #008069; color: white; padding: 15px; margin: -20px -20px 20px -20px; border-radius: 10px 10px 0 0; display: flex; justify-content: space-between; align-items: center; z-index: 100; }}
            .msg-row {{ display: flex; margin-bottom: 10px; align-items: flex-end; }}
            .msg-row.user {{ justify-content: flex-end; }}
            .msg-row.bot {{ justify-content: flex-start; }}
            
            .msg-bubble {{ max-width: 70%; padding: 8px 12px; border-radius: 7px; position: relative; font-size: 14px; line-height: 1.4; box-shadow: 0 1px 1px rgba(0,0,0,0.1); }}
            .user .msg-bubble {{ background: #dcf8c6; border-radius: 7px 0 7px 7px; }}
            .bot .msg-bubble {{ background: #fff; border-radius: 0 7px 7px 7px; }}
            
            .meta {{ font-size: 10px; color: #999; text-align: right; margin-top: 4px; }}
            
            .del-btn {{ background: none; border: none; cursor: pointer; font-size: 16px; opacity: 0.5; margin: 0 8px; transition: 0.2s; }}
            .del-btn:hover {{ opacity: 1; color: red; transform: scale(1.1); }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>👤 <b>{user_name}</b> <small>({user_id})</small></div>
                <a href="#" onclick="location.reload()" style="color:white; text-decoration:none">🔄</a>
            </div>
            <div id="chat-box">
                {items_html}
            </div>
        </div>

        <script>
        async function deleteMsg(msgId, mongoId, btnElement) {{
            if (!confirm('Удалить это сообщение?')) return;
            
            try {{
                let response = await fetch('/delete_msg', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ 
                        message_id: msgId, 
                        mongo_id: mongoId,
                        chat_id: {user_id} 
                    }})
                }});
                
                let res = await response.json();
                if (res.status === 'ok') {{
                    // Удаляем визуально
                    btnElement.closest('.msg-row').remove();
                }} else {{
                    alert('Ошибка: ' + res.message);
                }}
            }} catch (e) {{
                alert('Ошибка сети: ' + e);
            }}
        }}
        </script>
    </body>
    </html>
    """

async def start_admin_server(bot, db):
    """Запуск сервера из main.py"""
    global bot_instance, db_instance
    bot_instance = bot
    db_instance = db
    
    app = web.Application()
    app.router.add_get('/history', handle_history)
    app.router.add_post('/delete_msg', handle_delete)
    
    runner = web.AppRunner(app)
    await runner.setup()
    # Запускаем на порту 8080
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logging.info("🌍 Admin Web Interface started at http://localhost:8080")