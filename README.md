# Stokçu (PFSM Engine)

**Python & Flask Tabanlı Stok Entegrasyon ve Fiyatlandırma Motoru**

Stokçu, e-ticaret operasyonlarında farklı tedarikçilerden (XML, Excel, CSV) gelen dağınık verileri birleştiren, yapay zeka destekli eşleştirme yapan ve doğal dil işleme (NLP) ile dinamik fiyatlandırma kuralları uygulayan açık kaynaklı bir karar destek sistemidir.

![Python](https://img.shields.io/badge/Python-3.10-blue?style=flat&logo=python)
![Flask](https://img.shields.io/badge/Flask-2.x-black?style=flat&logo=flask)
![Pandas](https://img.shields.io/badge/Pandas-ETL-150458?style=flat&logo=pandas)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat&logo=docker)

## 1. Giriş ve Problem Tanımı

E-ticaret ekosisteminde, farklı tedarikçilerden gelen verilerin pazar yerleri (Trendyol, Hepsiburada vb.) ile senkronize edilmesi karmaşık bir problemdir.

* **Veri Standardizasyonu Sorunu:** Tedarikçiler aynı ürünü farklı isimlerle tanımlar (Örn: "Bosch GSB 180" vs "Gsr 180-Li Matkap"). Klasik "Tam Eşleşme" (Exact Match) algoritmaları bu durumda başarısız olur.
* **Yanlış Eşleşme Riski:** Bulanık (Fuzzy) arama algoritmaları, "Matkap Ucu" ile "Matkap" ürününü yanlış eşleştirerek (False Positive) ticari zarara yol açabilir.

**Çözüm:** Stokçu; deterministik (Barkod/SKU) ve olasılıksal (Vektörel Benzerlik) yöntemleri birleştiren hibrit bir yapı kullanır.

---

## 2. Kullanıcı İşlemleri ve Teknik Karşılıkları (Süreç Haritası)

Kullanıcının arayüzde yaptığı işlemlerin arka plandaki teknik karşılıkları aşağıdadır:

| Adım | Kullanıcı Arayüzü (Frontend) | Arka Plan (Backend) & Teknoloji |
| :--- | :--- | :--- |
| **1** | **Sistem Açılışı**<br>Sistem otomatik olarak güncel kurları ve şablonları kontrol eder. | **XML Parsing (Requests)**<br>`fetch_exchange_rates()` fonksiyonu TCMB API'sine bağlanır, XML verisini parse eder ve USD/EUR kurlarını hafızaya (RAM) alır. |
| **2** | **Veri Yükleme (ETL)**<br>İç stok ve tedarikçi dosyaları yüklenir, sütun eşleştirmesi yapılır. | **Pandas Normalization**<br>`read_and_normalize_file()` fonksiyonu dosyaları okur. Pandas ve OpenPyXL kullanarak sütun isimlerini JSON şablonuna göre standardize eder. |
| **3** | **NLP Kural Girişi**<br>Kullanıcı: "TÜM BOSCH ÜRÜNLERİNE %10 ZAM YAP" komutunu girer. | **Regex Parsing Engine**<br>`parse_natural_language_rules()` fonksiyonu metni analiz eder. Hedef (Bosch), Aksiyon (Multiplier) ve Değer (1.10) objesini oluşturur. |
| **4** | **Analiz ve Raporlama**<br>İşlem başlatılır, ilerleme çubuğu takip edilir. | **Vector Matching & Threading**<br>`UniversalSmartMatcher.run_engine()` TF-IDF algoritmasını çalıştırır. İşlem uzun sürdüğü için `threading` ile asenkron yönetilir. |

---

## 3. Sistem Mimarisi

Uygulama, ölçeklenebilirlik ve izolasyon prensipleri gözetilerek **Docker** üzerinde çalışmaktadır.

* **Docker Runtime:** Uygulama, `python:3.10-slim` imajı üzerinde, sadece gerekli bağımlılıkları (Pandas, Scikit-learn) barındıran izole bir ortamda çalışır.
* **Gunicorn WSGI Server:** Python'un tek iş parçacıklı yapısını aşmak için `--workers 3` konfigürasyonu ile çalışır. Bu sayede sistem aynı anda birden fazla dosya işleme talebini CPU çekirdeklerine dağıtır.
* **Traefik Proxy:** Sistem dış dünyaya doğrudan değil, Traefik üzerinden açılır. Traefik, SSL sertifikalarını (Let's Encrypt) yönetir ve yük dengeleme (Load Balancing) yapar.

---

## 4. Algoritmik Metodoloji

Sistemin çekirdeğini `UniversalSmartMatcher` sınıfı oluşturur. Eşleşme 3 katmanlı bir filtrelemeden geçer:

1.  **Normalizasyon:** Türkçe karakterler (ı->i, ş->s) ve gürültü kelimeler (kargo, bedava) temizlenir.
2.  **TF-IDF Vektörel Uzay:** Ürün isimleri vektörlere dönüştürülür ve Kosinüs Benzerliği (Cosine Similarity) hesaplanır.
3.  **Heuristic Kurallar (Güvenlik Duvarı):** Yüksek benzerlik skoru tek başına yeterli değildir. Mantıksal kurallar devreye girer:

| Kural Adı | Açıklama | Örnek Durum |
| :--- | :--- | :--- |
| **Brand Conflict** | Farklı markaların eşleşmesini engeller. | `Bosch` ≠ `Makita` (Skor yüksek olsa bile REDDEDİLİR) |
| **Set Conflict** | Paket miktarlarını kontrol eder. | `10'lu Set` ≠ `Tekli` (REDDEDİLİR) |
| **Golden Code** | Model kodunu yakalar. | "GSR-120-LI" kodu her iki tarafta varsa ONAYLANIR. |

---

## 5. NLP Fiyatlandırma Motoru

Kullanıcıların kod yazmadan, doğal dil ile karmaşık fiyatlandırma senaryoları oluşturmasını sağlar.

* **Döviz Endeksleme:**
    * Komut: `MAKITA GUNCEL KURA ESITLE`
    * İşlem: TCMB kurunu çeker ve uygular.
* **Matematiksel İşlem:**
    * Komut: `BOSCH %10 ZAM YAP`
    * İşlem: Maliyet üzerine %10 ekler.
* **Kur Çevrimi:**
    * Komut: `ESKI_KUR=32.50 YENI KURA CEVIR`
    * İşlem: `(Fiyat / 32.50) * Güncel_Kur`

---

## 6. Teknoloji Yığını

| Bileşen | Teknoloji / Kütüphane | Kullanım Amacı |
| :--- | :--- | :--- |
| **Core** | Python 3.10 | Ana programlama dili. |
| **Web** | Flask 2.x | REST API endpoint yönetimi. |
| **Data** | Pandas, NumPy | Vektörel veri işleme ve matris operasyonları. |
| **ML** | Scikit-Learn | TF-IDF Vektörleştirme ve Cosine Similarity. |
| **File I/O** | OpenPyXL, Xlrd | Excel dosyalarını okuma ve yazma. |
| **Integration** | Requests | TCMB API XML entegrasyonu. |
| **Server** | Gunicorn, Docker | Production ortamı ve sunucu. |

---

## 7. Rapor Yorumlama ve Sorumluluk

Sistem çıktısı olan Excel raporundaki **Algoritma Skoru** sütunu dikkate alınmalıdır.

* **Skor > 85 (Yeşil):** Yüksek Güven.
* **Skor 50 - 85 (Sarı):** Orta Güven. Kontrol edilmelidir.
* **Skor < 50 (Kırmızı):** Düşük Güven. Manuel işlem şarttır.

> **YASAL UYARI:** Bu yazılım (Stokçu), stok ve fiyat eşleştirmeleri için bir **Karar Destek Sistemi**dir. Algoritmik eşleştirmeler %100 doğruluk garantisi vermez. Kullanıcı, pazar yerine veri yüklemeden önce sonuçları (özellikle fiyat ve stok farklarını) kontrol etmekle yükümlüdür.

---

## 8. Kurulum ve Çalıştırma

Projeyi kendi sunucunuzda Docker ile ayağa kaldırmak için:

```bash
# 1. Projeyi klonlayın
git clone [https://github.com/AhmetKorkmazMe/stok-projesi.git](https://github.com/AhmetKorkmazMe/stok-projesi.git)
cd stok-projesi

# 2. Docker ile başlatın
docker-compose up -d --build
