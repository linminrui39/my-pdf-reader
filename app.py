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
# 請確保這裡填寫的是您的資料夾 ID
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN"
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
PROGRESS_FILE = "drive_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2

os.makedirs(SAVE_DIR, exist_ok=True)

# --- Google Drive 服務初始化 ---
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

# --- 雲端檔案同步功能 ---
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

def upload_file(local_path, filename):
    try:
        file_metadata = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(local_path, mimetype='application/pdf')
        drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception as e:
        if "storageQuotaExceeded" in str(e):
            st.warning("⚠️ 因 Google 空間限制，請直接將 PDF 丟進雲端硬碟，App 會自動抓取。")
        else:
            st.error(f"上傳失敗: {e}")
        return False

# --- 進度儲存 (修正 TypeError 關鍵區塊) ---
def load_remote_progress():
    try:
        query = f"name = '{PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents"
        res = drive_service.files().list(q=query).execute().get('files', [])
        if res:
            request = drive_service.files().get_media(fileId=res[0]['id'])
            return json.loads(request.execute())
    except: pass
    return {}

def save_remote_progress(book_name, page_num):
    try:
        data = load_remote_progress()
        data[book_name] = page_num
        content = json.dumps(data).encode('utf-8')
        
        query = f"name = '{PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents"
        res = drive_service.files().list(q=query).execute().get('files', [])
        
        # 修正：使用 MediaIoBaseUpload 處理記憶體中的 Bytes，防止 TypeError
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/json', resumable=True)
        
        if res:
            drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
        else:
            meta = {'name': PROGRESS_FILE, 'parents': [DRIVE_FOLDER_ID]}
            drive_service.files().create(body=meta, media_body=media).execute()
    except:
        # 即使同步失敗也不要跳出紅色報錯，背景處理即可
        pass

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

# --- 背景預讀 ---
def background_prefetch(book_path, current_page, total_pages):
    def prefetch_worker():
        for i in range(1, PREFETCH_COUNT + 1):
            target = current_page + i
            if target < total_pages:
                _ = get_page_content(book_path, target)
    threading.Thread(target=prefetch_worker, daemon=True).start()

# --- UI 介面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

if st.session_state.get("current_book") is None:
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
                        with st.spinner("同步中..."): download_file(f['id'], l_path)
                    st.session_state.current_book = f['name']
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    st.rerun()
    st.divider()
    up = st.file_uploader("匯入新 PDF", type="pdf")
    if up:
        l_path = os.path.join(SAVE_DIR, up.name)
        with open(l_path, "wb") as f: f.write(up.getbuffer())
        if upload_file(l_path, up.name):
            st.session_state.current_book = up.name
            st.rerun()
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    doc = fitz.open(book_path)
    total = len(doc)
    
    if "temp_page" not in st.session_state:
        st.session_state.temp_page = load_remote_progress().get(book_name, 0)

    # 頂部控制
    col_nav1, col_nav2 = st.columns([0.3, 0.7])
    with col_nav1:
        if st.button("❮ 返回"):
            st.session_state.current_book = None
            st.rerun()
    with col_nav2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 跳轉按鈕在上方 (優先顯示)
    t_page = st.number_input(f"頁碼 / 共 {total} 頁", 1, total, st.session_state.temp_page + 1)
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        save_remote_progress(book_name, st.session_state.temp_page)
        st.rerun()

    st.divider()
    
    # 內容顯示
    img, txt = get_page_content(book_path, st.session_state.temp_page)
    st.image(img, use_container_width=True)
    
    with st.spinner("產生語音中..."):
        audio = get_audio(txt)
    if audio:
        st.audio(audio, format="audio/mp3", autoplay=auto_next)

    # 觸發背景預讀
    background_prefetch(book_path, st.session_state.temp_page, total)

    # 底部按鈕
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

