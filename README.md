# PM2220 RS485 to InfluxDB Collector

Script ini membaca data dari satu atau lebih PM2220 lewat RS485 (Modbus RTU) dari MiniPC, lalu menulis hasilnya ke InfluxDB.

Konfigurasi dibagi begini:
- `config.json` hanya untuk konfigurasi device PM2220.
- `.env` untuk seluruh konfigurasi InfluxDB dan interval polling.

## Data yang dikirim
- power_active_kw (register 3060)
- power_reactive_kvar (register 3068)
- power_apparent_kva (register 3076)
- v1_volt (register 3028)
- v2_volt (register 3030)
- v3_volt (register 3032)
- i1_amp (register 3000)
- i2_amp (register 3002)
- i3_amp (register 3004)
- cospi_raw_4q (register 3084)
- cospi (hasil decoding 4Q power factor)

## 1) Koneksi fisik
- Gunakan converter USB-RS485 pada MiniPC. Satu bus RS485 dapat menampung hingga 32 device PM2220 dengan slave ID berbeda.
- Hubungkan RS485 A(+) ke A(+) PM2220 dan B(-) ke B(-).
- Pastikan ground/reference sesuai kebutuhan instalasi.
- Samakan parameter serial PM2220 dengan konfigurasi script (baudrate/parity/stopbits/slave ID).

## 2) Setup Python
```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## 3) Setup konfigurasi

Gunakan file `config.json` hanya untuk device:

```json
{
  "devices": [
    {
      "name": "pm2220-01",
      "enabled": true,
      "serial_port": "COM3",
      "baudrate": 9600,
      "bytesize": 8,
      "parity": "N",
      "stopbits": 1,
      "timeout_sec": 1.0,
      "slave_id": 1,
      "address_offset": -1,
      "byte_order": "BIG",
      "word_order": "BIG"
    }
  ]
}
```

Gunakan `.env` untuk InfluxDB dan interval polling:
```env
INFLUX_URL=http://127.0.0.1:8086
INFLUX_TOKEN=your-token
INFLUX_ORG=your-org
INFLUX_BUCKET=powermeter
INFLUX_MEASUREMENT=pm2220
PM2220_MACHINE_POLL_INTERVAL_SEC=5
PM2220_API_POLL_INTERVAL_SEC=10
PM2220_DUMMY_MODE=false
```

Setiap bus serial (port) jalan di thread terpisah. Satu thread dapat menangani banyak device di bus RS485 yang sama.

Kalau ingin menjalankan tanpa perangkat fisik, ubah `PM2220_DUMMY_MODE=true` di `.env` atau `dummy_mode: true` pada device di `config.json`.

Perilaku default collector:
- baca device atau dummy generator tiap 5 detik
- kirim data terbaru ke InfluxDB tiap 10 detik

## 4) Menjalankan collector
PowerShell:
```powershell
python .\\pm2220_to_influx_totals.py
```

Tekan Ctrl+C untuk stop graceful.

## 5) Validasi di InfluxDB
Contoh Flux query:
```flux
from(bucket: "powermeter")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "pm2220")
  |> filter(fn: (r) => r.device == "pm2220-01")
```

## 6) Verifikasi data di InfluxDB
Gunakan `verify_influx_data.py` untuk mengecek apakah data dari power meter sudah masuk ke InfluxDB:
```powershell
python .\\verify_influx_data.py
```
Script ini akan:
- Menghubungkan ke InfluxDB
- Query data measurement `pm2220` dalam 10 menit terakhir
- Menampilkan jumlah record, waktu, device, field, dan nilai

Pastikan `.env` sudah berisi konfigurasi InfluxDB yang benar.

## 7) Membuat Service systemd (Auto-Start di Linux)

Agar skrip monitoring berjalan otomatis setiap kali Raspberry Pi / server Linux booting, buat layanan systemd.

### 7.1 Buat File Service Unit
```bash
sudo nano /etc/systemd/system/monitoring-mdp.service
```

### 7.2 Isi File Service
> **PENTING:**
> - Pastikan `WorkingDirectory` dan `ExecStart` sesuai dengan path absolut proyek Anda.
> - `ExecStart` harus menunjuk ke interpreter Python di dalam virtual environment.

```ini
[Unit]
Description=Monitoring Main Distribution Panel Service
After=network.target

[Service]
User=tti
WorkingDirectory=/home/tti/Monitoring_Power/raspi
ExecStart=/home/tti/Monitoring_Power/raspi/venv/bin/python pm2220_to_influx2.py
# ExecStart=/home/pi/MonitoringDyeing_TTI/raspi/venv/bin/python mod_influx.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Penjelasan Konfigurasi:**
- `After=network.target` — memastikan koneksi jaringan siap sebelum layanan mulai.
- `WorkingDirectory` — penting agar skrip menemukan file `.env` dan `config/config.json`.
- `Restart=always` — layanan otomatis dimulai ulang jika crash.
- `ExecStart` — jalankan Python dari virtual environment dengan skrip utama.

### 7.3 Aktifkan dan Jalankan Layanan
```bash
sudo systemctl daemon-reload
sudo systemctl enable monitoring-mdp.service
sudo systemctl start monitoring-mdp.service
```

### 7.4 Periksa Status dan Log
```bash
sudo systemctl status monitoring-mdp.service
sudo journalctl -u monitoring-mdp.service -f
```

## Troubleshooting cepat
- Tidak ada data: cek wiring A/B RS485, slave ID, baudrate/parity/stopbits.
- Data tidak masuk akal: ubah `address_offset` dari `-1` ke `0`.
- Nilai float kacau: coba kombinasi `word_order`/`byte_order`.
- Error serial port: pastikan port tidak dipakai aplikasi lain.
- Multiple device timeout: periksa slave ID, koneksi serial, dan interval polling di `.env`.

