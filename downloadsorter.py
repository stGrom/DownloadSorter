#!/usr/bin/env python3
"""
DownloadSorter — автоматический органайзер папки загрузок.
- Фото: распознаёт текст → категория → переименование
- Видео: метаданные/дата → папка Видео, имя почти не меняется (только чистка)
- Аудио: как видео, но в папку Аудио
- PDF/Текст: извлекает содержимое → категория → осмысленное имя
- Полная приватность, всё работает локально.
"""

import os
import re
import shutil
import subprocess
import time
import sqlite3
from pathlib import Path
from datetime import datetime

import pytesseract
from PIL import Image
import pdf2image
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------- Настройки ----------
WATCH_FOLDER = str(Path.home() / "Downloads")
SORTED_ROOT = str(Path.home() / "Downloads" / "Sorted")
DB_PATH = "sorter_index.db"

# Расширения по типам
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.gif', '.webp'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.mpeg', '.mpg'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma'}

# Категории (как раньше)
CATEGORY_RULES = {
    "Финансы": ["счёт", "invoice", "квитанция", "чек", "оплата", "налог", "банк", "зарплата", "pay", "receipt"],
    "Билеты": ["билет", "ticket", "boarding", "посадочный", "рейс", "flight", "маршрут"],
    "Работа": ["резюме", "cv", "отчёт", "report", "договор", "contract", "презентация", "presentation"],
    "Фото": ["image", "фото", "скриншот", "screenshot", "photo", "img"],
    "Документы": ["паспорт", "passport", "справка", "certificate", "диплом", "удостоверение"],
}

