FROM python:3.11-slim
# корневой сертификат Минцифры: без него не открывается GigaChat (как в боте модерации). Не скачался — сборка идёт дальше
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && (curl -fsSL --retry 3 -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
        https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
        && update-ca-certificates || echo "russian root CA not installed — GigaChat will not work") \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY webapp webapp
CMD ["python", "-m", "app.main"]
