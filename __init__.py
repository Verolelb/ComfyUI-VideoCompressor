# Fichier : __init__.py

from .compress_video_node import VideoCompressor

NODE_CLASS_MAPPINGS = {
    "VideoCompressor": VideoCompressor
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VideoCompressor": "🎬 Compress Video"
}

print("✅ ComfyUI-VideoCompressor: Node chargé avec succès.")