# ---------- База данных ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS files
                 (original_name TEXT, new_name TEXT, path TEXT, content TEXT, date_added TEXT)''')
    conn.commit()
    return conn

# ---------- Безопасное имя файла ----------
def sanitize_filename(name):
    """
    Удаляет все символы, недопустимые в имени файла (Windows/Linux).
    Оставляет буквы, цифры, пробелы, _, -, .
    Заменяет пробелы на _.
    Если имя становится пустым – возвращает 'file'.
    """
    # Удаляем всё, кроме разрешённых символов (Unicode буквы/цифры, пробел, точка, дефис, подчёркивание)
    cleaned = re.sub(r'[^\w\s.\-]', '', name, flags=re.UNICODE)
    # Заменяем пробелы и их последовательности на одиночное подчёркивание
    cleaned = re.sub(r'\s+', '_', cleaned)
    # Точка в начале или конце в Windows нежелательна
    cleaned = cleaned.strip('.')
    return cleaned if cleaned else "file"

# ---------- Извлечение текста ----------
def extract_text_from_pdf(filepath):
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        if text.strip():
            return text
    except:
        pass
    # fallback: OCR через картинки
    try:
        images = pdf2image.convert_from_path(filepath)
        full_text = []
        for img in images:
            full_text.append(pytesseract.image_to_string(img, lang='rus+eng'))
        return "\n".join(full_text)
    except Exception as e:
        print(f"Не удалось прочитать PDF: {e}")
        return ""

def extract_text_from_image(filepath):
    try:
        img = Image.open(filepath)
        return pytesseract.image_to_string(img, lang='rus+eng')
    except Exception as e:
        print(f"Не удалось прочитать изображение: {e}")
        return ""

def extract_text(filepath):
    ext = Path(filepath).suffix.lower()
    if ext == '.pdf':
        return extract_text_from_pdf(filepath)
    elif ext in IMAGE_EXTENSIONS:
        return extract_text_from_image(filepath)
    elif ext in ('.txt', '.md', '.csv', '.log'):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except:
            return ""
    else:
        return ""

# ---------- Метаданные видео/аудио (через ffprobe) ----------
def get_media_creation_time(filepath):
    """
    Пытается извлечь дату создания медиафайла через ffprobe.
    Возвращает datetime или None.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format_tags=creation_time',
             '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, timeout=10
        )
        date_str = result.stdout.strip()
        if date_str:
            # Ищем первую дату в формате YYYY-MM-DD
            match = re.search(r'\d{4}-\d{2}-\d{2}', date_str)
            if match:
                return datetime.strptime(match.group(), '%Y-%m-%d')
    except Exception:
        pass
    return None

# ---------- Категоризация по тексту ----------
def categorize(text):
    text_lower = text.lower()
    for category, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw in text_lower:
                return category
    return "Разное"

def find_date_in_text(text):
    patterns = [
        r'(\d{2})[./-](\d{2})[./-](\d{4})',
        r'(\d{4})[./-](\d{2})[./-](\d{2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            if len(groups[0]) == 4:
                year, month, day = groups[0], groups[1], groups[2]
            else:
                day, month, year = groups
            try:
                return datetime(int(year), int(month), int(day)).date()
            except:
                pass
    return None

def extract_description(text, category):
    """Берёт первую строку текста для описания."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        desc = lines[0][:60]
        # Очищаем запрещённые символы
        return sanitize_filename(desc)
    return category

def rename_file(filepath, category, date_obj, text=None):
    """
    Генерирует новое имя файла.
    Для медиа (видео/аудио) имя почти не трогаем, только чистим.
    Для остального создаём осмысленное.
    """
    file = Path(filepath)
    original_stem = file.stem
    ext = file.suffix
    date_str = date_obj.strftime("%Y-%m-%d") if date_obj else "бд"

    # Очищаем оригинальное имя (на случай, если используем его)
    clean_stem = sanitize_filename(original_stem)

    # Если имя уже содержит дату и выглядит осмысленным, не меняем (для видео/аудио)
    if (ext.lower() in VIDEO_EXTENSIONS or ext.lower() in AUDIO_EXTENSIONS) and len(clean_stem) > 5:
        # Проверим, есть ли уже дата в имени (YYYY-MM-DD или DD.MM.YYYY и т.п.)
        if re.search(r'\d{4}[-.]\d{2}[-.]\d{2}', original_stem):
            # Оставляем очищенное оригинальное имя, но без дублирования "Видео_"
            return f"{clean_stem}{ext}"

    # Для остальных или если имя неинформативное – строим по формату
    if text and text.strip():
        desc = extract_description(text, category)
    else:
        # Для медиа без текста: используем очищенное оригинальное имя
        desc = clean_stem if clean_stem != "file" else category

    base = f"{category}_{desc}_{date_str}{ext}"
    # Обрезаем слишком длинное
    if len(base) > 100:
        base = f"{category}_{date_str}{ext}"

    return base

# ---------- Главный обработчик файла ----------
def sort_file(filepath, conn):
    print(f"Обрабатываю: {filepath}")
    if not os.path.isfile(filepath):
        return
    time.sleep(2)  # ждём завершения записи

    ext = Path(filepath).suffix.lower()

    # === ВИДЕО ===
    if ext in VIDEO_EXTENSIONS:
        category = "Видео"
        date_obj = get_media_creation_time(filepath)
        if not date_obj:
            date_obj = datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        text = None  # не индексируем текст видео
        new_name = rename_file(filepath, category, date_obj, text)

    # === АУДИО ===
    elif ext in AUDIO_EXTENSIONS:
        category = "Аудио"
        date_obj = get_media_creation_time(filepath)
        if not date_obj:
            date_obj = datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        new_name = rename_file(filepath, category, date_obj, text=None)

    # === ФОТО ===
    elif ext in IMAGE_EXTENSIONS:
        text = extract_text(filepath)
        if text.strip():
            category = categorize(text)
            date_obj = find_date_in_text(text) or datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        else:
            category = "Фото"
            date_obj = datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        new_name = rename_file(filepath, category, date_obj, text)

    # === PDF, ТЕКСТ, ОСТАЛЬНОЕ ===
    else:
        try:
            text = extract_text(filepath)
        except:
            text = ""
        if text and text.strip():
            category = categorize(text)
            date_obj = find_date_in_text(text) or datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        else:
            category = "Неопределённое"
            date_obj = datetime.fromtimestamp(os.path.getmtime(filepath)).date()
        new_name = rename_file(filepath, category, date_obj, text)

    # --- Перемещение с проверкой коллизий ---
    dest_dir = Path(SORTED_ROOT) / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / new_name

    # Если такой файл уже есть – добавляем счётчик
    counter = 1
    while dest_path.exists():
        stem = Path(new_name).stem
        ext_suf = Path(new_name).suffix
        dest_path = dest_dir / f"{stem}_{counter}{ext_suf}"
        counter += 1

    try:
        shutil.move(filepath, str(dest_path))
        print(f"  -> {dest_path}")
    except Exception as e:
        print(f"  Ошибка перемещения: {e}")
        return

    # --- Запись в базу (текст для поиска) ---
    # Для поиска по видео/аудио используем имя, для остальных – извлечённый текст
    if ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
        text_snippet = f"{category}: {Path(filepath).name}"
    else:
        text_snippet = text if text else Path(filepath).name

    c = conn.cursor()
    c.execute("INSERT INTO files (original_name, new_name, path, content, date_added) VALUES (?, ?, ?, ?, ?)",
              (Path(filepath).name, new_name, str(dest_path), text_snippet, datetime.now().isoformat()))
    conn.commit()

# ---------- Наблюдатель за папкой ----------
class DownloadHandler(FileSystemEventHandler):
    def __init__(self, db_conn):
        self.conn = db_conn

    def on_created(self, event):
        if event.is_directory:
            return
        # Пропускаем временные файлы загрузок браузеров
        if event.src_path.endswith(('.crdownload', '.part', '.tmp')):
            return
        sort_file(event.src_path, self.conn)

# ---------- Поиск по базе ----------
def search_files(query, conn):
    c = conn.cursor()
    c.execute("SELECT new_name, path, date_added FROM files WHERE content LIKE ? OR new_name LIKE ?",
              (f'%{query}%', f'%{query}%'))
    return c.fetchall()

# ---------- Разовый прогон существующих файлов ----------
def process_existing(folder, conn):
    for root, dirs, files in os.walk(folder):
        for name in files:
            filepath = os.path.join(root, name)
            if "Sorted" in filepath:  # не трогаем уже отсортированное
                continue
            sort_file(filepath, conn)

# ---------- Запуск ----------
def main():
    conn = init_db()
    print("Обрабатываю существующие файлы...")
    process_existing(WATCH_FOLDER, conn)

    event_handler = DownloadHandler(conn)
    observer = Observer()
    observer.schedule(event_handler, path=WATCH_FOLDER, recursive=False)
    observer.start()
    print(f"Слежу за папкой: {WATCH_FOLDER}")
    print("Нажмите Ctrl+C для выхода.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    conn.close()

if __name__ == "__main__":
    main()