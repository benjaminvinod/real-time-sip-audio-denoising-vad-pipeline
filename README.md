# 🎧 Real-Time Speech Denoising, VAD & AI Call Analysis Pipeline

A low-latency, CPU-efficient system for **real-time telephony audio processing**, combining:

* 📞 SIP/RTP call handling
* 🔊 Noise suppression (RNNoise)
* 🗣 Voice Activity Detection (WebRTC VAD)
* 🧠 Post-call AI semantic analysis (LLM via Ollama)
* 💾 Persistent storage (SQLite)
* 🌐 Live UI visualization

---

# 🚀 What This Project Does

This system processes live audio from SIP calls and converts it into **structured intelligence**.

### 🔁 End-to-End Flow

```text
SIP Call → RTP Audio → Denoising → VAD → Transcript
                                     ↓
                              LLM Analysis (Ollama)
                                     ↓
                            Structured Output (JSON)
                                     ↓
                              UI + SQLite Storage
```

---

# ✨ Key Features

* ⚡ Real-time audio pipeline (30ms frames)
* 🔇 RNNoise-based denoising
* 🎯 Accurate speech detection (WebRTC VAD)
* 📊 Live UI with metrics (RTF, TRT, speech detection)
* 🧠 LLM-powered semantic analysis:

  * Summary
  * Intent
  * Sentiment
  * Risk level
  * Suggested action
* 💾 Persistent call storage (SQLite)
* 🔌 Fully local — no external APIs required

---

# 📂 Project Structure

```text
project/
│
├── pipeline/                 # Backend processing engine
│   ├── sip_server.py         # Main server (SIP + RTP + LLM + API)
│   ├── denoiseVADHandler.py  # Audio processing pipeline
│   ├── metricsLogger.py      # Performance metrics
│   ├── appConfig.py          # Configurations
│   ├── db_manager.py         # SQLite DB logic
│   └── db/
│       └── calls.db          # Persistent database
│
├── web_ui/                   # Frontend UI
│   ├── index.html
│   ├── app.js
│   └── styles.css
│
├── .env
├── requirements.txt
└── README.md
```

---

# 🧠 File-by-File Breakdown

## 🔹 `sip_server.py`

* SIP signaling (INVITE / BYE)
* RTP audio ingestion
* LLM integration (Ollama)
* WebSocket communication
* API endpoints (`/history`, `/call/<id>`)

---

## 🔹 `denoiseVADHandler.py`

* RNNoise denoising
* WebRTC VAD
* Speech boundary detection
* Audio buffering + resampling

---

## 🔹 `metricsLogger.py`

* Logs performance metrics
* Tracks latency (RTF, TRT)

---

## 🔹 `db_manager.py`

* Creates SQLite database
* Stores call analysis results
* Fetches call history

---

## 🔹 `appConfig.py`

* Central configuration (VAD, buffer sizes, thresholds)

---

## 🔹 `web_ui/`

### `index.html`

* UI layout

### `app.js`

* WebSocket connection
* Real-time updates
* LLM result rendering

### `styles.css`

* Styling

---

# ⚙️ Full Setup Guide

---

# 🧩 1. Clone the Repository

```bash
git clone <your-repo-url>
cd real-time-sip-audio-denoising-vad-pipeline
```

---

# 🐍 2. Create Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate   # Windows
```

---

# 📦 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

# 🧠 4. Install & Run Ollama (LLM)

## 🔹 Install Ollama

Download from: https://ollama.com

---

## 🔹 Start Ollama server

```bash
ollama serve
```

👉 Keep this terminal open

---

## 🔹 Pull model (first time only)

```bash
ollama run llama3.1:8b
```

Then exit (`Ctrl + C`)

---

## ⚠️ Important

Ensure Ollama is running:

```bash
netstat -ano | findstr :11434
```

---

# ▶️ 5. Run Backend Server

```bash
python pipeline/sip_server.py
```

---

# 🌐 6. Open UI

```text
http://localhost:5000
```

---

# 📞 SIP Client Setup (MicroSIP)

---

## 🧩 1. Install MicroSIP

Download: https://www.microsip.org/downloads

---

## ⚙️ 2. Configure MicroSIP

Go to: **Menu → Accounts → Add**

```text
SIP Server:   127.0.0.1
Port:         5060
Username:     1001
Domain:       127.0.0.1
Transport:    UDP
```

---

## 📞 3. Make a Call

Dial:

```text
sip:127.0.0.1:5060
```

Click **Call**

---

## 🗣 4. Speak

Example:

```text
"I want to cancel my subscription because it's too expensive"
```

---

## 📴 5. End Call

👉 Required — triggers AI analysis

---

# 🤖 Expected Output

## During Call

* Live transcript
* Speech detection
* Metrics

---

## After Call Ends

* "AI generating report…"
* Then structured output:

```text
Summary
Intent
Sentiment
Risk Level
Suggested Action
```

---

# 💾 View Stored Data

## API

```bash
curl http://localhost:5000/history
curl http://localhost:5000/call/<call_id>
```

---

## SQLite (VS Code)

Open:

```text
pipeline/db/calls.db
```

---

# 🛠 Troubleshooting

---

## ❌ Ollama not running

```bash
ollama serve
```

---

## ❌ Port already in use

```bash
taskkill /IM ollama.exe /F
```

---

## ❌ LLM errors

```bash
setx OLLAMA_NO_GPU 1
```

Restart terminal.

---

## ❌ No DB entries

Check logs:

```text
💾 [DB] Saved call
```

---

## ❌ MicroSIP issues

* Ensure backend running
* Check port 5060
* Verify microphone input

---

# 🧠 Technologies Used

* Python (Flask, Socket.IO)
* RNNoise
* WebRTC VAD
* Ollama (LLaMA 3.1)
* SQLite
* JavaScript

---

# 📈 Future Improvements

* Multi-call support
* Cloud deployment (AWS ALB, ASG)
* Downstream integrations (CRM, APIs)
* Real-time LLM streaming

---

# 🏁 Conclusion

This project demonstrates a **complete real-time speech intelligence system**, combining:

* Signal Processing
* Networking (SIP/RTP)
* AI (LLM analysis)
* Systems Design (streaming + persistence)

---

# 👥 Team

* Benjamin Mammen Vinod
* Sahil Waghere
* Ansh Brahmbhatt
* Yash Patil

