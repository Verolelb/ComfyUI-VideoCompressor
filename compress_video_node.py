import os
import subprocess
import folder_paths
import torch
import numpy as np
from PIL import Image
import shutil
import datetime
import platform
# NOUVEL IMPORT OBLIGATOIRE
from scipy.io.wavfile import write as write_wav

class VideoCompressor:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.01}),
                "mode": (["2pass", "fast_crf", "gpu"], {"default": "2pass"}),
                "target_mb": ("FLOAT", {"min": 0.5, "max": 4000.0, "step": 0.5, "default": 8.0}),
                "codec": (["libx264", "libx265", "h264_nvenc", "hevc_nvenc"], {"default": "libx264"}),
            },
            "optional": {
                "audio": ("*",),
                "crf": ("INT", {"default": 23, "min": 10, "max": 40}),
                "preset": (["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"], {"default": "fast"}),
            }
        }

    RETURN_TYPES = ("STRING", "IMAGE", "*")
    RETURN_NAMES = ("video_path", "images", "audio")
    FUNCTION = "process"
    CATEGORY = "🎥Video Utils"

    def _run_ffmpeg(self, command):
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        except subprocess.CalledProcessError as e:
            print(f"--- ERREUR FFmpeg ---\nCommande: {' '.join(e.cmd)}\nErreur: {e.stderr}\n--- FIN ERREUR ---")
            raise e

    def process(self, images, fps, mode, target_mb, codec, audio=None, crf=23, preset="fast"):
        gpu_codecs = ["h264_nvenc", "hevc_nvenc"]
        if mode == "gpu" and codec not in gpu_codecs: codec = "h264_nvenc"
        elif mode in ["fast_crf", "2pass"] and codec in gpu_codecs: codec = "libx264"

        temp_dir = os.path.join(folder_paths.get_temp_directory(), "compressor_final")
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        
        frames_path = os.path.join(temp_dir, "frame_%05d.png")
        clean_video_path = os.path.join(temp_dir, "clean_video.mp4")
        pass_log_file = os.path.join(temp_dir, "ffmpeg_passlog")
        
        # --- LOGIQUE AUDIO FINALE ET ROBUSTE ---
        audio_file_path = None
        has_audio = False

        if audio is not None:
            # CAS 1: L'entrée est des données audio brutes (un dictionnaire)
            if isinstance(audio, dict) and 'waveform' in audio and 'sample_rate' in audio:
                print("Détection de données audio brutes. Sauvegarde en fichier temporaire...")
                waveform = audio['waveform'].cpu().numpy()
                sample_rate = audio['sample_rate']
                
                # S'assurer que la forme est [samples, channels]
                if waveform.ndim == 3: waveform = waveform.squeeze(0)
                if waveform.shape[0] < waveform.shape[1]: waveform = waveform.T

                # Convertir de float [-1, 1] à int16 pour le fichier .wav
                waveform_int16 = (waveform * 32767).astype(np.int16)
                
                temp_wav_path = os.path.join(temp_dir, "temp_audio.wav")
                write_wav(temp_wav_path, sample_rate, waveform_int16)
                audio_file_path = temp_wav_path
                has_audio = True

            # CAS 2: L'entrée est un chemin de fichier (string, list, ou tuple)
            else:
                path_candidate = ""
                if isinstance(audio, (list, tuple)) and len(audio) > 0:
                    path_candidate = str(audio[0])
                elif isinstance(audio, str):
                    path_candidate = audio

                if os.path.exists(path_candidate):
                    audio_file_path = path_candidate
                    has_audio = True
                else:
                    print(f"Avertissement: L'entrée audio fournie '{audio}' n'est ni des données brutes, ni un chemin valide.")

        try:
            for i, img in enumerate(images):
                Image.fromarray((255. * img.cpu().numpy()).astype(np.uint8)).save(frames_path % i)
            
            self._run_ffmpeg(["ffmpeg", "-y", "-framerate", str(fps), "-i", frames_path, "-c:v", "libx264", "-pix_fmt", "yuv420p", clean_video_path])
            duration_s = len(images) / float(fps)

            output_dir = os.path.join(folder_paths.get_output_directory(), "compressed_videos")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            final_video_path = os.path.join(output_dir, f"compressed_{timestamp}.mp4")

            if duration_s <= 0: raise ValueError("Durée invalide.")
            
            # ... Le reste du code utilise 'audio_file_path' qui est maintenant GARANTI d'être un chemin valide ...
            
            if mode == "2pass":
                audio_bitrate_bits = 128000 if has_audio else 0
                video_bitrate_k = int(((target_mb * 1024 * 1024 * 8 / duration_s) - audio_bitrate_bits) / 1000)
                if video_bitrate_k < 100: video_bitrate_k = 100

                pass1_cmd = ["ffmpeg", "-y", "-i", clean_video_path, "-c:v", codec, "-b:v", f"{video_bitrate_k}k", "-preset", preset, "-pass", "1", "-an", "-f", "mp4", "-passlogfile", pass_log_file, "NUL" if platform.system() == "Windows" else "/dev/null"]
                self._run_ffmpeg(pass1_cmd)

                pass2_cmd = ["ffmpeg", "-y", "-i", clean_video_path]
                if has_audio: pass2_cmd.extend(["-i", audio_file_path])
                pass2_cmd.extend(["-c:v", codec, "-b:v", f"{video_bitrate_k}k", "-preset", preset, "-pass", "2", "-passlogfile", pass_log_file])
                if has_audio: pass2_cmd.extend(["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"])
                pass2_cmd.append(final_video_path)
                self._run_ffmpeg(pass2_cmd)
            else:
                final_cmd = ["ffmpeg", "-y", "-i", clean_video_path]
                if has_audio: final_cmd.extend(["-i", audio_file_path])
                quality_param = ["-crf", str(crf)] if mode == "fast_crf" else ["-cq", str(crf)]
                final_cmd.extend(["-c:v", codec, "-preset", preset] + quality_param)
                if has_audio: final_cmd.extend(["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"])
                final_cmd.append(final_video_path)
                self._run_ffmpeg(final_cmd)
            
            return (final_video_path, images, audio if has_audio else "")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)