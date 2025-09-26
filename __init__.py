# On importe le nom correct de la classe : "VideoCompressorUnified"
from .compress_video_node import VideoCompressorUnified

NODE_CLASS_MAPPINGS = {
    # On utilise le même nom ici
    "VideoCompressorUnified": VideoCompressorUnified
}

NODE_DISPLAY_NAME_MAPPINGS = {
    # Et ici, pour le nom affiché dans le menu
    "VideoCompressorUnified": "🎬 Compress Video (Unified)"
}

print("✅ ComfyUI-VideoCompressor: Node unifié chargé.")