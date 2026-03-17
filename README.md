# Kaam Bharat – Daily Wage Labour Hiring Platform

Architecture: **Firebase Auth (OTP) + Firebase Storage (Aadhaar) → Flask Backend → PostgreSQL**

## Architecture Flow

1. **Firebase Auth** – Phone OTP authentication (multi-device compatible)
2. **Firebase Storage** – Aadhaar card images
3. **Flask Backend** – Business logic, job posting, applications
4. **PostgreSQL** – Persistent data

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Firebase Setup

1. Create a [Firebase project](https://console.firebase.google.com/)
2. Enable **Authentication → Phone** sign-in
3. Enable **Storage**
4. Add your domain (e.g. `localhost`) in Authentication → Settings → Authorized domains
5. Download **Service Account JSON** (Project Settings → Service Accounts) and save as `firebase-credentials.json`
6. Get **Web client config** from Project Settings → General → Your apps

### 3. PostgreSQL

Create a database:

```sql
CREATE DATABASE kaam_bharat;
```

### 4. Environment Variables

Copy `.env.example` to `.env` and fill in:

```env
FLASK_SECRET_KEY=your-secret-key
DATABASE_URL=postgresql://user:password@localhost:5432/kaam_bharat
FIREBASE_CREDENTIALS_PATH=./firebase-credentials.json

# From Firebase Console > Project Settings
FIREBASE_API_KEY=...
FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
FIREBASE_PROJECT_ID=your-project-id
FIREBASE_STORAGE_BUCKET=your-project.appspot.com
FIREBASE_MESSAGING_SENDER_ID=...
FIREBASE_APP_ID=...
```

### 5. Firebase Storage Rules

Deploy `firebase-storage.rules` via Firebase Console (Storage → Rules) or Firebase CLI.

### 6. Run

```bash
python app.py
```

- **Same device:** http://127.0.0.1:5000
- **Other devices on same WiFi:** http://YOUR_IP:5000 (see below)

### 7. Run on Multiple Devices (Manager + Worker)

To use one device as **Manager** and another as **Worker**:

1. **Start the app** on your computer:
   ```bash
   python app.py
   ```

2. **Find your computer's IP** on the local network:
   - **Windows:** `ipconfig` → look for "IPv4 Address" (e.g. `192.168.1.5`)
   - **Mac/Linux:** `ifconfig` or `ip addr` → look for inet (e.g. `192.168.1.5`)

3. **Connect both devices** to the **same WiFi** as the computer.

4. **Device 1 (Manager):** Open `http://YOUR_IP:5000` (e.g. `http://192.168.1.5:5000`), register as **Manager**.

5. **Device 2 (Worker):** Open the same URL, register as **Worker**.

6. **More devices:** There is **no limit**. Add more workers, more managers – any number of devices on the same WiFi can connect to `http://YOUR_IP:5000` and use the app simultaneously.

7. **Flow:** Manager posts jobs → Worker sees them and applies → Manager accepts → Worker confirms.

**Firewall:** If other devices cannot connect, allow port 5000 in Windows Firewall or your router.

**Firebase (if used):** Firebase may restrict login from local IPs. For multi-device testing, use legacy mode: open `http://YOUR_IP:5000/login?legacy=1` and `http://YOUR_IP:5000/register?legacy=1` – OTP will appear in the server terminal.

### 8. Real OTP via SMS (Twilio)

To send OTP to real phone numbers instead of printing in terminal:

1. Sign up at [Twilio](https://www.twilio.com/) and get a phone number
2. Add to `.env`:
   ```env
   TWILIO_ACCOUNT_SID=your-sid
   TWILIO_AUTH_TOKEN=your-token
   TWILIO_PHONE_NUMBER=+1234567890
   ```
3. OTP will be sent as SMS. Without Twilio config, OTP is printed in the server terminal.

**Phone validation:** Indian mobile numbers only (10 digits, starts with 6/7/8/9).

## Fallback Mode

If Firebase is not configured (no `FIREBASE_API_KEY`), the app falls back to:
- Session-based OTP (SMS via Twilio if configured, else printed in terminal)
- Local Aadhaar storage in `static/aadhaar/`
- SQLite instead of PostgreSQL (if `DATABASE_URL` is empty)

## Features

- Phone-based login/register with Firebase OTP
- Aadhaar verification (OCR + face detection)
- Job posting (manager) and job discovery (worker)
- Application workflow: interest → accept → confirm
- Multi-device support via Firebase Auth
