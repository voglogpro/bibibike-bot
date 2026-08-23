# Точка входа проекта зафиксирована явно.
# Без этого файла автоопределение BotHost находило tests/crm_ui_check.js,
# считало проект Node.js и запускало тест вместо бота: контейнер печатал
# "PASS CRM UI ..." и сразу завершался.
FROM python:3.11-slim

WORKDIR /app

# Зависимости ставим отдельным слоем, чтобы правки кода не пересобирали их.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Рабочая база и загруженные фото живут в постоянном хранилище BotHost.
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 3000

CMD ["python", "main.py"]
