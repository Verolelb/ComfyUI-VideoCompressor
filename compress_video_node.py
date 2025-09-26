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
    # ... (le début de la classe ne change pas) ...
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "mode": (["fast_crf", "2pass", "gpu"], {"default": "2pass"}),
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

    def _get_video_info(self, video_path):
        command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate,duration", "-of", "csv=p=0", video_path]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        parts = result.stdout.strip().split(',')
        if len(parts) < 2: raise ValueError("Impossible d'analyser la vidéo.")
        fps_fraction = parts[0].split('/')
        fps = float(fps_fraction[0]) / float(fps_fraction[1]) if len(fps_fraction) == 2 else 30.0
        duration = float(parts[1])
        return {"duration": duration, "fps": fps}

    def _load_video_to_tensor(self, video_path, temp_dir):
        frames_dir = os.path.join(temp_dir, "loaded_frames")
        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(["ffmpeg", "-i", video_path, os.path.join(frames_dir, "frame_%05d.png")], check=True, capture_output=True)
        frame_files = sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir) if f.endswith('.png')])
        images = [torch.from_numpy(np.array(Image.open(f).convert("RGB")).astype(np.float32) / 255.0).unsqueeze(0) for f in frame_files]
        if not images: return torch.empty(0)
        return torch.cat(images, 0)

    def process(self, mode, target_mb, codec, images=None, video_path_in=None, fps=24, crf=23, preset="fast"):
        if images is None and video_path_in is None:
            return ("", torch.empty(0))

        gpu_codecs = ["h264_nvenc", "hevc_nvenc"]
        cpu_codecs = ["libx264", "libx265"]
        original_codec = codec
        
        if mode == "gpu":
            if codec not in gpu_codecs:
                codec = "h264_nvenc"
                print(f"Avertissement : Le mode 'gpu' a été sélectionné avec un codec CPU ({original_codec}). Correction automatique vers '{codec}'.")
        elif mode in ["fast_crf", "2pass"]:
            if codec in gpu_codecs:
                codec = "libx264"
                print(f"Avertissement : Le mode '{mode}' a été sélectionné avec un codec GPU ({original_codec}). Correction automatique vers '{codec}'.")
        
        temp_dir = os.path.join(folder_paths.get_temp_directory(), "compressor_unified")
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)

        pass_log_file = os.path.join(temp_dir, "ffmpeg_passlog")

        try:
            if images is not None:
                output_images = images
                for i, img_tensor in enumerate(images):
                    Image.fromarray((255. * img_tensor.cpu().numpy()).astype(np.uint8)).save(os.path.join(temp_dir, f"frame_{i:05d}.png"))
                initial_video_path = os.path.join(temp_dir, "initial_from_images.mp4")
                
                # --- LA CORRECTION EST ICI ---
                # "yuv4s20p" a été remplacé par "yuv420p"
                subprocess.run(["ffmpeg", "-y", "-framerate", str(fps), "-i", os.path.join(temp_dir, "frame_%05d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", initial_video_path], check=True, capture_output=True)
            else:
                if not os.path.exists(video_path_in): raise FileNotFoundError(f"Fichier vidéo non trouvé: {video_path_in}")
                initial_video_path = video_path_in
                output_images = self._load_video_to_tensor(video_path_in, temp_dir)

            output_dir = os.path.join(folder_paths.get_output_directory(), "compressed_videos")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"compressed_{timestamp}.mp4"
            final_video_path = os.path.join(output_dir, filename)

            info = self._get_video_info(initial_video_path)
            duration_s = info["duration"]
            if duration_s <= 0: raise ValueError("Durée de la vidéo nulle ou invalide.")

            if mode == "2pass":
                target_size_bits = target_mb * 1024 * 1024 * 8
                audio_bitrate_bits = 128 * 1000
                video_bitrate_bits = (target_size_bits / duration_s) - audio_bitrate_bits
                if video_bitrate_bits < 100 * 1000: video_bitrate_bits = 100 * 1000
                video_bitrate_k = int(video_bitrate_bits / 1000)
                null_output = "NUL" if platform.system() == "Windows" else "/dev/null"
                common_args = ["-y", "-i", initial_video_path, "-c:v", codec, "-b:v", f"{video_bitrate_k}k", "-preset", preset, "-threads", "0"]
                subprocess.run(["ffmpeg"] + common_args + ["-pass", "1", "-an", "-f", "mp4", "-passlogfile", pass_log_file, null_output], check=True, capture_output=True)
                subprocess.run(["ffmpeg"] + common_args + ["-pass", "2", "-c:a", "aac", "-b:a", "128k", "-passlogfile", pass_log_file, final_video_path], check=True, capture_output=True)
            elif mode == "fast_crf":
                subprocess.run(["ffmpeg", "-y", "-i", initial_video_path, "-c:v", codec, "-preset", preset, "-crf", str(crf), "-c:a", "aac", "-b:a", "128k", "-threads", "0", final_video_path], check=True, capture_output=True)
            elif mode == "gpu":
                subprocess.run(["ffmpeg", "-y", "-i", initial_video_path, "-c:v", codec, "-preset", preset, "-cq", str(crf), "-c:a", "aac", "-b:a", "128k", "-threads", "0", final_video_path], check=True, capture_output=True)
            else:
                raise ValueError(f"Mode inconnu: {mode}")
            return (final_video_path, output_images)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)