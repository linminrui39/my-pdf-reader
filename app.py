import streamlit as st  # 必須是第一個指令
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

# --- 【關鍵修正 1】：這行必須放在所有 st 指令的最前面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# --- 配置區 ---
# 請在此填入您的 Google Drive 資料夾 ID
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN" 

# Tesseract 於 Streamlit Cloud Linux 的標準路徑
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"
PREFETCH_COUNT = 2

os.makedirs(SAVE_DIR, exist_ok=True)

# --- Google Drive 服務初始化 ---
@st.cache_resource
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            st.error(f"Google 憑證解析失敗: {e}")
    return None

drive_service = get_drive_service()

# --- 【核心修正 2】：全域進度同步系統 ---

def sync_progress_from_cloud():
    """從雲端強制抓取最新進度總表"""
    if not drive_service: return {}
    try:
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        if res:
            file_id = res[0]['id']
            # 強制跳過快取下載最新內容
            content = drive_service.files().get_media(fileId=file_id).execute()
            if content:
                return json.loads(content)
    except Exception as e:
        st.sidebar.warning(f"進度載入稍有延遲: {e}")
    return {}

def save_progress_to_cloud():
    """儲存進度，失敗時顯示錯誤 (解決 0GB 空間問題)"""
    if not drive_service: return
    try:
        data = st.session_state.global_progress
        content = json.dumps(data).encode('utf-8')
        
        query = f"name = '{MASTER_PROGRESS_FILE}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/json')
        
        if res:
            # 檔案已存在，更新內容
            drive_service.files().update(fileId=res[0]['id'], media_body=media).execute()
        else:
            # 檔案不存在，建立新檔
            meta = {'name': MASTER_PROGRESS_FILE, 'parents': [DRIVE_FOLDER_ID]}
            drive_service.files().create(body=meta, media_body=media).execute()
    except Exception as e:
        st.error(f"🚨 雲端儲存失敗！若重複出現，請在 Google Drive 手動建立一個名為 {MASTER_PROGRESS_FILE} 的空白檔案。原因：{e}")

# --- 檔案下載 ---
def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

# --- 內容處理 (OCR & 圖片) ---
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

# --- 初始化 Session State ---
if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()
if "current_book" not in st.session_state:
    st.session_state.current_book = None
if "temp_page" not in st.session_state:
    st.session_state.temp_page = 0

# --- 1. 圖書館模式 ---
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    
    # 頂部控制列
    col_ctrl1, col_ctrl2 = st.columns([0.7, 0.3])
    with col_ctrl1:
        if st.button("🔄 刷新雲端清單與進度"):
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
                # 顯示儲存在總表中的進度
                saved_page = st.session_state.global_progress.get(f['name'], 0)
                if st.button(f"📖 {f['name']} (讀至第 {saved_page + 1} 頁)", key=f['id']):
                    l_path = os.path.join(SAVE_DIR, f['name'])
                    if not os.path.exists(l_path):
                        with st.spinner("首次閱讀，下載書籍中..."):
                            download_file(f['id'], l_path)
                    
                    st.session_state.current_book = f['name']
                    st.session_state.temp_page = saved_page
                    st.rerun()
            with c2:
                if st.button("🗑️", key=f"del_{f['id']}"):
                    drive_service.files().delete(fileId=f['id']).execute()
                    st.rerun()
    else:
        st.write("目前資料夾中沒有 PDF 檔案。")
    
# --- 2. 閱讀器模式 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    
    # 確保檔案存在
    if not os.path.exists(book_path):
        st.session_state.current_book = None
        st.rerun()

    doc = fitz.open(book_path)
    total = len(doc)
    
    # 頂部導覽
    col_nav1, col_nav2 = st.columns([0.3, 0.7])
    with col_nav1:
        if st.button("❮ 返回"):
            # 返回前最後儲存一次
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.session_state.current_book = None
            st.rerun()
    with col_nav2:
        auto_next = st.toggle("自動翻頁", value=False)

    # 頁碼跳轉
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
    
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        st.session_state.global_progress[book_name] = st.session_state.temp_page
        save_progress_to_cloud()
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
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
