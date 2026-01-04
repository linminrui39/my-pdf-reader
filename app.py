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

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" # <--- 請務必確認填寫正確
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json" # 單一總表檔案
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2

os.makedirs(SAVE_DIR, exist_ok=True)

@st.cache_resource
def get_drive_service():
    try:
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds)
    except: pass
    return None

drive_service = get_drive_service()

# --- 【核心修正：全域進度管理系統】 ---

def sync_progress_from_cloud():
    """從雲端下載最新的進度總表"""
    try:
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        if res:
            content = drive_service.files().get_media(fileId=res[0]['id']).execute()
            return json.loads(content)
    except: pass
    return {}

def save_progress_to_cloud():
    """將目前的進度總表同步回雲端"""
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

# --- 檔案下載功能 ---
def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

# --- 核心功能 (OCR 與圖片) ---
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

# --- UI 介面設定 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# 【初始化】 整個 Session 期間只在最開始抓一次雲端進度
if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()
if "current_book" not in st.session_state:
    st.session_state.current_book = None
if "temp_page" not in st.session_state:
    st.session_state.temp_page = 0

# --- 1. 圖書館模式 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    
    # 強制手動同步按鈕
    if st.button("🔄 刷新雲端清單與進度"):
        st.session_state.global_progress = sync_progress_from_cloud()
        st.rerun()

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    files = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
    
    if pdf_files:
        for f in pdf_files:
            c1, c2 = st.columns([0.8, 0.2])
            with c1:
                # 取得該書的進度，若無則為 0
                saved_page = st.session_state.global_progress.get(f['name'], 0)
                if st.button(f"📖 {f['name']} (上次讀到第 {saved_page + 1} 頁)", key=f['id']):
                    l_path = os.path.join(SAVE_DIR, f['name'])
                    if not os.path.exists(l_path):
                        with st.spinner("下載書籍中..."): download_file(f['id'], l_path)
                    
                    # 進入閱讀器，使用總表中的頁碼
                    st.session_state.current_book = f['name']
                    st.session_state.temp_page = saved_page
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    # 刪除書籍也一併移除進度
                    if f['name'] in st.session_state.global_progress:
                        del st.session_state.global_progress[f['name']]
                        save_progress_to_cloud()
                    st.rerun()
    st.info("💡 提示：若發現進度不對，請點擊上方的「刷新進度」按鈕。")

# --- 2. 閱讀器模式 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    doc = fitz.open(book_path)
    total = len(doc)
    
    # 頂部導覽
    col_nav1, col_nav2 = st.columns([0.3, 0.7])
    with col_nav1:
        if st.button("❮ 返回圖書館"):
            # 返回前確保最後一次進度被同步
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.session_state.current_book = None
            st.rerun()
    with col_nav2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 頁碼跳轉
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
    
    # 只要頁碼一變動，就更新本地總表並非同步存回雲端
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        st.session_state.global_progress[book_name] = st.session_state.temp_page
        save_progress_to_cloud()
        st.rerun()

    st.divider()
    
    img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
    st.image(img_data, use_container_width=True)
    
    with st.spinner("產生語音中..."):
        audio_bytes = get_audio(text_content)
    if audio_bytes:
        st.audio(audio_bytes, format="audio/mp3", autoplay=auto_next)

    # 底部按鈕
    st.divider()
    b1, b2 = st.columns(2)
    with b1:
        if st.button("❮ 上一頁") and st.session_state.temp_page > 0:
            st.session_state.temp_page -= 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
