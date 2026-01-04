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

# --- 【功能 1】：設定必須放在首行，解決崩潰問題 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")

# --- 配置區 ---
DRIVE_FOLDER_ID = "1_vHNLHwMNT-mzSJSH5QCS5f5UGxgacGN"  # <--- 請務必填寫正確的 ID
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'
SAVE_DIR = "temp_books"
MASTER_PROGRESS_FILE = "all_books_progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"

os.makedirs(SAVE_DIR, exist_ok=True)

# --- 【功能 2】：穩定版 Google API 連線 ---
@st.cache_resource(ttl=3600)
def get_drive_service():
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
            creds = service_account.Credentials.from_service_account_info(info)
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except Exception as e:
            st.error(f"Google 認證失敗: {e}")
    return None

drive_service = get_drive_service()

# --- 【功能 3】：強化版進度儲存與讀取 (解決歸零與混淆) ---
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
    """儲存進度，採非同步概念避免卡頓"""
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
    except Exception as e:
        print(f"背景儲存延遲: {e}")

# --- 檔案下載功能 ---
def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

# --- 【功能 4】：中英 OCR 與頁面渲染 ---
@st.cache_data(show_spinner=False)
def get_page_content(book_path, page_num):
    try:
        doc = fitz.open(book_path)
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        img_bytes = pix.tobytes("png")
        text = page.get_text().strip()
        if not text: # 如果沒文字，啟動 Tesseract OCR
            text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)), lang='chi_tra+eng')
        doc.close()
        return img_bytes, text.replace('\n', ' ')
    except: return None, ""

# --- 【功能 5】：語音朗讀 (Edge-TTS) ---
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
# 主邏輯與網址紀錄 (Query Params)
# ---------------------------------------------------------

# 初始化進度
if "global_progress" not in st.session_state:
    st.session_state.global_progress = sync_progress_from_cloud()

# 偵測網址參數，解決刷新歸零問題
params = st.query_params
url_book = params.get("book")
url_page = int(params.get("page")) if params.get("page") else None

# 設定目前書本
if url_book:
    st.session_state.current_book = url_book
elif "current_book" not in st.session_state:
    st.session_state.current_book = None

# 設定目前頁碼
if url_page is not None:
    st.session_state.temp_page = url_page
elif st.session_state.current_book:
    st.session_state.temp_page = st.session_state.global_progress.get(st.session_state.current_book, 0)
else:
    st.session_state.temp_page = 0

# --- 圖書館介面 ---
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
        
        if pdf_files:
            for f in pdf_files:
                saved_p = st.session_state.global_progress.get(f['name'], 0)
                col1, col2 = st.columns([0.8, 0.2])
                with col1:
                    if st.button(f"📖 {f['name']} (讀至第 {saved_p + 1} 頁)", key=f['id']):
                        l_path = os.path.join(SAVE_DIR, f['name'])
                        if not os.path.exists(l_path):
                            with st.spinner("首次閱讀，下載中..."): download_file(f['id'], l_path)
                        
                        # 寫入網址紀錄並跳轉
                        st.query_params["book"] = f['name']
                        st.query_params["page"] = saved_p
                        st.session_state.current_book = f['name']
                        st.session_state.temp_page = saved_p
                        st.rerun()
                with col2:
                    if st.button("🗑️", key=f"del_{f['id']}"):
                        drive_service.files().delete(fileId=f['id']).execute()
                        st.rerun()
        else:
            st.info("請將 PDF 放入 Google Drive 後點擊刷新。")
    except Exception as e:
        st.error(f"連線失敗: {e}")

# --- 閱讀器介面 ---
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)

    # 如果刷新後本地檔案消失，自動重新下載
    if not os.path.exists(book_path):
        with st.spinner("重新連線書籍..."):
            q = f"name = '{book_name}' and '{DRIVE_FOLDER_ID}' in parents"
            res = drive_service.files().list(q=q).execute().get('files', [])
            if res: download_file(res[0]['id'], book_path)
            else:
                st.query_params.clear()
                st.rerun()

    doc = fitz.open(book_path)
    total = len(doc)

    # 頂部控制
    c1, c2 = st.columns([0.3, 0.7])
    with c1:
        if st.button("❮ 返回圖書館"):
            st.query_params.clear()
            st.session_state.current_book = None
            st.rerun()
    with c2:
        auto_next = st.toggle("自動播放語音", value=True)

    # 【核心功能】：跳頁控制
    t_page = st.number_input(f"頁碼 (1-{total})", 1, total, value=st.session_state.temp_page + 1)
    
    if t_page - 1 != st.session_state.temp_page:
        st.session_state.temp_page = t_page - 1
        st.session_state.global_progress[book_name] = st.session_state.temp_page
        st.query_params["page"] = st.session_state.temp_page
        save_progress_to_cloud() # 同步到雲端 JSON
        st.rerun()

    st.divider()
    
    # 顯示內容
    img_data, text_content = get_page_content(book_path, st.session_state.temp_page)
    if img_data:
        st.image(img_data, use_column_width=True)
    
    if text_content:
        with st.spinner("語音載入中..."):
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
            st.query_params["page"] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
    with b2:
        if st.button("下一頁 ❯") and st.session_state.temp_page < total - 1:
            st.session_state.temp_page += 1
            st.session_state.global_progress[book_name] = st.session_state.temp_page
            st.query_params["page"] = st.session_state.temp_page
            save_progress_to_cloud()
            st.rerun()
