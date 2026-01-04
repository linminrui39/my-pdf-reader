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
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# --- [功能 1]：初始化設定 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" 
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2  # 向後預讀 2 頁

os.makedirs(SAVE_DIR, exist_ok=True)

@st.cache_resource(ttl=3600)
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except: pass
    return None

drive_service = get_drive_service()

# --- [功能 2]：雲端進度管理 ---
def sync_progress_from_cloud():
    if not drive_service: return {}
    try:
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        if res:
            content = drive_service.files().get_media(fileId=res[0]['id']).execute()
            return json.loads(content)
    except: pass
    return {}

def save_progress_to_cloud():
    if not drive_service: return
    try:
        data = st.session_state.global_progress
        content = json.dumps(data).encode('utf-8')
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/json')
        if res:
            drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
        else:
            meta = {'name': MASTER_PROGRESS_FILE, 'parents': [DRIVE_FOLDER_ID]}
            drive_service.files().create(body=meta, media_body=media).execute()
    except: pass

# --- [功能 3]：核心渲染與 OCR (含快取) ---
@st.cache_data(show_spinner=False)
def get_page_content(book_path, page_num):
    try:
        doc = fitz.open(book_path)
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        img_bytes = pix.tobytes("png")
        text = page.get_text().strip()
        if not text:
            text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)), lang='chi_tra+eng')
        doc.close()
        return img_bytes, text.replace('\n', ' ')
    except: return None, ""

# --- [功能 4]：預讀機制 (Prefetch) ---
def background_prefetch(book_path, current_page, total_pages):
    """在背景偷偷載入後面的頁面內容"""
    def prefetch_worker():
        for i in range(1, PREFETCH_COUNT + 1):
            target = current_page + i
            if target < total_pages:
                # 呼叫 get_page_content 會觸發 st.cache_data 儲存結果
                _ = get_page_content(book_path, target)
    
    threading.Thread(target=prefetch_worker, daemon=True).start()

# --- 語音生成 ---
@st.cache_data(show_spinner=False)
def get_audio(text):
    if not text or not text.strip(): return None
    async def gen():
        c = edge_tts.Communicate(text, VOICE, rate=SPEED)
        data = b""
        async for chunk in c.stream():
            if chunk["type"] == "audio": data += chunk["data"]
        return data
    return asyncio.run(gen())

# ---------------------------------------------------------
# 主邏輯
# ---------------------------------------------------------

if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()

params = st.query_params
url_book = params.get("book")
url_page = int(params.get("page")) if params.get("page") else None

if url_book:
    st.session_state.current_book = url_book
elif "current_book" not in st.session_state:
    st.session_state.current_book = None

if url_page is not None:
    st.session_state.temp_page = url_page
elif st.session_state.current_book:
    st.session_state.temp_page = st.session_state.global_progress.get(st.session_state.current_book, 0)
else:
    st.session_state.temp_page = 0

# --- 圖書館 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    if st.button("🔄 刷新雲端清單"):
        st.cache_data.clear()
        st.session_state.global_progress = sync_progress_from_cloud()
        st.rerun()

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    try:
        files = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
        for f in pdf_files:
            saved_p = st.session_state.global_progress.get(f['name'], 0)
            if st.button(f"📖 {f['name']} (讀至第 {saved_p + 1} 頁)", key=f['id']):
                # 下載書籍... (省略)
                st.query_params["book"] = f['name']
                st.query_params["page"] = saved_p
                st.session_state.current_book = f['name']
                st.session_state.temp_page = saved_p
                st.rerun()
    except: pass
else:
    # --- 閱讀器 (包含預讀呼叫) ---
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)

    # 確保書籍已下載... (邏輯同前)
    if os.path.exists(book_path):
        doc = fitz.open(book_path)
        total = len(doc)

        if st.button("❮ 返回圖書館"):
            st.query_params.clear()
            st.session_state.current_book = None
            st.rerun()

        t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
        if t_page - 1 != st.session_state.temp_page:
            st.session_state.temp_page = t_page - 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            st.query_params["page"] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()

        st.divider()
        
        # 顯示當前頁面
        img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
        if img_data:
            st.image(img_data, use_column_width=True)
            
            # 【關鍵功能】：觸發背景預讀
            background_prefetch(book_path, st.session_state.temp_page, total)
        
        if text_content:
            with st.spinner("語音載入中..."):
                audio = get_audio(text_content)
            if audio:
                st.audio(audio, format="audio/mp3", autoplay=True)

        # 底部翻頁 (省略)

