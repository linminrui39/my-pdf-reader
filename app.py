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

# --- [1] 初始化設定 (必須在最前面) ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# --- [2] 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" # <--- 請填入您的 ID
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2

os.makedirs(SAVE_DIR, exist_ok=True)

# --- [3] Google Drive 服務 ---
@st.cache_resource(ttl=3600)
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except: return None
    return None

drive_service = get_drive_service()

# --- [4] 進度管理系統 ---
def sync_progress_from_cloud():
    """從雲端下載進度總表"""
    if not drive_service: return {}
    try:
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        if res:
            file_id = res[0]['id']
            content = drive_service.files().get_media(fileId=file_id).execute()
            if content:
                return json.loads(content)
    except: pass
    return {}

def save_progress_to_cloud():
    """儲存當前所有書本進度到雲端"""
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

# --- [5] 核心渲染與預讀功能 ---
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

def background_prefetch(book_path, current_page, total_pages):
    """背景預讀執行緒：偷偷載入後面的頁面到快取"""
    def prefetch_worker():
        for i in range(1, PREFETCH_COUNT + 1):
            target = current_page + i
            if target < total_pages:
                _ = get_page_content(book_path, target)
    threading.Thread(target=prefetch_worker, daemon=True).start()

# --- [6] 檔案下載 ---
def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    with open(local_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

# --- [7] 語音生成 ---
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
# [8] 主程式邏輯
# ---------------------------------------------------------

# 1. 初始進度抓取
if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()

# 2. 初始化書本狀態
if "current_book" not in st.session_state:
    st.session_state.current_book = None
if "temp_page" not in st.session_state:
    st.session_state.temp_page = 0

# --- A. 圖書館介面 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    
    # 刷新按鈕
    if st.button("🔄 刷新雲端清單與進度"):
        st.cache_data.clear()
        st.session_state.global_progress = sync_progress_from_cloud()
        st.rerun()

    if not drive_service:
        st.error("無法連連 Google Drive，請檢查 Secrets。")
    else:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
        files = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        pdf_files = [x for x in files if x['name'].lower().endswith('.pdf')]
        
        if pdf_files:
            for f in pdf_files:
                # 關鍵：直接從雲端總表讀取進度顯示
                saved_p = st.session_state.global_progress.get(f['name'], 0)
                col1, col2 = st.columns([0.8, 0.2])
                with col1:
                    if st.button(f"📖 {f['name']} (讀至第 {saved_p + 1} 頁)", key=f['id']):
                        l_path = os.path.join(SAVE_DIR, f['name'])
                        if not os.path.exists(l_path):
                            with st.spinner("首次下載書籍中..."): download_file(f['id'], l_path)
                        
                        st.session_state.current_book = f['name']
                        st.session_state.temp_page = saved_p
                        st.rerun()
                with col2:
                    if st.button("🗑️", key=f"del_{f['id']}"):
                        drive_service.files().delete(fileId=f['id']).execute()
                        st.rerun()
        else:
            st.info("資料夾內無 PDF，請放入檔案後按刷新。")

# --- B. 閱讀器介面 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)

    if os.path.exists(book_path):
        doc = fitz.open(book_path)
        total = len(doc)

        # 頂部控制
        c1, c2 = st.columns([0.3, 0.7])
        with c1:
            if st.button("❮ 返回書庫"):
                st.session_state.current_book = None
                st.rerun()
        with c2:
            auto_next = st.toggle("自動播放語音", value=True)

        # 頁碼輸入
        t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
        if t_page - 1 != st.session_state.temp_page:
            st.session_state.temp_page = t_page - 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud() # 跳頁時立即儲存
            st.rerun()

        st.divider()
        
        # 顯示當前頁
        img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
        if img_data:
            st.image(img_data, use_column_width=True)
            # 背景預讀後續頁面
            background_prefetch(book_path, st.session_state.temp_page, total)
        
        if text_content:
            with st.spinner("語音載入中..."):
                audio = get_audio(text_content)
            if audio:
                st.audio(audio, format="audio/mp3", autoplay=auto_next)

        # 底部翻頁
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


