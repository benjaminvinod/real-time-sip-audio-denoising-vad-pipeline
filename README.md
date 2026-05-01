# \# Real-Time SIP Audio Denoising \& VAD Pipeline

# 

# A real-time VoIP audio processing system that captures SIP/RTP streams, performs denoising and voice activity detection (VAD), and visualizes metrics via a live dashboard.

# 

# \## Features

# \- Custom SIP server (INVITE, ACK, BYE handling)

# \- RTP audio stream processing (G.711 μ-law)

# \- Real-time denoising + VAD pipeline

# \- Live metrics dashboard (Streamlit + Socket.IO)

# \- Frame-level analytics (speech vs silence)

# 

# \## Tech Stack

# \- Python

# \- SIP / RTP (UDP sockets)

# \- Streamlit

# \- Socket.IO

# \- Audio processing (G.711, audioop)

# 

# \## How it works

# MicroSIP → SIP Server → RTP Stream → Audio Pipeline → Metrics → Dashboard

