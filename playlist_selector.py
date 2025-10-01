import streamlit as st
import os
import tempfile
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
from yt_dlp.utils import sanitize_filename
from io import BytesIO
import zipfile
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_playlist_entries(playlist_url, cookies_file=None):
    """Fetch video IDs and titles from a YouTube playlist."""
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'cookiefile': cookies_file,
    }
    with YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(playlist_url, download=False)
        entries = result.get('entries', [])
        video_ids = [entry.get('id') for entry in entries if entry.get('id')]
        titles = [entry.get('title', f'video_{i+1}') for i, entry in enumerate(entries) if entry.get('id')]
        playlist_title = result.get('title', 'playlist_subtitles')
        return list(zip(video_ids, titles)), playlist_title

def download_video_with_quality(video_url, quality, temp_dir, cookies_file=None):
    """Download single video with selected quality using yt-dlp."""
    ydl_opts = {
        'format': quality,  # e.g., 'best', 'best[height<=720]', 'bestaudio'
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'cookiefile': cookies_file,
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            title = info.get('title', 'video')
            files = [f for f in os.listdir(temp_dir) if title in f]
            if files:
                video_path = os.path.join(temp_dir, files[0])
                with open(video_path, 'rb') as f:
                    video_data = f.read()
                os.remove(video_path)
                return video_data, f"{sanitize_filename(title)[:150]}.%(ext)s"  # ext from info
            raise ValueError("Download failed")
        except Exception as e:
            raise ValueError(f"Video download error: {str(e)}")

def create_video_zip(video_files, subtitle_files, title):
    """Create ZIP with videos + matching subtitles."""
    zip_buffer = BytesIO()
    safe_title = sanitize_filename(title)[:150]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for (vid_title, vid_data, vid_filename), (sub_title, sub_text) in zip(video_files, subtitle_files):
            zipf.writestr(vid_filename, vid_data)
            sub_filename = f"{sanitize_filename(sub_title)[:150]}.srt"  # Assume SRT; adjust as needed
            zipf.writestr(sub_filename, sub_text.encode('utf-8'))
    zip_buffer.seek(0)
    return zip_buffer, f"{safe_title}_videos_subs.zip"

def enhanced_playlist_handler(
    url, cookies_file=None, download_videos=False, quality='best',
    format_choice='srt', target_lang='en', clean_transcript=True,
    get_transcript_api=None, get_subtitles_yt_dlp=None, download_sub_func=None
):
    """
    Enhanced handler for playlists/videos: Select, download subs/videos, return files.
    
    Args:
        url (str): Video or playlist URL.
        cookies_file (str): Cookies path.
        download_videos (bool): If True, download videos too.
        quality (str): yt-dlp format selector.
        format_choice (str): Sub format (srt/vtt/txt).
        target_lang (str): Language.
        clean_transcript (bool): Clean subs.
        get_transcript_api, get_subtitles_yt_dlp, download_sub_func: Original functions from app.py.
    
    Returns:
        selected_entries (list): [(id, title), ...]
        title (str): Playlist/video title.
        subtitle_files (list): [(title, text), ...]
        video_files (list): [(title, data, filename), ...] if download_videos else []
    """
    is_playlist = 'playlist' in url or 'list=' in url
    video_url = url if not is_playlist else None
    
    with st.spinner("Fetching content..."):
        if is_playlist:
            entries, title = get_playlist_entries(url, cookies_file)
            if not entries:
                st.error("No videos found.")
                return [], title, [], []
            
            st.subheader(f"Playlist: {title} ({len(entries)} videos)")
            titles = [t for _, t in entries]
            selected_titles = st.multiselect(
                "Select videos (all default):", titles, default=titles,
                help="Ctrl/Cmd for multiple."
            )
            if not selected_titles:
                st.warning("No selection.")
                return [], title, [], []
            selected_entries = [entries[titles.index(t)] for t in selected_titles]
            st.success(f"Selected {len(selected_entries)} videos.")
        else:
            # Single video
            video_id = parse_qs(urlparse(url).query).get('v', [None])[0]
            if not video_id:
                st.error("Invalid video URL.")
                return [], "Single Video", [], []
            with YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'video')
            selected_entries = [(video_id, title)]
            st.subheader(f"Single Video: {title}")
    
    # Download loop with progress
    subtitle_files = []
    video_files = []
    total = len(selected_entries)
    progress_bar = st.progress(0)
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, (vid_id, vid_title) in enumerate(selected_entries):
            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            try:
                # Subs (using original funcs)
                try:
                    sub_text, _, _ = get_transcript_api(vid_id, format_choice, target_lang)
                except:
                    sub_text, _, _ = get_subtitles_yt_dlp(vid_url, format_choice, cookies_file, temp_dir, target_lang)
                if clean_transcript:
                    sub_text = clean_subtitle_text(sub_text)  # Assume you pass this func
                if format_choice == 'txt':
                    sub_text = convert_srt_to_txt(sub_text)  # Assume passed
                subtitle_files.append((vid_title, sub_text))
                
                # Videos if enabled
                if download_videos:
                    vid_data, vid_filename = download_video_with_quality(vid_url, quality, temp_dir, cookies_file)
                    video_files.append((vid_title, vid_data, vid_filename))
                    st.info(f"Downloaded video + sub for '{vid_title}' (quality: {quality})")
                else:
                    st.info(f"Downloaded sub for '{vid_title}'")
                
            except Exception as e:
                st.warning(f"Error for '{vid_title}': {str(e)}")
            
            progress_bar.progress((i + 1) / total)
    
    if not subtitle_files:
        st.error("No files downloaded.")
    
    return selected_entries, title, subtitle_files, video_files
