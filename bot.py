def bot_engine():
    last_update_id = 0
    print("Swing AI Engine Online...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            updates = json.loads(urllib.request.urlopen(req, timeout=35).read().decode()).get("result", [])
            for u in updates:
                last_update_id = u["update_id"] + 1
                chat_id = u.get("message", {}).get("chat", {}).get("id") or u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                data = u.get("callback_query", {}).get("data")
                
                if chat_id:
                    active_chats.add(chat_id)
                    
                    if data == "SHOW_ASSETS":
                        assets_list = "\n".join([f"🔹 `{s.replace('USDT', '')}`" for s in SYMBOLS])
                        msg = (
                            "🪙 **МОНИТОРИНГ АКТИВОВ (10)**\n\n"
                            f"Алгоритм непрерывно анализирует:\n{assets_list}"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "BOT_STATUS":
                        uptime_sec = int(time.time() - start_time)
                        hours = uptime_sec // 3600
                        minutes = (uptime_sec % 3600) // 60
                        msg = (
                            "🟢 **СИСТЕМНЫЙ СТАТУС**\n\n"
                            f"▫️ **Ядро:** Активно (24/7)\n"
                            f"▫️ **Аптайм:** {hours}ч {minutes}м\n"
                            f"▫️ **Активных чатов:** {len(active_chats)}\n"
                            f"▫️ **Модель:** Swing AI (4H) + Dual TP\n\n"
                            f"✅ *Служба авто-трекинга сделок работает в фоновом режиме.*"
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_STRATEGY":
                        msg = (
                            "🧠 **ТОРГОВАЯ МОДЕЛЬ: SWING AI**\n\n"
                            "**Тип:** 4H Институциональное Машинное Обучение\n"
                            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            "⚙️ **Логика ИИ (k-NN):**\n"
                            "Бот создает цифровой слепок текущей волатильности и объема. Затем он находит 5 точных совпадений в истории за последние 1000 часов и предсказывает движение на основе прошлого.\n\n"
                            "🛡 **Управление риском (Dual TP):**\n"
                            "• **TP1 (Сейф):** 1.0 ATR. Быстрый сбор ликвидности. После взятия бот просигналит перевести сделку в безубыток.\n"
                            "• **TP2 (Макс):** Рассчитывается ИИ на основе силы прошлых исторических движений.\n\n"
                            "🧭 **Макро-фильтр:**\n"
                            "Бот сверяется с дневным (1D) графиком Биткоина и блокирует сделки против глобального тренда."
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    elif data == "SHOW_HELP":
                        msg = (
                            "ℹ️ **СПРАВКА ПО ТЕРМИНАЛУ**\n\n"
                            "💡 **Советы по использованию:**\n"
                            "1. Все цифры в сигналах (цены входа, стопы, тейки) **кликабельны**. Нажми на цифру, и она скопируется в буфер обмена.\n"
                            "2. Тебе не нужно следить за графиком. Бот сам отредактирует сообщение с сигналом, как только цена возьмет TP1, TP2 или выбьет стоп-лосс.\n"
                            "3. Строго соблюдай риск, который рекомендует ИИ (от 0.5% до 2%)."
                        )
                        send_msg(chat_id, msg, get_main_keyboard())
                        
                    else:
                        welcome_text = (
                            "🚀 **SWING AI ТЕРМИНАЛ АКТИВЕН**\n"
                            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
                            "Система инициализирована. ИИ сканирует 4H график и макро-данные...\n\n"
                            "Ожидайте сигналов. Выберите действие в меню ниже 👇"
                        )
                        send_msg(chat_id, welcome_text, get_main_keyboard())
            time.sleep(1)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(10)
