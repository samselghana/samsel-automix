
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
import shutil
import os

from dj_engine_pro import make_deck

app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

deck_state = {"A": None, "B": None}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload/{deck}")
async def upload(deck: str, file: UploadFile = File(...)):
    path = os.path.join(UPLOAD_DIR, file.filename)
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    deck_obj = make_deck(deck, path)
    deck_state[deck] = {
        "file": file.filename,
        "bpm": float(deck_obj.bpm),
        "duration": float(deck_obj.duration_sec)
    }

    return deck_state[deck]

@app.get("/status")
def status():
    return deck_state
