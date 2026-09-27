import asyncio
import os
import yt_dlp

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass

def _process_info(ydl, info, out_dir, format_type) -> dict | None:
    if 'entries' in info:
        # playlist / multi-video url
        entries = list(info['entries'])
        if not entries:
            return None
        info = entries[0]
        
    file_path = ydl.prepare_filename(info)
    
    if format_type == "audio" and not file_path.endswith(".mp3"):
        base, _ = os.path.splitext(file_path)
        file_path_mp3 = base + ".mp3"
        if os.path.exists(file_path_mp3):
            file_path = file_path_mp3
            
    if not os.path.exists(file_path):
        # Search directory for downloaded files if name doesn't match perfectly
        files = os.listdir(out_dir)
        if files:
            file_path = os.path.join(out_dir, files[0])
        else:
            return None
            
    return {
        "file_path": file_path,
        "title": info.get("title") or "media_file",
        "ext": os.path.splitext(file_path)[1].replace(".", ""),
        "duration": info.get("duration") or 0,
        "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
    }

def download_media_sync(url: str, out_dir: str, format_type: str = "video") -> dict | None:
    """
    Downloads media from a URL using yt-dlp.
    format_type can be:
      - 'video': tries to get best format or merged mp4
      - 'audio': tries to extract best audio and convert to mp3
    """
    os.makedirs(out_dir, exist_ok=True)
    
    from bot_config import Config

    # Attempt 1: Optimal options
    if format_type == "audio":
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(out_dir, '%(title)s_%(id)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
        }
    else:
        ydl_opts = {
            'format': 'best', # Using 'best' is safe and does not strictly require ffmpeg merging
            'outtmpl': os.path.join(out_dir, '%(title)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }

    # Add proxy if configured
    if Config.PROXY:
        ydl_opts['proxy'] = Config.PROXY

    # Configure YouTube client spoofing and JS runtimes
    ydl_opts['extractor_args'] = {
        'youtube': {
            'player_client': ['android', 'web', 'ios']
        }
    }
    ydl_opts['js_runtimes'] = {'node': {}, 'bun': {}}
        
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                return _process_info(ydl, info, out_dir, format_type)
    except Exception as e:
        print(f"[MediaDownloader] Attempt 1 failed: {e}. Trying fallback...")
        
    # Attempt 2: Fallback options (No postprocessors, format='best')
    fallback_opts = {
        'format': 'best',
        'outtmpl': os.path.join(out_dir, '%(title)s_%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }
    if Config.PROXY:
        fallback_opts['proxy'] = Config.PROXY
    fallback_opts['extractor_args'] = {
        'youtube': {
            'player_client': ['android', 'web', 'ios']
        }
    }
    fallback_opts['js_runtimes'] = {'node': {}, 'bun': {}}

    try:
        with yt_dlp.YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                return _process_info(ydl, info, out_dir, "video")
    except Exception as fe:
        print(f"[MediaDownloader] Fallback attempt failed: {fe}")
        return None

async def download_media_async(url: str, out_dir: str, format_type: str = "video") -> dict | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, download_media_sync, url, out_dir, format_type
    )
