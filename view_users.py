import os
import json
from dotenv import load_dotenv # type: ignore
from secure_store import SecureStore

load_dotenv()

def main():
    password = os.getenv("STORAGE_PASSWORD")
    if not password:
        print("❌ Ошибка: STORAGE_PASSWORD не найден в .env файле.")
        print("Пожалуйста, убедитесь, что вы добавили этот параметр.")
        return

    filepath = os.path.join("data", "secure_users.enc")
    if not os.path.exists(filepath):
        print(f"❌ Ошибка: Файл {filepath} не найден. База данных пока пуста, так как никто еще не запустил бота.")
        return

    print("⏳ Расшифровка базы данных...")
    store = SecureStore(filepath, password)
    data = store._read_encrypted()

    if not data:
        print("ℹ️ База пользователей пуста.")
        return

    print("✅ Успешно расшифровано. Содержимое базы данных:")
    print("-" * 40)
    print(json.dumps(data, indent=4, ensure_ascii=False))
    print("-" * 40)
    print(f"Всего пользователей: {len(data)}")

if __name__ == "__main__":
    main()
