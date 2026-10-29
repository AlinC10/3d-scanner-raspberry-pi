from fastapi import APIRouter, Response, status
from system.reboot import reboot
import threading

router = APIRouter(prefix="/reboot")

@router.get("")
async def trigger_reboot():
    timer = threading.Timer(1.5, reboot())
    timer.daemon = True
    timer.start()

    return {"status": "rebooting", "message": "Raspberry Pi repornește..."}



