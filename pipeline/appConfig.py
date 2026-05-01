import os
from dotenv import load_dotenv
from threading import Lock

class AppConfig:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(AppConfig, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        load_dotenv()

        # WebPubSub fields removed. Keep only local server basics.
        self.HOST = os.getenv("HOST", "0.0.0.0")
        self.PORT = int(os.getenv("PORT", "7000"))

        # Audio/VAD knobs here if you want them configurable later
        self.VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))
        self.RESAMPLE_CONVERTER = os.getenv("RESAMPLE_CONVERTER", "sinc_fastest")

        self._initialized = True

    @staticmethod
    def get_instance():
        return AppConfig()
