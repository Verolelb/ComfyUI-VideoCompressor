import os
import subprocess
import folder_paths
import torch
import numpy as np
from PIL import Image
import shutil
import datetime
import platform

class VideoCompressorUnified:
    # ... (INPUT_TYPES, etc. ne changent pas) ...
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mode": (["2pass", "fast_crf", "gpu"], {"default": "2pass"}),
                "target_mb": ("FLOAT", {"min": 0.5, "max": 2000.0, "step": 0.5, "default": 8.0}),
                "codec": (["libx264", "libx265", "h264_nvenc", "hevc_nvenc"], {"default": "libx264"}),
            },
            "optional": {
                "images": ("IMAGE",),
                "video_path_in": ("STRING", {"forceInput": True}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 60}),
                "crf": ("INT", {"default": 23, "min": 10, "max": 40}),
                "preset": (["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"], {"default": "fast"}),
            }
        }

    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("video_path_out", "images")
    FUNCTION = "process"
    CATEGORY = "🎥Video Utils"

    def _get_video_duration(self, video_path):
        command = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout)

    def process(self, mode, target_mb, codec, images=None, video_path_in=None, fps=24, crf=23, preset="fast"):
        if images is None and video_path_in is None:
            return ("", torch.empty(0))

        # ... (La logique de correction de codec reste la même) ...
        gpu_codecs = ["h264_nvenc", "hevc_nvenc"]
        cpu_codecs = ["libx264", "libx265"]
        original_codec = codec
        if mode == "gpu":
            if codec not in gpu_codecs:
                codec = "h264_nvenc"
                print(f"Avertissement: Mode 'gpu' sélectionné avec un codec CPU ({original_codec}). Correction auto vers '{codec}'.")
        elif mode in ["fast_crf", "2pass"]:
            if codec in gpu_codecs:
                codec = "libx264"
                print(f"Avertissement: Mode '{mode}' sélectionné avec un codec GPU ({original_codec}). Correction auto vers '{codec}'.")

        temp_dir = os.path.join(folder_paths.get_temp_directory(), "compressor_unified")
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        
        frames_path_pattern = os.path.join(temp_dir, "frame_%05d.png")
        pass_log_file = os.path.join(temp_dir, "ffmpeg_passlog")
        output_images = None
        duration_s = 0

        try:
            # --- ÉTAPE 1: Standardiser l'entrée en une séquence de frames ---
            if images is not None:
                print("Mode: Input depuis le tenseur d'images.")
                output_images = images
                for i, img_tensor in enumerate(images):
                    Image.fromarray((255. * img_tensor.cpu().numpy()).astype(np.uint8)).save(frames_path_pattern % i)
                duration_s = len(images) / fps
            else: # video_path_in est utilisé
                print("Mode: Input depuis un fichier vidéo. Extraction des frames pour nettoyage...")
                if not os.path.exists(video_path_in): raise FileNotFoundError(f"Fichier vidéo non trouvé: {video_path_in}")
                subprocess.run(["ffmpeg", "-i", video_path_in, "-vsync", "vfr", frames_path_pattern], check=True, capture_output=True)
                
                frame_files = sorted([f for f in os.listdir(temp_dir) if f.startswith('frame_') and f.endswith('.png')])
                images_list = [torch.from_numpy(np.array(Image.open(os.path.join(temp_dir, f)).convert("RGB")).astype(np.float32) / 255.0).unsqueeze(0) for f in frame_files]
                if not images_list: return ("", torch.empty(0))
                output_images = torch.cat(images_list, 0)
                # On utilise la durée réelle de la vidéo d'origine pour la précision
                duration_s = self._get_video_duration(video_path_in)

            # --- ÉTAPE 2: Créer une vidéo source "propre", SANS AUDIO ---
            print("Création d'une source vidéo propre (sans audio)...")
            clean_video_source_path = os.path.join(temp_dir, "clean_source.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-framerate", str(fps), "-i", frames_path_pattern,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", # -an = No Audio
                clean_video_source_path
            ], check=True, capture_output=True)

            # --- ÉTAPE 3: Compresser la vidéo PROPRE en utilisant le mode choisi ---
            output_dir = os.path.join(folder_paths.get_output_directory(), "compressed_videos")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"compressed_{timestamp}.mp4"
            final_video_path = os.path.join(output_dir, filename)

            if duration_s <= 0: raise ValueError("Durée de la vidéo nulle ou invalide.")
            
            # La commande de base utilise maintenant la source propre
            base_ffmpeg_cmd = ["ffmpeg", "-y", "-i", clean_video_source_path]

            if mode == "2pass":
                target_size_bits = target_mb * 1024 * 1024 * 8
                audio_bitrate_bits = 128 * 1000
                video_bitrate_bits = (target_size_bits / duration_s) - audio_bitrate_bits
                if video_bitrate_bits < 100 * 1000: video_bitrate_bits = 100 * 1000
                video_bitrate_k = int(video_bitrate_bits / 1000)
                
                null_output = "NUL" if platform.system() == "Windows" else "/dev/null"
                common_args = ["-c:v", codec, "-b:v", f"{video_bitrate_k}k", "-preset", preset, "-threads", "0"]
                
                print("Compression 2-Pass (Passe 1)...")
                subprocess.run(base_ffmpeg_cmd + common_args + ["-pass", "1", "-an", "-f", "mp4", "-passlogfile", pass_log_file, null_output], check=True, capture_output=True)
                
                print("Compression 2-Pass (Passe 2)...")
                subprocess.run(base_ffmpeg_cmd + common_args + ["-pass", "2", "-c:a", "aac", "-b:a", "128k", "-passlogfile", pass_log_file, final_video_path], check=True, capture_output=True)

            elif mode == "fast_crf":
                subprocess.run(base_ffmpeg_cmd + ["-c:v", codec, "-preset", preset, "-crf", str(crf), "-c:a", "aac", "-b:a", "128k", "-threads", "0", final_video_path], check=True, capture_output=True)
            
            elif mode == "gpu":
                subprocess.run(base_ffmpeg_cmd + ["-c:v", codec, "-preset", preset, "-cq", str(crf), "-c:a", "aac", "-b:a", "128k", "-threads", "0", final_video_path], check=True, capture_output=True)

            return (final_video_path, output_images)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)