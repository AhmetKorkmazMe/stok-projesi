# Stokçu (PFSM Engine)

**Python & Flask Tabanlı Stok Entegrasyon ve Fiyatlandırma Motoru**

Stokçu, e-ticaret operasyonlarında farklı tedarikçilerden (XML, Excel, CSV) gelen dağınık verileri birleştiren, yapay zeka destekli eşleştirme yapan ve doğal dil işleme (NLP) ile dinamik fiyatlandırma kuralları uygulayan açık kaynaklı bir karar destek sistemidir.

![Python](https://img.shields.io/badge/Python-3.10-blue?style=flat&logo=python)
![Flask](https://img.shields.io/badge/Flask-2.x-black?style=flat&logo=flask)
![Pandas](https://img.shields.io/badge/Pandas-ETL-150458?style=flat&logo=pandas)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat&logo=docker)

## Temel Özellikler

* **ETL Motoru:** XML, Excel ve CSV formatındaki tedarikçi dosyalarını okur ve standartlaştırır.
* **Akıllı Eşleştirme (Smart Matching):** Ürünleri sadece isme göre değil, model kodu ve TF-IDF vektörel benzerlik algoritmalarıyla eşleştirir.
* **NLP Fiyatlandırma:** "Tüm Bosch ürünlerine %10 zam yap" gibi doğal dil komutlarını anlar ve uygular.
* **Smart Freeze:** Hatalı fiyat düşüşlerini engelleyen güvenlik mekanizması.
* **Döviz Entegrasyonu:** TCMB'den anlık kur çekerek dinamik fiyat hesaplar.

## Kurulum ve Çalıştırma

Projeyi kendi sunucunuzda Docker ile ayağa kaldırmak için:

```bash
# 1. Projeyi klonlayın
git clone https://github.com/AhmetKorkmazMe/stok-projesi.git
cd stok-projesi

# 2. Docker ile başlatın
docker-compose up -d --build
```

Tarayıcınızdan `http://localhost:5000` adresine giderek arayüze erişebilirsiniz.

## Teknik Dokümantasyon

Detaylı sistem mimarisi, algoritma açıklamaları ve kullanım kılavuzu için proje içindeki **[Teknik Dokümantasyon](static/teknik_dokumantasyon.html)** sayfasına göz atabilirsiniz.

---
**YASAL UYARI:** Bu yazılım bir karar destek sistemidir. Oluşturulan fiyat ve stok raporları pazar yerine yüklenmeden önce kontrol edilmelidir.
