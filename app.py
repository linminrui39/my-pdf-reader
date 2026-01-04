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
    except Exception as e:
        st.error(f"Google Drive 初始化失敗: {e}")
    return None

drive_service = get_drive_service()

# --- 強化版進度系統 ---

def get_prog_filename(book_name):
    # 移除副檔名並只留字母數字，確保進度檔名穩定
    clean_name = "".join([c for c in book_name.replace(".pdf", "") if c.isalnum()])
    return f"p_{clean_name}.json"

def load_book_progress(book_name):
    try:
        filename = get_prog_filename(book_name)
        # 直接精確搜尋檔名
        query = f"name = '{filename}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        if res:
            file_id = res[0]['id']
            content = drive_service.files().get_media(fileId=file_id).execute()
            prog_data = json.loads(content)
            return int(prog_data.get("page", 0))
    except Exception as e:
        print(f"讀取進度失敗: {e}")
    return 0

def save_book_progress(book_name, page_num):
    try:
        filename = get_prog_filename(book_name)
        content = json.dumps({"page": int(page_num)}).encode('utf-8')
        
        query = f"name = '{filename}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        
        # 進度檔很小，不使用 resumable=True 以求即時寫入
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/json')
        
        if res:
            drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
        else:
            meta = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
            drive_service.files().create(body=meta, media_body=media).execute()
    except Exception as e:
        print(f"儲存進度失敗: {e}")

# --- 檔案下載 ---
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

# --- UI 介面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# 初始化 Session State (確保重新整理時也能從正確位置開始)
if "current_book" not in st.session_state:
    st.session_state.current_book = None
if "temp_page" not in st.session_state:
    st.session_state.temp_page = 0

# --- 1. 圖書館模式 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    files = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
    
    if pdf_files:
        for f in pdf_files:
            c1, c2 = st.columns([0.8, 0.2])
            with c1:
                if st.button(f"📖 {f['name']}", key=f['id']):
                    l_path = os.path.join(SAVE_DIR, f['name'])
                    if not os.path.exists(l_path):
                        with st.spinner("首次閱讀，下載書籍中..."):
                            download_file(f['id'], l_path)
                    
                    # 【核心修正】：點入書籍時，強制去雲端抓進度
                    st.session_state.current_book = f['name']
                    st.session_state.temp_page = load_book_progress(f['name'])
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    st.rerun()
    st.info("💡 提示：請直接在 Google Drive 丟入 PDF，然後刷新此頁。")

# --- 2. 閱讀器模式 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    
    # 防止檔案意外消失
    if not os.path.exists(book_path):
        st.session_state.current_book = None
        st.rerun()
        
    doc = fitz.open(book_path)
    total = len(doc)
    
    # 頂部控制
    col_nav1, col_nav2 = st.columns([0.3, 0.7])
    with col_nav1:
        if st.button("❮ 返回"):
            st.session_state.current_book = None
            st.rerun()
    with col_nav2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 頁碼跳轉
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
    
    # 判斷頁碼是否有變動 (手動輸入跳轉)
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        save_book_progress(book_name, st.session_state.temp_page)
        st.rerun()

    st.divider()
    
    # 顯示圖片與朗讀
    img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
    st.image(img_data, use_container_width=True)
    
    with st.spinner("產生語音中..."):
        audio_bytes = get_audio(text_content)
    if audio_bytes:
        st.audio(audio_bytes, format="audio/mp3", autoplay=auto_next)

    # 底部導覽
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

