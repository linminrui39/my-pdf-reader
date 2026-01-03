import streamlit as st
import fitz
import asyncio
import edge_tts
import os
import json
import threading
import pytesseract
from PIL import Image
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload, MediaIoBaseUpload

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" 
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2

os.makedirs(SAVE_DIR, exist_ok=True)

# --- Google Drive 服務 ---
@st.cache_resource
def get_drive_service():
    try:
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Google Drive 初始化失敗: {e}")
    return None

drive_service = get_drive_service()

# --- 雲端檔案同步 ---
def list_drive_files():
    if not drive_service: return []
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

# --- 【修正 1：獨立進度儲存系統】 ---
def get_prog_filename(book_name):
    # 將書名轉為合法的進度檔名
    safe_name = "".join([c for c in book_name if c.isalnum() or c in (' ', '.', '_')]).rstrip()
    return f"prog_{safe_name}.json"

def load_book_progress(book_name):
    try:
        filename = get_prog_filename(book_name)
        query = f"name = '{filename}' and '{DRIVE_FOLDER_ID}' in parents"
        res = drive_service.files().list(q=query).execute().get('files', [])
        if res:
            request = drive_service.files().get_media(fileId=res[0]['id'])
            prog_data = json.loads(request.execute())
            return prog_data.get("page", 0)
    except:
        pass
    return 0

def save_book_progress(book_name, page_num):
    try:
        filename = get_prog_filename(book_name)
        content = json.dumps({"page": page_num}).encode('utf-8')
        
        query = f"name = '{filename}' and '{DRIVE_FOLDER_ID}' in parents"
        res = drive_service.files().list(q=query).execute().get('files', [])
        
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/json', resumable=True)
        if res:
            drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
        else:
            meta = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
            drive_service.files().create(body=meta, media_body=media).execute()
    except:
        pass

# --- 核心閱讀功能 ---
@st.cache_data(show_spinner=False)
def get_page_content(book_path, page_num):
    doc = fitz.open(book_path)
    page = doc[page_num]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    img_bytes = pix.tobytes("png")
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

def background_prefetch(book_path, current_page, total_pages):
    def prefetch_worker():
        for i in range(1, PREFETCH_COUNT + 1):
            target = current_page + i
            if target < total_pages:
                _ = get_page_content(book_path, target)
    threading.Thread(target=prefetch_worker, daemon=True).start()

# --- UI 介面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# 初始化 Session State
if "current_book" not in st.session_state:
    st.session_state.current_book = None
if "temp_page" not in st.session_state:
    st.session_state.temp_page = 0

# --- 1. 圖書館模式 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    files = list_drive_files()
    pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
    
    if pdf_files:
        for f in pdf_files:
            c1, c2 = st.columns([0.8, 0.2])
            with c1:
                if st.button(f"📖 {f['name']}", key=f['id']):
                    l_path = os.path.join(SAVE_DIR, f['name'])
                    if not os.path.exists(l_path):
                        with st.spinner("下載中..."): download_file(f['id'], l_path)
                    
                    # 【修正 2：切換書籍時強制從雲端讀取該書進度】
                    st.session_state.current_book = f['name']
                    st.session_state.temp_page = load_book_progress(f['name'])
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    st.rerun()
    st.divider()
    st.info("💡 提示：請直接將 PDF 放入 Google Drive 資料夾，然後重新整理本頁面。")

# --- 2. 閱讀器模式 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    doc = fitz.open(book_path)
    total = len(doc)
    
    # 頂部導覽
    col_nav1, col_nav2 = st.columns([0.3, 0.7])
    with col_nav1:
        if st.button("❮ 返回"):
            st.session_state.current_book = None
            st.rerun()
    with col_nav2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 頁碼輸入
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, st.session_state.temp_page + 1)
    
    # 如果頁碼變動，則儲存
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        save_book_progress(book_name, st.session_state.temp_page)
        st.rerun()

    st.divider()
    
    # 內容顯示
    img, txt = get_page_content(book_path, st.session_state.temp_page)
    st.image(img, use_container_width=True)
    
    with st.spinner("產生語音中..."):
        audio = get_audio(txt)
    if audio:
        st.audio(audio, format="audio/mp3", autoplay=auto_next)

    # 背景預讀
    background_prefetch(book_path, st.session_state.temp_page, total)

    # 底部按鈕
    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        if st.button("❮ 上一頁") and st.session_state.temp_page > 0:
            st.session_state.temp_page -= 1
            save_book_progress(book_name, st.session_state.temp_page)
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            save_book_progress(book_name, st.session_state.temp_page)
            st.rerun()


