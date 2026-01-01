# Gunakan image dasar Python versi 3.11.9
FROM python:3.11.9-slim

# Instal dependensi sistem yang diperlukan
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    gcc \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Atur direktori kerja di dalam container
WORKDIR /app

# Upgrade pip tooling
RUN pip install --upgrade pip setuptools wheel

# Salin file requirements.txt dan install dependency Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin semua file kode ke dalam container
COPY . .

# Buat shell script untuk menjalankan kedua bot
RUN printf '#!/bin/sh\npython xylence-helper/main.py & wait\n' > start.sh
RUN chmod +x start.sh

# (Optional) expose port hanya jika server_monitors membuka HTTP server
EXPOSE 8000

# Jalankan script
CMD ["./start.sh"]
