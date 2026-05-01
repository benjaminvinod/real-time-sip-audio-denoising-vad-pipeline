# Real-Time Denoise + Voice Activity Detection (VAD) Pipeline

A simple client–server system that performs **real-time audio denoising and voice activity detection**.

The client sends **30 ms audio frames** to the server using **Socket.IO**.
The server performs:

* Noise suppression using **RNNoise**
* Voice Activity Detection using **WebRTC VAD**

Only speech frames are written to the output audio file.

---

# Python Version

Recommended Python version:

Python 3.11

Check your version:

```
python --version
```

Example output:

```
Python 3.11.x
```

---

# Project Structure

```
project/
│
├── denoisevadserver.py
├── denoiseVADHandler.py
├── denoisevadclient.py
├── appConfig.py
├── requirements.txt
│
├── input.wav
├── output.wav
└── .env
```

---

# Setup Instructions

## 1. Create Virtual Environment

```
python -m venv venv
```

Activate environment:

### Windows

```
venv\Scripts\activate
```

### Linux / Mac

```
source venv/bin/activate
```

---

## 2. Install Dependencies

```
pip install -r requirements.txt
```

---

## 3. Create `.env` File

Create a file called `.env` in the project root.

Example:

```
SERVER_URL=http://127.0.0.1:7000
HOST=0.0.0.0
PORT=7000
VAD_AGGRESSIVENESS=2
```

---

# Running the System

## Step 1 — Start the Server

Run:

```
python denoisevadserver.py
```

Expected output:

```
Server listening on 0.0.0.0:7000
```

---

## Step 2 — Run the Client

Open another terminal and run:

```
python denoisevadclient.py
```

The client will:

1. Load `input.wav`
2. Split it into **30 ms frames**
3. Send frames to the server
4. Receive processed frames
5. Save **speech-only audio** to `output.wav`

Example logs:

```
Connected to server
Sending frames...
Wrote speech frame 12
Skipped non-speech frame 13
Completed writing speech-only audio
```

---

# Audio Requirements

Input audio should be:

* WAV format
* Mono
* 16 kHz sample rate

Example conversion using ffmpeg:

```
ffmpeg -i audio.mp3 -ar 16000 -ac 1 input.wav
```

---

# Processing Pipeline

Server processing steps:

```
Audio Frame
     ↓
Base64 Decode
     ↓
16kHz → 48kHz Resample
     ↓
RNNoise Denoising
     ↓
48kHz → 16kHz Resample
     ↓
WebRTC VAD
     ↓
Return Speech Frame
```

---

# Dependencies

Main libraries used:

* Flask
* python-socketio
* numpy
* samplerate
* webrtcvad
* pyrnnoise
* python-dotenv

Install them with:

```
pip install -r requirements.txt
```

---

# Output

After processing completes:

```
output.wav
```

This file contains **speech-only audio**.

Silence and background noise frames are removed.

---

# Troubleshooting

### Server not connecting

Check `.env`:

```
SERVER_URL=http://127.0.0.1:7000
```

---

### Missing dependencies

Run:

```
pip install -r requirements.txt
```

---

### Invalid audio format

Ensure audio is **16kHz mono WAV**.

Convert using:

```
ffmpeg -i audio.mp3 -ar 16000 -ac 1 input.wav
```

---

# Future Improvements

Possible improvements:

* Stream microphone audio instead of WAV files
* Replace base64 transport with RTP/WebRTC
* Reduce latency with 10 ms frames
* Deploy server on cloud

---
