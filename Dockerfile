# Temel Python imajını kullan
FROM python:3.10-slim

# Konteyner içinde /app adında bir çalışma dizini oluştur
WORKDIR /app

# BAŞLANGIÇ: GÜNCELLEME (İSTEK 4a: Cronjob Kurulumu)
# cron servisini kur
RUN apt-get update && apt-get install -y cron && apt-get clean
# BİTİŞ: GÜNCELLEME

# Önce gereksinim dosyasını kopyala ve kütüphaneleri kur
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Proje kodlarının geri kalanını kopyala
COPY . .

# BAŞLANGIÇ: GÜNCELLEME (İSTEK 4a: Cronjob ve Entrypoint Kopyalama)
# Oluşturduğumuz cronjob dosyasını cron'un klasörüne kopyala
COPY cronjob /etc/cron.d/stok-projesi-cron

# Cronjob dosyasına doğru izinleri ver
RUN chmod 0644 /etc/cron.d/stok-projesi-cron

# Entrypoint script'ini kopyala (bu zaten chmod +x yapılmıştı)
COPY entrypoint.sh /entrypoint.sh
# BİTİŞ: GÜNCELLEME

# Flask uygulamasının bu portta çalışacağını belirt
ENV FLASK_APP=app.py

# BAŞLANGIÇ: GÜNCELLEME (İSTEK 4a: Entrypoint'i Çalıştır)
# Konteyner çalıştığında Gunicorn yerine entrypoint script'imizi başlat
CMD ["/entrypoint.sh"]
# BİTİŞ: GÜNCELLEME
