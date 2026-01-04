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

# --- 【核心修正 1】：設定頁面並立即處理網址參數 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" 
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"

os.makedirs(SAVE_DIR, exist_ok=True)

@st.cache_resource
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds)
        except: pass
    return None

drive_service = get_drive_service()

# --- 雲端進度管理 ---
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

def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

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

# --- 【核心修正 2】：從網址參數初始化狀態 ---
# 如果網址有 book 參數，優先使用網址的
params = st.query_params

if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()

# 決定目前書籍
if "book" in params:
    st.session_state.current_book = params["book"]
elif "current_book" not in st.session_state:
    st.session_state.current_book = None

# 決定目前頁碼 (網址優先 -> 雲端優先 -> 預設 0)
if "page" in params:
    st.session_state.temp_page = int(params["page"])
elif st.session_state.current_book:
    st.session_state.temp_page = st.session_state.global_progress.get(st.session_state.current_book, 0)
else:
    st.session_state.temp_page = 0

# --- 1. 圖書館 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    
    # 確保網址清乾淨
    st.query_params.clear()

    if st.button("🔄 刷新雲端"):
        st.cache_data.clear()
        st.session_state.global_progress = sync_progress_from_cloud()
        st.rerun()

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    files = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
    
    if pdf_files:
        for f in pdf_files:
            c1, c2 = st.columns([0.8, 0.2])
            with c1:
                saved_p = st.session_state.global_progress.get(f['name'], 0)
                if st.button(f"📖 {f['name']} (第 {saved_p + 1} 頁)", key=f['id']):
                    l_path = os.path.join(SAVE_DIR, f['name'])
                    if not os.path.exists(l_path):
                        with st.spinner("下載中..."): download_file(f['id'], l_path)
                    
                    # 更新網址參數並跳轉
                    st.query_params.update({"book": f['name'], "page": saved_p})
                    st.session_state.current_book = f['name']
                    st.session_state.temp_page = saved_p
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    st.rerun()
else:
    # --- 2. 閱讀器 ---
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    
    # 如果本地沒檔案(例如刷新後)，重新下載
    if not os.path.exists(book_path):
        query = f"name = '{book_name}' and '{DRIVE_FOLDER_ID}' in parents"
        res = drive_service.files().list(q=query).execute().get('files', [])
        if res:
            with st.spinner("重新載入書籍..."): download_file(res[0]['id'], book_path)
        else:
            st.query_params.clear()
            st.session_state.current_book = None
            st.rerun()

    doc = fitz.open(book_path)
    total = len(doc)
    
    # 頂部控制
    c1, c2 = st.columns([0.3, 0.7])
    with c1:
        if st.button("❮ 返回"):
            st.query_params.clear()
            st.session_state.current_book = None
            st.rerun()
    with c2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 頁碼跳轉
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
    
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        # 同步更新雲端、Session 和 網址列
        st.session_state.global_progress[book_name] = st.session_state.temp_page
        st.query_params.update({"book": book_name, "page": st.session_state.temp_page})
        save_progress_to_cloud()
        st.rerun()

    st.divider()
    
    img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
    if img_data:
        st.image(img_data, use_column_width=True)
    
    if text_content:
        with st.spinner("朗讀中..."):
            audio = get_audio(text_content)
        if audio:
            st.audio(audio, format="audio/mp3", autoplay=auto_next)

    # 底部按鈕
    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        if st.button("❮ 上一頁") and st.session_state.temp_page > 0:
            st.session_state.temp_page -= 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            st.query_params.update({"book": book_name, "page": st.session_state.temp_page})
            save_progress_to_cloud()
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            st.query_params.update({"book": book_name, "page": st.session_state.temp_page})
            save_progress_to_cloud()
            st.rerun()
