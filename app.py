import streamlit as st
import fitz
import asyncio
import edge_tts
import os
import json
import re
import threading
import pytesseract
from PIL import Image
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN"
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
PROGRESS_FILE = "drive_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"

os.makedirs(SAVE_DIR, exist_ok=True)

# --- Google Drive 服務初始化 ---
@st.cache_resource
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        info = dict(st.secrets["gcp_service_account"])
        creds = service_account.Credentials.from_service_account_info(info)
        return build('drive', 'v3', credentials=creds)
    return None

drive_service = get_drive_service()

# --- 雲端檔案同步功能 ---
def list_drive_files():
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    return results.get('files', [])

def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

def upload_file(local_path, filename):
    file_metadata = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(local_path, resumable=True)
    drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

# --- 進度儲存至 Drive ---
def load_remote_progress():
    query = f"name = '{PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents"
    res = drive_service.files().list(q=query).execute().get('files', [])
    if res:
        request = drive_service.files().get_media(fileId=res[0]['id'])
        return json.loads(request.execute())
    return {}

def save_remote_progress(book_name, page_num):
    data = load_remote_progress()
    data[book_name] = page_num
    content = json.dumps(data)
    
    query = f"name = '{PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents"
    res = drive_service.files().list(q=query).execute().get('files', [])
    
    media = MediaFileUpload(io.BytesIO(content.encode()), mimetype='application/json')
    if res:
        drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
    else:
        meta = {'name': PROGRESS_FILE, 'parents': [DRIVE_FOLDER_ID]}
        drive_service.files().create(body=meta, media_body=media).execute()

# --- 核心閱讀功能 (保留預讀與 OCR) ---
@st.cache_data(show_spinner=False)
def get_page_content(book_path, page_num):
    doc = fitz.open(book_path)
    page = doc[page_num]
    # 圖片
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    img_bytes = pix.tobytes("png")
    # 文字 OCR
    text = page.get_text().strip()
    if not text:
        text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)), lang='chi_tra+eng')
    doc.close()
    return img_bytes, text.replace('\n', ' ')

@st.cache_data(show_spinner=False)
def get_audio(text):
    if not text.strip(): return None
    async def gen():
        c = edge_tts.Communicate(text, VOICE, rate=SPEED)
        data = b""
        async for chunk in c.stream():
            if chunk["type"] == "audio": data += chunk["data"]
        return data
    return asyncio.run(gen())

# --- UI 介面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")
st.markdown("<style>.stApp { background-color: white; } .stButton>button { border: 1px solid black !important; border-radius: 4px !important; }</style>", unsafe_allow_html=True)

if "current_book" not in st.session_state:
    st.session_state.current_book = None

# --- 1. 圖書館 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    files = list_drive_files()
    for f in [x for x in files if x['name'].endswith('.pdf')]:
        col1, col2 = st.columns([0.8, 0.2])
        with col1:
            if st.button(f"📖 {f['name']}", key=f['id']):
                local_path = os.path.join(SAVE_DIR, f['name'])
                if not os.path.exists(local_path):
                    with st.spinner("從雲端下載中..."):
                        download_file(f['id'], local_path)
                st.session_state.current_book = f['name']
                st.rerun()
    st.divider()
    up = st.file_uploader("匯入新書", type="pdf")
    if up:
        l_path = os.path.join(SAVE_DIR, up.name)
        with open(l_path, "wb") as f: f.write(up.getbuffer())
        with st.spinner("同步至雲端硬碟..."):
            upload_file(l_path, up.name)
        st.session_state.current_book = up.name
        st.rerun()

# --- 2. 閱讀器 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    doc = fitz.open(book_path)
    total = len(doc)
    
    if "temp_page" not in st.session_state:
        st.session_state.temp_page = load_remote_progress().get(book_name, 0)

    # 頂部控制
    c1, c2 = st.columns([0.4, 0.6])
    with c1:
        if st.button("❮ 返回"):
            st.session_state.current_book = None
            st.rerun()
    with c2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 跳轉
    t_page = st.number_input(f"頁碼 / 共 {total} 頁", 1, total, st.session_state.temp_page + 1)
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        save_remote_progress(book_name, st.session_state.temp_page)
        st.rerun()

    # 顯示與朗讀
    img, txt = get_page_content(book_path, st.session_state.temp_page)
    st.image(img, use_container_width=True)
    
    with st.spinner("載入語音..."):
        audio = get_audio(txt)
    if audio:
        st.audio(audio, format="audio/mp3", autoplay=auto_next)

    # 翻頁
    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        if st.button("❮ 上一頁") and st.session_state.temp_page > 0:
            st.session_state.temp_page -= 1
            save_remote_progress(book_name, st.session_state.temp_page)
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            save_remote_progress(book_name, st.session_state.temp_page)
            st.rerun()
