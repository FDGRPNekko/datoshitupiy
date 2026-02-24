import logging
import threading
import asyncio
import signal
import re
# ... другие импорты (asyncio, logging и т.д.) ...
try:
    from shop_bot.support_bot_controller import SupportBotController  # Как в app.py
    support_controller = SupportBotController()  # Глобальный инстанс
except ImportError as e:
    support_controller = None  # Заглушка, чтобы не крашить
try:
    # Helps show ANSI colors on Windows terminals and some TTY-less streams
    import colorama  # type: ignore
    colorama_available = True
except Exception:
    colorama_available = False

from shop_bot.data_manager import database

from shop_bot.webhook_server import app as app_module
support_controller = app_module._support_bot_controller

def main():
    if colorama_available:
        try:
            colorama.just_fix_windows_console()
        except Exception:
            pass
    # Colored, concise logging formatter
    class ColoredFormatter(logging.Formatter):
        COLORS = {
            'DEBUG': '\x1b[36m',    # Cyan
            'INFO': '\x1b[32m',     # Green
            'WARNING': '\x1b[33m',  # Yellow
            'ERROR': '\x1b[31m',    # Red
            'CRITICAL': '\x1b[41m', # Red background
        }
        RESET = '\x1b[0m'

        def format(self, record: logging.LogRecord) -> str:
            level = record.levelname
            color = self.COLORS.get(level, '')
            reset = self.RESET if color else ''
            # Compact example: [12:34:56] [INFO] Message
            fmt = f"%(asctime)s [%(levelname)s] %(message)s"
            # Time only
            datefmt = "%H:%M:%S"
            base = logging.Formatter(fmt=fmt, datefmt=datefmt)
            msg = base.format(record)
            if color:
                # Color only the [LEVEL] part
                msg = msg.replace(f"[{level}]", f"{color}[{level}]{reset}")
            return msg

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clean existing handlers to avoid duplicate logs
    for h in list(root.handlers):
        root.removeHandler(h)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(ColoredFormatter())
    root.addHandler(ch)

    # Suppress noisy third-party loggers
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    # Вернём aiogram.event на INFO, но переведём сообщения фильтром ниже
    aio_event_logger = logging.getLogger('aiogram.event')
    aio_event_logger.setLevel(logging.INFO)
    logging.getLogger('aiogram.dispatcher').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    logging.getLogger('paramiko').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    class RussianizeAiogramFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
                if 'Update id=' in msg:
                    # Пример исходной строки:
                    # "Update id=236398370 is handled. Duration 877 ms by bot id=8241346998"
                    m = re.search(r"Update id=(\d+)\s+is\s+(not handled|handled)\.\s+Duration\s+(\d+)\s+ms\s+by bot id=(\d+)", msg)
                    if m:
                        upd_id, state, dur_ms, bot_id = m.groups()
                        state_ru = 'не обработано' if state == 'not handled' else 'обработано'
                        msg = f"Обновление {upd_id} {state_ru} за {dur_ms} мс (бот {bot_id})"
                        record.msg = msg
                        record.args = ()
                    else:
                        # Фолбэк: минимальная русификация
                        msg = msg.replace('Update id=', 'Обновление ')
                        msg = msg.replace(' is handled.', ' обработано.')
                        msg = msg.replace(' is not handled.', ' не обработано.')
                        msg = msg.replace('Duration', 'за')
                        msg = msg.replace('by bot id=', '(бот ')
                        if msg.endswith(')') is False and 'бот ' in msg:
                            msg = msg + ')'
                        record.msg = msg
                        record.args = ()
            except Exception:
                pass
            return True

    # Навешиваем фильтр только на aiogram.event
    aio_event_logger.addFilter(RussianizeAiogramFilter())
    logger = logging.getLogger(__name__)

    # ВАЖНО: сначала инициализируем базу данных, чтобы таблицы (включая bot_settings) были созданы
    database.initialize_db()
    logger.info("Проверка инициализации базы данных завершена.")

    # Импортируем модули, которые косвенно тянут handlers.py, только после инициализации БД
    from shop_bot.bot_controller import BotController
    from shop_bot.webhook_server.app import create_webhook_app
    from shop_bot.data_manager.scheduler import periodic_subscription_check

    bot_controller = BotController()
    flask_app = create_webhook_app(bot_controller)
    
    async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
        logger.info(f"Получен сигнал: {sig.name}. Запускаю завершение работы...")
        if bot_controller.get_status()["is_running"]:
            bot_controller.stop()
            await asyncio.sleep(2)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    async def start_services():
        loop = asyncio.get_running_loop()

        flask_app.config['EVENT_LOOP'] = loop
        bot_controller.set_loop(loop)

        # теперь работаем с общим support_controller из app.py
        support_controller.set_loop(loop)
        
        flask_thread = threading.Thread(
            target=lambda: flask_app.run(host='0.0.0.0', port=1488, use_reloader=False, debug=False),
            daemon=True
        )
        flask_thread.start()
        
        logger.info("Flask-сервер запущен: http://0.0.0.0:1488")
            
        #logger.info("Приложение запущено. Бота можно стартовать из веб-панели.")

        # НОВОЕ: Автоматический запуск бота, если настройки готовы
        # Удалить до === если не нужен автозапуск бота
        start_result = bot_controller.start()
        if start_result["status"] == "success":
            logger.info("Бот запущен автоматически.")
        else:
            logger.warning(f"Не удалось запустить бота автоматически: {start_result['message']}. Запустите вручную через веб-панель.")
    
        logger.info("Приложение запущено. Фоновые задачи активны.")

        # НОВОЕ: Автозапуск support-бота
        loop = asyncio.get_running_loop()
        flask_app.config['EVENT_LOOP'] = loop  # Для app.py (run_coroutine_threadsafe)
    
        # ... запуск основного бота: bot_start_result = await bot_controller.start() ...
    
    # FIX: Запуск support-бота (аналогично app.py)
        support_start_result = {'status': 'skipped', 'message': 'Support не настроен'}
        if support_controller:
            try:
                support_controller.set_loop(loop)  # Передаём loop, как в app.py
                support_start_result = support_controller.start()  # Sync-вызов, но async внутри
                logger.info(f"Support-бот запущен: {support_start_result}")
            
                # Опционально: жди готовности (как _wait_for_stop, но для start)
                await asyncio.sleep(1)  # Короткая пауза, чтобы polling стартанул
            
                if support_start_result.get('status') != 'success':
                    logger.warning(f"Support-бот не запустился: {support_start_result}")
            except Exception as e:
                logger.error(f"Ошибка запуска support-бота: {e}", exc_info=True)
                support_start_result = {'status': 'error', 'message': str(e)}
        else:
            logger.info("Support-бот пропущен (модуль не найден)")
    
        logger.info("Приложение запущено. Фоновые задачи активны.")
        # ===
        
        asyncio.create_task(periodic_subscription_check(bot_controller))

        # Бесконечное ожидание в мягком цикле сна, чтобы корректно ловить отмену без трейсбека
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Нормальное завершение: заглушаем исключение отмены
            logger.info("Главная задача отменена, выполняю корректное завершение...")
            return

    try:
        asyncio.run(start_services())
    except asyncio.CancelledError:
        # Может всплыть при остановке цикла — игнорируем как штатное поведение
        logger.info("Получен сигнал остановки, сервисы остановлены.")
    finally:
        logger.info("Приложение завершается.")

if __name__ == "__main__":
    main()
