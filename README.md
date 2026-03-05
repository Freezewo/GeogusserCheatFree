# 🌍 GeoGuessr Steam — Coordinate Tool

Адаптация подхода PlonkIT для Steam Edition. Использует Chrome DevTools Protocol для инжекта XHR-перехватчика прямо в CEF-браузер игры.

## Как работает

1. Подключается к встроенному Chromium через **CDP** (порт 9222)
2. Инжектит перехватчик XHR-запросов к Google Maps Internal API
3. Ловит координаты из ответов `MapsJsInternalService/GetMetadata`
4. Определяет страну/город через **LocationIQ API**
5. Показывает результат в полупрозрачном оверлее

> Тот же принцип что PlonkIT использует для браузерной версии, но через CDP вместо Tampermonkey.

## Установка

```bash
pip install -r requirements.txt
```

## Настройка Steam

1. **Steam** → ПКМ по **GeoGuessr** → **Свойства**
2. В **Параметры запуска** добавьте:
   ```
   --remote-debugging-port=9222
   ```
3. Запустите GeoGuessr

## Запуск

```bash
python geoguessr_tool.py
```

Скрипт автоматически:
- подключится к игре
- инжектнет XHR-перехватчик
- покажет оверлей с координатами

Оверлей можно перетаскивать мышкой. Координаты обновляются автоматически при каждом новом раунде.

## Файлы

| Файл | Описание |
|------|----------|
| `geoguessr_tool.py` | Основной скрипт |
| `requirements.txt` | Python-зависимости |
