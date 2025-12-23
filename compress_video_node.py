import os
import subprocess
import folder_paths
import torch
import numpy as np
from PIL import Image
import shutil
import datetime
import platform
from scipy.io.wavfile import write as write_wav

class VideoCompressor:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                # --- ENTRÉES DOT ---
                "images": ("IMAGE",),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.01}),
                
                # --- PARAMÈTRES (WIDGETS) ---
                # Si target_mb = 0, on utilise le CRF. Sinon, on vise la taille en Mo.
                "target_mb": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10000.0, "step": 0.5, "display": "number"}),
                
                # Qualité (si target_mb est à 0)
                "crf": ("INT", {"default": 23, "min": 0, "max": 51, "step": 1}),
                
                # Codec : Liste explicite de chaînes de caractères pour éviter l'index "1"
                "codec": (["libx264", "libx265", "h264_nvenc", "hevc_nvenc"], {"default": "libx264"}),
                
                "preset": (["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"], {"default": "fast"}),
            },
            "optional": {
                "audio": ("AUDIO",),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "STRING")
    RETURN_NAMES = ("images", "audio", "fps", "video_path")
    FUNCTION = "process"
    CATEGORY = "🎥Video Utils"

    def _run_ffmpeg(self, command):
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        except subprocess.CalledProcessError as e:
            print(f"--- ERREUR FFmpeg ---\nCommande: {' '.join(e.cmd)}\nErreur: {e.stderr}\n--- FIN ERREUR ---")
            raise e

    def process(self, images, fps, target_mb, codec, crf=23, preset="fast", audio=None):
        # --- 1. DÉTECTION DU MODE ---
        # Si target_mb > 0, on passe en mode "Taille Cible" (2-pass), sinon mode "Qualité" (CRF)
        if target_mb > 0.0:
            mode = "target_size"
            print(f"🔧 Mode détecté : Taille cible de {target_mb} MB (2-Pass)")
        else:
            mode = "crf"
            print(f"🔧 Mode détecté : Qualité constante (CRF {crf}) - Taille cible ignorée (0)")

        # Gestion GPU/CPU
        gpu_codecs = ["h264_nvenc", "hevc_nvenc"]
        is_gpu = codec in gpu_codecs

        # --- 2. PRÉPARATION DOSSIERS ---
        temp_dir = os.path.join(folder_paths.get_temp_directory(), "comfy_compressor_v2")
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        
        frames_path = os.path.join(temp_dir, "frame_%05d.png")
        clean_video_path = os.path.join(temp_dir, "clean_video.mp4")
        pass_log_file = os.path.join(temp_dir, "ffmpeg_passlog")
        
        # --- 3. GESTION AUDIO (Identique, robuste) ---
        audio_file_path = None
        has_audio = False

        if audio is not None:
            if isinstance(audio, dict) and 'waveform' in audio and 'sample_rate' in audio:
                waveform = audio['waveform']
                sample_rate = audio['sample_rate']
                if torch.is_tensor(waveform): waveform = waveform.cpu().numpy()
                if waveform.ndim == 3: waveform = waveform.squeeze(0)
                if waveform.shape[0] < waveform.shape[1]: waveform = waveform.T
                waveform_int16 = (waveform * 32767).astype(np.int16)
                temp_wav_path = os.path.join(temp_dir, "temp_audio.wav")
                write_wav(temp_wav_path, sample_rate, waveform_int16)
                audio_file_path = temp_wav_path
                has_audio = True
            else:
                path_candidate = str(audio[0]) if isinstance(audio, (list, tuple)) and len(audio) > 0 else str(audio)
                if os.path.exists(path_candidate):
                    audio_file_path = path_candidate
                    has_audio = True

        try:
            # --- 4. SAUVEGARDE IMAGES ---
            print(f"💾 Écriture des {len(images)} frames...")
            for i, img in enumerate(images):
                Image.fromarray((255. * img.cpu().numpy()).astype(np.uint8)).save(frames_path % i)
            
            # --- 5. CRÉATION VIDÉO SOURCE ---
            self._run_ffmpeg([
                "ffmpeg", "-y", "-framerate", str(fps), "-i", frames_path, 
                "-c:v", "libx264", "-pix_fmt", "yuv420p", clean_video_path
            ])
            
            duration_s = len(images) / float(fps)
            if duration_s <= 0.001: duration_s = 1.0 # Sécurité division par zéro

            # --- 6. CALCUL BITRATE ET PARAMÈTRES ---
            output_dir = os.path.join(folder_paths.get_output_directory(), "compressed_videos")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            final_video_path = os.path.join(output_dir, f"render_{timestamp}.mp4")

            if mode == "target_size":
                # Calcul Bitrate : (Taille_MB * 8192) / durée
                # On soustrait l'audio (approx 128k)
                audio_bitrate_kbps = 128 if has_audio else 0
                total_bitrate_kbps = (target_mb * 8192) / duration_s
                video_bitrate_k = int(total_bitrate_kbps - audio_bitrate_kbps)
                
                if video_bitrate_k < 50: 
                    print("⚠️ Bitrate calculé trop bas, forcé à 100k.")
                    video_bitrate_k = 100

                # --- ENCODAGE 2 PASSES (Target Size) ---
                print(f"⚙️ Compression 2-Passes -> Cible: {target_mb}MB (Bitrate: {video_bitrate_k}k)")
                
                # Pass 1
                pass1 = [
                    "ffmpeg", "-y", "-i", clean_video_path, "-c:v", codec, 
                    "-b:v", f"{video_bitrate_k}k", "-preset", preset, 
                    "-pass", "1", "-an", "-f", "mp4", 
                    "-passlogfile", pass_log_file, 
                    "NUL" if platform.system() == "Windows" else "/dev/null"
                ]
                self._run_ffmpeg(pass1)

                # Pass 2
                pass2 = ["ffmpeg", "-y", "-i", clean_video_path]
                if has_audio: pass2.extend(["-i", audio_file_path])
                pass2.extend([
                    "-c:v", codec, "-b:v", f"{video_bitrate_k}k", 
                    "-preset", preset, "-pass", "2", "-passlogfile", pass_log_file
                ])
                if has_audio: pass2.extend(["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"])
                pass2.append(final_video_path)
                self._run_ffmpeg(pass2)

            else:
                # --- ENCODAGE CRF/CQ (Qualité) ---
                print(f"⚙️ Compression Qualité -> CRF: {crf}")
                cmd = ["ffmpeg", "-y", "-i", clean_video_path]
                if has_audio: cmd.extend(["-i", audio_file_path])
                
                # NVENC utilise -cq au lieu de -crf, et nécessite souvent -b:v 0 pour ignorer le bitrate
                if is_gpu:
                    quality_args = ["-cq", str(crf), "-b:v", "0"]
                else:
                    quality_args = ["-crf", str(crf)]

                cmd.extend(["-c:v", codec, "-preset", preset] + quality_args)
                
                if has_audio: cmd.extend(["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"])
                cmd.append(final_video_path)
                self._run_ffmpeg(cmd)
            
            return (images, audio, fps, final_video_path)

        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)