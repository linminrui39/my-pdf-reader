%%writefile app.py
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

# --- 關鍵修正：指定雲端 Linux 的 Tesseract 路徑 ---
pytesseract.pytesseract.tesseract_cmd = r'/usr/bin/tesseract'

SAVE_DIR = "my_books"
PROGRESS_FILE = "progress.json"
VOICE = "zh-TW-HsiaoChenNeural"
SPEED = "+10%"

# 確保資料夾存在
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR, exist_ok=True)

@st.cache_data(show_spinner=False)
def get_page_image(book_path, page_num):
    try:
        doc = fitz.open(book_path)
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        return pix.tobytes("png")
    except:
        return None

@st.cache_data(show_spinner=False)
def get_processed_text(book_path, page_num):
    try:
        doc = fitz.open(book_path)
        page = doc[page_num]
        text = page.get_text().strip()
        if not text:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            # 強制指定繁體中文與英文
            text = pytesseract.image_to_string(img, lang='chi_tra+eng')
        return text.replace('\n', ' ')
    except:
        return ""

@st.cache_data(show_spinner=False)
def get_cached_audio(text):
    if not text or not text.strip(): return None
    async def generate():
        c = edge_tts.Communicate(text, VOICE, rate=SPEED)
        data = b""
        async for chunk in c.stream():
            if chunk["type"] == "audio": data += chunk["data"]
        return data
    try:
        return asyncio.run(generate())
    except:
        return None

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_progress(book_name, page_num):
    data = load_progress()
    data[book_name] = page_num
    with open(PROGRESS_FILE, "w") as f: json.dump(data, f)

# --- UI 介面 ---
st.set_page_config(page_title="專業雲端閱讀器", layout="centered")
st.markdown("<style>.stApp { background-color: white; } .stButton>button { border: 1px solid black !important; border-radius: 4px !important; background-color: white !important; color: black !important; }</style>", unsafe_allow_html=True)

if "current_book" not in st.session_state:
    st.session_state.current_book = None

existing_books = [f for f in os.listdir(SAVE_DIR) if f.endswith(".pdf")]

# 1. 圖書館模式
if st.session_state.current_book is None:
    st.title("📚 我的雲端書庫")
    if existing_books:
        for book in existing_books:
            col_b1, col_b2 = st.columns([0.8, 0.2])
            with col_b1:
                if st.button(f"📖 繼續閱讀：{book}", key=book):
                    st.session_state.current_book = book
                    st.rerun()
            with col_b2:
                if st.button("🗑️", key=f"del_{book}"):
                    os.remove(os.path.join(SAVE_DIR, book))
                    st.rerun()
    st.divider()
    uploaded_file = st.file_uploader("匯入新 PDF", type="pdf")
    if uploaded_file:
        with open(os.path.join(SAVE_DIR, uploaded_file.name), "wb") as f:
            f.write(uploaded_file.getbuffer())
        st.session_state.current_book = uploaded_file.name
        st.rerun()

# 2. 閱讀器模式
else:
    book_name = st.session_state.current_book
    book_path = os.path.join(SAVE_DIR, book_name)
    try:
        doc_info = fitz.open(book_path)
        total_pages = len(doc_info)
        doc_info.close()
        
        if "temp_page" not in st.session_state:
            st.session_state.temp_page = load_progress().get(book_name, 0)

        # 頂部控制
        c1, c2 = st.columns([0.4, 0.6])
        with c1:
            if st.button("❮ 返回"):
                st.session_state.current_book = None
                st.rerun()
        with c2:
            auto_next = st.toggle("自動翻頁", value=False)

        # 跳轉頁面
        col_j1, col_j2 = st.columns([0.6, 0.4])
        with col_j1:
            target_page = st.number_input(f"頁碼 (共 {total_pages} 頁)", min_value=1, max_value=total_pages, value=st.session_state.temp_page + 1)
        with col_j2:
            if st.button("🚀 跳轉"):
                st.session_state.temp_page = target_page - 1
                save_progress(book_name, st.session_state.temp_page)
                st.rerun()

        st.image(get_page_image(book_path, st.session_state.temp_page), use_container_width=True)
        
        with st.spinner("載入中..."):
            current_text = get_processed_text(book_path, st.session_state.temp_page)
            audio_data = get_cached_audio(current_text)
        
        if audio_data:
            st.audio(audio_data, format="audio/mp3", autoplay=auto_next)
        
        # 底部翻頁
        st.divider()
        b1, b2 = st.columns(2)
        with b1:
            if st.button("❮ 上一頁") and st.session_state.temp_page > 0:
                st.session_state.temp_page -= 1
                save_progress(book_name, st.session_state.temp_page)
                st.rerun()
        with b2:
            if st.button("下一頁 ❯") and st.session_state.temp_page < total_pages - 1:
                st.session_state.temp_page += 1
                save_progress(book_name, st.session_state.temp_page)
                st.rerun()
    except Exception as e:
        st.error(f"讀取書籍出錯: {e}")
        if st.button("返回重試"):
            st.session_state.current_book = None
            st.rerun()
