import os
import sys
import logging

# 1. Force the MOCK_HARDWARE flag so we don't have to remember to type it
os.environ["MOCK_HARDWARE"] = "1"

# 2. Apply all the hardware library mocks BEFORE the app imports anything
try:
    from hardware.mock_hardware import MockHardware
    MockHardware.apply()
    print("🚀 Running in DEV MODE: Hardware dependencies mocked successfully!")
except Exception as e:
    print(f"Error applying hardware mocks: {e}")

# 3. Import the main production app
from main_prod import app

# (Optional) We can add Development-only overrides here later, 
# like permissive CORS for frontend frameworks (React/Vue/etc.)
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
